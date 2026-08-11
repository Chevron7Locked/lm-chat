# SPDX-License-Identifier: Apache-2.0
"""Regression pin: inject-message severs the LM Studio response chain.

CURRENT (intentionally-broken) behavior:
1. Sub-session ``inject-message`` persists ``messages.response_id = NULL``
   because ``MessageService.append()`` does not set the column.
2. The frontend reads that NULL ``response_id`` on the next user turn and
   sends ``previous_response_id = None`` in the stream request.
3. LM Studio receives ``previous_response_id=None`` and treats the turn as a
   fresh conversation, replying "I don't have prior conversation context."

This test pins that broken behaviour so the eventual fix
(BE-owns-chain: ``MessageService.append()`` sets ``response_id``)
has a tripwire to flip.

TODO: flip polarity assertions when BE chain ownership ships.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import chats, messages, metadata
from lmchat.lmstudio.types import (
    CanonicalChatRequest,
    CanonicalEvent,
    CanonicalInputBlock,
)
from lmchat.services.message_service import MessageService
from lmchat.services.streaming_service import (
    ChatStreamRequest,
    StreamingService,
)

# ---------------------------------------------------------------------------
# Helpers (mirrored from test_streaming_service.py)
# ---------------------------------------------------------------------------


async def _make_engine() -> AsyncEngine:
    """Create an in-memory SQLite engine with the full schema."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return engine


async def _make_service(
    engine: AsyncEngine,
    lm_client: Any = None,  # noqa: ANN401
    memory_service: Any = None,  # noqa: ANN401
) -> StreamingService:
    """Build a StreamingService with sensible test defaults."""
    if lm_client is None:
        lm_client = MagicMock()
    if memory_service is None:
        memory_service = AsyncMock()
        memory_service.index_message = AsyncMock(return_value=None)
    return StreamingService(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_service,
        chat_locks={},
    )


def _mock_user(user_id: int = 1) -> MagicMock:
    """Build a mock User with ``id`` attribute."""
    user = MagicMock()
    user.id = user_id
    return user


def _mock_request(disconnected: bool = False) -> AsyncMock:
    """Build a mock FastAPI Request with an ASGI ``receive()`` channel."""
    from tests.services.conftest import make_disconnect_receive

    request = AsyncMock()
    request.receive = make_disconnect_receive(disconnected)
    return request


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def engine() -> AsyncIterator[AsyncEngine]:
    """Provide an in-memory SQLite engine with the full schema."""
    eng = await _make_engine()
    yield eng
    await eng.dispose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inject_message_response_id_is_null(engine: AsyncEngine) -> None:
    """``inject-message`` via ``MessageService.append`` leaves `response_id` NULL.

    This is the CURRENT (intentionally-broken) behaviour.  When the BE owns
    the response chain, ``inject-message`` should set
    ``response_id`` so the FE can reference it in subsequent turns.

    TODO: flip to assert response_id IS NOT NULL.
    """
    # -- Arrange ---------------------------------------------------------------
    async with engine.begin() as conn:
        await conn.execute(chats.insert().values(user_id=1, title="bug-9-test"))

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)
    svc = MessageService(engine=engine, memory_service=memory_mock)

    # Act: perform an inject-message (same call pattern as the route handler).
    msg = await svc.append(
        chat_id=1,
        user_id=1,
        role="assistant",
        content="This is an injected summary from a sub-session.",
        model_id="test-model",
    )

    # -- Assert: response_id IS NULL ------------------------------------------
    # TODO: flip to assert response_id is not None.
    assert msg.response_id is None, (
        f"Expected response_id=None for injected message (current broken behaviour), "
        f"got {msg.response_id!r}"
    )

    # Also verify at the DB level: the column is NULL.
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(messages.c.response_id).where(messages.c.id == msg.id)
            )
        ).fetchone()
    assert row is not None
    # TODO: flip to assert row[0] is not None (after BE chain ownership fix).
    assert row[0] is None, (
        f"Expected DB response_id=NULL for injected message (current broken behaviour), "
        f"got {row[0]!r}"
    )


@pytest.mark.asyncio
async def test_next_turn_sends_previous_response_id_none(engine: AsyncEngine) -> None:
    """After an injected message with NULL response_id, the next stream turn
    sends ``previous_response_id=None`` to LM Studio.

    CURRENT (intentionally-broken) behaviour: the FE reads the last assistant
    message's ``response_id``, finds it NULL, and therefore sends
    ``previous_response_id=None`` on the next turn.  LM Studio then treats
    the turn as a fresh conversation, discarding prior context.

    TODO: flip to assert previous_response_id is NOT None after
    BE-owned chain assignment.
    """
    # -- Arrange ---------------------------------------------------------------
    async with engine.begin() as conn:
        await conn.execute(chats.insert().values(user_id=1, title="bug-9-stream-test"))

    # Insert an injected message with response_id=NULL (as append does today).
    memory_svc = AsyncMock()
    memory_svc.index_message = AsyncMock(return_value=None)
    msg_svc = MessageService(engine=engine, memory_service=memory_svc)
    await msg_svc.append(
        chat_id=1,
        user_id=1,
        role="assistant",
        content="Injected summary.",
        model_id="test-model",
    )

    # Build the ChatStreamRequest the FE would send: previous_response_id=None
    # because the last assistant message's response_id was NULL.
    payload = ChatStreamRequest(
        chat_id=1,
        payload=CanonicalChatRequest(
            model="test-model",
            input=[CanonicalInputBlock(type="text", content="Tell me more")],
            previous_response_id=None,  # FE sends None when response_id was NULL
        ),
    )

    # Create a mock lm_client that captures the wire request.
    captured_wire_request: dict[str, object] = {}

    async def _capture_fake_stream(
        *args: object, **kwargs: object
    ) -> AsyncIterator[CanonicalEvent]:
        # Capture the wire request the streaming service will send.
        if args:
            first = args[0]
            if isinstance(first, CanonicalChatRequest):
                captured_wire_request["previous_response_id"] = (
                    first.previous_response_id
                )
                captured_wire_request["model"] = first.model
                captured_wire_request["input"] = first.input
        if "request" in kwargs:
            req = kwargs["request"]
            if isinstance(req, CanonicalChatRequest):
                captured_wire_request["previous_response_id"] = (
                    req.previous_response_id
                )
                captured_wire_request["model"] = req.model
                captured_wire_request["input"] = req.input
        # Yield a minimal happy-path event sequence so the stream completes.
        yield CanonicalEvent(type="chat.start")
        yield CanonicalEvent(type="message.start")
        yield CanonicalEvent(type="message.delta", content="Hello response.")
        yield CanonicalEvent(type="message.end")
        yield CanonicalEvent(type="chat.end", response_id="resp-abc")

    lm_client = MagicMock()
    lm_client.stream = _capture_fake_stream

    streaming_svc = await _make_service(
        engine, lm_client=lm_client, memory_service=memory_svc
    )
    user = _mock_user(1)
    request = _mock_request(disconnected=False)

    # -- Act: trigger the next stream turn ------------------------------------
    frames: list[bytes] = []
    async for frame in streaming_svc.stream_chat(
        chat_id=1,
        user=user,
        payload=payload,
        request=request,
    ):
        frames.append(frame)

    # -- Assert ---------------------------------------------------------------
    assert len(frames) > 0, "Expected at least one SSE frame"

    # TODO: flip to assert previous_response_id is NOT None.
    assert captured_wire_request.get("previous_response_id") is None, (
        f"Expected previous_response_id=None (current broken behaviour), "
        f"got {captured_wire_request.get('previous_response_id')!r}"
    )

    # Confirm the wire request was correctly captured.
    assert captured_wire_request.get("model") == "test-model"