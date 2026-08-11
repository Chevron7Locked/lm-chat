# SPDX-License-Identifier: Apache-2.0
"""Unit tests for Fix A — reasoning_content persistence to messages table.

Per CHAT-ENHANCE-PHASE1/PLAN_v2.md §4 (Test plan).

Tests:
- Synthetic stream with message.delta + reasoning.delta: _finalize_message
  receives final_reasoning matching concatenated reasoning content.
- DB-row check post-finalize: reasoning_content populated.
- Empty-reasoning case: reasoning_content IS NULL (not empty string).
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import chats, messages, metadata
from lmchat.lmstudio.types import CanonicalChatRequest, CanonicalEvent, CanonicalInputBlock
from lmchat.services.streaming_service import ChatStreamRequest, StreamingService

# ---------------------------------------------------------------------------
# Helpers (mirroring patterns from test_streaming_service.py)
# ---------------------------------------------------------------------------


def _make_payload(model: str = "test-model") -> ChatStreamRequest:
    return ChatStreamRequest(
        chat_id=1,
        payload=CanonicalChatRequest(
            model=model,
            input=[CanonicalInputBlock(type="text", content="hi")],
        ),
    )


async def _make_service(
    engine: AsyncEngine,
    events: list[CanonicalEvent],
    chat_locks: dict | None = None,
) -> StreamingService:
    """Build a StreamingService backed by a synthetic event sequence."""

    async def _fake_stream(*_args: Any, **_kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        for ev in events:
            yield ev

    lm_client = MagicMock()
    lm_client.stream = _fake_stream

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    return StreamingService(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        chat_locks=chat_locks if chat_locks is not None else {},
    )


async def _run_stream(svc: StreamingService, chat_id: int = 1) -> None:
    """Drive stream_chat to completion, discarding frames."""
    from tests.services.conftest import make_disconnect_receive

    request = AsyncMock()
    request.receive = make_disconnect_receive(False)
    user = MagicMock()
    user.id = 1
    async for _ in svc.stream_chat(
        chat_id=chat_id,
        user=user,
        payload=_make_payload(),
        request=request,
    ):
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def engine() -> AsyncIterator[AsyncEngine]:
    """In-memory SQLite engine with full schema."""
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture()
async def engine_with_chat(engine: AsyncEngine) -> AsyncEngine:
    """Engine with a pre-inserted chat row (id=1, user_id=1)."""
    async with engine.begin() as conn:
        await conn.execute(chats.insert().values(user_id=1, title="test"))
    return engine


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finalize_message_receives_reasoning(engine_with_chat: AsyncEngine) -> None:
    """_finalize_message is called with final_reasoning matching concatenated reasoning."""
    reasoning_chunks = ["I need to think", " about this carefully"]
    expected_reasoning = "".join(reasoning_chunks)

    events = [
        CanonicalEvent(type="chat.start", response_id="rid-1"),
        CanonicalEvent(type="message.start"),
        CanonicalEvent(type="reasoning.start"),
        CanonicalEvent(type="reasoning.delta", content=reasoning_chunks[0]),
        CanonicalEvent(type="reasoning.delta", content=reasoning_chunks[1]),
        CanonicalEvent(type="reasoning.end"),
        CanonicalEvent(type="message.delta", content="The answer is 42."),
        CanonicalEvent(type="message.end"),
        CanonicalEvent(type="chat.end", response_id="rid-1"),
    ]

    svc = await _make_service(engine_with_chat, events)

    # Spy on _finalize_message to capture kwargs.
    captured: dict[str, Any] = {}
    original_finalize = svc._finalize_message

    async def _spy(**kwargs: Any) -> bool:
        captured.update(kwargs)
        return await original_finalize(**kwargs)

    svc._finalize_message = _spy  # type: ignore[method-assign]

    await _run_stream(svc)

    assert "final_reasoning" in captured, "_finalize_message not called with final_reasoning"
    assert captured["final_reasoning"] == expected_reasoning


@pytest.mark.asyncio
async def test_reasoning_content_written_to_db(engine_with_chat: AsyncEngine) -> None:
    """After finalize, reasoning_content column holds the concatenated reasoning."""
    reasoning_text = "Step 1: understand. Step 2: answer."
    content_text = "The answer is 42."

    events = [
        CanonicalEvent(type="chat.start", response_id="rid-2"),
        CanonicalEvent(type="message.start"),
        CanonicalEvent(type="reasoning.start"),
        CanonicalEvent(type="reasoning.delta", content=reasoning_text),
        CanonicalEvent(type="reasoning.end"),
        CanonicalEvent(type="message.delta", content=content_text),
        CanonicalEvent(type="message.end"),
        CanonicalEvent(type="chat.end", response_id="rid-2"),
    ]

    svc = await _make_service(engine_with_chat, events)
    await _run_stream(svc)

    async with engine_with_chat.connect() as conn:
        row = (
            await conn.execute(
                select(messages).where(
                    messages.c.chat_id == 1,
                    messages.c.role == "assistant",
                )
            )
        ).fetchone()

    assert row is not None
    assert row.reasoning_content == reasoning_text
    assert row.content == content_text


@pytest.mark.asyncio
async def test_empty_reasoning_writes_null_to_db(engine_with_chat: AsyncEngine) -> None:
    """When no reasoning.delta events arrive, reasoning_content IS NULL (not empty string)."""
    events = [
        CanonicalEvent(type="chat.start", response_id="rid-3"),
        CanonicalEvent(type="message.start"),
        CanonicalEvent(type="message.delta", content="Just an answer."),
        CanonicalEvent(type="message.end"),
        CanonicalEvent(type="chat.end", response_id="rid-3"),
    ]

    svc = await _make_service(engine_with_chat, events)
    await _run_stream(svc)

    async with engine_with_chat.connect() as conn:
        row = (
            await conn.execute(select(messages).where(messages.c.chat_id == 1))
        ).fetchone()

    assert row is not None
    assert row.reasoning_content is None, (
        f"Expected NULL reasoning_content but got: {row.reasoning_content!r}"
    )
