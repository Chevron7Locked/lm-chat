# SPDX-License-Identifier: Apache-2.0
"""Comprehensive contract tests for auth_service.

Tests for the auth service.

Uses a per-test tmp_path SQLite engine + SQLiteSessionStore.
No mocks — real scrypt, real TOTP, real DB writes.

scrypt cost: all direct calls to register(), login(), change_password() pass
``**LOW_COST`` (n=2^10) so tests run within the OpenSSL maxmem limit of
this environment.  The _create_user_low_cost helper inserts users directly
with a low-cost hash.  The dummy hash cache in auth_service is also primed
at the same low cost via the ``_hash_n`` param.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path

import pyotp
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import audit_log, metadata, users
from lmchat.services.auth_errors import (
    BadCredentialsError,
    TotpNotConfiguredError,
    TotpRequiredError,
    UsernameTakenError,
)
from lmchat.services.auth_service import (
    User,
    change_password,
    count_users,
    disable_totp,
    get_current_user,
    login,
    logout,
    register,
    setup_totp,
    verify_totp_setup,
)
from lmchat.session.sqlite_store import SQLiteSessionStore
from lmchat.utils.hashing import hash_password

#: Low-cost scrypt parameters — keeps tests within OpenSSL maxmem in this environment.
_LOW_COST: dict[str, int] = {"_hash_n": 2**10, "_hash_r": 8, "_hash_p": 1}
LOW_COST = _LOW_COST

# ---------------------------------------------------------------------------
# Shared env setup for config
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _set_lm_chat_secret(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Ensure LM_CHAT_SECRET is set (required for TOTP encryption).

    Also resets the dummy hash cache so tests that use different scrypt cost
    parameters (LOW_COST = n=2^10) compute the dummy at the right cost, and
    the per-user-id single-session lock dict so a lock reused across this
    suite's recurring low user_ids (each test gets a fresh function-scoped
    event loop from pytest-asyncio) never risks a "bound to a different
    event loop" error should a later test genuinely contend it.
    """
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-key-for-auth-service-tests")
    # clear the lru_cache so monkeypatched env is picked up
    from lmchat.config import get_settings
    from lmchat.services.auth_service import (
        _reset_dummy_hash_cache,
        _reset_single_session_locks,
    )

    get_settings.cache_clear()
    _reset_dummy_hash_cache()
    _reset_single_session_locks()
    yield
    get_settings.cache_clear()
    _reset_dummy_hash_cache()
    _reset_single_session_locks()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def db_engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    """Yield a fresh per-test SQLite engine with the full schema."""
    db_path = tmp_path / "test_auth.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}", pool_pre_ping=True)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture()
async def store(db_engine: AsyncEngine) -> AsyncGenerator[SQLiteSessionStore]:
    """Yield a SQLiteSessionStore backed by the test engine."""
    yield SQLiteSessionStore(engine=db_engine)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_user_low_cost(
    engine: AsyncEngine,
    *,
    username: str = "alice",
    password: str = "correct-horse-battery",
) -> User:
    """Insert a user with low-cost scrypt (n=2^10) directly into the DB.

    Uses explicit id generation (MAX+1) because BigInteger PRIMARY KEY
    on SQLite does not auto-increment without an explicit id value.
    """
    from sqlalchemy import func, select

    from lmchat.db.schema import users as users_table

    pw_hash = hash_password(password, n=2**10, r=8, p=1)
    async with engine.begin() as conn:
        id_result = await conn.execute(
            select(func.coalesce(func.max(users_table.c.id), 0) + 1)
        )
        next_id = id_result.scalar()
        result = await conn.execute(
            text(
                "INSERT INTO users (id, username, password_hash)"
                " VALUES (:id, :u, :ph)"
                " RETURNING id, username, password_hash, totp_secret,"
                "           is_admin, created_at, updated_at"
            ),
            {"id": next_id, "u": username, "ph": pw_hash},
        )
        row = result.fetchone()
    assert row is not None
    return User.model_validate(row, from_attributes=True)


async def _count_audit_rows(engine: AsyncEngine, event: str | None = None) -> int:
    """Count audit_log rows, optionally filtering by event string."""
    async with engine.connect() as conn:
        q = select(audit_log)
        if event is not None:
            q = q.where(audit_log.c.event == event)
        result = await conn.execute(q)
        return len(result.fetchall())


async def _get_user_row(engine: AsyncEngine, user_id: int):  # type: ignore[return]
    """Fetch raw user row for assertions."""
    async with engine.connect() as conn:
        result = await conn.execute(select(users).where(users.c.id == user_id))
        return result.fetchone()


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


async def test_register_creates_user(db_engine: AsyncEngine) -> None:
    """register() inserts a user row and returns a User with correct fields.

    Note: ``bob`` is the FIRST user in this fresh per-test DB, so the
    bootstrap-admin semantic grants ``is_admin=True``.  See
    :func:`test_first_registered_user_is_admin` for the explicit
    bootstrap contract; this test asserts the broader row shape.
    """
    user = await register(username="bob", password="hunter2", engine=db_engine, **LOW_COST)
    assert isinstance(user, User)
    assert user.username == "bob"
    assert user.id > 0
    assert user.is_admin is True  # first registered user becomes admin
    assert user.password_hash.startswith("scrypt$")
    assert user.totp_secret is None


async def test_register_rejects_duplicate_username(db_engine: AsyncEngine) -> None:
    """register() raises UsernameTakenError on a duplicate username."""
    await register(username="charlie", password="pw1", engine=db_engine, **LOW_COST)
    with pytest.raises(UsernameTakenError):
        await register(username="charlie", password="pw2", engine=db_engine, **LOW_COST)


@pytest.mark.parametrize(
    "username",
    [
        "ab",  # too short
        "a" * 65,  # too long
        "bad name",  # space
        "bad-name",  # hyphen
        "bad.name",  # dot
        "",  # empty
        "user@host",  # @
    ],
)
async def test_register_rejects_invalid_username(
    db_engine: AsyncEngine, username: str
) -> None:
    """register() raises ValueError for usernames that fail the format check."""
    with pytest.raises(ValueError):
        await register(username=username, password="pw", engine=db_engine, **LOW_COST)


async def test_register_writes_audit_log(db_engine: AsyncEngine) -> None:
    """register() writes an auth.register audit row."""
    await register(username="dave", password="pw", engine=db_engine, **LOW_COST)
    count = await _count_audit_rows(db_engine, "auth.register")
    assert count == 1



async def test_first_registered_user_is_admin(db_engine: AsyncEngine) -> None:
    """The very first registration on an empty users table is_admin=True.

    Implements the bootstrap-admin semantic claimed in README §Quickstart
    and AGENTS.md.  Prior to this fix, register() inserted with the
    server_default ``is_admin=false`` and no user was ever auto-promoted.
    """
    user = await register(
        username="alice", password="bootstrap-pw", engine=db_engine, **LOW_COST
    )
    assert user.is_admin is True


async def test_second_registered_user_is_not_admin(db_engine: AsyncEngine) -> None:
    """The second registration on a non-empty users table is_admin=False.

    Once any row exists in users, subsequent registrations get the
    column default (False).  Promotion to admin after the bootstrap row
    is an explicit admin-route action (POST /api/admin/users/{id}/role).
    """
    first = await register(
        username="first", password="pw-one", engine=db_engine, **LOW_COST
    )
    second = await register(
        username="second", password="pw-two", engine=db_engine, **LOW_COST
    )
    assert first.is_admin is True
    assert second.is_admin is False


async def test_first_user_admin_is_atomic_under_race(db_engine: AsyncEngine) -> None:
    """Concurrent first-time registrations — exactly one wins is_admin.

    Fires N parallel register() coroutines against an empty users table
    via ``asyncio.gather``.  The bootstrap-admin check runs inside the
    same ``engine.begin()`` transaction as the INSERT, so SQLite's
    serializable isolation guarantees at most one transaction observes
    ``count == 0`` and is promoted.  We assert exactly one row has
    is_admin=True and the rest are False.

    SQLite serializes write transactions (database-level write lock), so
    the count + insert pair runs serially across connections even with
    asyncio's cooperative concurrency.  On PostgreSQL the same guarantee
    follows from SERIALIZABLE / REPEATABLE READ — see ADR notes.
    """
    n = 5
    results = await asyncio.gather(
        *(
            register(
                username=f"racer_{i}",
                password="race-pw",
                engine=db_engine,
                **LOW_COST,
            )
            for i in range(n)
        )
    )
    admins = [u for u in results if u.is_admin]
    assert len(admins) == 1, (
        f"expected exactly one bootstrap admin, got {len(admins)}: "
        f"{[u.username for u in admins]}"
    )
    non_admins = [u for u in results if not u.is_admin]
    assert len(non_admins) == n - 1


# ---------------------------------------------------------------------------
# login — success path
# ---------------------------------------------------------------------------


async def test_login_success_returns_session(
    db_engine: AsyncEngine, store: SQLiteSessionStore
) -> None:
    """login() returns a (Session, User) tuple on valid credentials."""
    user = await _create_user_low_cost(db_engine)
    session, logged_in_user = await login(
        username=user.username,
        password="correct-horse-battery",
        totp_code=None,
        ip="127.0.0.1",
        user_agent="test",
        session_store=store,
        engine=db_engine,
        **LOW_COST,
    )
    assert session.user_id == user.id
    assert len(session.id) > 8
    assert logged_in_user.id == user.id
    assert logged_in_user.username == user.username


async def test_login_writes_success_audit_log(
    db_engine: AsyncEngine, store: SQLiteSessionStore
) -> None:
    """login() writes an auth.login.success audit row on success."""
    user = await _create_user_low_cost(db_engine)
    await login(
        username=user.username,
        password="correct-horse-battery",
        totp_code=None,
        ip="127.0.0.1",
        user_agent="test",
        session_store=store,
        engine=db_engine,
        **LOW_COST,
    )
    count = await _count_audit_rows(db_engine, "auth.login.success")
    assert count == 1


# ---------------------------------------------------------------------------
# login — failure paths
# ---------------------------------------------------------------------------


async def test_login_wrong_password(
    db_engine: AsyncEngine, store: SQLiteSessionStore
) -> None:
    """login() raises BadCredentialsError on wrong password."""
    user = await _create_user_low_cost(db_engine)
    with pytest.raises(BadCredentialsError):
        await login(
            username=user.username,
            password="wrong-password",
            totp_code=None,
            ip=None,
            user_agent=None,
            session_store=store,
            engine=db_engine,
            **LOW_COST,
        )


async def test_login_unknown_user(
    db_engine: AsyncEngine, store: SQLiteSessionStore
) -> None:
    """login() raises BadCredentialsError for an unknown username."""
    with pytest.raises(BadCredentialsError):
        await login(
            username="nonexistent_user",
            password="pw",
            totp_code=None,
            ip=None,
            user_agent=None,
            session_store=store,
            engine=db_engine,
            **LOW_COST,
        )


async def test_login_failure_writes_audit_log_bad_password(
    db_engine: AsyncEngine, store: SQLiteSessionStore
) -> None:
    """Bad password produces an audit row with reason=bad_password."""
    user = await _create_user_low_cost(db_engine)
    with pytest.raises(BadCredentialsError):
        await login(
            username=user.username,
            password="wrong",
            totp_code=None,
            ip=None,
            user_agent=None,
            session_store=store,
            engine=db_engine,
            **LOW_COST,
        )
    count = await _count_audit_rows(db_engine, "auth.login.failure")
    assert count == 1


async def test_login_failure_writes_audit_log_unknown_user(
    db_engine: AsyncEngine, store: SQLiteSessionStore
) -> None:
    """Unknown username produces an audit row with reason=unknown_user."""
    with pytest.raises(BadCredentialsError):
        await login(
            username="nobody_here",
            password="pw",
            totp_code=None,
            ip=None,
            user_agent=None,
            session_store=store,
            engine=db_engine,
            **LOW_COST,
        )
    count = await _count_audit_rows(db_engine, "auth.login.failure")
    assert count == 1


# ---------------------------------------------------------------------------
# login — TOTP paths
# ---------------------------------------------------------------------------


async def _setup_totp_for_user(db_engine: AsyncEngine, user_id: int) -> str:
    """Set up TOTP for a user; return the raw secret."""
    _, secret = await setup_totp(user_id=user_id, engine=db_engine)
    code = pyotp.TOTP(secret).now()
    await verify_totp_setup(
        user_id=user_id, secret=secret, code=code, engine=db_engine
    )
    return secret


async def test_login_with_totp_required(
    db_engine: AsyncEngine, store: SQLiteSessionStore
) -> None:
    """login() raises TotpRequiredError when user has TOTP but no code supplied."""
    user = await _create_user_low_cost(db_engine)
    await _setup_totp_for_user(db_engine, user.id)

    with pytest.raises(TotpRequiredError):
        await login(
            username=user.username,
            password="correct-horse-battery",
            totp_code=None,
            ip=None,
            user_agent=None,
            session_store=store,
            engine=db_engine,
            **LOW_COST,
        )


async def test_login_with_totp_success(
    db_engine: AsyncEngine, store: SQLiteSessionStore
) -> None:
    """login() succeeds when a valid TOTP code is supplied."""
    user = await _create_user_low_cost(db_engine)
    secret = await _setup_totp_for_user(db_engine, user.id)
    code = pyotp.TOTP(secret).now()

    session, _ = await login(
        username=user.username,
        password="correct-horse-battery",
        totp_code=code,
        ip=None,
        user_agent=None,
        session_store=store,
        engine=db_engine,
        **LOW_COST,
    )
    assert session.user_id == user.id


async def test_login_with_totp_wrong_code(
    db_engine: AsyncEngine, store: SQLiteSessionStore
) -> None:
    """login() raises BadCredentialsError when the TOTP code is wrong."""
    user = await _create_user_low_cost(db_engine)
    await _setup_totp_for_user(db_engine, user.id)

    with pytest.raises(BadCredentialsError):
        await login(
            username=user.username,
            password="correct-horse-battery",
            totp_code="000000",
            ip=None,
            user_agent=None,
            session_store=store,
            engine=db_engine,
            **LOW_COST,
        )


async def test_login_failure_writes_audit_log_totp_required(
    db_engine: AsyncEngine, store: SQLiteSessionStore
) -> None:
    """TOTP-required failure produces an audit row with reason=totp_required."""
    user = await _create_user_low_cost(db_engine)
    await _setup_totp_for_user(db_engine, user.id)

    with pytest.raises(TotpRequiredError):
        await login(
            username=user.username,
            password="correct-horse-battery",
            totp_code=None,
            ip=None,
            user_agent=None,
            session_store=store,
            engine=db_engine,
            **LOW_COST,
        )
    count = await _count_audit_rows(db_engine, "auth.login.failure")
    assert count == 1


async def test_login_failure_writes_audit_log_bad_totp(
    db_engine: AsyncEngine, store: SQLiteSessionStore
) -> None:
    """Bad TOTP code produces an audit row with reason=bad_totp."""
    user = await _create_user_low_cost(db_engine)
    await _setup_totp_for_user(db_engine, user.id)

    with pytest.raises(BadCredentialsError):
        await login(
            username=user.username,
            password="correct-horse-battery",
            totp_code="000000",
            ip=None,
            user_agent=None,
            session_store=store,
            engine=db_engine,
            **LOW_COST,
        )
    count = await _count_audit_rows(db_engine, "auth.login.failure")
    assert count == 1


# ---------------------------------------------------------------------------
# login — transparent rehash
# ---------------------------------------------------------------------------


async def test_login_rehashes_below_default_cost(
    db_engine: AsyncEngine, store: SQLiteSessionStore
) -> None:
    """login() transparently upgrades a below-default-cost hash on successful verify."""
    from lmchat.utils.hashing import needs_rehash

    user = await _create_user_low_cost(db_engine)  # created with n=2^10

    # Confirm the stored hash is below default cost before login.
    user_row = await _get_user_row(db_engine, user.id)
    assert user_row is not None
    assert needs_rehash(user_row.password_hash) is True

    # Login with the correct password — triggers rehash at LOW_COST params.
    await login(
        username=user.username,
        password="correct-horse-battery",
        totp_code=None,
        ip=None,
        user_agent=None,
        session_store=store,
        engine=db_engine,
        **LOW_COST,
    )

    # After login the hash should differ from before — rehash path ran.
    # Both are n=2^10 (same cost from LOW_COST) but different salts, so
    # the hash values differ even though cost params are equal.
    user_row_after = await _get_user_row(db_engine, user.id)
    assert user_row_after is not None
    assert user_row_after.password_hash != user_row.password_hash


# ---------------------------------------------------------------------------
# login — single-session enforcement
# ---------------------------------------------------------------------------


async def test_login_revokes_prior_session_when_single_session_enabled(
    db_engine: AsyncEngine,
    store: SQLiteSessionStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """login() revokes the prior session when lm_chat_single_session=True."""
    from lmchat.config import get_settings

    monkeypatch.setenv("LM_CHAT_SINGLE_SESSION", "true")
    get_settings.cache_clear()

    user = await _create_user_low_cost(db_engine)
    session1, _ = await login(
        username=user.username,
        password="correct-horse-battery",
        totp_code=None,
        ip=None,
        user_agent=None,
        session_store=store,
        engine=db_engine,
        **LOW_COST,
    )
    # Second login should revoke the first session.
    session2, _ = await login(
        username=user.username,
        password="correct-horse-battery",
        totp_code=None,
        ip=None,
        user_agent=None,
        session_store=store,
        engine=db_engine,
        **LOW_COST,
    )
    assert session1.id != session2.id
    # First session must now be gone.
    first = await store.get(session1.id)
    assert first is None

    get_settings.cache_clear()


async def test_login_keeps_prior_session_when_single_session_disabled(
    db_engine: AsyncEngine,
    store: SQLiteSessionStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """login() does NOT revoke prior sessions when lm_chat_single_session=False."""
    from lmchat.config import get_settings

    monkeypatch.setenv("LM_CHAT_SINGLE_SESSION", "false")
    get_settings.cache_clear()

    user = await _create_user_low_cost(db_engine)
    session1, _ = await login(
        username=user.username,
        password="correct-horse-battery",
        totp_code=None,
        ip=None,
        user_agent=None,
        session_store=store,
        engine=db_engine,
        **LOW_COST,
    )
    session2, _ = await login(
        username=user.username,
        password="correct-horse-battery",
        totp_code=None,
        ip=None,
        user_agent=None,
        session_store=store,
        engine=db_engine,
        **LOW_COST,
    )
    assert session1.id != session2.id
    # First session must STILL be present.
    first = await store.get(session1.id)
    assert first is not None

    get_settings.cache_clear()


async def test_login_tolerates_rotate_race_with_concurrent_single_session_revoke(
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED-ON-REVERT: login() must not 500 on the create()/rotate() race.

    In single-session mode, a concurrent login for the SAME user can revoke()
    every session row for that user between our create() and our rotate() --
    e.g. login A creates its session, login B's revoke() deletes it, then A's
    rotate() finds nothing to rotate and raises KeyError. That KeyError must
    not propagate out of login() as an unhandled exception (a 500 for a user
    who authenticated with correct credentials).

    Simulated deterministically via a SQLiteSessionStore subclass whose
    rotate() first performs the "concurrent" revoke() itself (exactly what
    another login's single-session enforcement does) and then calls through
    to the real rotate(), which raises KeyError because the row is gone.
    """
    from lmchat.config import get_settings
    from lmchat.db.schema import sessions as sessions_table
    from lmchat.session.sqlite_store import SQLiteSessionStore

    monkeypatch.setenv("LM_CHAT_SINGLE_SESSION", "true")
    get_settings.cache_clear()

    user = await _create_user_low_cost(db_engine)

    class _RaceyStore(SQLiteSessionStore):
        """rotate() simulates a concurrent login's revoke() winning the race."""

        async def rotate(self, session_id: str):  # noqa: ANN201 - test double
            # Another concurrent login for the SAME user just called
            # revoke(), wiping the row this rotate() is about to look up.
            await self.revoke(user_id=user.id)
            return await super().rotate(session_id)

    racey_store = _RaceyStore(engine=db_engine)

    session, _ = await login(
        username=user.username,
        password="correct-horse-battery",
        totp_code=None,
        ip=None,
        user_agent=None,
        session_store=racey_store,
        engine=db_engine,
        **LOW_COST,
    )

    # login() must not have raised, and must return a session that is
    # actually live in the store -- not the deleted pre-rotation row.
    live = await racey_store.get(session.id)
    assert live is not None, (
        "login() returned a session that isn't actually in the store; the "
        "rotate()-race fallback must re-mint a real, persisted session"
    )

    # Single-session invariant must still hold after the fallback: exactly
    # one live (non-rotated) row for the user.
    async with db_engine.begin() as conn:
        result = await conn.execute(
            select(sessions_table).where(
                sessions_table.c.user_id == user.id,
                sessions_table.c.rotated_at.is_(None),
            )
        )
        live_rows = result.fetchall()
    assert len(live_rows) == 1, (
        f"expected exactly one live session after the rotate-race fallback "
        f"re-asserts single-session; found {len(live_rows)}"
    )
    assert live_rows[0].id == session.id

    get_settings.cache_clear()


async def test_login_closes_success_success_race_exactly_one_live_session(
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED-ON-REVERT: two concurrent logins that BOTH succeed must not both survive.

    Unlike the rotate()-race test above (where one login's rotate() collides
    with another's revoke() and raises KeyError), this reproduces the race
    where NEITHER login's rotate() ever raises: a second, fully independent
    login for the SAME user completes its entire revoke()/create()/rotate()
    cycle *before* this login even creates its own session row, so this
    login's own create()+rotate() never collides with anything and succeeds
    cleanly. Prior to the success-path revoke_others() fix, login() only
    re-asserted single-session on the KeyError fallback, so this interleaving
    left TWO live sessions for the user.

    Simulated deterministically via a SQLiteSessionStore subclass whose
    create() — on its first invocation only, which is the pre-rotation
    create() inside login() — runs a whole separate "other login" (its own
    revoke() + create() + rotate()) to completion first, then defers to the
    real create() for this login's own session. Because the "other" login's
    revoke() runs strictly before this login's own session exists, it can
    never delete it, so this login's own rotate() always succeeds.
    """
    from lmchat.config import get_settings
    from lmchat.db.schema import sessions as sessions_table
    from lmchat.session.sqlite_store import SQLiteSessionStore

    monkeypatch.setenv("LM_CHAT_SINGLE_SESSION", "true")
    get_settings.cache_clear()

    user = await _create_user_low_cost(db_engine)

    class _ConcurrentSuccessStore(SQLiteSessionStore):
        """create() injects a fully-completed concurrent login on first call."""

        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)  # type: ignore[arg-type]
            self._injected = False
            self.other_session_id: str | None = None

        async def create(self, *, user_id: int, ttl_seconds: int):  # noqa: ANN201
            if not self._injected and user_id == user.id:
                self._injected = True
                # A second, independent login for the SAME user runs to
                # completion here — before OUR OWN session exists — exactly
                # what single-session enforcement's revoke() does at the
                # start of another concurrent login().
                await self.revoke(user_id=user_id)
                other = await super().create(user_id=user_id, ttl_seconds=ttl_seconds)
                other = await super().rotate(other.id)
                self.other_session_id = other.id
            return await super().create(user_id=user_id, ttl_seconds=ttl_seconds)

    racey_store = _ConcurrentSuccessStore(engine=db_engine)

    session, _ = await login(
        username=user.username,
        password="correct-horse-battery",
        totp_code=None,
        ip=None,
        user_agent=None,
        session_store=racey_store,
        engine=db_engine,
        **LOW_COST,
    )

    assert racey_store.other_session_id is not None, (
        "test setup bug: the injected concurrent login never ran"
    )
    assert racey_store.other_session_id != session.id

    # login() must not have raised, and its returned session must be live.
    live = await racey_store.get(session.id)
    assert live is not None

    # Single-session invariant must hold: exactly one live session for the
    # user, even though NEITHER login's rotate() ever hit the KeyError path.
    async with db_engine.begin() as conn:
        result = await conn.execute(
            select(sessions_table).where(
                sessions_table.c.user_id == user.id,
                sessions_table.c.rotated_at.is_(None),
            )
        )
        live_rows = result.fetchall()
    assert len(live_rows) == 1, (
        f"expected exactly one live session after a success/success race; "
        f"found {len(live_rows)}"
    )
    assert live_rows[0].id == session.id

    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# logout
# ---------------------------------------------------------------------------


async def test_logout_revokes_session(
    db_engine: AsyncEngine, store: SQLiteSessionStore
) -> None:
    """logout() revokes the specified session."""
    user = await _create_user_low_cost(db_engine)
    session, _ = await login(
        username=user.username,
        password="correct-horse-battery",
        totp_code=None,
        ip=None,
        user_agent=None,
        session_store=store,
        engine=db_engine,
        **LOW_COST,
    )
    await logout(session_id=session.id, session_store=store, engine=db_engine)
    result = await store.get(session.id)
    assert result is None


async def test_logout_writes_audit_log(
    db_engine: AsyncEngine, store: SQLiteSessionStore
) -> None:
    """logout() writes an auth.logout audit row."""
    user = await _create_user_low_cost(db_engine)
    session, _ = await login(
        username=user.username,
        password="correct-horse-battery",
        totp_code=None,
        ip=None,
        user_agent=None,
        session_store=store,
        engine=db_engine,
        **LOW_COST,
    )
    await logout(session_id=session.id, session_store=store, engine=db_engine)
    count = await _count_audit_rows(db_engine, "auth.logout")
    assert count == 1


# ---------------------------------------------------------------------------
# Loud-on-failure escalation for the security-critical event subset: a
# failed audit write for login success/failure or logout must escalate
# to an ERROR log + metric increment instead of vanishing into the old
# best-effort WARNING — while the user-visible action (login/logout)
# must still succeed regardless (proceed-but-loud, never blocking).
# ---------------------------------------------------------------------------


async def test_login_success_survives_audit_write_failure_and_logs_error(
    db_engine: AsyncEngine, store: SQLiteSessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """login() still succeeds when the auth.login.success audit write fails."""
    from lmchat.services import audit_service

    user = await _create_user_low_cost(db_engine)

    async def _boom(**_kwargs: object) -> None:
        raise RuntimeError("simulated DB write failure")

    monkeypatch.setattr(audit_service, "write_audit_event", _boom)

    error_events: list[str] = []
    original_error = audit_service.log.error

    def _spy_error(event: str, **kwargs: object) -> None:
        error_events.append(event)
        original_error(event, **kwargs)

    monkeypatch.setattr(audit_service.log, "error", _spy_error)

    before = audit_service.AUDIT_WRITE_FAILURES_CRITICAL.labels(
        event="auth.login.success"
    )._value.get()

    # Must NOT raise — the action (login) has already succeeded and must not
    # be blocked or rolled back by a downstream audit-write failure.
    session, returned_user = await login(
        username=user.username,
        password="correct-horse-battery",
        totp_code=None,
        ip="127.0.0.1",
        user_agent="test",
        session_store=store,
        engine=db_engine,
        **LOW_COST,
    )

    assert session is not None
    assert returned_user.id == user.id

    after = audit_service.AUDIT_WRITE_FAILURES_CRITICAL.labels(
        event="auth.login.success"
    )._value.get()
    assert after == before + 1
    assert error_events == [
        "critical audit write failed — compliance record incomplete"
    ]

    # The row genuinely did not land (the simulated DB write itself failed).
    count = await _count_audit_rows(db_engine, "auth.login.success")
    assert count == 0


async def test_login_failure_survives_audit_write_failure_and_logs_error(
    db_engine: AsyncEngine, store: SQLiteSessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """login() still raises BadCredentialsError when the audit write fails.

    The auth.login.failure audit-write failure is escalated loudly (ERROR +
    metric); the actual auth-failure behavior is unaffected.
    """
    from lmchat.services import audit_service

    user = await _create_user_low_cost(db_engine)

    async def _boom(**_kwargs: object) -> None:
        raise RuntimeError("simulated DB write failure")

    monkeypatch.setattr(audit_service, "write_audit_event", _boom)

    error_events: list[str] = []
    original_error = audit_service.log.error

    def _spy_error(event: str, **kwargs: object) -> None:
        error_events.append(event)
        original_error(event, **kwargs)

    monkeypatch.setattr(audit_service.log, "error", _spy_error)

    before = audit_service.AUDIT_WRITE_FAILURES_CRITICAL.labels(
        event="auth.login.failure"
    )._value.get()

    with pytest.raises(BadCredentialsError):
        await login(
            username=user.username,
            password="wrong",
            totp_code=None,
            ip=None,
            user_agent=None,
            session_store=store,
            engine=db_engine,
            **LOW_COST,
        )

    after = audit_service.AUDIT_WRITE_FAILURES_CRITICAL.labels(
        event="auth.login.failure"
    )._value.get()
    assert after == before + 1
    assert error_events == [
        "critical audit write failed — compliance record incomplete"
    ]


async def test_logout_survives_audit_write_failure_and_logs_error(
    db_engine: AsyncEngine, store: SQLiteSessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """logout() still revokes the session when the auth.logout audit write fails."""
    from lmchat.services import audit_service

    user = await _create_user_low_cost(db_engine)
    session, _ = await login(
        username=user.username,
        password="correct-horse-battery",
        totp_code=None,
        ip=None,
        user_agent=None,
        session_store=store,
        engine=db_engine,
        **LOW_COST,
    )

    async def _boom(**_kwargs: object) -> None:
        raise RuntimeError("simulated DB write failure")

    monkeypatch.setattr(audit_service, "write_audit_event", _boom)

    error_events: list[str] = []
    original_error = audit_service.log.error

    def _spy_error(event: str, **kwargs: object) -> None:
        error_events.append(event)
        original_error(event, **kwargs)

    monkeypatch.setattr(audit_service.log, "error", _spy_error)

    before = audit_service.AUDIT_WRITE_FAILURES_CRITICAL.labels(
        event="auth.logout"
    )._value.get()

    # Must not raise — session revocation already happened.
    await logout(session_id=session.id, session_store=store, engine=db_engine)

    after = audit_service.AUDIT_WRITE_FAILURES_CRITICAL.labels(
        event="auth.logout"
    )._value.get()
    assert after == before + 1
    assert error_events == [
        "critical audit write failed — compliance record incomplete"
    ]

    # Session revocation still happened despite the audit failure.
    result = await store.get(session.id)
    assert result is None


async def test_logout_idempotent_on_missing_session(
    db_engine: AsyncEngine, store: SQLiteSessionStore
) -> None:
    """logout() on a non-existent session is a no-op (no error)."""
    await logout(
        session_id="nonexistent-token-xyz",
        session_store=store,
        engine=db_engine,
    )
    # No exception — that's the contract.


# ---------------------------------------------------------------------------
# change_password
# ---------------------------------------------------------------------------


async def test_change_password_verifies_old(
    db_engine: AsyncEngine, store: SQLiteSessionStore
) -> None:
    """change_password() raises BadCredentialsError if old_password is wrong."""
    user = await _create_user_low_cost(db_engine)
    with pytest.raises(BadCredentialsError):
        await change_password(
            user_id=user.id,
            old_password="wrong-old-password",
            new_password="new-password-123",
            current_session_id=None,
            session_store=store,
            engine=db_engine,
            **LOW_COST,
        )


async def test_change_password_updates_hash(
    db_engine: AsyncEngine, store: SQLiteSessionStore
) -> None:
    """change_password() updates the stored password hash."""
    user = await _create_user_low_cost(db_engine)
    old_hash = user.password_hash

    await change_password(
        user_id=user.id,
        old_password="correct-horse-battery",
        new_password="new-password-123",
        current_session_id=None,
        session_store=store,
        engine=db_engine,
        **LOW_COST,
    )

    row = await _get_user_row(db_engine, user.id)
    assert row is not None
    assert row.password_hash != old_hash
    assert row.password_hash.startswith("scrypt$")


async def test_change_password_revokes_other_sessions(
    db_engine: AsyncEngine, store: SQLiteSessionStore
) -> None:
    """change_password() revokes sessions other than current_session_id."""
    user = await _create_user_low_cost(db_engine)
    # Create two sessions directly.
    session_current = await store.create(user_id=user.id, ttl_seconds=3600)
    session_other = await store.create(user_id=user.id, ttl_seconds=3600)

    await change_password(
        user_id=user.id,
        old_password="correct-horse-battery",
        new_password="new-pw",
        current_session_id=session_current.id,
        session_store=store,
        engine=db_engine,
        **LOW_COST,
    )

    # Other session should be gone.
    assert await store.get(session_other.id) is None


async def test_change_password_keeps_current_session(
    db_engine: AsyncEngine, store: SQLiteSessionStore
) -> None:
    """change_password() preserves the current session (stay logged in)."""
    user = await _create_user_low_cost(db_engine)
    session_current = await store.create(user_id=user.id, ttl_seconds=3600)
    await store.create(user_id=user.id, ttl_seconds=3600)

    await change_password(
        user_id=user.id,
        old_password="correct-horse-battery",
        new_password="new-pw",
        current_session_id=session_current.id,
        session_store=store,
        engine=db_engine,
        **LOW_COST,
    )

    # Current session must still be valid.
    assert await store.get(session_current.id) is not None


# ---------------------------------------------------------------------------
# setup_totp
# ---------------------------------------------------------------------------


async def test_setup_totp_returns_provisioning_uri_and_secret(
    db_engine: AsyncEngine,
) -> None:
    """setup_totp() returns a (provisioning_uri, secret) tuple."""
    user = await _create_user_low_cost(db_engine)
    uri, secret = await setup_totp(user_id=user.id, engine=db_engine)

    assert uri.startswith("otpauth://totp/")
    assert len(secret) > 8  # base32 TOTP secret is at least 16 chars


async def test_setup_totp_does_not_persist_secret(db_engine: AsyncEngine) -> None:
    """setup_totp() does NOT write the secret to the DB."""
    user = await _create_user_low_cost(db_engine)
    await setup_totp(user_id=user.id, engine=db_engine)

    row = await _get_user_row(db_engine, user.id)
    assert row is not None
    assert row.totp_secret is None


# ---------------------------------------------------------------------------
# verify_totp_setup
# ---------------------------------------------------------------------------


async def test_verify_totp_setup_persists_encrypted_secret(
    db_engine: AsyncEngine,
) -> None:
    """verify_totp_setup() writes an enc$v1$ envelope to users.totp_secret."""
    user = await _create_user_low_cost(db_engine)
    _, secret = await setup_totp(user_id=user.id, engine=db_engine)
    code = pyotp.TOTP(secret).now()

    await verify_totp_setup(
        user_id=user.id, secret=secret, code=code, engine=db_engine
    )

    row = await _get_user_row(db_engine, user.id)
    assert row is not None
    assert row.totp_secret is not None
    assert row.totp_secret.startswith("enc$v1$")


async def test_verify_totp_setup_wrong_code_does_not_persist(
    db_engine: AsyncEngine,
) -> None:
    """verify_totp_setup() with a wrong code leaves totp_secret unchanged."""
    user = await _create_user_low_cost(db_engine)
    _, secret = await setup_totp(user_id=user.id, engine=db_engine)

    with pytest.raises(BadCredentialsError):
        await verify_totp_setup(
            user_id=user.id, secret=secret, code="000000", engine=db_engine
        )

    row = await _get_user_row(db_engine, user.id)
    assert row is not None
    assert row.totp_secret is None


# ---------------------------------------------------------------------------
# disable_totp
# ---------------------------------------------------------------------------


async def test_disable_totp_requires_password(db_engine: AsyncEngine) -> None:
    """disable_totp() raises BadCredentialsError if password is wrong."""
    user = await _create_user_low_cost(db_engine)
    await _setup_totp_for_user(db_engine, user.id)

    with pytest.raises(BadCredentialsError):
        await disable_totp(
            user_id=user.id, password="wrong-pw", engine=db_engine
        )


async def test_disable_totp_clears_secret(db_engine: AsyncEngine) -> None:
    """disable_totp() sets totp_secret to NULL on success."""
    user = await _create_user_low_cost(db_engine)
    await _setup_totp_for_user(db_engine, user.id)

    await disable_totp(
        user_id=user.id, password="correct-horse-battery", engine=db_engine
    )

    row = await _get_user_row(db_engine, user.id)
    assert row is not None
    assert row.totp_secret is None


async def test_disable_totp_not_configured_raises(db_engine: AsyncEngine) -> None:
    """disable_totp() raises TotpNotConfiguredError when no TOTP is active."""
    user = await _create_user_low_cost(db_engine)
    with pytest.raises(TotpNotConfiguredError):
        await disable_totp(
            user_id=user.id, password="correct-horse-battery", engine=db_engine
        )


# ---------------------------------------------------------------------------
# get_current_user — FastAPI dependency
# ---------------------------------------------------------------------------


class _FakeRequest:
    """Minimal request stand-in for get_current_user tests."""

    def __init__(
        self,
        *,
        cookies: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.cookies: dict[str, str] = cookies or {}
        self.headers: dict[str, str] = headers or {}


async def test_get_current_user_returns_user_for_valid_session(
    db_engine: AsyncEngine, store: SQLiteSessionStore
) -> None:
    """get_current_user() returns the User for a valid session cookie."""
    user = await _create_user_low_cost(db_engine)
    session = await store.create(user_id=user.id, ttl_seconds=3600)

    request = _FakeRequest(cookies={"lmchat_session": session.id})
    result = await get_current_user(
        request=request,  # type: ignore[arg-type]
        session_store=store,
        engine=db_engine,
    )
    assert result is not None
    assert result.id == user.id


async def test_get_current_user_returns_none_for_missing_session(
    db_engine: AsyncEngine, store: SQLiteSessionStore
) -> None:
    """get_current_user() returns None when no session cookie is present."""
    request = _FakeRequest()
    result = await get_current_user(
        request=request,  # type: ignore[arg-type]
        session_store=store,
        engine=db_engine,
    )
    assert result is None


async def test_get_current_user_returns_none_for_expired_session(
    db_engine: AsyncEngine, store: SQLiteSessionStore
) -> None:
    """get_current_user() returns None when the session has expired."""
    from datetime import UTC, datetime

    user = await _create_user_low_cost(db_engine)
    session = await store.create(user_id=user.id, ttl_seconds=3600)

    # Expire the session directly in the DB.
    past = datetime(2000, 1, 1, tzinfo=UTC)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET expires_at = :exp WHERE id = :sid"),
            {"exp": past.isoformat(), "sid": session.id},
        )

    request = _FakeRequest(cookies={"lmchat_session": session.id})
    result = await get_current_user(
        request=request,  # type: ignore[arg-type]
        session_store=store,
        engine=db_engine,
    )
    assert result is None


async def test_get_current_user_bearer_token(
    db_engine: AsyncEngine, store: SQLiteSessionStore
) -> None:
    """get_current_user() falls back to Authorization Bearer header."""
    user = await _create_user_low_cost(db_engine)
    session = await store.create(user_id=user.id, ttl_seconds=3600)

    request = _FakeRequest(headers={"Authorization": f"Bearer {session.id}"})
    result = await get_current_user(
        request=request,  # type: ignore[arg-type]
        session_store=store,
        engine=db_engine,
    )
    assert result is not None
    assert result.id == user.id


# ---------------------------------------------------------------------------
# P13f — count_users helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_count_users_empty_returns_zero(db_engine: AsyncEngine) -> None:
    """An empty users table yields count_users == 0."""
    n = await count_users(engine=db_engine)
    assert n == 0


@pytest.mark.asyncio
async def test_count_users_after_register_returns_one(
    db_engine: AsyncEngine,
) -> None:
    """One register call yields count_users == 1."""
    await register(
        username="alice",
        password="pw-with-enough-length",
        engine=db_engine,
        **_LOW_COST,
    )
    n = await count_users(engine=db_engine)
    assert n == 1


@pytest.mark.asyncio
async def test_count_users_after_two_registers_returns_two(
    db_engine: AsyncEngine,
) -> None:
    """Two register calls yield count_users == 2."""
    await register(
        username="alice",
        password="pw-with-enough-length",
        engine=db_engine,
        **_LOW_COST,
    )
    await register(
        username="bob",
        password="pw-with-enough-length",
        engine=db_engine,
        **_LOW_COST,
    )
    n = await count_users(engine=db_engine)
    assert n == 2


# ---------------------------------------------------------------------------
# login() session-token rotation (AUTH-SESSION): SQLiteSessionStore.rotate()
# had zero production callers before this wiring. login() now rotates the
# freshly-created session before returning it — defense-in-depth against
# session fixation (OWASP: issue a new session identifier on every
# authentication transition) even though this codebase has no pre-auth
# session concept today (create() always mints a fresh, unpredictable token
# independent of any client-supplied cookie).
# ---------------------------------------------------------------------------


async def test_login_rotates_session_token_after_creation(
    db_engine: AsyncEngine, store: SQLiteSessionStore
) -> None:
    """login() must rotate the session it just created before returning it.

    RED-ON-REVERT: without the rotate-at-login wiring, login() returns
    the exact row session_store.create() inserted (rotated_at left
    None) — only one session row exists and its id matches what was
    returned. After the fix, that first row is superseded (rotated_at
    set) and login() returns a DIFFERENT, second token.
    """
    from sqlalchemy import select

    from lmchat.db.schema import sessions as sessions_table

    user = await _create_user_low_cost(db_engine)

    session, _ = await login(
        username=user.username,
        password="correct-horse-battery",
        totp_code=None,
        ip=None,
        user_agent=None,
        session_store=store,
        engine=db_engine,
        **LOW_COST,
    )

    async with db_engine.begin() as conn:
        result = await conn.execute(
            select(sessions_table).where(sessions_table.c.user_id == user.id)
        )
        rows = result.fetchall()

    assert len(rows) == 2, (
        f"Expected create() + rotate() to leave 2 rows (pre- and "
        f"post-rotation) for this login; found {len(rows)}"
    )
    rotated_rows = [r for r in rows if r.rotated_at is not None]
    live_rows = [r for r in rows if r.rotated_at is None]
    assert len(rotated_rows) == 1, "exactly one row must be marked rotated"
    assert len(live_rows) == 1, "exactly one live (non-rotated) row must remain"
    assert live_rows[0].id == session.id, (
        "login() must return the ROTATED (final) token, not the "
        "pre-rotation one session_store.create() minted"
    )
    assert rotated_rows[0].id != session.id
