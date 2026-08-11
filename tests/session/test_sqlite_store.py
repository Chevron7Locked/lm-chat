# SPDX-License-Identifier: Apache-2.0
"""Contract tests for SQLiteSessionStore.

Tests for the SQLite session store.

Test-engine injection approach
-------------------------------
``SQLiteSessionStore`` accepts an optional ``engine`` constructor parameter
(the production path passes ``None`` and uses the application singleton).
Tests pass a dedicated test engine directly — no env-var monkeypatching, no
module-level global, no ``dispose_engine()`` side-effects on the singleton.

Why ``tmp_path``-backed file engine, not ``":memory:"``
-------------------------------------------------------
aiosqlite opens a **new SQLite connection** for every ``engine.connect()``
or ``engine.begin()`` call.  For an in-memory database each new connection
sees an **empty database** — rows inserted in one connection are invisible
to the next.  A ``tmp_path``-backed file database has a single WAL file
shared across all connections, so all calls within the same test observe the
same rows.  This matches production behaviour (all sessions in one file).
"""
from __future__ import annotations

import asyncio
import string
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from lmchat.db.schema import metadata
from lmchat.session.base import Session
from lmchat.session.sqlite_store import SQLiteSessionStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# URL-safe base64 character set per RFC 4648 §5
_URLSAFE_CHARS: frozenset[str] = frozenset(
    string.ascii_letters + string.digits + "-_"
)


@pytest.fixture()
async def store(tmp_path: Path) -> AsyncGenerator[SQLiteSessionStore]:
    """Yield a ``SQLiteSessionStore`` backed by a fresh per-test SQLite file.

    The engine is created from a ``tmp_path``-backed file so all
    connections within the same test see the same rows.  ``create_all()``
    bootstraps the schema; ``dispose()`` releases the file handle after the
    test.
    """
    db_path = tmp_path / "test_sessions.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        pool_pre_ping=True,
    )
    # Bootstrap the schema (sessions table + all others that share metadata).
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)

    store = SQLiteSessionStore(engine=engine)
    yield store

    await engine.dispose()


# Helper: a fake user id used across tests.
_USER_A: int = 1
_USER_B: int = 2
_TTL: int = 3600  # 1 hour, enough that none of our tests hit expiry naturally


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


async def test_create_returns_valid_session(store: SQLiteSessionStore) -> None:
    """create() must return a Session with correct fields."""
    before = datetime.now(UTC)
    session = await store.create(user_id=_USER_A, ttl_seconds=_TTL)
    after = datetime.now(UTC)

    assert isinstance(session, Session)
    assert session.user_id == _USER_A
    assert session.rotated_at is None
    assert before <= session.created_at <= after
    assert session.expires_at > session.created_at
    assert len(session.id) == 43  # 32 bytes → 43 base64url chars


async def test_session_id_is_url_safe_token(store: SQLiteSessionStore) -> None:
    """Session ID must use only url-safe characters (no '/' or '+')."""
    session = await store.create(user_id=_USER_A, ttl_seconds=_TTL)
    # Must not contain '/' or '+' (standard-base64 chars not in url-safe alphabet).
    assert "/" not in session.id, "session id must not contain '/'"
    assert "+" not in session.id, "session id must not contain '+'"
    # All characters must be in the url-safe base64 + padding/dash/underscore set.
    illegal = set(session.id) - _URLSAFE_CHARS
    assert not illegal, f"session id contains non-url-safe chars: {illegal}"


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


async def test_get_existing_session(store: SQLiteSessionStore) -> None:
    """get() must return the session that was just created."""
    session = await store.create(user_id=_USER_A, ttl_seconds=_TTL)
    fetched = await store.get(session.id)

    assert fetched is not None
    assert fetched.id == session.id
    assert fetched.user_id == _USER_A
    assert fetched.rotated_at is None


async def test_get_missing_session_returns_none(store: SQLiteSessionStore) -> None:
    """get() with a non-existent token must return None."""
    result = await store.get("nonexistent-token-that-does-not-exist")
    assert result is None


async def test_get_expired_session_returns_none(store: SQLiteSessionStore) -> None:
    """get() must return None when the session's expires_at is in the past.

    We create a session with a very short TTL, wait (via datetime arithmetic
    rather than real sleep) by directly querying after manually expiring the
    row.  To avoid a real sleep we create a session whose TTL is effectively
    expired by inserting with a negative TTL via the internal ``create()``
    path — but ``create()`` does not expose that.  Instead we create, then
    verify expiry semantics by creating a session with ttl=1 and patching the
    row directly via a raw UPDATE.
    """
    # Create a valid session and then manually expire it via raw SQL.
    from sqlalchemy import text

    session = await store.create(user_id=_USER_A, ttl_seconds=_TTL)

    # Set expires_at to the past.
    past = datetime(2000, 1, 1, tzinfo=UTC)
    async with store._engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET expires_at = :exp WHERE id = :sid"),
            {"exp": past.isoformat(), "sid": session.id},
        )

    result = await store.get(session.id)
    assert result is None


# ---------------------------------------------------------------------------
# rotate
# ---------------------------------------------------------------------------


async def test_rotate_creates_new_session_and_marks_old(
    store: SQLiteSessionStore,
) -> None:
    """rotate() must return a new Session with a different id."""
    old = await store.create(user_id=_USER_A, ttl_seconds=_TTL)
    new = await store.rotate(old.id)

    assert new.id != old.id
    assert new.user_id == _USER_A
    assert new.rotated_at is None
    # New session is accessible.
    fetched = await store.get(new.id)
    assert fetched is not None
    assert fetched.id == new.id


async def test_rotate_old_session_still_in_db_with_rotated_at_set(
    store: SQLiteSessionStore,
) -> None:
    """After rotate(), the old row must persist with rotated_at set (audit trail)."""
    from sqlalchemy import select

    from lmchat.db.schema import sessions

    old = await store.create(user_id=_USER_A, ttl_seconds=_TTL)
    old_id = old.id
    await store.rotate(old_id)

    # Old row must still exist in the DB.
    async with store._engine.connect() as conn:
        result = await conn.execute(
            select(sessions).where(sessions.c.id == old_id)
        )
        row = result.fetchone()

    assert row is not None, "old session row must not be deleted after rotate"
    assert row.rotated_at is not None, "rotated_at must be set on the old row"


async def test_get_rejects_rotated_session(store: SQLiteSessionStore) -> None:
    """get() must treat a rotated (superseded) token as absent.

    rotate() marks the old row ``rotated_at`` but leaves it in the DB as a
    short audit trail; get() must not authenticate that dead token even while
    the row lingers before cleanup reaps it at expiry.
    """
    old = await store.create(user_id=_USER_A, ttl_seconds=_TTL)
    await store.rotate(old.id)

    # The old (rotated) token is dead immediately, despite the row still
    # existing and its expires_at still being in the future.
    assert await store.get(old.id) is None


async def test_rotate_missing_session_raises(store: SQLiteSessionStore) -> None:
    """rotate() on a non-existent session must raise KeyError."""
    with pytest.raises(KeyError):
        await store.rotate("no-such-session-token-xyz")


async def test_rotate_expired_session_raises(store: SQLiteSessionStore) -> None:
    """rotate() on an expired session must raise KeyError."""
    from sqlalchemy import text

    session = await store.create(user_id=_USER_A, ttl_seconds=_TTL)
    past = datetime(2000, 1, 1, tzinfo=UTC)
    async with store._engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET expires_at = :exp WHERE id = :sid"),
            {"exp": past.isoformat(), "sid": session.id},
        )

    with pytest.raises(KeyError):
        await store.rotate(session.id)


# ---------------------------------------------------------------------------
# extend
# ---------------------------------------------------------------------------


async def test_extend_updates_expires_at(store: SQLiteSessionStore) -> None:
    """extend() must push expires_at forward."""
    session = await store.create(user_id=_USER_A, ttl_seconds=_TTL)
    original_expires = session.expires_at

    extended = await store.extend(session.id, ttl_seconds=7200)

    assert extended.expires_at > original_expires
    assert extended.id == session.id
    assert extended.user_id == _USER_A

    # Confirm the DB was actually updated.
    fetched = await store.get(session.id)
    assert fetched is not None
    # Allow up to 2-second tolerance for wall-clock jitter.
    assert abs((fetched.expires_at - extended.expires_at).total_seconds()) < 2


async def test_extend_missing_session_raises(store: SQLiteSessionStore) -> None:
    """extend() on a non-existent session must raise KeyError."""
    with pytest.raises(KeyError):
        await store.extend("no-such-session-token", ttl_seconds=_TTL)


# ---------------------------------------------------------------------------
# revoke
# ---------------------------------------------------------------------------


async def test_revoke_deletes_all_sessions_for_user(
    store: SQLiteSessionStore,
) -> None:
    """revoke() must delete every session owned by the target user."""
    s1 = await store.create(user_id=_USER_A, ttl_seconds=_TTL)
    s2 = await store.create(user_id=_USER_A, ttl_seconds=_TTL)

    await store.revoke(_USER_A)

    assert await store.get(s1.id) is None
    assert await store.get(s2.id) is None


async def test_revoke_does_not_touch_other_users(
    store: SQLiteSessionStore,
) -> None:
    """revoke(user_a) must not delete sessions belonging to user_b."""
    # Need a users row for FK constraint — insert minimal rows.
    from sqlalchemy import text

    async with store._engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO users (id, username, password_hash)"
                " VALUES (:id, :u, :ph)"
            ),
            {"id": _USER_A, "u": "alice", "ph": "x"},
        )
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO users (id, username, password_hash)"
                " VALUES (:id, :u, :ph)"
            ),
            {"id": _USER_B, "u": "bob", "ph": "x"},
        )

    s_a = await store.create(user_id=_USER_A, ttl_seconds=_TTL)
    s_b = await store.create(user_id=_USER_B, ttl_seconds=_TTL)

    await store.revoke(_USER_A)

    # USER_A's session is gone.
    assert await store.get(s_a.id) is None
    # USER_B's session must be untouched.
    assert await store.get(s_b.id) is not None


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------


async def test_cleanup_deletes_expired(store: SQLiteSessionStore) -> None:
    """cleanup() must remove expired non-rotated rows."""
    from sqlalchemy import text

    session = await store.create(user_id=_USER_A, ttl_seconds=_TTL)
    past = datetime(2000, 1, 1, tzinfo=UTC)
    async with store._engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET expires_at = :exp WHERE id = :sid"),
            {"exp": past.isoformat(), "sid": session.id},
        )

    count = await store.cleanup()

    assert count >= 1
    # Row must be gone from the DB.
    from sqlalchemy import select

    from lmchat.db.schema import sessions

    async with store._engine.connect() as conn:
        result = await conn.execute(
            select(sessions).where(sessions.c.id == session.id)
        )
        assert result.fetchone() is None


async def test_cleanup_keeps_recently_rotated(store: SQLiteSessionStore) -> None:
    """cleanup() must NOT delete rotated rows whose expires_at is still in the future.

    The audit-trail contract: rotated rows are kept until their own
    ``expires_at`` passes.  A row marked ``rotated_at IS NOT NULL`` but with
    ``expires_at`` still in the future must survive cleanup.
    """
    from sqlalchemy import text

    session = await store.create(user_id=_USER_A, ttl_seconds=_TTL)
    # Simulate rotation by setting rotated_at without expiring the row.
    now = datetime.now(UTC)
    async with store._engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET rotated_at = :rot WHERE id = :sid"),
            {"rot": now.isoformat(), "sid": session.id},
        )

    await store.cleanup()

    # The rotated-but-not-expired row must survive.
    from sqlalchemy import select

    from lmchat.db.schema import sessions

    async with store._engine.connect() as conn:
        result = await conn.execute(
            select(sessions).where(sessions.c.id == session.id)
        )
        row = result.fetchone()

    assert row is not None, "rotated-but-not-expired row must not be deleted by cleanup()"


async def test_cleanup_deletes_rotated_and_expired(
    store: SQLiteSessionStore,
) -> None:
    """cleanup() must reap rows that are both rotated AND expired.

    A rotated row is preserved only as a short audit trail until its own
    ``expires_at`` passes; once expired it carries no value and there is no
    separate reaper, so routine cleanup removes it.  Keeping rotated-expired
    rows forever would leak one permanent dead row per ``rotate()`` (e.g. per
    login).  This test pins the bounded-growth contract.
    """
    from sqlalchemy import select, text

    from lmchat.db.schema import sessions

    session = await store.create(user_id=_USER_A, ttl_seconds=_TTL)
    past = datetime(2000, 1, 1, tzinfo=UTC)
    rot_time = datetime(2000, 1, 1, 1, tzinfo=UTC)
    async with store._engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE sessions SET expires_at = :exp, rotated_at = :rot"
                " WHERE id = :sid"
            ),
            {"exp": past.isoformat(), "rot": rot_time.isoformat(), "sid": session.id},
        )

    await store.cleanup()

    # Rotated + expired row must be deleted — no unbounded audit-trail leak.
    async with store._engine.connect() as conn:
        result = await conn.execute(
            select(sessions).where(sessions.c.id == session.id)
        )
        row = result.fetchone()

    assert row is None, (
        "rotated+expired row must be reaped by cleanup() — otherwise the "
        "sessions table leaks one dead row per rotate()"
    )


async def test_cleanup_returns_count(store: SQLiteSessionStore) -> None:
    """cleanup() must return the number of rows actually deleted."""
    from sqlalchemy import text

    # Create 3 sessions and expire them.
    past = datetime(2000, 1, 1, tzinfo=UTC)
    ids = []
    for _ in range(3):
        s = await store.create(user_id=_USER_A, ttl_seconds=_TTL)
        ids.append(s.id)

    async with store._engine.begin() as conn:
        for sid in ids:
            await conn.execute(
                text("UPDATE sessions SET expires_at = :exp WHERE id = :sid"),
                {"exp": past.isoformat(), "sid": sid},
            )

    count = await store.cleanup()
    assert count == 3


# ---------------------------------------------------------------------------
# Concurrent uniqueness
# ---------------------------------------------------------------------------


async def test_concurrent_creates_get_unique_ids(store: SQLiteSessionStore) -> None:
    """Concurrent create() calls must produce distinct session IDs."""
    tasks = [store.create(user_id=_USER_A, ttl_seconds=_TTL) for _ in range(20)]
    sessions_list: list[Session] = await asyncio.gather(*tasks)

    ids = [s.id for s in sessions_list]
    assert len(ids) == len(set(ids)), "concurrent creates must produce unique IDs"


# ---------------------------------------------------------------------------
# revoke_session
# ---------------------------------------------------------------------------


async def test_revoke_session_removes_target_session(store: SQLiteSessionStore) -> None:
    """revoke_session() must delete the exact session specified."""
    s1 = await store.create(user_id=_USER_A, ttl_seconds=_TTL)
    s2 = await store.create(user_id=_USER_A, ttl_seconds=_TTL)

    await store.revoke_session(s1.id)

    # Target session gone.
    assert await store.get(s1.id) is None
    # Other session untouched.
    assert await store.get(s2.id) is not None


async def test_revoke_session_idempotent(store: SQLiteSessionStore) -> None:
    """revoke_session() on a non-existent token is a no-op."""
    # Should not raise.
    await store.revoke_session("token-that-does-not-exist")


# ---------------------------------------------------------------------------
# revoke_others
# ---------------------------------------------------------------------------


async def test_revoke_others_keeps_except_session(store: SQLiteSessionStore) -> None:
    """revoke_others() must preserve the excepted session."""
    from sqlalchemy import text

    # Both users need rows for FK.
    async with store._engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO users (id, username, password_hash)"
                " VALUES (:id, :u, :ph)"
            ),
            {"id": _USER_A, "u": "revoke_others_alice", "ph": "x"},
        )

    s_keep = await store.create(user_id=_USER_A, ttl_seconds=_TTL)
    s_other1 = await store.create(user_id=_USER_A, ttl_seconds=_TTL)
    s_other2 = await store.create(user_id=_USER_A, ttl_seconds=_TTL)

    await store.revoke_others(user_id=_USER_A, except_session_id=s_keep.id)

    assert await store.get(s_keep.id) is not None, "kept session must survive"
    assert await store.get(s_other1.id) is None, "other session 1 must be revoked"
    assert await store.get(s_other2.id) is None, "other session 2 must be revoked"


async def test_revoke_others_does_not_touch_other_users(
    store: SQLiteSessionStore,
) -> None:
    """revoke_others(user_a, ...) must not delete sessions owned by user_b."""
    from sqlalchemy import text

    async with store._engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO users (id, username, password_hash)"
                " VALUES (:id, :u, :ph)"
            ),
            {"id": _USER_A, "u": "revoke_others_ua", "ph": "x"},
        )
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO users (id, username, password_hash)"
                " VALUES (:id, :u, :ph)"
            ),
            {"id": _USER_B, "u": "revoke_others_ub", "ph": "x"},
        )

    s_a_keep = await store.create(user_id=_USER_A, ttl_seconds=_TTL)
    s_a_other = await store.create(user_id=_USER_A, ttl_seconds=_TTL)
    s_b = await store.create(user_id=_USER_B, ttl_seconds=_TTL)

    await store.revoke_others(user_id=_USER_A, except_session_id=s_a_keep.id)

    assert await store.get(s_a_keep.id) is not None
    assert await store.get(s_a_other.id) is None
    assert await store.get(s_b.id) is not None, "user_b session must be untouched"


async def test_revoke_others_idempotent_no_other_sessions(
    store: SQLiteSessionStore,
) -> None:
    """revoke_others() when user has only one session is a no-op."""
    s = await store.create(user_id=_USER_A, ttl_seconds=_TTL)
    # Call with no other sessions — must not raise.
    await store.revoke_others(user_id=_USER_A, except_session_id=s.id)
    assert await store.get(s.id) is not None
