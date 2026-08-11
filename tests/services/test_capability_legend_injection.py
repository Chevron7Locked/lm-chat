# SPDX-License-Identifier: Apache-2.0
"""Capability legend is injected into the assembled system prompt.

Proves ``_assemble_system_prompt`` appends the ``[Capabilities]`` block in
BOTH chain mode (default LM Studio path, no provider_registry) and replay
mode (a resolved cloud/compat provider) — the two branches that each build
``_existing_sys`` independently.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import chats, metadata
from lmchat.lmstudio.types import CanonicalChatRequest, CanonicalEvent, CanonicalInputBlock
from lmchat.services.streaming_service import ChatStreamRequest, StreamingService


def _happy_events() -> list[CanonicalEvent]:
    return [
        CanonicalEvent(type="chat.start"),
        CanonicalEvent(type="message.start"),
        CanonicalEvent(type="message.delta", content="ack"),
        CanonicalEvent(type="message.end"),
        CanonicalEvent(type="chat.end", response_id="r-legend"),
    ]


def _mock_user(user_id: int = 1) -> MagicMock:
    u = MagicMock()
    u.id = user_id
    return u


def _mock_request() -> MagicMock:
    from tests.services.conftest import make_disconnect_receive

    r = MagicMock()
    r.receive = make_disconnect_receive(False)
    return r


async def _drain(stream: AsyncIterator[Any]) -> list[Any]:
    return [frame async for frame in stream]


def _parse_frames(frames: list[bytes]) -> list[dict]:  # type: ignore[type-arg]
    """Decode SSE frames into their parsed ``data:`` JSON payloads."""
    results = []
    for frame in frames:
        for line in frame.decode("utf-8").splitlines():
            if line.startswith("data:"):
                results.append(json.loads(line[5:].strip()))
    return results


@pytest.fixture
async def engine() -> AsyncEngine:
    e = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with e.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return e


async def _insert_chat(engine: AsyncEngine, *, settings: dict | None = None) -> int:
    async with engine.begin() as conn:
        result = await conn.execute(
            chats.insert().values(user_id=1, title="t", settings=settings or {})
        )
        return result.inserted_primary_key[0]  # type: ignore[index]


async def _seed_endpoint_mode(engine: AsyncEngine, mode: str) -> None:
    """Seed server_lm_studio_default.lm_studio_endpoint_mode directly."""
    from lmchat.db.schema import server_lm_studio_default  # noqa: PLC0415

    async with engine.begin() as conn:
        await conn.execute(
            server_lm_studio_default.insert().values(id=1, lm_studio_endpoint_mode=mode)
        )


def _make_lmstudio_native_stub(compat_provider: object) -> MagicMock:
    """Stub the native LmstudioAdapter registered under the "lmstudio" name."""
    native = MagicMock()
    native.name = "lmstudio"
    native.context_mode = "chain"
    native.as_openai_compat_provider = MagicMock(return_value=compat_provider)
    return native


@pytest.mark.asyncio
async def test_chain_mode_injects_capability_legend(engine: AsyncEngine) -> None:
    """Default LM Studio (chain) path: the wire system_prompt carries the
    [Capabilities] block, and an enabled integration is reflected in it."""
    chat_id = await _insert_chat(engine)

    captured: dict[str, Any] = {}

    async def _fake_stream(*args: Any, **kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        captured["payload"] = kwargs.get("request") or (args[0] if args else None)
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
    )

    payload = ChatStreamRequest(
        chat_id=chat_id,
        payload=CanonicalChatRequest(
            model="test-model",
            input=[CanonicalInputBlock(type="text", content="hello")],
            integrations=["mcp/searxng"],
        ),
    )

    await _drain(
        svc.stream_chat(
            chat_id=chat_id, user=_mock_user(), payload=payload, request=_mock_request()
        )
    )

    sent = captured.get("payload")
    assert sent is not None, "lm_client.stream was not called"
    sys_p = getattr(sent, "system_prompt", "") or ""
    assert "[Capabilities]" in sys_p
    assert "Suggest to the user (they run these):" in sys_p
    assert "Tools you can call directly:" in sys_p
    assert "- searxng —" in sys_p


@pytest.mark.asyncio
async def test_replay_mode_injects_capability_legend(engine: AsyncEngine) -> None:
    """Replay (cloud/compat) path: the wire system_prompt carries the
    [Capabilities] block, with the none-enabled fallback when no
    integrations were selected for the turn."""
    chat_id = await _insert_chat(engine, settings={"provider": "openrouter"})

    captured: dict[str, Any] = {}

    async def _provider_stream_chat(
        request: Any, **kwargs: Any
    ) -> AsyncIterator[CanonicalEvent]:
        captured["request"] = request
        for ev in _happy_events():
            yield ev

    stub_provider = MagicMock()
    stub_provider.context_mode = "replay"
    stub_provider.name = "openrouter"
    stub_provider.stream_chat = _provider_stream_chat

    registry = MagicMock()
    registry.get = MagicMock(
        side_effect=lambda name: stub_provider if name == "openrouter" else None
    )

    lm_client = MagicMock()
    lm_client.stream = MagicMock(
        side_effect=AssertionError("lm_client.stream must NOT be called in replay mode")
    )

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    svc = StreamingService(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        chat_locks={},
        provider_registry=registry,
    )

    payload = ChatStreamRequest(
        chat_id=chat_id,
        payload=CanonicalChatRequest(
            model="test-model",
            input=[CanonicalInputBlock(type="text", content="hello")],
        ),
    )

    await _drain(
        svc.stream_chat(
            chat_id=chat_id, user=_mock_user(), payload=payload, request=_mock_request()
        )
    )

    sent = captured.get("request")
    assert sent is not None, "provider.stream_chat was not called"
    sys_p = getattr(sent, "system_prompt", "") or ""
    assert "[Capabilities]" in sys_p
    assert "Suggest to the user (they run these):" in sys_p
    assert "Tools you can call directly:" in sys_p
    assert "none enabled" in sys_p


@pytest.mark.asyncio
async def test_replay_mode_with_tools_renders_tool_row_not_fallback(
    engine: AsyncEngine,
) -> None:
    """Replay path WITH an integration selected: the DO section renders the
    actual tool row, not the none-enabled fallback.

    A non-empty ``integrations`` list on a replay turn routes wire_payload
    through ``maybe_wrap_agentic`` (see ``lmchat.mcp.agentic``), which would
    otherwise try to actually connect to an MCP host / server-policy store.
    ``AgenticMcpProvider`` is patched to return its ``inner`` unchanged —
    this test is about what reached the wire system_prompt (already
    finalized by ``_assemble_system_prompt`` before the wrap happens), not
    about exercising the real tool-execution loop.
    """
    chat_id = await _insert_chat(engine, settings={"provider": "openrouter"})

    captured: dict[str, Any] = {}

    async def _provider_stream_chat(
        request: Any, **kwargs: Any
    ) -> AsyncIterator[CanonicalEvent]:
        captured["request"] = request
        for ev in _happy_events():
            yield ev

    stub_provider = MagicMock()
    stub_provider.context_mode = "replay"
    stub_provider.name = "openrouter"
    stub_provider.stream_chat = _provider_stream_chat

    registry = MagicMock()
    registry.get = MagicMock(
        side_effect=lambda name: stub_provider if name == "openrouter" else None
    )

    lm_client = MagicMock()
    lm_client.stream = MagicMock(
        side_effect=AssertionError("lm_client.stream must NOT be called in replay mode")
    )

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    svc = StreamingService(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        chat_locks={},
        provider_registry=registry,
    )

    payload = ChatStreamRequest(
        chat_id=chat_id,
        payload=CanonicalChatRequest(
            model="test-model",
            input=[CanonicalInputBlock(type="text", content="search something")],
            integrations=["mcp/searxng"],
        ),
    )

    request = _mock_request()
    request.app.state.mcp_host = MagicMock()
    request.app.state.mcp_server_store = None

    with patch(
        "lmchat.mcp.agentic.AgenticMcpProvider",
        MagicMock(side_effect=lambda **kw: kw["inner"]),
    ):
        await _drain(
            svc.stream_chat(chat_id=chat_id, user=_mock_user(), payload=payload, request=request)
        )

    sent = captured.get("request")
    assert sent is not None, "provider.stream_chat was not called"
    sys_p = getattr(sent, "system_prompt", "") or ""
    assert "[Capabilities]" in sys_p
    assert "- searxng —" in sys_p
    assert "none enabled" not in sys_p, (
        f"tool row missing — fallback line rendered instead: {sys_p!r}"
    )


@pytest.mark.asyncio
async def test_openai_compat_builtin_web_search_reaches_capability_legend(
    engine: AsyncEngine,
) -> None:
    """openai_compat endpoint mode flips ``builtin_web_search=True`` through
    ``_resolve_provider_and_context_mode`` -> ``_assemble_system_prompt``;
    the DO section must carry the web_search row even though no MCP
    integration was selected for the turn."""
    chat_id = await _insert_chat(engine)
    await _seed_endpoint_mode(engine, "openai_compat")

    captured: dict[str, Any] = {}

    async def _compat_stream_chat(
        request: Any, **kwargs: Any
    ) -> AsyncIterator[CanonicalEvent]:
        captured["request"] = request
        for ev in _happy_events():
            yield ev

    compat_provider = MagicMock()
    compat_provider.name = "lmstudio"
    compat_provider.context_mode = "replay"
    compat_provider.stream_chat = _compat_stream_chat

    native_stub = _make_lmstudio_native_stub(compat_provider)
    registry = MagicMock()
    registry.get = MagicMock(side_effect=lambda name: native_stub if name == "lmstudio" else None)

    lm_client = MagicMock()
    lm_client.stream = MagicMock(
        side_effect=AssertionError("lm_client.stream must NOT be called in openai_compat mode")
    )

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    svc = StreamingService(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        chat_locks={},
        provider_registry=registry,
    )

    payload = ChatStreamRequest(
        chat_id=chat_id,
        payload=CanonicalChatRequest(
            model="test-model",
            input=[CanonicalInputBlock(type="text", content="what's new today")],
        ),
    )

    request = _mock_request()
    request.app.state.mcp_host = MagicMock()
    request.app.state.mcp_host.list_tools = MagicMock(return_value=[])
    request.app.state.mcp_server_store = None
    # No real web_search_service — the legend only needs builtin_web_search
    # to be True; execution wiring (WebSearchService itself) is irrelevant
    # to what already reached the wire system_prompt by this point.
    request.app.state.web_search_service = None

    await _drain(
        svc.stream_chat(chat_id=chat_id, user=_mock_user(), payload=payload, request=request)
    )

    sent = captured.get("request")
    assert sent is not None, "compat provider.stream_chat was not called"
    sys_p = getattr(sent, "system_prompt", "") or ""
    assert "[Capabilities]" in sys_p
    assert "- web_search — Search the live web for current information." in sys_p


@pytest.mark.asyncio
async def test_chain_mode_integrations_none_renders_fallback_without_crash(
    engine: AsyncEngine,
) -> None:
    """``integrations=None`` (the field's default — no ``integrations`` key
    on the wire payload at all, not an empty list) must not crash
    ``_assemble_system_prompt``'s ``list(payload.payload.integrations or
    [])`` normalization, and must render the none-enabled fallback line."""
    chat_id = await _insert_chat(engine)

    captured: dict[str, Any] = {}

    async def _fake_stream(*args: Any, **kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        captured["payload"] = kwargs.get("request") or (args[0] if args else None)
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
    )

    payload = ChatStreamRequest(
        chat_id=chat_id,
        payload=CanonicalChatRequest(
            model="test-model",
            input=[CanonicalInputBlock(type="text", content="hello")],
        ),
    )
    assert payload.payload.integrations is None, "test premise: integrations must be None"

    frames = await _drain(
        svc.stream_chat(
            chat_id=chat_id, user=_mock_user(), payload=payload, request=_mock_request()
        )
    )

    parsed = _parse_frames(frames)
    event_types = [d.get("type") for d in parsed]
    assert "error" not in event_types, (
        f"integrations=None must not error the stream. Got events: {event_types}"
    )
    assert "chat.end" in event_types, f"Stream must complete. Got events: {event_types}"

    sent = captured.get("payload")
    assert sent is not None, "lm_client.stream was not called"
    sys_p = getattr(sent, "system_prompt", "") or ""
    assert "(none enabled — the user can add tools with the composer's tool picker)" in sys_p
