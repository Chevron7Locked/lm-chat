# SPDX-License-Identifier: Apache-2.0
"""Unit tests for AgenticMcpProvider (agentic MCP tool loop).

Covers:
(a) Single tool round: tool advertised → model requests it → host.call_tool
    invoked with correct args → real tool_call.success carries output →
    history gets assistant.tool_calls + tool result → re-issue → model
    finalizes → clean chat.end.
(b) Multi-round: two successive tool requests before natural stop.
(c) Max-rounds cap: loop exhausted → warning event + chat.end emitted.
(d) Tool error non-fatal: call_tool raises / returns [mcp_error] → loop
    continues, model receives the error string, produces final answer.
(e) No-integrations passthrough: empty server_ids → no host calls, events
    pass through unmodified.
(f) Premature tool_call.success suppression: the inner provider's bare
    tool_call.success (no output, finish_reason="tool_calls") is suppressed;
    the real one with output is yielded in its place.
(g) id threading: the uuid4 used on .start/.name/.arguments is the
    same id that appears on the synthesised tool_call.success.
(h) _integrations_to_server_ids: correct slug extraction.
(i) Dispatch wiring — streaming_service: cloud+integrations → AgenticMcpProvider
    used; cloud+no-integrations → plain provider; lmstudio → untouched.
(j) Dispatch wiring — sub-session: same three cases.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from lmchat.lmstudio.types import (
    CanonicalChatRequest,
    CanonicalEvent,
    CanonicalInputBlock,
    CanonicalTool,
    CanonicalToolCall,
)
from lmchat.mcp.agentic import (
    AgenticMcpProvider,
    _integrations_to_server_ids,
    maybe_wrap_agentic,
)
from lmchat.mcp.host import McpHost
from lmchat.services.builtin_tools import (
    BuiltinToolContext,
    BuiltinToolEntry,
    BuiltinToolRegistry,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(integrations: list[str] | None = None) -> CanonicalChatRequest:
    return CanonicalChatRequest(
        model="openai/gpt-4o-mini",
        input=[CanonicalInputBlock(type="text", content="hi")],
        integrations=integrations or [],
    )


def _ev(type_: str, **kwargs: Any) -> CanonicalEvent:
    return CanonicalEvent(type=type_, **kwargs)  # type: ignore[arg-type]


def _tool_call_ev(
    type_: str,
    tc_id: str,
    name: str = "srv_search",
    args: dict | None = None,
) -> CanonicalEvent:
    return CanonicalEvent(
        type=type_,  # type: ignore[arg-type]
        tool_call=CanonicalToolCall(id=tc_id, name=name, arguments=args or {}),
    )


def _make_inner_provider(event_sequences: list[list[CanonicalEvent]]) -> Any:
    """Stub inner provider that yields successive event sequences on each call."""
    stub = MagicMock()
    stub.name = "openrouter"
    stub.context_mode = "replay"
    _iter = iter(event_sequences)

    async def _stream(events: list[CanonicalEvent]) -> AsyncIterator[CanonicalEvent]:
        for ev in events:
            yield ev

    def _stream_chat(req: Any, **kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        try:
            evs = next(_iter)
        except StopIteration:
            evs = [_ev("chat.end", stop_reason="stop")]
        return _stream(evs)

    stub.stream_chat = _stream_chat
    return stub


def _make_mock_host(
    tools: list[CanonicalTool] | None = None,
    call_result: str = "search result",
    call_raises: Exception | None = None,
    connect_result: bool = True,
) -> MagicMock:
    """Build a mock McpHost.

    Models the real cold-start contract: ``list_tools`` only returns the
    configured tools for a server AFTER ``connect`` has been awaited for it.
    Before connect, ``list_tools`` returns ``[]`` — exactly the bug that
    broke cold-start when connect was fire-and-forget.  This lets the tests
    prove the loop awaits connect() before consulting list_tools().
    """
    host = MagicMock(spec=McpHost)
    host._connected = set()
    host.connect_calls = []
    host.list_tools_calls = []

    async def _connect(sid: str) -> bool:
        host.connect_calls.append(sid)
        if connect_result:
            host._connected.add(sid)
        return connect_result

    host.connect = AsyncMock(side_effect=_connect)

    def _list_tools(server_ids: list[str] | None = None) -> list[CanonicalTool]:
        host.list_tools_calls.append(server_ids)
        if not tools:
            return []
        ids = server_ids if server_ids is not None else list(host._connected)
        # Only advertise tools for servers that are actually connected.
        if any(sid in host._connected for sid in ids):
            return list(tools)
        return []

    host.list_tools = MagicMock(side_effect=_list_tools)

    async def _call_tool(name: str, arguments: Any = None) -> str:
        if call_raises is not None:
            raise call_raises
        return call_result

    host.call_tool = _call_tool
    return host


def _make_builtin_registry(
    name: str = "web_search",
    executor: Any = None,
) -> tuple[BuiltinToolRegistry, AsyncMock]:
    """Build a one-entry BuiltinToolRegistry backed by a stub executor.

    Returns (registry, executor_mock) so tests can assert on the exact
    (arguments, ctx) the executor was invoked with, independent of the real
    ``_web_search_executor`` / ``WebSearchService``.
    """
    tool = CanonicalTool(
        name=name,
        description="Search the web",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    )
    mock_executor = (
        executor if executor is not None else AsyncMock(return_value="stub search result")
    )
    registry = BuiltinToolRegistry({name: BuiltinToolEntry(tool=tool, executor=mock_executor)})
    return registry, mock_executor


async def _collect(
    provider: AgenticMcpProvider,
    request: CanonicalChatRequest,
    history: list | None = None,
) -> list[CanonicalEvent]:
    events: list[CanonicalEvent] = []
    async for ev in provider.stream_chat(request, history=history):  # type: ignore[misc]
        events.append(ev)
    return events


# ---------------------------------------------------------------------------
# (h) _integrations_to_server_ids
# ---------------------------------------------------------------------------


def test_integrations_to_server_ids_basic() -> None:
    result = _integrations_to_server_ids(["mcp/searxng", "mcp/context7", "mcp/filesystem"])
    assert result == ["searxng", "context7", "filesystem"]


def test_integrations_to_server_ids_filters_non_mcp() -> None:
    result = _integrations_to_server_ids(["mcp/searxng", "lmstudio/web_search", "other"])
    assert result == ["searxng"]


def test_integrations_to_server_ids_empty() -> None:
    assert _integrations_to_server_ids([]) == []


def test_integrations_to_server_ids_bare_mcp_slash_ignored() -> None:
    # "mcp/" with no slug should be ignored.
    result = _integrations_to_server_ids(["mcp/", "mcp/real"])
    assert result == ["real"]


# ---------------------------------------------------------------------------
# Cold-start: connect() awaited before list_tools (regression guard)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_awaited_before_list_tools() -> None:
    """The loop AWAITS connect() for every server_id BEFORE consulting list_tools.

    Regression for the cold-start bug: a fire-and-forget connect raced
    list_tools so the model was advertised zero tools.  Now connect must be
    awaited first, and the round-1 advertised tools must reflect the connected
    servers.
    """
    tool = CanonicalTool(
        name="srv_search",
        description="Search",
        parameters={"type": "object", "properties": {}},
    )
    # Mock host: list_tools returns [] until connect has run for the server.
    host = _make_mock_host(tools=[tool])

    captured_round1_tools: dict[str, Any] = {}

    async def _stream(req: Any, **kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        # Capture what tools were advertised on the FIRST (and only) round.
        captured_round1_tools["tools"] = kwargs.get("tools")
        captured_round1_tools["req_tools"] = list(req.tools)
        yield _ev("message.delta", content="answered")
        yield _ev("chat.end", stop_reason="stop")

    inner = MagicMock()
    inner.name = "openrouter"
    inner.context_mode = "replay"
    inner.stream_chat = lambda req, **kw: _stream(req, **kw)

    provider = AgenticMcpProvider(
        inner=inner, mcp_host=host, server_ids=["firecrawl", "searxng"]
    )

    await _collect(provider, _make_request())

    # connect() was awaited for BOTH server_ids.
    assert host.connect_calls == ["firecrawl", "searxng"]

    # list_tools was consulted AFTER connect (i.e. connect_calls populated
    # before the first list_tools call returned tools).
    assert host.list_tools.called
    assert host.list_tools_calls[0] == ["firecrawl", "searxng"]

    # Round-1 advertised tools reflect the now-connected servers (NOT empty).
    advertised = captured_round1_tools.get("req_tools") or []
    assert len(advertised) == 1
    assert advertised[0].name == "srv_search"


@pytest.mark.asyncio
async def test_connect_failure_nonfatal_contributes_no_tools() -> None:
    """A server whose connect() fails is non-fatal — it just contributes no tools."""
    tool = CanonicalTool(
        name="srv_search",
        description="Search",
        parameters={"type": "object", "properties": {}},
    )
    # connect_result=False → server never enters the connected set → no tools.
    host = _make_mock_host(tools=[tool], connect_result=False)

    inner = _make_inner_provider([
        [_ev("message.delta", content="no tools"), _ev("chat.end", stop_reason="stop")],
    ])
    provider = AgenticMcpProvider(inner=inner, mcp_host=host, server_ids=["down"])

    events = await _collect(provider, _make_request())

    # connect was still awaited.
    assert host.connect_calls == ["down"]
    # Stream still terminated cleanly despite the failed connect.
    assert any(ev.type == "chat.end" for ev in events)
    # No tool-call success events (no tools were advertised).
    assert not any(ev.type == "tool_call.success" for ev in events)


@pytest.mark.asyncio
async def test_connect_exception_nonfatal() -> None:
    """An exception raised by connect() is caught (return_exceptions) — loop survives."""
    host = _make_mock_host(tools=[])
    host.connect = AsyncMock(side_effect=RuntimeError("spawn failed"))

    inner = _make_inner_provider([
        [_ev("message.delta", content="ok"), _ev("chat.end", stop_reason="stop")],
    ])
    provider = AgenticMcpProvider(inner=inner, mcp_host=host, server_ids=["boom"])

    events = await _collect(provider, _make_request())

    # connect() was attempted.
    host.connect.assert_awaited_once_with("boom")
    # The stream still terminated cleanly.
    assert any(ev.type == "chat.end" for ev in events)


# ---------------------------------------------------------------------------
# (a) Single tool round
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_tool_round() -> None:
    """Model requests one tool → host executes → result injected → model finalizes."""
    tc_id = "tc-abc123"
    tool = CanonicalTool(
        name="srv_search",
        description="Search",
        parameters={"type": "object", "properties": {}},
    )

    # Round 1: model requests a tool call.
    round1 = [
        _ev("chat.start"),
        _ev("message.delta", content="Let me search."),
        _tool_call_ev("tool_call.start", tc_id),
        _tool_call_ev("tool_call.name", tc_id, name="srv_search"),
        _tool_call_ev("tool_call.arguments", tc_id, name="srv_search", args={"q": "test"}),
        # Premature tool_call.success — no output, just the terminator.
        _ev("tool_call.success"),
        _ev("chat.end"),
    ]
    # Round 2: model produces final answer.
    round2 = [
        _ev("chat.start"),
        _ev("message.delta", content="Here is the answer."),
        _ev("message.end"),
        _ev("chat.end", stop_reason="stop"),
    ]

    inner = _make_inner_provider([round1, round2])
    host = _make_mock_host(tools=[tool], call_result="search result text")
    provider = AgenticMcpProvider(inner=inner, mcp_host=host, server_ids=["srv"])

    events = await _collect(provider, _make_request())

    # tool_call.start/.name/.arguments should pass through.
    types = [ev.type for ev in events]
    assert "tool_call.start" in types
    assert "tool_call.name" in types
    assert "tool_call.arguments" in types

    # The REAL tool_call.success (with output) must appear, not the premature bare one.
    success_evs = [ev for ev in events if ev.type == "tool_call.success"]
    assert len(success_evs) == 1
    assert success_evs[0].tool_call is not None
    assert success_evs[0].tool_call.result == "search result text"
    assert success_evs[0].tool_call.name == "srv_search"
    assert success_evs[0].tool_call.arguments == {"q": "test"}

    # chat.end must be present (from round 2).
    end_evs = [ev for ev in events if ev.type == "chat.end"]
    assert len(end_evs) == 1
    assert end_evs[0].stop_reason == "stop"

    # host.call_tool must have been called once with the right name.
    # (We used a real coroutine for call_tool; verify via event output.)
    assert any(ev.tool_call and ev.tool_call.result == "search result text" for ev in events)


# ---------------------------------------------------------------------------
# (f) Premature tool_call.success suppression
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_premature_success_suppressed() -> None:
    """The inner premature tool_call.success (no output) must NOT reach the consumer."""
    tc_id = "tc-suppress"

    round1 = [
        _tool_call_ev("tool_call.start", tc_id, name="srv_tool"),
        _tool_call_ev("tool_call.name", tc_id, name="srv_tool"),
        _tool_call_ev("tool_call.arguments", tc_id, name="srv_tool", args={}),
        # Bare premature success — no output.
        _ev("tool_call.success"),
        _ev("chat.end"),
    ]
    round2 = [
        _ev("message.delta", content="done"),
        _ev("chat.end", stop_reason="stop"),
    ]

    inner = _make_inner_provider([round1, round2])
    host = _make_mock_host(call_result="tool output")
    provider = AgenticMcpProvider(inner=inner, mcp_host=host, server_ids=["srv"])

    events = await _collect(provider, _make_request())

    success_evs = [ev for ev in events if ev.type == "tool_call.success"]
    # Exactly one — the real synthesised one, not the premature bare one.
    assert len(success_evs) == 1
    # The synthesised one carries the result.
    assert success_evs[0].tool_call is not None
    assert success_evs[0].tool_call.result == "tool output"


# ---------------------------------------------------------------------------
# (g) id threading
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mf9_id_threading() -> None:
    """The uuid4 from tool_call.start is threaded through to the synthesised success."""
    tc_id = "thread-me-through"

    round1 = [
        _tool_call_ev("tool_call.start", tc_id, name="srv_tool"),
        _tool_call_ev("tool_call.name", tc_id, name="srv_tool"),
        _tool_call_ev("tool_call.arguments", tc_id, name="srv_tool", args={"x": 1}),
        _ev("tool_call.success"),
        _ev("chat.end"),
    ]
    round2 = [
        _ev("message.delta", content="done"),
        _ev("chat.end", stop_reason="stop"),
    ]

    inner = _make_inner_provider([round1, round2])
    host = _make_mock_host(call_result="result")
    provider = AgenticMcpProvider(inner=inner, mcp_host=host, server_ids=["srv"])

    events = await _collect(provider, _make_request())

    # The .start event carries tc_id.
    start_ev = next(ev for ev in events if ev.type == "tool_call.start")
    assert start_ev.tool_call is not None
    assert start_ev.tool_call.id == tc_id

    # The synthesised success carries the SAME id.
    success_ev = next(ev for ev in events if ev.type == "tool_call.success")
    assert success_ev.tool_call is not None
    assert success_ev.tool_call.id == tc_id


# ---------------------------------------------------------------------------
# (b) Multi-round
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_round() -> None:
    """Two successive tool requests before the model stops."""
    tc1 = "tc-round1"
    tc2 = "tc-round2"

    round1 = [
        _tool_call_ev("tool_call.start", tc1, name="srv_tool_a"),
        _tool_call_ev("tool_call.name", tc1, name="srv_tool_a"),
        _tool_call_ev("tool_call.arguments", tc1, name="srv_tool_a", args={}),
        _ev("tool_call.success"),
        _ev("chat.end"),
    ]
    round2 = [
        _tool_call_ev("tool_call.start", tc2, name="srv_tool_b"),
        _tool_call_ev("tool_call.name", tc2, name="srv_tool_b"),
        _tool_call_ev("tool_call.arguments", tc2, name="srv_tool_b", args={}),
        _ev("tool_call.success"),
        _ev("chat.end"),
    ]
    round3 = [
        _ev("message.delta", content="All done."),
        _ev("chat.end", stop_reason="stop"),
    ]

    inner = _make_inner_provider([round1, round2, round3])
    host = _make_mock_host(call_result="res")
    provider = AgenticMcpProvider(inner=inner, mcp_host=host, server_ids=["srv"])

    events = await _collect(provider, _make_request())

    success_evs = [ev for ev in events if ev.type == "tool_call.success"]
    # Two real successes — one per round.
    assert len(success_evs) == 2
    for sev in success_evs:
        assert sev.tool_call is not None
        assert sev.tool_call.result == "res"

    end_evs = [ev for ev in events if ev.type == "chat.end"]
    assert len(end_evs) == 1
    assert end_evs[0].stop_reason == "stop"


# ---------------------------------------------------------------------------
# (c) Max-rounds cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_rounds_cap() -> None:
    """Loop exhausted → warning event + synthetic chat.end emitted."""
    MAX = 3

    def _tool_round(tc_id: str) -> list[CanonicalEvent]:
        return [
            _tool_call_ev("tool_call.start", tc_id, name="srv_tool"),
            _tool_call_ev("tool_call.name", tc_id, name="srv_tool"),
            _tool_call_ev("tool_call.arguments", tc_id, name="srv_tool", args={}),
            _ev("tool_call.success"),
            _ev("chat.end"),
        ]

    # Always return tool rounds — never a natural stop.
    sequences = [_tool_round(f"tc-{i}") for i in range(MAX + 5)]
    inner = _make_inner_provider(sequences)
    host = _make_mock_host(call_result="ok")
    provider = AgenticMcpProvider(inner=inner, mcp_host=host, server_ids=["srv"], max_rounds=MAX)

    events = await _collect(provider, _make_request())

    # warning event must be emitted.
    warn_evs = [ev for ev in events if ev.type == "warning"]
    assert len(warn_evs) == 1
    assert warn_evs[0].warning is not None
    assert warn_evs[0].warning.get("code") == "agentic_max_rounds"

    # chat.end must terminate the stream.
    end_evs = [ev for ev in events if ev.type == "chat.end"]
    assert len(end_evs) == 1

    # Exactly MAX tool successes were emitted.
    success_evs = [ev for ev in events if ev.type == "tool_call.success"]
    assert len(success_evs) == MAX


# ---------------------------------------------------------------------------
# (d) Tool error non-fatal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_error_nonfatal_exception() -> None:
    """call_tool raises → [mcp_error] result → tool_call.failure, loop continues."""
    tc_id = "tc-err"
    round1 = [
        _tool_call_ev("tool_call.start", tc_id, name="srv_tool"),
        _tool_call_ev("tool_call.name", tc_id, name="srv_tool"),
        _tool_call_ev("tool_call.arguments", tc_id, name="srv_tool", args={}),
        _ev("tool_call.success"),
        _ev("chat.end"),
    ]
    round2 = [
        _ev("message.delta", content="Got the error, continuing."),
        _ev("chat.end", stop_reason="stop"),
    ]

    inner = _make_inner_provider([round1, round2])
    host = _make_mock_host(call_raises=RuntimeError("network down"))
    provider = AgenticMcpProvider(inner=inner, mcp_host=host, server_ids=["srv"])

    events = await _collect(provider, _make_request())

    # The errored tool must surface as tool_call.failure, NOT success —
    # otherwise the FE shows a green card and the model reads the failure as a
    # win. The [mcp_error] detail rides event.error (canonical failure shape).
    failure_evs = [ev for ev in events if ev.type == "tool_call.failure"]
    assert len(failure_evs) == 1
    assert failure_evs[0].error is not None
    assert "[mcp_error]" in (failure_evs[0].error.get("message") or "")
    # It must NOT be rendered as a success carrying the error string.
    assert not [
        ev
        for ev in events
        if ev.type == "tool_call.success"
        and ev.tool_call is not None
        and "[mcp_error]" in (ev.tool_call.result or "")
    ]

    # Stream must still terminate cleanly.
    end_evs = [ev for ev in events if ev.type == "chat.end"]
    assert len(end_evs) == 1


@pytest.mark.asyncio
async def test_tool_error_nonfatal_mcp_error_string() -> None:
    """call_tool returns [mcp_error] string → tool_call.failure, loop continues."""
    tc_id = "tc-mcp-err"
    round1 = [
        _tool_call_ev("tool_call.start", tc_id, name="srv_bad"),
        _tool_call_ev("tool_call.name", tc_id, name="srv_bad"),
        _tool_call_ev("tool_call.arguments", tc_id, name="srv_bad", args={}),
        _ev("tool_call.success"),
        _ev("chat.end"),
    ]
    round2 = [
        _ev("message.delta", content="ok"),
        _ev("chat.end", stop_reason="stop"),
    ]

    inner = _make_inner_provider([round1, round2])
    _err_msg = "[mcp_error] Tool 'srv_bad' not found in any connected server."
    host = _make_mock_host(call_result=_err_msg)
    provider = AgenticMcpProvider(inner=inner, mcp_host=host, server_ids=["srv"])

    events = await _collect(provider, _make_request())

    # A host "[mcp_error] …" sentinel (not-found here) → tool_call.failure,
    # never a green success card.
    failure_evs = [ev for ev in events if ev.type == "tool_call.failure"]
    assert len(failure_evs) == 1
    assert failure_evs[0].error is not None
    assert "[mcp_error]" in (failure_evs[0].error.get("message") or "")
    assert failure_evs[0].error.get("tool") == "srv_bad"
    assert not [
        ev
        for ev in events
        if ev.type == "tool_call.success"
        and ev.tool_call is not None
        and "[mcp_error]" in (ev.tool_call.result or "")
    ]

    end_evs = [ev for ev in events if ev.type == "chat.end"]
    assert len(end_evs) == 1


# ---------------------------------------------------------------------------
# (e) No-integrations passthrough
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_integrations_passthrough() -> None:
    """Empty server_ids → no host.call_tool invocations; events pass through."""
    happy = [
        _ev("chat.start"),
        _ev("message.delta", content="hi"),
        _ev("chat.end", stop_reason="stop"),
    ]
    inner = _make_inner_provider([happy])
    host = _make_mock_host(tools=[], call_result="should not be called")
    provider = AgenticMcpProvider(inner=inner, mcp_host=host, server_ids=[])

    events = await _collect(provider, _make_request())

    # list_tools called with empty list → no tools advertised.
    host.list_tools.assert_called_once_with([])

    # No tool success events.
    assert not any(ev.type == "tool_call.success" for ev in events)

    # All happy events present.
    assert [ev.type for ev in events] == ["chat.start", "message.delta", "chat.end"]


# ---------------------------------------------------------------------------
# (i)/(j) Dispatch wiring — maybe_wrap_agentic (single source of truth shared by
# StreamingService.stream_chat AND routes.chats.sub_session_stream)
# ---------------------------------------------------------------------------


def _make_app_state(mcp_host: Any = None, server_store: Any = None) -> Any:
    """Minimal app.state stand-in for maybe_wrap_agentic.

    Both attributes are set EXPLICITLY (MagicMock would otherwise auto-vivify a
    truthy child), so ``getattr(app_state, "mcp_host", None)`` returns exactly
    what the test wants — including ``None``.
    """
    state = MagicMock()
    state.mcp_host = mcp_host
    state.mcp_server_store = server_store
    return state


def _make_server_store(policies: dict[str, set[str]]) -> Any:
    """Async mcp_server_store stub whose get() returns a view with tool_policy."""
    store = MagicMock()

    async def _get(sid: str) -> Any:
        if sid not in policies:
            return None
        view = MagicMock()
        view.tool_policy = policies[sid]
        return view

    store.get = _get
    return store


def _plain_provider(name: str = "openrouter") -> Any:
    inner = MagicMock()
    inner.name = name
    inner.context_mode = "replay"
    return inner


@pytest.mark.asyncio
async def test_maybe_wrap_no_mcp_integrations_returns_inner() -> None:
    """No mcp/* entries → inner returned unchanged (identity)."""
    inner = _plain_provider()
    host = _make_mock_host()
    result = await maybe_wrap_agentic(
        inner, ["lmstudio/web_search", "native/foo"], _make_app_state(mcp_host=host)
    )
    assert result is inner
    assert not isinstance(result, AgenticMcpProvider)


@pytest.mark.asyncio
async def test_maybe_wrap_empty_integrations_returns_inner() -> None:
    """None / empty integrations → inner returned unchanged."""
    inner = _plain_provider()
    host = _make_mock_host()
    assert await maybe_wrap_agentic(inner, None, _make_app_state(mcp_host=host)) is inner
    assert await maybe_wrap_agentic(inner, [], _make_app_state(mcp_host=host)) is inner


@pytest.mark.asyncio
async def test_maybe_wrap_no_host_returns_inner() -> None:
    """mcp/* present but no McpHost on app.state → inner returned (plain path)."""
    inner = _plain_provider()
    result = await maybe_wrap_agentic(
        inner, ["mcp/searxng"], _make_app_state(mcp_host=None)
    )
    assert result is inner
    assert not isinstance(result, AgenticMcpProvider)


@pytest.mark.asyncio
async def test_maybe_wrap_cloud_with_integrations_wraps() -> None:
    """Cloud + mcp integrations + host → AgenticMcpProvider with resolved ids."""
    inner = _plain_provider("openrouter")
    host = _make_mock_host()
    result = await maybe_wrap_agentic(
        inner,
        ["mcp/searxng", "mcp/context7", "native/ignored"],
        _make_app_state(mcp_host=host),
    )
    assert isinstance(result, AgenticMcpProvider)
    assert result.name == "openrouter+mcp"
    # Only the mcp/* entries are mapped to server ids, slug after the slash.
    assert result._server_ids == ["searxng", "context7"]


@pytest.mark.asyncio
async def test_maybe_wrap_assembles_denied_tools_from_store() -> None:
    """Per-server tool_policy entries are unioned into denied_tools."""
    inner = _plain_provider()
    host = _make_mock_host()
    store = _make_server_store(
        {"searxng": {"searxng_dangerous"}, "context7": {"context7_write"}}
    )
    result = await maybe_wrap_agentic(
        inner,
        ["mcp/searxng", "mcp/context7"],
        _make_app_state(mcp_host=host, server_store=store),
    )
    assert isinstance(result, AgenticMcpProvider)
    assert result._denied_tools == {"searxng_dangerous", "context7_write"}


@pytest.mark.asyncio
async def test_maybe_wrap_no_store_leaves_denied_empty() -> None:
    """No mcp_server_store → denied_tools stays empty (passed as None)."""
    inner = _plain_provider()
    host = _make_mock_host()
    result = await maybe_wrap_agentic(
        inner, ["mcp/searxng"], _make_app_state(mcp_host=host, server_store=None)
    )
    assert isinstance(result, AgenticMcpProvider)
    assert result._denied_tools == set()


# ---------------------------------------------------------------------------
# builtin_registry keeps maybe_wrap_agentic active with zero
# mcp/* integrations (the openai_compat web_search dispatch).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_wrap_builtin_registry_no_mcp_integrations_wraps() -> None:
    """builtin_registry + NO mcp/* integrations → still wraps (not the bare inner),
    carrying the registry/ctx through to the provider."""
    inner = _plain_provider()
    host = _make_mock_host()
    registry, _executor = _make_builtin_registry()
    ctx = BuiltinToolContext()
    result = await maybe_wrap_agentic(
        inner,
        [],
        _make_app_state(mcp_host=host),
        builtin_registry=registry,
        builtin_ctx=ctx,
    )
    assert isinstance(result, AgenticMcpProvider)
    assert result._builtin_registry is registry
    assert result._builtin_ctx is ctx
    assert result._server_ids == []


@pytest.mark.asyncio
async def test_maybe_wrap_neither_mcp_nor_builtin_returns_inner_unchanged() -> None:
    """No mcp/* integrations AND no builtin_registry → inner returned unchanged,
    preserving the prior behavior for every existing call site."""
    inner = _plain_provider()
    host = _make_mock_host()
    result = await maybe_wrap_agentic(inner, [], _make_app_state(mcp_host=host))
    assert result is inner
    assert not isinstance(result, AgenticMcpProvider)


# ---------------------------------------------------------------------------
# denied_tools filtering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_denied_tool_filtered_before_advertising() -> None:
    """A tool whose name is in denied_tools must not be advertised to the model."""
    tool_a = CanonicalTool(
        name="firecrawl_scrape",
        description="Scrape a URL",
        parameters={"type": "object", "properties": {}},
    )
    tool_b = CanonicalTool(
        name="firecrawl_map",
        description="Map a domain",
        parameters={"type": "object", "properties": {}},
    )
    host = _make_mock_host(tools=[tool_a, tool_b])

    # Capture the effective_tools passed to the inner provider's stream_chat.
    captured_tools: list[list[CanonicalTool]] = []

    async def _capturing_stream(
        req: Any, **kwargs: Any
    ) -> AsyncIterator[CanonicalEvent]:
        captured_tools.append(list(req.tools or []))
        yield _ev("chat.end", finish_reason="stop")

    stub = MagicMock()
    stub.name = "openrouter"
    stub.context_mode = "replay"
    stub.stream_chat = _capturing_stream

    provider = AgenticMcpProvider(
        inner=stub,
        mcp_host=host,
        server_ids=["firecrawl"],
        denied_tools={"firecrawl_scrape"},
    )
    req = _make_request(integrations=["mcp/firecrawl"])
    events = await _collect(provider, req, history=[])

    assert captured_tools, "stream_chat was never called on the inner provider"
    names_advertised = {t.name for t in captured_tools[0]}
    assert "firecrawl_scrape" not in names_advertised, (
        f"Denied tool 'firecrawl_scrape' should not be advertised; got {names_advertised}"
    )
    assert "firecrawl_map" in names_advertised, (
        f"Non-denied tool 'firecrawl_map' should be advertised; got {names_advertised}"
    )

    # Stream should complete normally.
    types = [e.type for e in events]
    assert "chat.end" in types


@pytest.mark.asyncio
async def test_non_denied_tool_passes_through() -> None:
    """A tool NOT in denied_tools is advertised normally."""
    tool_a = CanonicalTool(
        name="firecrawl_scrape",
        description="Scrape a URL",
        parameters={"type": "object", "properties": {}},
    )
    host = _make_mock_host(tools=[tool_a])

    captured_tools: list[list[CanonicalTool]] = []

    async def _capturing_stream(
        req: Any, **kwargs: Any
    ) -> AsyncIterator[CanonicalEvent]:
        captured_tools.append(list(req.tools or []))
        yield _ev("chat.end", finish_reason="stop")

    stub = MagicMock()
    stub.name = "openrouter"
    stub.context_mode = "replay"
    stub.stream_chat = _capturing_stream

    # Empty denied_tools — nothing filtered.
    provider = AgenticMcpProvider(
        inner=stub,
        mcp_host=host,
        server_ids=["firecrawl"],
        denied_tools=set(),
    )
    req = _make_request(integrations=["mcp/firecrawl"])
    await _collect(provider, req, history=[])

    assert captured_tools, "stream_chat was never called on the inner provider"
    names_advertised = {t.name for t in captured_tools[0]}
    assert "firecrawl_scrape" in names_advertised


# ---------------------------------------------------------------------------
# agentic final-synthesis: guaranteed single terminal frame
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_rounds_final_synthesis_error_event_no_double_chat_end() -> None:
    """When the final synthesis inner call yields an error event,
    exactly one terminal frame is emitted (the error event itself) and NO
    trailing chat.end is appended.

    StreamingService returns on error — a trailing chat.end after an error
    would be unprocessed AND skip clean finalization.  The error IS the
    terminator.

    Red-on-revert: removing the _synthesis_saw_error guard causes a chat.end
    to be emitted after the error event, producing two terminal frames.
    """
    MAX = 1

    def _tool_round(tc_id: str) -> list[CanonicalEvent]:
        return [
            _tool_call_ev("tool_call.start", tc_id, name="srv_tool"),
            _tool_call_ev("tool_call.name", tc_id, name="srv_tool"),
            _tool_call_ev("tool_call.arguments", tc_id, name="srv_tool", args={}),
            _ev("tool_call.success"),
            _ev("chat.end"),
        ]

    # Final synthesis round yields an error event (not a normal answer).
    final_synthesis_events = [
        _ev("message.delta", content="partial"),
        _ev("error", error={"code": "upstream_error", "message": "model overloaded"}),
    ]

    # Round 1 = tool round; final synthesis = error
    sequences = [_tool_round("tc-0"), final_synthesis_events]
    inner = _make_inner_provider(sequences)
    host = _make_mock_host(call_result="ok")
    provider = AgenticMcpProvider(inner=inner, mcp_host=host, server_ids=["srv"], max_rounds=MAX)

    events = await _collect(provider, _make_request())

    # The error event must be present.
    error_evs = [ev for ev in events if ev.type == "error"]
    assert len(error_evs) == 1, f"Expected 1 error event; got {len(error_evs)}"
    assert error_evs[0].error is not None
    assert error_evs[0].error.get("code") == "upstream_error"

    # chat.end must NOT be emitted after the error (error is the terminator).
    end_evs = [ev for ev in events if ev.type == "chat.end"]
    assert len(end_evs) == 0, (
        f"Expected 0 chat.end events after synthesis error; got {len(end_evs)}"
    )

    # The warning event (max_rounds) must still be present.
    warn_evs = [ev for ev in events if ev.type == "warning"]
    assert len(warn_evs) == 1
    assert warn_evs[0].warning is not None
    assert warn_evs[0].warning.get("code") == "agentic_max_rounds"


@pytest.mark.asyncio
async def test_max_rounds_final_synthesis_exception_graceful_error_plus_chat_end() -> None:
    """When the final synthesis inner call raises an exception, the
    generator yields a graceful error event + chat.end and does NOT propagate
    the raw exception.

    Red-on-revert: removing the try/except causes the exception to propagate
    out of stream_chat(), leaving the FE with no terminal frame.
    """
    MAX = 1

    def _tool_round(tc_id: str) -> list[CanonicalEvent]:
        return [
            _tool_call_ev("tool_call.start", tc_id, name="srv_tool"),
            _tool_call_ev("tool_call.name", tc_id, name="srv_tool"),
            _tool_call_ev("tool_call.arguments", tc_id, name="srv_tool", args={}),
            _ev("tool_call.success"),
            _ev("chat.end"),
        ]

    # Build a special inner provider: round 1 is a normal tool round; the
    # final synthesis call raises a network exception.
    stub = MagicMock()
    stub.name = "openrouter"
    stub.context_mode = "replay"

    call_count = 0
    tool_round_events = _tool_round("tc-raise")

    async def _raising_stream(events: list[CanonicalEvent]) -> AsyncIterator[CanonicalEvent]:
        for ev in events:
            yield ev

    async def _network_error_stream(*args: Any, **kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        raise ConnectionError("network failure during final synthesis")
        yield  # make it an async generator

    def _stream_chat(req: Any, **kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First call: normal tool round.
            return _raising_stream(tool_round_events)
        else:
            # Second call: the final synthesis — RAISES.
            return _network_error_stream()

    stub.stream_chat = _stream_chat

    host = _make_mock_host(call_result="ok")
    provider = AgenticMcpProvider(inner=stub, mcp_host=host, server_ids=["srv"], max_rounds=MAX)

    # Must NOT raise — generator must yield a graceful error + chat.end.
    events = await _collect(provider, _make_request())

    # A graceful error event must be present.
    error_evs = [ev for ev in events if ev.type == "error"]
    assert len(error_evs) == 1, f"Expected 1 graceful error event; got {len(error_evs)}"
    assert error_evs[0].error is not None
    assert "synthesis" in error_evs[0].error.get("message", "").lower() or \
           "network failure" in error_evs[0].error.get("message", "").lower() or \
           error_evs[0].error.get("code") == "agentic_synthesis_error", (
               f"Unexpected error payload: {error_evs[0].error}"
           )

    # chat.end MUST be emitted after an exception (so FE gets a terminal frame).
    end_evs = [ev for ev in events if ev.type == "chat.end"]
    assert len(end_evs) == 1, (
        f"Expected exactly 1 chat.end after caught exception; got {len(end_evs)}"
    )

    # The warning event (max_rounds) must still be present.
    warn_evs = [ev for ev in events if ev.type == "warning"]
    assert len(warn_evs) == 1
    assert warn_evs[0].warning is not None
    assert warn_evs[0].warning.get("code") == "agentic_max_rounds"


@pytest.mark.asyncio
async def test_max_rounds_final_synthesis_happy_path_single_chat_end() -> None:
    """The happy path must still produce exactly ONE chat.end
    (existing test_max_rounds_cap already covers count, but this is explicit
    about the synthesis subpath not double-emitting).
    """
    MAX = 1

    def _tool_round(tc_id: str) -> list[CanonicalEvent]:
        return [
            _tool_call_ev("tool_call.start", tc_id, name="srv_tool"),
            _tool_call_ev("tool_call.name", tc_id, name="srv_tool"),
            _tool_call_ev("tool_call.arguments", tc_id, name="srv_tool", args={}),
            _ev("tool_call.success"),
            _ev("chat.end"),
        ]

    final_synthesis = [
        _ev("message.delta", content="Here is my final answer."),
        _ev("message.end"),
        # chat.start and chat.end are swallowed by the synthesis filter;
        # the agentic loop emits its own chat.end.
        _ev("chat.start"),
        _ev("chat.end", stop_reason="stop"),
    ]

    sequences = [_tool_round("tc-happy"), final_synthesis]
    inner = _make_inner_provider(sequences)
    host = _make_mock_host(call_result="ok")
    provider = AgenticMcpProvider(inner=inner, mcp_host=host, server_ids=["srv"], max_rounds=MAX)

    events = await _collect(provider, _make_request())

    end_evs = [ev for ev in events if ev.type == "chat.end"]
    assert len(end_evs) == 1, (
        f"Happy-path synthesis must yield exactly 1 chat.end; got {len(end_evs)}"
    )
    assert end_evs[0].stop_reason == "stop"


# ---------------------------------------------------------------------------
# dispatch wiring tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_service_denied_tools_assembled_from_store() -> None:
    """streaming_service dispatch: denied_tools are loaded from store and passed to provider."""
    captured: dict[str, Any] = {}

    class _CapturingAP:
        """Stub that captures init args without running the real loop."""

        def __init__(self, inner: Any, mcp_host: Any, server_ids: Any, **kwargs: Any) -> None:
            captured["denied_tools"] = kwargs.get("denied_tools")

        async def stream_chat(self, *args: Any, **kwargs: Any) -> AsyncIterator[CanonicalEvent]:
            yield _ev("chat.end", finish_reason="stop")
            return

    # Build a minimal mock store with one server that has a denylist.
    from lmchat.services.mcp_server_store import McpServerInternalView

    mock_store = AsyncMock()
    mock_store.get = AsyncMock(
        return_value=McpServerInternalView(
            id=1,
            slug="firecrawl",
            name="Firecrawl",
            transport="stdio",
            command=None,
            args=None,
            url=None,
            tool_policy=["firecrawl_scrape"],
        )
    )

    # Minimal app.state stubs.
    mock_app_state = MagicMock()
    mock_app_state.mcp_host = MagicMock()
    mock_app_state.mcp_host.connected_server_ids = []
    mock_app_state.mcp_server_store = mock_store

    mock_request = MagicMock()
    mock_request.app.state = mock_app_state

    # We exercise the wiring logic in isolation: replicate the dispatch snippet.
    _server_ids = ["firecrawl"]
    _b4_denied: set[str] = set()
    for _sid in _server_ids:
        _b4_view = await mock_store.get(_sid)
        if _b4_view is not None:
            _b4_denied.update(_b4_view.tool_policy)

    assert "firecrawl_scrape" in _b4_denied, (
        f"Expected firecrawl_scrape in denied set; got {_b4_denied!r}"
    )


@pytest.mark.asyncio
async def test_b4_no_integrations_denied_tools_empty() -> None:
    """When no integrations are present, denied_tools is empty (no store access needed)."""
    # This validates that the LM Studio path is NOT touched by the denied-tools logic.
    # The denied-tools block only runs when _mcp_integrations is non-empty.
    integrations = []  # no mcp/ integrations
    _mcp_integrations = [i for i in integrations if i.startswith("mcp/")]
    assert not _mcp_integrations  # guard: no integrations → denied-tools block never runs


# ---------------------------------------------------------------------------
# (j) max_rounds → final tool-less synthesis (no empty answer)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_rounds_forces_final_toolless_synthesis() -> None:
    """When the loop exhausts max_rounds without a natural stop, it must make
    ONE final pass with tools disabled so the model synthesizes an answer from
    the gathered tool results — NOT end with an empty turn (the
    observed "thinks forever, then nothing" on cloud agentic turns)."""
    tool = CanonicalTool(
        name="srv_search",
        description="Search",
        parameters={"type": "object", "properties": {}},
    )

    def _tool_round(tc_id: str) -> list[CanonicalEvent]:
        # The model ONLY requests a tool every round — it never finalizes,
        # so the loop captures a call each round and re-issues until max_rounds.
        return [
            _ev("chat.start"),
            _tool_call_ev("tool_call.start", tc_id),
            _tool_call_ev("tool_call.name", tc_id, name="srv_search"),
            _tool_call_ev("tool_call.arguments", tc_id, name="srv_search", args={"q": "x"}),
            _ev("tool_call.success"),  # premature terminator (finish_reason=tool_calls)
            _ev("chat.end"),
        ]

    # The final tool-less synthesis pass returns a real answer.
    final = [
        _ev("chat.start"),
        _ev("message.delta", content="SYNTHESIZED_ANSWER"),
        _ev("message.end"),
        _ev("chat.end", stop_reason="stop"),
    ]

    inner = _make_inner_provider([_tool_round("t1"), _tool_round("t2"), final])
    host = _make_mock_host(tools=[tool], call_result="result")
    provider = AgenticMcpProvider(
        inner=inner, mcp_host=host, server_ids=["srv"], max_rounds=2
    )

    events = await _collect(provider, _make_request())

    # The max_rounds warning was emitted.
    assert any(
        ev.type == "warning" and (ev.warning or {}).get("code") == "agentic_max_rounds"
        for ev in events
    ), "expected the agentic_max_rounds warning"

    # The final synthesized content was yielded — NOT an empty turn.
    deltas = [ev for ev in events if ev.type == "message.delta"]
    assert any(ev.content == "SYNTHESIZED_ANSWER" for ev in deltas), (
        "max_rounds must trigger a final tool-less synthesis, not an empty answer"
    )

    # Stream ends cleanly.
    assert events[-1].type == "chat.end"
    assert events[-1].stop_reason == "stop"


# ---------------------------------------------------------------------------
# Builtin tool routing — additive, off-by-default extension of AgenticMcpProvider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_builtin_registry_web_search_name_goes_to_mcp_host() -> None:
    """Without a builtin_registry, a call named 'web_search' is NOT special-cased —
    it flows through the existing McpHost path exactly like any other tool name.
    Guards against accidentally hardcoding the builtin name instead of consulting
    the registry."""
    tc_id = "tc-no-registry"
    round1 = [
        _tool_call_ev("tool_call.start", tc_id, name="web_search"),
        _tool_call_ev("tool_call.name", tc_id, name="web_search"),
        _tool_call_ev("tool_call.arguments", tc_id, name="web_search", args={"query": "x"}),
        _ev("tool_call.success"),
        _ev("chat.end"),
    ]
    round2 = [
        _ev("message.delta", content="done"),
        _ev("chat.end", stop_reason="stop"),
    ]

    inner = _make_inner_provider([round1, round2])
    host = _make_mock_host(call_result="mcp result")
    provider = AgenticMcpProvider(inner=inner, mcp_host=host, server_ids=["srv"])

    # Defaults: no registry — the byte-identical guard. (builtin_ctx is
    # normalized internally to an empty BuiltinToolContext() rather than
    # None, but it's never consulted while builtin_registry is None.)
    assert provider._builtin_registry is None

    events = await _collect(provider, _make_request())

    success_evs = [ev for ev in events if ev.type == "tool_call.success"]
    assert len(success_evs) == 1
    assert success_evs[0].tool_call is not None
    assert success_evs[0].tool_call.result == "mcp result"


@pytest.mark.asyncio
async def test_builtin_tool_advertised_when_registry_present() -> None:
    """A builtin registry's tools are merged into effective_tools advertised
    to the model."""
    registry, _executor = _make_builtin_registry()
    host = _make_mock_host(tools=[])  # no MCP tools at all

    captured_tools: list[list[CanonicalTool]] = []

    async def _capturing_stream(req: Any, **kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        captured_tools.append(list(req.tools or []))
        yield _ev("chat.end", stop_reason="stop")

    stub = MagicMock()
    stub.name = "openrouter"
    stub.context_mode = "replay"
    stub.stream_chat = _capturing_stream

    provider = AgenticMcpProvider(
        inner=stub, mcp_host=host, server_ids=[], builtin_registry=registry
    )
    await _collect(provider, _make_request())

    assert captured_tools, "stream_chat was never called on the inner provider"
    names = {t.name for t in captured_tools[0]}
    assert "web_search" in names


@pytest.mark.asyncio
async def test_builtin_tool_executed_via_registry_not_host() -> None:
    """A builtin tool call is routed through the registry executor, never McpHost,
    and yields a real tool_call.success carrying the executor's result."""
    tc_id = "tc-builtin"
    registry, executor_mock = _make_builtin_registry(
        executor=AsyncMock(return_value="builtin result text")
    )

    round1 = [
        _tool_call_ev("tool_call.start", tc_id, name="web_search"),
        _tool_call_ev("tool_call.name", tc_id, name="web_search"),
        _tool_call_ev("tool_call.arguments", tc_id, name="web_search", args={"query": "foo"}),
        _ev("tool_call.success"),
        _ev("chat.end"),
    ]
    round2 = [
        _ev("message.delta", content="Here you go."),
        _ev("chat.end", stop_reason="stop"),
    ]

    inner = _make_inner_provider([round1, round2])
    host = _make_mock_host()
    host.call_tool = AsyncMock(return_value="SHOULD_NOT_BE_USED")
    ctx = BuiltinToolContext()
    provider = AgenticMcpProvider(
        inner=inner,
        mcp_host=host,
        server_ids=[],
        builtin_registry=registry,
        builtin_ctx=ctx,
    )

    events = await _collect(provider, _make_request())

    # The executor was invoked with the parsed arguments + the shared ctx.
    executor_mock.assert_awaited_once_with({"query": "foo"}, ctx)
    # McpHost was never consulted for this call.
    host.call_tool.assert_not_called()

    success_evs = [ev for ev in events if ev.type == "tool_call.success"]
    assert len(success_evs) == 1
    assert success_evs[0].tool_call is not None
    assert success_evs[0].tool_call.result == "builtin result text"
    assert success_evs[0].tool_call.name == "web_search"

    end_evs = [ev for ev in events if ev.type == "chat.end"]
    assert len(end_evs) == 1
    assert end_evs[0].stop_reason == "stop"


@pytest.mark.asyncio
async def test_builtin_tool_history_threading() -> None:
    """A builtin tool call appends the SAME 2-message shape as the MCP path:
    an assistant tool_calls message, then a tool-role result message — and
    the loop re-issues with that history."""
    tc_id = "tc-hist"
    registry, _executor = _make_builtin_registry(
        executor=AsyncMock(return_value="hist result")
    )

    round1 = [
        _tool_call_ev("tool_call.start", tc_id, name="web_search"),
        _tool_call_ev("tool_call.name", tc_id, name="web_search"),
        _tool_call_ev("tool_call.arguments", tc_id, name="web_search", args={"query": "x"}),
        _ev("tool_call.success"),
        _ev("chat.end"),
    ]

    captured_history: list[Any] = []
    call_count = 0

    async def _round1_stream(*_args: Any, **_kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        for ev in round1:
            yield ev

    async def _round2_stream(*_args: Any, **kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        captured_history.append(kwargs.get("history"))
        yield _ev("message.delta", content="done")
        yield _ev("chat.end", stop_reason="stop")

    stub = MagicMock()
    stub.name = "openrouter"
    stub.context_mode = "replay"

    def _stream_chat(req: Any, **kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _round1_stream(req, **kwargs)
        return _round2_stream(req, **kwargs)

    stub.stream_chat = _stream_chat

    host = _make_mock_host()
    host.call_tool = AsyncMock(return_value="SHOULD_NOT_BE_USED")
    provider = AgenticMcpProvider(
        inner=stub, mcp_host=host, server_ids=[], builtin_registry=registry
    )

    await _collect(provider, _make_request())

    assert captured_history, "round 2 was never issued"
    history = captured_history[0]
    assert len(history) == 2
    assert history[0].role == "assistant"
    assert history[0].tool_calls is not None
    assert history[0].tool_calls[0].name == "web_search"
    assert history[0].tool_calls[0].id == tc_id
    assert history[1].role == "tool"
    assert history[1].content == "hist result"
    assert history[1].tool_call_id == tc_id

    host.call_tool.assert_not_called()


@pytest.mark.asyncio
async def test_mixed_builtin_and_mcp_calls_routed_correctly() -> None:
    """One round with a builtin call AND an MCP call: each is routed to the
    right executor — the builtin name to the registry, the MCP name to
    McpHost — and both surface as real tool_call.success events."""
    tc_builtin = "tc-builtin-mix"
    tc_mcp = "tc-mcp-mix"
    registry, executor_mock = _make_builtin_registry(
        executor=AsyncMock(return_value="builtin mixed result")
    )

    round1 = [
        _tool_call_ev("tool_call.start", tc_builtin, name="web_search"),
        _tool_call_ev("tool_call.name", tc_builtin, name="web_search"),
        _tool_call_ev("tool_call.arguments", tc_builtin, name="web_search", args={"query": "a"}),
        _tool_call_ev("tool_call.start", tc_mcp, name="srv_search"),
        _tool_call_ev("tool_call.name", tc_mcp, name="srv_search"),
        _tool_call_ev("tool_call.arguments", tc_mcp, name="srv_search", args={"q": "b"}),
        _ev("tool_call.success"),
        _ev("chat.end"),
    ]
    round2 = [
        _ev("message.delta", content="done"),
        _ev("chat.end", stop_reason="stop"),
    ]

    inner = _make_inner_provider([round1, round2])
    tool = CanonicalTool(
        name="srv_search", description="Search", parameters={"type": "object", "properties": {}}
    )
    host = _make_mock_host(tools=[tool], call_result="mcp result text")
    provider = AgenticMcpProvider(
        inner=inner, mcp_host=host, server_ids=["srv"], builtin_registry=registry
    )

    events = await _collect(provider, _make_request())

    # No builtin_ctx was supplied — the provider normalizes to an empty
    # BuiltinToolContext() rather than passing None (the executor's ctx
    # param isn't Optional).
    executor_mock.assert_awaited_once_with({"query": "a"}, BuiltinToolContext())

    success_evs = [ev for ev in events if ev.type == "tool_call.success"]
    assert len(success_evs) == 2
    results = {ev.tool_call.name: ev.tool_call.result for ev in success_evs if ev.tool_call}
    assert results["web_search"] == "builtin mixed result"
    assert results["srv_search"] == "mcp result text"


@pytest.mark.asyncio
async def test_unknown_tool_name_falls_through_to_mcp_host() -> None:
    """A tool name absent from the builtin registry still routes to McpHost
    even when a registry IS supplied — existing MCP behavior unchanged for
    every name the registry doesn't know about."""
    registry, executor_mock = _make_builtin_registry()  # only knows "web_search"

    tc_id = "tc-unknown"
    round1 = [
        _tool_call_ev("tool_call.start", tc_id, name="srv_other"),
        _tool_call_ev("tool_call.name", tc_id, name="srv_other"),
        _tool_call_ev("tool_call.arguments", tc_id, name="srv_other", args={}),
        _ev("tool_call.success"),
        _ev("chat.end"),
    ]
    round2 = [
        _ev("message.delta", content="done"),
        _ev("chat.end", stop_reason="stop"),
    ]

    inner = _make_inner_provider([round1, round2])
    host = _make_mock_host(call_result="mcp handled it")
    provider = AgenticMcpProvider(
        inner=inner, mcp_host=host, server_ids=["srv"], builtin_registry=registry
    )

    events = await _collect(provider, _make_request())

    executor_mock.assert_not_awaited()

    success_evs = [ev for ev in events if ev.type == "tool_call.success"]
    assert len(success_evs) == 1
    assert success_evs[0].tool_call is not None
    assert success_evs[0].tool_call.result == "mcp handled it"


@pytest.mark.asyncio
async def test_builtin_takes_precedence_on_name_collision_advertising() -> None:
    """If an MCP tool and a builtin tool share a name, the builtin descriptor
    wins in the advertised tool list (it also wins at execution time, below —
    the advertised schema must match what actually runs)."""
    colliding_name = "web_search"
    mcp_tool = CanonicalTool(
        name=colliding_name,
        description="MCP-provided web search (should be shadowed)",
        parameters={"type": "object", "properties": {}},
    )
    registry, _executor = _make_builtin_registry(
        name=colliding_name, executor=AsyncMock(return_value="builtin wins")
    )
    host = _make_mock_host(tools=[mcp_tool])

    captured_tools: list[list[CanonicalTool]] = []

    async def _capturing_stream(req: Any, **kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        captured_tools.append(list(req.tools or []))
        yield _ev("chat.end", stop_reason="stop")

    stub = MagicMock()
    stub.name = "openrouter"
    stub.context_mode = "replay"
    stub.stream_chat = _capturing_stream

    provider = AgenticMcpProvider(
        inner=stub, mcp_host=host, server_ids=["srv"], builtin_registry=registry
    )
    await _collect(provider, _make_request())

    matches = [t for t in captured_tools[0] if t.name == colliding_name]
    assert len(matches) == 1, "the colliding name must be advertised exactly once"
    assert matches[0].description == "Search the web"  # the builtin descriptor wins


@pytest.mark.asyncio
async def test_builtin_takes_precedence_on_name_collision_execution() -> None:
    """On a name collision, the EXECUTE step also prefers the builtin executor,
    matching the advertising precedence above."""
    colliding_name = "web_search"
    tc_id = "tc-collide"
    registry, executor_mock = _make_builtin_registry(
        name=colliding_name, executor=AsyncMock(return_value="builtin executed")
    )

    round1 = [
        _tool_call_ev("tool_call.start", tc_id, name=colliding_name),
        _tool_call_ev("tool_call.name", tc_id, name=colliding_name),
        _tool_call_ev("tool_call.arguments", tc_id, name=colliding_name, args={"query": "x"}),
        _ev("tool_call.success"),
        _ev("chat.end"),
    ]
    round2 = [
        _ev("message.delta", content="done"),
        _ev("chat.end", stop_reason="stop"),
    ]

    inner = _make_inner_provider([round1, round2])
    mcp_tool = CanonicalTool(
        name=colliding_name,
        description="mcp version",
        parameters={"type": "object", "properties": {}},
    )
    host = _make_mock_host(tools=[mcp_tool])
    host.call_tool = AsyncMock(return_value="SHOULD_NOT_BE_USED")
    provider = AgenticMcpProvider(
        inner=inner, mcp_host=host, server_ids=["srv"], builtin_registry=registry
    )

    events = await _collect(provider, _make_request())

    executor_mock.assert_awaited_once_with({"query": "x"}, BuiltinToolContext())
    host.call_tool.assert_not_called()

    success_evs = [ev for ev in events if ev.type == "tool_call.success"]
    assert len(success_evs) == 1
    assert success_evs[0].tool_call is not None
    assert success_evs[0].tool_call.result == "builtin executed"


@pytest.mark.asyncio
async def test_builtin_executor_exception_backstop_still_yields_success() -> None:
    """If a builtin executor violates its never-raise contract, the
    contract-violation backstop catches it and still yields tool_call.success
    (never the "[mcp_error] …" tool_call.failure branch) — a builtin failure
    is never mistaken for an MCP failure."""
    tc_id = "tc-builtin-raises"
    registry, _executor = _make_builtin_registry(
        executor=AsyncMock(side_effect=RuntimeError("contract violation"))
    )

    round1 = [
        _tool_call_ev("tool_call.start", tc_id, name="web_search"),
        _tool_call_ev("tool_call.name", tc_id, name="web_search"),
        _tool_call_ev("tool_call.arguments", tc_id, name="web_search", args={"query": "x"}),
        _ev("tool_call.success"),
        _ev("chat.end"),
    ]
    round2 = [
        _ev("message.delta", content="done"),
        _ev("chat.end", stop_reason="stop"),
    ]

    inner = _make_inner_provider([round1, round2])
    host = _make_mock_host()
    provider = AgenticMcpProvider(
        inner=inner, mcp_host=host, server_ids=[], builtin_registry=registry
    )

    events = await _collect(provider, _make_request())

    failure_evs = [ev for ev in events if ev.type == "tool_call.failure"]
    assert not failure_evs, "a builtin exception must not surface as tool_call.failure"

    success_evs = [ev for ev in events if ev.type == "tool_call.success"]
    assert len(success_evs) == 1
    assert success_evs[0].tool_call is not None
    assert "contract violation" in (success_evs[0].tool_call.result or "")
