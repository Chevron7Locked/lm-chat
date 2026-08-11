# SPDX-License-Identifier: Apache-2.0
"""Contract tests for _stream_reaper — background draft cleanup.

Tests for the stream reaper.

Each test uses a per-test tmp_path SQLite engine bootstrapped via
``metadata.create_all``.  Internal helpers (``_recover_pending``,
``_reap_old_drafts``) are tested directly to avoid needing to tick the
full reaper loop; ``run_reaper`` cancellation is tested via
``asyncio.create_task`` + ``task.cancel()``.

Time-travel strategy
--------------------
SQLite stores timestamps as text in the format ``YYYY-MM-DD HH:MM:SS.ffffff``.
The ``messages.created_at`` column has a ``server_default=func.now()`` which
fires at INSERT time.  To simulate old rows, we INSERT the row with an
explicit ``created_at`` in the past rather than monkeypatching datetime.
This avoids patching anything outside our modules.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.pragmas import apply_sqlite_pragmas
from lmchat.db.schema import audit_log, messages, metadata
from lmchat.services._stream_reaper import _reap_old_drafts, _recover_pending, run_reaper

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    """Yield a per-test SQLite engine with the full schema."""
    db_path = tmp_path / "test_stream_reaper.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}", pool_pre_ping=True)

    @event.listens_for(eng.sync_engine, "connect")
    def _on_connect(dbapi_conn: object, _rec: object) -> None:
        apply_sqlite_pragmas(dbapi_conn)

    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


async def _insert_user(engine: AsyncEngine, user_id: int = 1) -> None:
    """Insert a minimal user row."""
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


async def _insert_message_at(
    engine: AsyncEngine,
    chat_id: int,
    state: str,
    created_at: datetime,
) -> int:
    """Insert a message with an explicit created_at timestamp."""
    ts = created_at.strftime("%Y-%m-%d %H:%M:%S.%f")
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "INSERT INTO messages (chat_id, role, content, state, created_at)"
                " VALUES (:cid, 'assistant', '', :state, :ts)"
            ),
            {"cid": chat_id, "state": state, "ts": ts},
        )
        return result.lastrowid  # type: ignore[return-value]


async def _read_state(engine: AsyncEngine, message_id: int) -> str | None:
    """Return the state for *message_id*, or None if the row is gone."""
    async with engine.connect() as conn:
        result = await conn.execute(
            select(messages.c.state).where(messages.c.id == message_id)
        )
        row = result.fetchone()
    return str(row[0]) if row is not None else None


async def _count_audit_rows(engine: AsyncEngine, event_name: str) -> int:
    """Return how many audit_log rows have the given event name."""
    async with engine.connect() as conn:
        result = await conn.execute(
            select(audit_log).where(audit_log.c.event == event_name)
        )
        return len(result.fetchall())


# ---------------------------------------------------------------------------
# _recover_pending() tests
# ---------------------------------------------------------------------------


async def test_reaper_finalizes_pending_rows_older_than_threshold(
    engine: AsyncEngine,
) -> None:
    """_recover_pending() transitions old pending_finalization rows to final."""
    await _insert_user(engine)
    chat_id = await _insert_chat(engine)

    # Row created 10 seconds ago — older than the 5s threshold.
    old_ts = datetime.now(UTC) - timedelta(seconds=10)
    msg_id = await _insert_message_at(engine, chat_id, "pending_finalization", old_ts)

    await _recover_pending(engine=engine, finalization_timeout_sec=5)

    assert await _read_state(engine, msg_id) == "final"


async def test_reaper_skips_pending_rows_below_threshold(
    engine: AsyncEngine,
) -> None:
    """_recover_pending() leaves recent pending_finalization rows untouched."""
    await _insert_user(engine)
    chat_id = await _insert_chat(engine)

    # Row created 2 seconds ago — below the 5s threshold.
    fresh_ts = datetime.now(UTC) - timedelta(seconds=2)
    msg_id = await _insert_message_at(engine, chat_id, "pending_finalization", fresh_ts)

    await _recover_pending(engine=engine, finalization_timeout_sec=5)

    # Row must remain in pending_finalization.
    assert await _read_state(engine, msg_id) == "pending_finalization"


async def test_reaper_skips_final_rows_in_recover_pending(
    engine: AsyncEngine,
) -> None:
    """_recover_pending() does not touch rows already in 'final' state."""
    await _insert_user(engine)
    chat_id = await _insert_chat(engine)

    old_ts = datetime.now(UTC) - timedelta(seconds=30)
    msg_id = await _insert_message_at(engine, chat_id, "final", old_ts)

    await _recover_pending(engine=engine, finalization_timeout_sec=5)

    assert await _read_state(engine, msg_id) == "final"


# ---------------------------------------------------------------------------
# _reap_old_drafts() tests
# ---------------------------------------------------------------------------


async def test_reaper_deletes_draft_rows_older_than_24h(
    engine: AsyncEngine,
) -> None:
    """_reap_old_drafts() deletes draft rows older than draft_max_age_hours."""
    await _insert_user(engine)
    chat_id = await _insert_chat(engine)

    # Row is 25 hours old — over the 24h threshold.
    old_ts = datetime.now(UTC) - timedelta(hours=25)
    msg_id = await _insert_message_at(engine, chat_id, "draft", old_ts)

    await _reap_old_drafts(engine=engine, draft_max_age_hours=24)

    # Row must be gone.
    assert await _read_state(engine, msg_id) is None


async def test_reaper_writes_audit_log_on_draft_reap(
    engine: AsyncEngine,
) -> None:
    """_reap_old_drafts() writes one stream.draft_reaped audit row per deleted message."""
    await _insert_user(engine)
    chat_id = await _insert_chat(engine)

    old_ts = datetime.now(UTC) - timedelta(hours=25)
    msg_id = await _insert_message_at(engine, chat_id, "draft", old_ts)

    await _reap_old_drafts(engine=engine, draft_max_age_hours=24)

    count = await _count_audit_rows(engine, "stream.draft_reaped")
    assert count == 1, f"Expected 1 audit row, got {count}"

    # Verify the audit detail contains the message_id.
    async with engine.connect() as conn:
        result = await conn.execute(
            select(audit_log).where(audit_log.c.event == "stream.draft_reaped")
        )
        row = result.fetchone()

    assert row is not None
    detail = row.detail
    if isinstance(detail, str):
        detail = json.loads(detail)
    assert detail["message_id"] == msg_id
    assert "draft_age_hours" in detail


async def test_reaper_skips_drafts_below_max_age(
    engine: AsyncEngine,
) -> None:
    """_reap_old_drafts() leaves recent draft rows untouched."""
    await _insert_user(engine)
    chat_id = await _insert_chat(engine)

    # Row is only 1 hour old — below the 24h threshold.
    fresh_ts = datetime.now(UTC) - timedelta(hours=1)
    msg_id = await _insert_message_at(engine, chat_id, "draft", fresh_ts)

    await _reap_old_drafts(engine=engine, draft_max_age_hours=24)

    # Row must still exist.
    assert await _read_state(engine, msg_id) == "draft"
    # No audit rows should have been written.
    assert await _count_audit_rows(engine, "stream.draft_reaped") == 0


async def test_reaper_does_not_delete_non_draft_old_rows(
    engine: AsyncEngine,
) -> None:
    """_reap_old_drafts() does not delete 'final' or 'aborted_by_client' rows."""
    await _insert_user(engine)
    chat_id = await _insert_chat(engine)

    old_ts = datetime.now(UTC) - timedelta(hours=48)
    final_id = await _insert_message_at(engine, chat_id, "final", old_ts)
    aborted_id = await _insert_message_at(engine, chat_id, "aborted_by_client", old_ts)

    await _reap_old_drafts(engine=engine, draft_max_age_hours=24)

    assert await _read_state(engine, final_id) == "final"
    assert await _read_state(engine, aborted_id) == "aborted_by_client"
    assert await _count_audit_rows(engine, "stream.draft_reaped") == 0


async def test_reaper_writes_multiple_audit_rows_for_multiple_drafts(
    engine: AsyncEngine,
) -> None:
    """_reap_old_drafts() writes one audit row per deleted message, not one total."""
    await _insert_user(engine)
    chat_id = await _insert_chat(engine)

    old_ts = datetime.now(UTC) - timedelta(hours=30)
    msg_ids = [
        await _insert_message_at(engine, chat_id, "draft", old_ts)
        for _ in range(3)
    ]

    await _reap_old_drafts(engine=engine, draft_max_age_hours=24)

    for mid in msg_ids:
        assert await _read_state(engine, mid) is None, f"message {mid} should be deleted"

    count = await _count_audit_rows(engine, "stream.draft_reaped")
    assert count == 3, f"Expected 3 audit rows, got {count}"


# ---------------------------------------------------------------------------
# run_reaper() cancellation test
# ---------------------------------------------------------------------------


async def test_reaper_finalize_recovered_no_op_if_already_finalized(
    engine: AsyncEngine,
) -> None:
    """_recover_pending() logs 'already_done' when finalize_pending returns False.

    Simulates the race where the row is in pending_finalization when selected
    but another process finalizes it before our call wins the WHERE predicate.
    We patch finalize_pending to return False to exercise the else branch.
    """
    from unittest.mock import AsyncMock, patch

    await _insert_user(engine)
    chat_id = await _insert_chat(engine)

    old_ts = datetime.now(UTC) - timedelta(seconds=10)
    msg_id = await _insert_message_at(engine, chat_id, "pending_finalization", old_ts)

    # Patch finalize_pending to return False (simulates "another process got it first").
    with patch(
        "lmchat.services._stream_reaper.finalize_pending",
        new_callable=AsyncMock,
        return_value=False,
    ):
        # Should complete without error; "already_done" log branch covered.
        await _recover_pending(engine=engine, finalization_timeout_sec=5)

    # Row still in pending_finalization (our mock didn't actually change DB).
    assert await _read_state(engine, msg_id) == "pending_finalization"


async def test_recover_pending_handles_finalize_exception(
    engine: AsyncEngine,
) -> None:
    """_recover_pending() catches exceptions from finalize_pending and continues."""
    from unittest.mock import AsyncMock, patch

    await _insert_user(engine)
    chat_id = await _insert_chat(engine)

    old_ts = datetime.now(UTC) - timedelta(seconds=10)
    msg_id = await _insert_message_at(engine, chat_id, "pending_finalization", old_ts)

    # Patch finalize_pending to raise on the first call.
    with patch(
        "lmchat.services._stream_reaper.finalize_pending",
        new_callable=AsyncMock,
        side_effect=RuntimeError("DB exploded"),
    ):
        # Must not raise — exception is caught and logged per-row.
        await _recover_pending(engine=engine, finalization_timeout_sec=5)

    # Row still in pending_finalization (our mock didn't move it).
    assert await _read_state(engine, msg_id) == "pending_finalization"


async def test_reaper_cancellation_exits_cleanly(engine: AsyncEngine) -> None:
    """run_reaper() handles CancelledError and returns cleanly without raising.

    The reaper is started with interval_sec=0 so the sleep completes
    immediately, then after the first tick we cancel the task.  The task
    must exit with CancelledError caught internally (no unhandled propagation
    from the task itself); awaiting the task must NOT raise CancelledError.
    """
    await _insert_user(engine)

    task = asyncio.create_task(
        run_reaper(
            engine=engine,
            interval_sec=0,
            finalization_timeout_sec=5,
            draft_max_age_hours=24,
        )
    )

    # Let the reaper sleep (interval_sec=0 → effectively immediate) and
    # enter the tick.  Two yields is enough.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    task.cancel()

    # Awaiting a cancelled task raises CancelledError ONLY if the task
    # propagated it rather than catching it.  run_reaper catches it and
    # returns normally, so await must complete without raising.
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except asyncio.CancelledError:
        pytest.fail(
            "run_reaper() propagated CancelledError instead of handling it cleanly"
        )
    except TimeoutError:
        pytest.fail("run_reaper() did not exit within 2s after cancellation")
