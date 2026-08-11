# SPDX-License-Identifier: Apache-2.0
"""Tests that chat_service.delete() notifies memory_service post-commit.

Tests that chat deletion notifies the memory service.

Verifies that handle_message_deleted is called once per affected message_id,
and that the calls happen AFTER the delete transaction has committed.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.pragmas import apply_sqlite_pragmas
from lmchat.db.schema import messages, metadata
from lmchat.services.chat_service import ChatService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    """Yield a fresh per-test SQLite engine with FK pragmas applied."""
    db_path = tmp_path / "test_delete_notify.db"
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


async def _insert_message(
    engine: AsyncEngine,
    chat_id: int,
    content: str = "msg",
) -> int:
    """Insert a message row and return its id."""
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "INSERT INTO messages (chat_id, role, content)"
                " VALUES (:cid, 'user', :c)"
            ),
            {"cid": chat_id, "c": content},
        )
        return result.lastrowid  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_chat_delete_calls_handle_message_deleted_for_every_message(
    engine: AsyncEngine,
) -> None:
    """delete() calls handle_message_deleted once per message_id, post-commit."""
    await _insert_user(engine)

    # Track whether rows are gone by the time the mock is called.
    deleted_at_call: list[bool] = []

    async def _mock_handle_deleted(message_id: int) -> None:
        # Verify the row is gone from the DB at notification time (post-commit).
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    select(messages).where(messages.c.id == message_id)
                )
            ).fetchone()
        deleted_at_call.append(row is None)

    memory_mock = MagicMock()
    memory_mock.handle_message_deleted = AsyncMock(side_effect=_mock_handle_deleted)
    models_mock = MagicMock()
    models_mock.get_capabilities = AsyncMock(side_effect=KeyError("no model"))

    svc = ChatService(
        engine=engine,
        memory_service=memory_mock,
        models_service=models_mock,
        chat_locks={},
    )

    # Create a chat with 3 messages.
    chat = await svc.create(user_id=1, title="Chat")
    mid1 = await _insert_message(engine, chat.id, content="msg1")
    mid2 = await _insert_message(engine, chat.id, content="msg2")
    mid3 = await _insert_message(engine, chat.id, content="msg3")

    await svc.delete(chat.id, user_id=1)

    # Exactly 3 calls, one per message.
    assert memory_mock.handle_message_deleted.call_count == 3

    # Each call had its message already deleted (post-commit).
    assert all(deleted_at_call), (
        "handle_message_deleted was called before the delete transaction committed"
    )

    # All three message ids were notified.
    notified_ids = {
        c.args[0]
        for c in memory_mock.handle_message_deleted.call_args_list
    }
    assert notified_ids == {mid1, mid2, mid3}


async def test_chat_delete_notification_failure_does_not_raise(
    engine: AsyncEngine,
) -> None:
    """delete() succeeds even if handle_message_deleted raises for some message."""
    await _insert_user(engine)

    memory_mock = MagicMock()
    memory_mock.handle_message_deleted = AsyncMock(side_effect=RuntimeError("P3 down"))
    models_mock = MagicMock()
    models_mock.get_capabilities = AsyncMock(side_effect=KeyError("no model"))

    svc = ChatService(
        engine=engine,
        memory_service=memory_mock,
        models_service=models_mock,
        chat_locks={},
    )

    chat = await svc.create(user_id=1, title="Chat")
    await _insert_message(engine, chat.id)

    # Should not raise despite the failing mock.
    await svc.delete(chat.id, user_id=1)
