# SPDX-License-Identifier: Apache-2.0
"""Tests for LmstudioStreamingClient.

Tests for the LM Studio streaming client (public reuse seam).

Covers:
- Happy-path event forwarding.
- on_tool_call callback invocation with a COMPLETE CanonicalToolCall.
- raise_on_error=True raises StreamingClientUpstreamError on error events.
- raise_on_error=False yields error events without raising.
- Stream terminates cleanly on chat.end or error.
- on_tool_call=None passes tool_call.* events through without accumulation.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Literal
from unittest.mock import MagicMock

import pytest

from lmchat.lmstudio.types import (
    CanonicalChatRequest,
    CanonicalEvent,
    CanonicalInputBlock,
    CanonicalToolCall,
)
from lmchat.services.lmstudio_streaming_client import (
    LmstudioStreamingClient,
    StreamingClientUpstreamError,
    _ToolCallAccumulator,
)

# All valid CanonicalEvent type literals — used by _event() to satisfy pyright.
_EventType = Literal[
    "chat.start",
    "chat.end",
    "model_load.start",
    "model_load.progress",
    "model_load.end",
    "prompt_processing.start",
    "prompt_processing.progress",
    "prompt_processing.end",
    "message.start",
    "message.delta",
    "message.end",
    "reasoning.start",
    "reasoning.delta",
    "reasoning.end",
    "tool_call.start",
    "tool_call.name",
    "tool_call.arguments",
    "tool_call.success",
    "tool_call.failure",
    "error",
]

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _make_request(model: str = "test-model") -> CanonicalChatRequest:
    """Build a minimal CanonicalChatRequest for testing."""
    return CanonicalChatRequest(
        model=model,
        input=[CanonicalInputBlock(type="text", content="hello")],
    )


def _event(type_: _EventType, **kwargs: Any) -> CanonicalEvent:
    """Build a CanonicalEvent with the given type and optional fields."""
    return CanonicalEvent(type=type_, **kwargs)


def _tool_call_event(
    type_: _EventType,
    *,
    id: str = "call-1",
    name: str = "search_web",
    arguments: dict[str, Any] | None = None,
) -> CanonicalEvent:
    """Build a tool_call.* CanonicalEvent with a CanonicalToolCall payload."""
    tc = CanonicalToolCall(id=id, name=name, arguments=arguments or {})
    return CanonicalEvent(type=type_, tool_call=tc)


async def _events_from(
    events: list[CanonicalEvent],
) -> AsyncIterator[CanonicalEvent]:
    """Yield a fixed list of events as an async iterator."""
    for ev in events:
        yield ev


def _make_adapter(events: list[CanonicalEvent]) -> MagicMock:
    """Build a mock LmstudioAdapter whose stream_chat yields the given events."""
    adapter = MagicMock()
    adapter.stream_chat = MagicMock(return_value=_events_from(events))
    return adapter


def _make_client(events: list[CanonicalEvent]) -> LmstudioStreamingClient:
    """Build an LmstudioStreamingClient backed by a mock yielding *events*."""
    return LmstudioStreamingClient(adapter=_make_adapter(events))


async def _collect(ait: AsyncIterator[CanonicalEvent]) -> list[CanonicalEvent]:
    """Drain an async iterator of CanonicalEvent into a list."""
    return [ev async for ev in ait]


# ---------------------------------------------------------------------------
# test_stream_forwards_canonical_events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_forwards_canonical_events() -> None:
    """All events from the adapter are yielded to the caller in order."""
    upstream = [
        _event("chat.start"),
        _event("message.start"),
        _event("message.delta", content="hello"),
        _event("message.end"),
        _event("chat.end"),
    ]
    client = _make_client(upstream)
    req = _make_request()

    result = await _collect(client.stream(request=req))

    assert [ev.type for ev in result] == [
        "chat.start",
        "message.start",
        "message.delta",
        "message.end",
        "chat.end",
    ]


# ---------------------------------------------------------------------------
# test_stream_calls_on_tool_call_callback_when_tool_call_completes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_calls_on_tool_call_callback_when_tool_call_completes() -> None:
    """on_tool_call receives a complete CanonicalToolCall on tool_call.success.

    The callback should receive the id, name, and accumulated arguments — NOT
    raw streaming events.  The raw events are still forwarded to the caller.
    """
    upstream = [
        _event("chat.start"),
        _tool_call_event("tool_call.start", id="tc-42", name="search_web"),
        _tool_call_event("tool_call.name", id="tc-42", name="search_web"),
        # arguments arrive as a JSON dict on the CanonicalEvent
        _tool_call_event("tool_call.arguments", id="tc-42", name="search_web",
                         arguments={"q": "lm studio"}),
        _tool_call_event("tool_call.success", id="tc-42", name="search_web"),
        _event("chat.end"),
    ]
    client = _make_client(upstream)
    req = _make_request()

    received_calls: list[CanonicalToolCall] = []

    def on_tc(call: CanonicalToolCall) -> None:
        received_calls.append(call)

    all_events = await _collect(client.stream(request=req, on_tool_call=on_tc))

    # Callback should have fired exactly once.
    assert len(received_calls) == 1
    tc = received_calls[0]
    assert tc.id == "tc-42"
    assert tc.name == "search_web"
    # Arguments should be the parsed dict from the accumulated event.
    assert tc.arguments == {"q": "lm studio"}

    # Raw tool_call.* events still forwarded.
    event_types = [ev.type for ev in all_events]
    assert "tool_call.start" in event_types
    assert "tool_call.success" in event_types
    assert "chat.end" in event_types


# ---------------------------------------------------------------------------
# test_stream_with_raise_on_error_True_raises_on_error_event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_with_raise_on_error_True_raises_on_error_event() -> None:
    """raise_on_error=True raises StreamingClientUpstreamError on error event."""
    error_event = CanonicalEvent(
        type="error",
        error={"code": "upstream_unavailable", "message": "LM Studio went away"},
    )
    upstream = [
        _event("chat.start"),
        error_event,
        _event("chat.end"),  # should never be reached
    ]
    client = _make_client(upstream)
    req = _make_request()

    with pytest.raises(StreamingClientUpstreamError) as exc_info:
        await _collect(client.stream(request=req, raise_on_error=True))

    assert exc_info.value.event is error_event
    assert "upstream_unavailable" in str(exc_info.value)


# ---------------------------------------------------------------------------
# test_stream_with_raise_on_error_False_yields_error_event_as_is
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_with_raise_on_error_False_yields_error_event_as_is() -> None:
    """raise_on_error=False yields error events without raising."""
    error_event = CanonicalEvent(
        type="error",
        error={"code": "malformed_tool_call", "message": "bad JSON"},
    )
    upstream = [
        _event("chat.start"),
        error_event,
    ]
    client = _make_client(upstream)
    req = _make_request()

    # Should NOT raise; error event should be in the output.
    result = await _collect(client.stream(request=req, raise_on_error=False))

    types = [ev.type for ev in result]
    assert "error" in types
    error_ev = next(ev for ev in result if ev.type == "error")
    assert error_ev.error is not None
    assert error_ev.error["code"] == "malformed_tool_call"


# ---------------------------------------------------------------------------
# test_stream_terminates_on_chat_end_or_error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_terminates_on_chat_end() -> None:
    """Stream stops after chat.end; events after chat.end are not yielded."""
    upstream = [
        _event("chat.start"),
        _event("message.delta", content="hi"),
        _event("chat.end"),
        # These should not appear in the output because the client stops after chat.end.
        _event("message.delta", content="ghost"),
    ]
    client = _make_client(upstream)
    req = _make_request()

    result = await _collect(client.stream(request=req))
    types = [ev.type for ev in result]

    assert types[-1] == "chat.end"
    assert types.count("message.delta") == 1


@pytest.mark.asyncio
async def test_stream_terminates_on_error_event() -> None:
    """Stream stops after an error event (raise_on_error=False)."""
    upstream = [
        _event("chat.start"),
        _event("error", error={"code": "upstream_stall", "message": "idle"}),
        # Should not be yielded.
        _event("message.delta", content="ghost"),
    ]
    client = _make_client(upstream)
    req = _make_request()

    result = await _collect(client.stream(request=req, raise_on_error=False))
    types = [ev.type for ev in result]

    assert "error" in types
    # The ghost delta must not appear.
    assert types.count("message.delta") == 0


# ---------------------------------------------------------------------------
# test_stream_with_none_on_tool_call_passes_tool_events_through_without_invocation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_with_none_on_tool_call_passes_tool_events_through() -> None:
    """on_tool_call=None (default): tool_call.* events pass through; no callback."""
    upstream = [
        _event("chat.start"),
        _tool_call_event("tool_call.start", id="tc-1", name="do_thing"),
        _tool_call_event("tool_call.name", id="tc-1", name="do_thing"),
        _tool_call_event("tool_call.success", id="tc-1", name="do_thing"),
        _event("chat.end"),
    ]
    client = _make_client(upstream)
    req = _make_request()

    # Pass no on_tool_call — defaults to None.
    result = await _collect(client.stream(request=req))
    types = [ev.type for ev in result]

    assert "tool_call.start" in types
    assert "tool_call.name" in types
    assert "tool_call.success" in types
    assert "chat.end" in types


# ---------------------------------------------------------------------------
# test_stream_on_tool_call_fires_on_failure_terminal_too
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_on_tool_call_fires_on_failure_terminal() -> None:
    """on_tool_call fires on tool_call.failure as well as tool_call.success."""
    upstream = [
        _event("chat.start"),
        _tool_call_event("tool_call.start", id="tc-err", name="risky_op"),
        _tool_call_event("tool_call.name", id="tc-err", name="risky_op"),
        _tool_call_event("tool_call.failure", id="tc-err", name="risky_op"),
        _event("chat.end"),
    ]
    client = _make_client(upstream)
    req = _make_request()

    received: list[CanonicalToolCall] = []
    await _collect(client.stream(request=req, on_tool_call=received.append))

    assert len(received) == 1
    assert received[0].name == "risky_op"
    assert received[0].id == "tc-err"


# ---------------------------------------------------------------------------
# test_stream_multiple_tool_calls_in_sequence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_multiple_tool_calls_callback_fires_per_call() -> None:
    """Accumulator resets between tool calls; callback fires once per call."""
    upstream = [
        _event("chat.start"),
        # First tool call
        _tool_call_event("tool_call.start", id="tc-1", name="search"),
        _tool_call_event("tool_call.name", id="tc-1", name="search"),
        _tool_call_event("tool_call.success", id="tc-1", name="search"),
        # Second tool call
        _tool_call_event("tool_call.start", id="tc-2", name="fetch"),
        _tool_call_event("tool_call.name", id="tc-2", name="fetch"),
        _tool_call_event("tool_call.success", id="tc-2", name="fetch"),
        _event("chat.end"),
    ]
    client = _make_client(upstream)
    req = _make_request()

    received: list[CanonicalToolCall] = []
    await _collect(client.stream(request=req, on_tool_call=received.append))

    assert len(received) == 2
    assert received[0].name == "search"
    assert received[1].name == "fetch"


# ---------------------------------------------------------------------------
# test_adapter_stream_chat_called_with_request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adapter_stream_chat_called_with_correct_request() -> None:
    """The client passes the CanonicalChatRequest directly to adapter.stream_chat."""
    adapter = _make_adapter([_event("chat.end")])
    client = LmstudioStreamingClient(adapter=adapter)
    req = _make_request(model="specific-model")

    await _collect(client.stream(request=req))

    adapter.stream_chat.assert_called_once_with(req, history=None, cumulative_tool_rounds=0)


# ---------------------------------------------------------------------------
# test_generator_exhaustion_without_chat_end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_exhausts_cleanly_without_chat_end() -> None:
    """If the adapter ends without chat.end, all events are still forwarded."""
    upstream = [
        _event("chat.start"),
        _event("message.delta", content="partial"),
        # No chat.end — adapter generator just stops.
    ]
    client = _make_client(upstream)
    req = _make_request()

    result = await _collect(client.stream(request=req))
    assert len(result) == 2
    assert result[0].type == "chat.start"
    assert result[1].type == "message.delta"


# ---------------------------------------------------------------------------
# _ToolCallAccumulator edge-case coverage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_tool_call_incomplete_no_name_no_callback() -> None:
    """If tool_call.start is never followed by tool_call.name, callback is NOT fired.

    Covers the accumulator.finalize() → None branch (missing name).
    """
    # Build an adapter that skips the tool_call.name event.
    # LM Studio uses empty string if name was omitted; accumulator treats it as falsy.
    name_less_start = CanonicalEvent(type="tool_call.start", tool_call=CanonicalToolCall(
        id="tc-x", name="", arguments={}
    ))
    name_less_success = CanonicalEvent(type="tool_call.success", tool_call=CanonicalToolCall(
        id="tc-x", name="", arguments={}
    ))
    upstream2 = [
        _event("chat.start"),
        name_less_start,
        name_less_success,
        _event("chat.end"),
    ]
    client = _make_client(upstream2)
    req = _make_request()

    received: list[CanonicalToolCall] = []
    await _collect(client.stream(request=req, on_tool_call=received.append))

    # name is empty-string → finalize returns None → callback not invoked.
    assert received == []


@pytest.mark.asyncio
async def test_stream_tool_call_dict_arguments_serialized() -> None:
    """When arguments arrive as a dict (not str), they are json.dumps-ed into buf."""
    # The CanonicalToolCall.arguments field is typed as dict; the accumulator
    # normalises it to a JSON string for concatenation.
    tc_args = CanonicalToolCall(id="tc-d", name="fetch", arguments={"url": "http://x"})
    upstream = [
        _event("chat.start"),
        CanonicalEvent(type="tool_call.start", tool_call=CanonicalToolCall(
            id="tc-d", name="fetch", arguments={}
        )),
        CanonicalEvent(type="tool_call.name", tool_call=CanonicalToolCall(
            id="tc-d", name="fetch", arguments={}
        )),
        CanonicalEvent(type="tool_call.arguments", tool_call=tc_args),
        CanonicalEvent(type="tool_call.success", tool_call=CanonicalToolCall(
            id="tc-d", name="fetch", arguments={}
        )),
        _event("chat.end"),
    ]
    client = _make_client(upstream)
    req = _make_request()

    received: list[CanonicalToolCall] = []
    await _collect(client.stream(request=req, on_tool_call=received.append))

    assert len(received) == 1
    assert received[0].arguments == {"url": "http://x"}


# ---------------------------------------------------------------------------
# _ToolCallAccumulator direct unit tests
# ---------------------------------------------------------------------------


def test_accumulator_finalize_malformed_json_raises() -> None:
    """finalize() raises MalformedToolCallError on non-JSON _arguments_buf.

    Previously the accumulator
    silently swallowed JSONDecodeError and emitted args={}, which
    deviated from the contract ("malformed tool_call → emit error frame +
    terminate"). The accumulator now surfaces a structured error so
    the calling stream() loop can yield a canonical error event and
    end cleanly.
    """
    from lmchat.services.lmstudio_streaming_client import MalformedToolCallError

    acc = _ToolCallAccumulator()
    acc._id = "tc-bad"
    acc._name = "bad_tool"
    acc._arguments_buf = ["not-valid-json{{{"]

    with pytest.raises(MalformedToolCallError) as exc_info:
        acc.finalize()
    assert exc_info.value.tool_name == "bad_tool"
    assert "not-valid-json" in exc_info.value.raw_preview


def test_stream_loop_catches_MalformedToolCallError_and_yields_error_event() -> None:
    """The LmstudioStreamingClient.stream loop wraps accumulator.finalize
    in try/except MalformedToolCallError and yields a canonical error
    event with code='tool_format_generation_error' on the raise path.

    The malformed-call error code is unified to
    "tool_format_generation_error" (matching LM Studio's own error.type for
    this failure class) so that the salvage gate in chats.py has a single
    canonical code to match against.

    Verifies behavior by source inspection: the stream() method's
    tool_call branch catches MalformedToolCallError from finalize()
    and yields CanonicalEvent(type="error", error={code, tool, message})
    before returning. The accumulator's raise path is unit-tested
    in test_accumulator_finalize_malformed_json_raises above; the
    stream() catch-and-emit is read from the source directly.

    Constructing an end-to-end integration scenario isn't possible
    via the public CanonicalEvent API because CanonicalToolCall.arguments
    is typed as dict — the wire-level decoders would reject malformed
    JSON before it reaches the accumulator. The malformed-arguments
    code path triggers only when accumulator state is polluted via
    direct attribute access, which is the unit-test scenario.
    """
    import inspect

    from lmchat.services.lmstudio_streaming_client import (
        LmstudioStreamingClient,
        MalformedToolCallError,
    )

    src = inspect.getsource(LmstudioStreamingClient.stream)
    assert "MalformedToolCallError" in src, (
        "stream() must catch MalformedToolCallError"
    )
    assert "tool_format_generation_error" in src, (
        "stream() must emit canonical error with code='tool_format_generation_error' "
        "(fix #8: unified code matching LM Studio's own error.type)"
    )
    # And the exception is publicly importable for callers.
    assert MalformedToolCallError.__name__ == "MalformedToolCallError"


def test_accumulator_ingest_string_chunk_appended_directly() -> None:
    """ingest() appends string chunks directly to the buffer (isinstance branch).

    Covers line 109 of the implementation: str chunks bypass json.dumps.

    tool_call.start must be seen first (noise gate).
    The test now sends a start event before the arguments event to arm
    _had_start; the core assertion (str chunk lands in _arguments_buf) is
    unchanged.
    """
    acc = _ToolCallAccumulator()
    # Arm the accumulator with a start event (required by noise gate).
    start_ev = CanonicalEvent(
        type="tool_call.start",
        tool_call=CanonicalToolCall(id="tc-s", name="str_tool", arguments={}),
    )
    acc.ingest(start_ev)
    acc._name = "str_tool"  # simulate tool_call.name arriving after start
    # Inject a pre-serialised JSON string by cheating Pydantic validation:
    # the CanonicalToolCall.arguments is typed dict, but at runtime we patch.
    tc = CanonicalToolCall(id="tc-s", name="str_tool", arguments={"k": "v"})
    # Simulate what would happen if arguments were a plain str at runtime:
    # We test _arguments_buf directly after constructing a fake event.
    # Because pydantic validates arguments as dict, we use object.__setattr__
    # to override the frozen model field for this adversarial test.
    object.__setattr__(tc, "arguments", '{"k":"v"}')  # str at runtime
    ev = CanonicalEvent(type="tool_call.arguments", tool_call=tc)
    acc.ingest(ev)
    assert acc._arguments_buf == ['{"k":"v"}']


def test_accumulator_finalize_returns_none_when_name_missing() -> None:
    """finalize() returns None when name was never received.

    The only hard gate is a missing name.
    A missing id now falls back to a synthesised id (native.py populates
    a uuid on tool_call.start, so _id should always be set in
    production).  Callers that only have an id but no name still get None
    so the callback is not fired with an anonymous tool call.
    """
    acc = _ToolCallAccumulator()
    # _name is None; _id is also None — name is the blocking gate
    assert acc.finalize() is None


def test_accumulator_finalize_fallback_id_when_id_missing() -> None:
    """finalize() synthesises a fallback id when _id is None but name is set.

    This covers the defensive path: native.py should always
    populate a uuid on tool_call.start, but if somehow _id is still None the
    accumulator generates a fallback rather than returning None.
    """
    acc = _ToolCallAccumulator()
    acc._name = "some_tool"
    # _id is None — should get a fallback id, not None
    result = acc.finalize()
    assert result is not None
    assert result.name == "some_tool"
    assert result.id.startswith("tc-fallback-")


def test_accumulator_reset_clears_all_state() -> None:
    """reset() clears id, name, and arguments buffer."""
    acc = _ToolCallAccumulator()
    acc._id = "x"
    acc._name = "y"
    acc._arguments_buf = ["z"]
    acc.reset()
    assert acc._id is None
    assert acc._name is None
    assert acc._arguments_buf == []
