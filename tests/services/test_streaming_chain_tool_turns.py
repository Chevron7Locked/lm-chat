# SPDX-License-Identifier: Apache-2.0
"""Tests for the chain-mode tool-turn fix.

Covers:
1. Chain + integrations → wire request has store=False, previous_response_id=None,
   and prior turns are composed into system_prompt as "## Prior turns".
2. Chain + NO integrations, first turn → unchanged (store not forced False, no
   "## Prior turns", previous_response_id passthrough from payload).
3. Chain + NO integrations + valid previous_response_id (follow-up) → unchanged
   (store unset, previous_response_id preserved, history NOT loaded/composed).
4. Chain + previous_response_id None (chain broken / first turn) → history IS
   loaded and composed into system_prompt when there are prior DB messages.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import chats, messages, metadata
from lmchat.lmstudio.types import (
    CanonicalChatRequest,
    CanonicalEvent,
    CanonicalInputBlock,
)
from lmchat.services._stream_state import PersistState
from lmchat.services.streaming_service import ChatStreamRequest, StreamingService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_engine() -> AsyncEngine:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return engine


def _make_payload(
    model: str = "test-model",
    chat_text: str = "use the search tool",
    *,
    previous_response_id: str | None = None,
    integrations: list[str] | None = None,
) -> ChatStreamRequest:
    req = CanonicalChatRequest(
        model=model,
        input=[CanonicalInputBlock(type="text", content=chat_text)],
        previous_response_id=previous_response_id,
    )
    if integrations is not None:
        req = req.model_copy(update={"integrations": integrations})
    return ChatStreamRequest(chat_id=1, payload=req)


def _happy_events(response_id: str = "rid-1") -> list[CanonicalEvent]:
    return [
        CanonicalEvent(type="chat.start"),
        CanonicalEvent(type="message.start"),
        CanonicalEvent(type="message.delta", content="tool result here"),
        CanonicalEvent(type="message.end"),
        CanonicalEvent(type="chat.end", response_id=response_id),
    ]


def _mock_user(user_id: int = 1) -> MagicMock:
    u = MagicMock()
    u.id = user_id
    return u


def _mock_request(disconnected: bool = False) -> AsyncMock:
    from tests.services.conftest import make_disconnect_receive

    req = AsyncMock()
    req.receive = make_disconnect_receive(disconnected)
    return req


async def _drain(gen: AsyncIterator[bytes]) -> list[bytes]:
    frames: list[bytes] = []
    async for frame in gen:
        frames.append(frame)
    return frames


def _parse_frames(frames: list[bytes]) -> list[dict]:  # type: ignore[type-arg]
    results = []
    for frame in frames:
        for line in frame.decode("utf-8").splitlines():
            if line.startswith("data:"):
                results.append(json.loads(line[5:].strip()))
    return results


def _make_models_service(wire_id: str = "test-model") -> AsyncMock:
    """Build a minimal models_service mock for chain-mode tests."""
    svc = AsyncMock()
    svc.auth_failed = False
    res = MagicMock()
    res.wire_id = wire_id
    res.substituted = False
    svc.resolve_to_loaded_or_fallback = AsyncMock(return_value=res)
    # Raise KeyError so capability/budget gates pass without side-effects.
    svc.get_capabilities = AsyncMock(side_effect=KeyError(wire_id))
    svc.get_max_context_length = AsyncMock(return_value=0)
    return svc


async def _insert_chat(engine: AsyncEngine, *, settings: dict | None = None) -> int:  # type: ignore[type-arg]
    async with engine.begin() as conn:
        result = await conn.execute(
            chats.insert().values(user_id=1, title="test", settings=settings or {})
        )
        return result.inserted_primary_key[0]  # type: ignore[index]


async def _insert_final_messages(
    engine: AsyncEngine,
    chat_id: int,
    rows: list[dict],  # type: ignore[type-arg]
) -> None:
    async with engine.begin() as conn:
        for row in rows:
            row_data: dict = {  # type: ignore[type-arg]
                "chat_id": chat_id,
                "role": row["role"],
                "content": row.get("content", ""),
                "state": PersistState.FINAL.value,
                "model_id": "test-model",
            }
            if "response_id" in row:
                row_data["response_id"] = row["response_id"]
            await conn.execute(messages.insert().values(**row_data))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = await _make_engine()
    yield eng
    await eng.dispose()


# ---------------------------------------------------------------------------
# Test 1: chain + integrations → store=False, previous_response_id=None,
#         prior turns composed into system_prompt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chain_tool_turn_sets_store_false_and_clears_chain(
    engine: AsyncEngine,
) -> None:
    """Chain mode + integrations → wire request has store=False and
    previous_response_id=None.

    Given:
    - A chat in chain mode (no provider key)
    - A payload with integrations=["mcp/searxng"] and a previous_response_id
    - Two prior FINAL messages (user + assistant)

    Asserts:
    - wire request.store == False
    - wire request.previous_response_id is None (chain cleared)
    - "## Prior turns" appears in system_prompt (history composed)
    - prior user/assistant content appears in system_prompt
    - lm_client.stream() is called (chain dispatch, not replay provider)
    """
    chat_id = await _insert_chat(engine, settings={})

    await _insert_final_messages(
        engine,
        chat_id,
        [
            {"role": "user", "content": "what is the weather?", "response_id": None},
            {"role": "assistant", "content": "let me search for that", "response_id": "rid-0"},
        ],
    )

    captured: dict[str, Any] = {}

    async def _fake_stream(**kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        captured.update(kwargs)
        for ev in _happy_events():
            yield ev

    lm_client = MagicMock()
    lm_client.stream = _fake_stream

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    svc = StreamingService(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        chat_locks={},
        models_service=_make_models_service(),
    )

    # Payload: previous_response_id set (simulating a follow-up) + integrations
    payload = _make_payload(
        previous_response_id="rid-0",
        integrations=["mcp/searxng"],
    )

    frames = await _drain(
        svc.stream_chat(
            chat_id=chat_id,
            user=_mock_user(1),
            payload=payload,
            request=_mock_request(),
        )
    )

    # Stream must complete.
    parsed = _parse_frames(frames)
    assert any(d.get("type") == "chat.end" for d in parsed), (
        f"Stream must complete with chat.end. Events: {[d.get('type') for d in parsed]}"
    )

    # lm_client.stream must have been called (chain path, not replay provider).
    assert "request" in captured, "lm_client.stream was not called"

    wire_req: CanonicalChatRequest = captured["request"]

    # store must be False.
    assert wire_req.store is False, (
        f"Expected store=False for tool turn, got store={wire_req.store!r}"
    )

    # previous_response_id must be cleared.
    assert wire_req.previous_response_id is None, (
        f"Expected previous_response_id=None after chain clear, "
        f"got {wire_req.previous_response_id!r}"
    )

    # history must be composed into system_prompt.
    sys_p = wire_req.system_prompt or ""
    assert "## Prior turns" in sys_p, (
        f"Expected '## Prior turns' in system_prompt after chain cleared. "
        f"Got system_prompt: {sys_p!r}"
    )
    assert "what is the weather?" in sys_p, (
        f"Prior user turn content missing from system_prompt. Got: {sys_p!r}"
    )
    assert "let me search for that" in sys_p, (
        f"Prior assistant turn content missing from system_prompt. Got: {sys_p!r}"
    )


# ---------------------------------------------------------------------------
# Test 2: chain + NO integrations, first turn (previous_response_id=None) →
#         store not forced to False, no "## Prior turns", no history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chain_no_integrations_first_turn_unchanged(engine: AsyncEngine) -> None:
    """Chain + no integrations + no previous_response_id (genuine first turn).

    No prior messages exist. The wire request must:
    - NOT have store=False (store should be None / unset)
    - NOT have "## Prior turns" in system_prompt (nothing to compose)
    - previous_response_id stays None (as passed in the payload)
    """
    chat_id = await _insert_chat(engine, settings={})
    # No prior messages inserted — genuine first turn.

    captured: dict[str, Any] = {}

    async def _fake_stream(**kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        captured.update(kwargs)
        for ev in _happy_events():
            yield ev

    lm_client = MagicMock()
    lm_client.stream = _fake_stream

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    svc = StreamingService(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        chat_locks={},
        models_service=_make_models_service(),
    )

    # No integrations, no previous_response_id.
    payload = _make_payload(integrations=[], previous_response_id=None)

    frames = await _drain(
        svc.stream_chat(
            chat_id=chat_id,
            user=_mock_user(1),
            payload=payload,
            request=_mock_request(),
        )
    )

    parsed = _parse_frames(frames)
    assert any(d.get("type") == "chat.end" for d in parsed), (
        f"Stream must complete. Events: {[d.get('type') for d in parsed]}"
    )

    assert "request" in captured, "lm_client.stream was not called"
    wire_req: CanonicalChatRequest = captured["request"]

    # store must NOT be forced to False (should be None).
    assert wire_req.store is not False, (
        f"store must not be forced to False on a no-integrations first turn. "
        f"Got store={wire_req.store!r}"
    )

    # No "## Prior turns" section (nothing to compose).
    sys_p = wire_req.system_prompt or ""
    assert "## Prior turns" not in sys_p, (
        f"'## Prior turns' must NOT appear on a first turn with no prior messages. "
        f"Got system_prompt: {sys_p!r}"
    )


# ---------------------------------------------------------------------------
# Test 3: chain + NO integrations + valid previous_response_id (follow-up) →
#         completely unchanged (store unset, chain intact, no history loaded)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chain_no_integrations_followup_unchanged(engine: AsyncEngine) -> None:
    """Chain + no integrations + valid previous_response_id (normal follow-up).

    The wire request must be byte-identical to pre-fix behavior:
    - store is None (not set)
    - previous_response_id preserved as-is from payload
    - "## Prior turns" does NOT appear (history not loaded/composed for this path)
    """
    chat_id = await _insert_chat(engine, settings={})

    # Insert the prior turns so the DB has data — including the assistant row
    # that "produced" rid-prior. Hybrid compaction's chain-reset backstop
    # cross-checks previous_response_id against a real message
    # row before honouring it; an unbacked rid is unknown and gets dropped.
    # No compactions exist here, so once the anchor row is found the backstop
    # is a pure pass-through — this test is about the no-integrations
    # follow-up path, not the backstop itself.
    await _insert_final_messages(
        engine,
        chat_id,
        [
            {"role": "user", "content": "prior question", "response_id": None},
            {"role": "assistant", "content": "prior answer", "response_id": "rid-prior"},
        ],
    )

    captured: dict[str, Any] = {}

    async def _fake_stream(**kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        captured.update(kwargs)
        for ev in _happy_events():
            yield ev

    lm_client = MagicMock()
    lm_client.stream = _fake_stream

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    svc = StreamingService(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        chat_locks={},
        models_service=_make_models_service(),
    )

    # Follow-up: valid previous_response_id, no integrations.
    payload = _make_payload(
        previous_response_id="rid-prior",
        integrations=[],
    )

    frames = await _drain(
        svc.stream_chat(
            chat_id=chat_id,
            user=_mock_user(1),
            payload=payload,
            request=_mock_request(),
        )
    )

    parsed = _parse_frames(frames)
    assert any(d.get("type") == "chat.end" for d in parsed), (
        f"Stream must complete. Events: {[d.get('type') for d in parsed]}"
    )

    assert "request" in captured, "lm_client.stream was not called"
    wire_req: CanonicalChatRequest = captured["request"]

    # store must be unset (None).
    assert wire_req.store is None, (
        f"store must remain None for a normal no-tool follow-up. "
        f"Got store={wire_req.store!r}"
    )

    # previous_response_id must be preserved.
    assert wire_req.previous_response_id == "rid-prior", (
        f"previous_response_id must be preserved for normal follow-up. "
        f"Got {wire_req.previous_response_id!r}"
    )

    # "## Prior turns" must NOT appear (history not loaded for this path).
    sys_p = wire_req.system_prompt or ""
    assert "## Prior turns" not in sys_p, (
        f"'## Prior turns' must NOT appear for a normal chain follow-up. "
        f"Got system_prompt: {sys_p!r}"
    )


# ---------------------------------------------------------------------------
# Test 4: chain + previous_response_id=None (chain broken or first turn) →
#         history IS loaded and composed when prior messages exist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chain_broken_chain_composes_history(engine: AsyncEngine) -> None:
    """Chain mode with no previous_response_id + prior DB messages.

    Even without integrations, a None previous_response_id triggers
    history loading from DB and composition into system_prompt. This covers the
    case where a prior tool turn broke the chain (cleared previous_response_id)
    and a subsequent non-tool message needs prior context.
    """
    chat_id = await _insert_chat(engine, settings={})

    await _insert_final_messages(
        engine,
        chat_id,
        [
            {"role": "user", "content": "hello world", "response_id": None},
            {"role": "assistant", "content": "greetings", "response_id": "rid-prev"},
        ],
    )

    captured: dict[str, Any] = {}

    async def _fake_stream(**kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        captured.update(kwargs)
        for ev in _happy_events():
            yield ev

    lm_client = MagicMock()
    lm_client.stream = _fake_stream

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    svc = StreamingService(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        chat_locks={},
        models_service=_make_models_service(),
    )

    # No integrations, no previous_response_id (chain broken).
    payload = _make_payload(integrations=[], previous_response_id=None)

    frames = await _drain(
        svc.stream_chat(
            chat_id=chat_id,
            user=_mock_user(1),
            payload=payload,
            request=_mock_request(),
        )
    )

    parsed = _parse_frames(frames)
    assert any(d.get("type") == "chat.end" for d in parsed), (
        f"Stream must complete. Events: {[d.get('type') for d in parsed]}"
    )

    assert "request" in captured, "lm_client.stream was not called"
    wire_req: CanonicalChatRequest = captured["request"]

    # history must be composed into system_prompt when there are
    # prior messages and previous_response_id is None.
    sys_p = wire_req.system_prompt or ""
    assert "## Prior turns" in sys_p, (
        f"Expected '## Prior turns' in system_prompt when chain is broken and "
        f"prior messages exist. Got system_prompt: {sys_p!r}"
    )
    assert "hello world" in sys_p, (
        f"Prior user content missing from system_prompt. Got: {sys_p!r}"
    )
    assert "greetings" in sys_p, (
        f"Prior assistant content missing from system_prompt. Got: {sys_p!r}"
    )
