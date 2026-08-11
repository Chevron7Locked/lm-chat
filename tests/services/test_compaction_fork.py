# SPDX-License-Identifier: Apache-2.0
"""Tests for ChatService.fork() remapping archived compaction spans.

fork() re-creates messages with NEW ids, so any `compaction_id`
FKs need remapping onto NEW `compactions` rows created on the fork chat —
copying the old row's summary/token counts and pointing `anchor_msg_id` at
the remapped message id.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.pragmas import apply_sqlite_pragmas
from lmchat.db.schema import compactions, messages, metadata
from lmchat.services.chat_service import ChatService, CompactResult

# ---------------------------------------------------------------------------
# Fixtures (mirrors tests/services/test_compaction.py)
# ---------------------------------------------------------------------------


@pytest.fixture()
async def engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    """Yield a fresh per-test SQLite engine with FK pragmas applied."""
    db_path = tmp_path / "test_compaction_fork.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}", pool_pre_ping=True)

    @event.listens_for(eng.sync_engine, "connect")
    def _on_connect(dbapi_conn: object, _rec: object) -> None:
        apply_sqlite_pragmas(dbapi_conn)

    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


async def _insert_user(engine: AsyncEngine, user_id: int = 1) -> None:
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
    role: str = "user",
    content: str = "hello",
) -> int:
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "INSERT INTO messages (chat_id, role, content)"
                " VALUES (:cid, :role, :content)"
            ),
            {"cid": chat_id, "role": role, "content": content},
        )
        return result.lastrowid  # type: ignore[return-value]


def _make_service(
    engine: AsyncEngine,
    chat_locks: dict[int, asyncio.Lock] | None = None,
) -> ChatService:
    memory_mock = MagicMock()
    memory_mock.handle_message_deleted = AsyncMock(return_value=None)
    models_mock = MagicMock()
    models_mock.resolve_to_loaded_or_fallback = AsyncMock(
        return_value=MagicMock(wire_id=None)
    )
    models_mock.get_capabilities = AsyncMock(side_effect=KeyError("no model"))
    # No synthetic message/chat in this file carries a model_id, so
    # compact()'s summary-model resolution falls through to list_loaded()
    # (tier 3). Default to a single loaded chat model so existing tests
    # keep passing unchanged.
    models_mock.list_loaded = AsyncMock(
        return_value=[MagicMock(key="qwen-test-7b", type="llm")]
    )
    return ChatService(
        engine=engine,
        memory_service=memory_mock,
        models_service=models_mock,
        chat_locks=chat_locks if chat_locks is not None else {},
    )


async def _compact(
    svc: ChatService,
    chat_id: int,
    *,
    user_id: int = 1,
    target_tokens: int,
    summary: str = "stub summary of the archived turns",
) -> CompactResult:
    """Call svc.compact() with the LLM-summary call stubbed (see test_compaction.py)."""
    svc._run_llm_distill = AsyncMock(return_value=summary)  # type: ignore[method-assign]
    return await svc.compact(
        chat_id,
        user_id=user_id,
        target_tokens=target_tokens,
        http_client=AsyncMock(),
        base_url="http://lm-studio.test",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_fork_remaps_archived_spans_onto_new_compactions_rows(
    engine: AsyncEngine,
) -> None:
    """fork() copies an archived span into a NEW compactions row with remapped FKs."""
    await _insert_user(engine)
    svc = _make_service(engine)
    chat = await svc.create(user_id=1, title="Chat")

    await _insert_message(engine, chat.id, role="assistant", content="old one two three")
    await _insert_message(engine, chat.id, role="assistant", content="old two two three")
    latest = await _insert_message(engine, chat.id, role="user", content="latest question here")

    result = await _compact(svc, chat.id, target_tokens=6)
    assert result.removed_message_ids, "expected something archived before fork"
    assert result.compaction_id is not None

    forked = await svc.fork(chat.id, user_id=1, at_message_id=latest)
    assert forked.id != chat.id

    # A new compactions row was created on the fork chat (not the same PK).
    async with engine.connect() as conn:
        fork_compaction_rows = (
            await conn.execute(
                select(compactions).where(compactions.c.chat_id == forked.id)
            )
        ).fetchall()
    assert len(fork_compaction_rows) == 1
    new_cid = fork_compaction_rows[0].id
    assert new_cid != result.compaction_id
    assert fork_compaction_rows[0].summary == result.summary

    # Token-count stats are copied verbatim from the source compaction row.
    async with engine.connect() as conn:
        source_compaction_row = (
            await conn.execute(
                select(compactions).where(compactions.c.id == result.compaction_id)
            )
        ).fetchone()
    assert source_compaction_row is not None
    assert (
        fork_compaction_rows[0].original_token_count
        == source_compaction_row.original_token_count
    )
    assert (
        fork_compaction_rows[0].summary_token_count
        == source_compaction_row.summary_token_count
    )

    # The forked messages that were archived now point at the NEW
    # compaction id, and there are as many of them as were archived
    # originally.
    async with engine.connect() as conn:
        fork_msgs = (
            await conn.execute(
                select(messages).where(messages.c.chat_id == forked.id)
            )
        ).fetchall()
    archived_fork_msgs = [m for m in fork_msgs if m.compaction_id is not None]
    assert len(archived_fork_msgs) == len(result.removed_message_ids)
    assert all(m.compaction_id == new_cid for m in archived_fork_msgs)

    # anchor_msg_id was remapped to a NEW message id (not one of the
    # source chat's original ids, since fork assigns fresh PKs).
    async with engine.connect() as conn:
        source_ids = {
            r[0]
            for r in (
                await conn.execute(
                    select(messages.c.id).where(messages.c.chat_id == chat.id)
                )
            ).fetchall()
        }
    assert fork_compaction_rows[0].anchor_msg_id not in source_ids
    fork_ids = {m.id for m in fork_msgs}
    assert fork_compaction_rows[0].anchor_msg_id in fork_ids


async def test_fork_before_compaction_point_does_not_copy_later_span(
    engine: AsyncEngine,
) -> None:
    """A fork cut BEFORE any archived message exists copies no compactions row.

    Uses a system-role first message: it's always invariant-protected (never
    archived), so forking AT that message's id copies exactly that one
    (never-archived) row and nothing else — a clean "before the archived
    span existed" cut. (A plain early user-role message would NOT work here:
    only the LATEST user message is protected, so an earlier one is just as
    eligible for archiving as anything else.)
    """
    await _insert_user(engine)
    svc = _make_service(engine)
    chat = await svc.create(user_id=1, title="Chat")

    sys_id = await _insert_message(engine, chat.id, role="system", content="System prompt.")

    # More turns AFTER the fork point, which later get archived.
    await _insert_message(engine, chat.id, role="assistant", content="old one two three")
    await _insert_message(engine, chat.id, role="assistant", content="old two two three")
    await _insert_message(engine, chat.id, role="user", content="latest question here")

    # total=14 tokens; invariant (system + latest user) = 6*1.1=6.6, so
    # target=10 both clears the invariant floor and forces the two old
    # assistant turns to archive.
    result = await _compact(svc, chat.id, target_tokens=10)
    assert result.removed_message_ids

    # Fork at the SYSTEM message — before the archived span existed at all.
    forked = await svc.fork(chat.id, user_id=1, at_message_id=sys_id)

    async with engine.connect() as conn:
        fork_compaction_rows = (
            await conn.execute(
                select(compactions).where(compactions.c.chat_id == forked.id)
            )
        ).fetchall()
    assert fork_compaction_rows == []

    async with engine.connect() as conn:
        fork_msgs = (
            await conn.execute(
                select(messages).where(messages.c.chat_id == forked.id)
            )
        ).fetchall()
    assert len(fork_msgs) == 1
    assert fork_msgs[0].compaction_id is None
