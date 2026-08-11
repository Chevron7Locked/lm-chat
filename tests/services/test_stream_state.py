# SPDX-License-Identifier: Apache-2.0
"""Contract tests for _stream_state — atomic state-machine transitions.

Tests for stream state management.

Every test uses a per-test tmp_path SQLite engine with the schema
bootstrapped via ``metadata.create_all``.  No Alembic migration is
required here: the ``messages.state`` column is defined in schema.py and
``create_all`` is sufficient for unit-test isolation.

Atomicity guarantee under test
-------------------------------
``transition()`` issues a single ``UPDATE messages SET state=:to WHERE
id=:mid AND state=:from``.  The concurrent test (test_transition_is_atomic_
under_concurrent_attempts) races two coroutines on the same row and asserts
that exactly one wins ``True`` and the other wins ``False``.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.pragmas import apply_sqlite_pragmas
from lmchat.db.schema import messages, metadata
from lmchat.services._stream_state import (
    PersistState,
    finalize_pending,
    safe_abort_draft,
    transition,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    """Yield a per-test in-memory-backed SQLite engine with the full schema."""
    db_path = tmp_path / "test_stream_state.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}", pool_pre_ping=True)

    @event.listens_for(eng.sync_engine, "connect")
    def _on_connect(dbapi_conn: object, _rec: object) -> None:
        apply_sqlite_pragmas(dbapi_conn)

    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


async def _insert_user(engine: AsyncEngine, user_id: int = 1) -> None:
    """Insert a minimal user row for FK satisfaction."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO users (id, username, password_hash)"
                " VALUES (:id, :u, :ph)"
            ),
            {"id": user_id, "u": f"user{user_id}", "ph": "scrypt$dummy"},
        )


async def _insert_chat(engine: AsyncEngine, user_id: int = 1) -> int:
    """Insert a minimal chat row and return its id."""
    async with engine.begin() as conn:
        result = await conn.execute(
            text("INSERT INTO chats (user_id, title) VALUES (:uid, :t)"),
            {"uid": user_id, "t": "test-chat"},
        )
        return result.lastrowid  # type: ignore[return-value]


async def _insert_message(
    engine: AsyncEngine,
    chat_id: int,
    state: str = "draft",
) -> int:
    """Insert a message row with the given state and return its id."""
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "INSERT INTO messages (chat_id, role, content, state)"
                " VALUES (:cid, 'assistant', '', :state)"
            ),
            {"cid": chat_id, "state": state},
        )
        return result.lastrowid  # type: ignore[return-value]


async def _read_state(engine: AsyncEngine, message_id: int) -> str:
    """Return the current state string for *message_id*."""
    async with engine.connect() as conn:
        result = await conn.execute(
            select(messages.c.state).where(messages.c.id == message_id)
        )
        row = result.fetchone()
    assert row is not None, f"message {message_id} not found"
    return str(row[0])


# ---------------------------------------------------------------------------
# PersistState enum
# ---------------------------------------------------------------------------


def test_persist_state_values_match_db_strings() -> None:
    """PersistState is a str-enum; each value equals the DB column string."""
    assert PersistState.DRAFT == "draft"
    assert PersistState.PENDING_FINALIZATION == "pending_finalization"
    assert PersistState.FINAL == "final"
    assert PersistState.ABORTED_BY_CLIENT == "aborted_by_client"
    # Can be used directly in string contexts without .value.
    assert f"{PersistState.DRAFT}" == "draft"


# ---------------------------------------------------------------------------
# transition()
# ---------------------------------------------------------------------------


async def test_transition_draft_to_pending_succeeds(engine: AsyncEngine) -> None:
    """transition() returns True and moves the row when from_state matches."""
    await _insert_user(engine)
    chat_id = await _insert_chat(engine)
    msg_id = await _insert_message(engine, chat_id, state="draft")

    won = await transition(
        engine=engine,
        message_id=msg_id,
        from_state=PersistState.DRAFT,
        to_state=PersistState.PENDING_FINALIZATION,
    )

    assert won is True
    assert await _read_state(engine, msg_id) == "pending_finalization"


async def test_transition_returns_false_on_state_mismatch(engine: AsyncEngine) -> None:
    """transition() returns False when the row is not in from_state (race lost)."""
    await _insert_user(engine)
    chat_id = await _insert_chat(engine)
    # Row starts as 'final' (already past draft).
    msg_id = await _insert_message(engine, chat_id, state="final")

    won = await transition(
        engine=engine,
        message_id=msg_id,
        from_state=PersistState.DRAFT,  # wrong from_state
        to_state=PersistState.PENDING_FINALIZATION,
    )

    assert won is False
    # Row must be unchanged.
    assert await _read_state(engine, msg_id) == "final"


async def test_transition_returns_false_for_nonexistent_row(engine: AsyncEngine) -> None:
    """transition() returns False when the message_id does not exist."""
    await _insert_user(engine)

    won = await transition(
        engine=engine,
        message_id=99999,
        from_state=PersistState.DRAFT,
        to_state=PersistState.PENDING_FINALIZATION,
    )

    assert won is False


async def test_transition_is_atomic_under_concurrent_attempts(engine: AsyncEngine) -> None:
    """Only one of two concurrent transition() calls wins the race.

    Both coroutines attempt the exact same transition on the same row.
    Exactly one must return True (rowcount==1) and the other False
    (rowcount==0).  This exercises the WHERE-predicate atomicity guarantee.
    """
    await _insert_user(engine)
    chat_id = await _insert_chat(engine)
    msg_id = await _insert_message(engine, chat_id, state="draft")

    # Launch both concurrently; gather preserves ordering of results.
    results = await asyncio.gather(
        transition(
            engine=engine,
            message_id=msg_id,
            from_state=PersistState.DRAFT,
            to_state=PersistState.PENDING_FINALIZATION,
        ),
        transition(
            engine=engine,
            message_id=msg_id,
            from_state=PersistState.DRAFT,
            to_state=PersistState.PENDING_FINALIZATION,
        ),
    )

    winners = sum(1 for r in results if r is True)
    losers = sum(1 for r in results if r is False)

    assert winners == 1, f"Expected exactly 1 winner, got {winners}"
    assert losers == 1, f"Expected exactly 1 loser, got {losers}"

    # Row must be in the target state.
    assert await _read_state(engine, msg_id) == "pending_finalization"


# ---------------------------------------------------------------------------
# safe_abort_draft()
# ---------------------------------------------------------------------------


async def test_safe_abort_draft_succeeds_on_draft(engine: AsyncEngine) -> None:
    """safe_abort_draft() moves a draft row to aborted_by_client."""
    await _insert_user(engine)
    chat_id = await _insert_chat(engine)
    msg_id = await _insert_message(engine, chat_id, state="draft")

    result = await safe_abort_draft(engine=engine, message_id=msg_id)

    assert result is True
    assert await _read_state(engine, msg_id) == "aborted_by_client"


async def test_safe_abort_draft_returns_false_if_pending_finalization(
    engine: AsyncEngine,
) -> None:
    """safe_abort_draft() returns False when the row is in pending_finalization.

    This is the normal race: upstream finished first, moving the row to
    pending_finalization before the disconnect handler ran.  The abort
    must be a no-op; the caller must not treat this as an error.
    """
    await _insert_user(engine)
    chat_id = await _insert_chat(engine)
    msg_id = await _insert_message(engine, chat_id, state="pending_finalization")

    result = await safe_abort_draft(engine=engine, message_id=msg_id)

    assert result is False
    # Row must remain in pending_finalization.
    assert await _read_state(engine, msg_id) == "pending_finalization"


async def test_safe_abort_draft_returns_false_if_final(engine: AsyncEngine) -> None:
    """safe_abort_draft() returns False when the row is already final."""
    await _insert_user(engine)
    chat_id = await _insert_chat(engine)
    msg_id = await _insert_message(engine, chat_id, state="final")

    result = await safe_abort_draft(engine=engine, message_id=msg_id)

    assert result is False
    assert await _read_state(engine, msg_id) == "final"


# ---------------------------------------------------------------------------
# finalize_pending()
# ---------------------------------------------------------------------------


async def test_finalize_pending_succeeds(engine: AsyncEngine) -> None:
    """finalize_pending() moves pending_finalization → final."""
    await _insert_user(engine)
    chat_id = await _insert_chat(engine)
    msg_id = await _insert_message(engine, chat_id, state="pending_finalization")

    result = await finalize_pending(engine=engine, message_id=msg_id)

    assert result is True
    assert await _read_state(engine, msg_id) == "final"


async def test_finalize_pending_returns_false_if_already_final(engine: AsyncEngine) -> None:
    """finalize_pending() is idempotent — returns False when already final.

    Concurrent reaper instances race on the same row; only one wins
    rowcount==1.  Subsequent callers see rowcount==0 and must return False.
    """
    await _insert_user(engine)
    chat_id = await _insert_chat(engine)
    msg_id = await _insert_message(engine, chat_id, state="final")

    result = await finalize_pending(engine=engine, message_id=msg_id)

    assert result is False
    # Row remains final.
    assert await _read_state(engine, msg_id) == "final"


async def test_finalize_pending_returns_false_if_draft(engine: AsyncEngine) -> None:
    """finalize_pending() returns False when the row is still in draft state."""
    await _insert_user(engine)
    chat_id = await _insert_chat(engine)
    msg_id = await _insert_message(engine, chat_id, state="draft")

    result = await finalize_pending(engine=engine, message_id=msg_id)

    assert result is False
    assert await _read_state(engine, msg_id) == "draft"
