# SPDX-License-Identifier: Apache-2.0
"""Unit tests for lmstudio/responses.py — encoder + SSE decoder.

Locked wire-truth for the /v1/responses adapter contract.

Covers:
- encode_responses: flat tool shape, input array,
  instructions/system_prompt, tool_choice="auto" lock, scalar params.
- decode_responses: event mapping from response.* SSE family to canonical events,
  function_call round-trip with call_id, reasoning blocks, message text.
- _parse_responses_sse_chunk: correct (event_type, data_json) extraction.
- call_id field on CanonicalToolCall populated from function_call output items.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import httpx
import pytest

from lmchat.lmstudio.responses import (
    _parse_responses_sse_chunk,
    _ResponsesDecoderState,
    decode_responses,
    encode_responses,
)
from lmchat.lmstudio.types import (
    CanonicalChatRequest,
    CanonicalInputBlock,
    CanonicalMessage,
    CanonicalTool,
    CanonicalToolCall,
)

# ---------------------------------------------------------------------------
# encode_responses
# ---------------------------------------------------------------------------


def _make_req(
    *,
    model: str = "test-model",
    tools: list[CanonicalTool] | None = None,
    system_prompt: str | None = None,
    temperature: float | None = None,
    input_text: str = "hello",
) -> CanonicalChatRequest:
    return CanonicalChatRequest(
        model=model,
        input=[CanonicalInputBlock(type="text", content=input_text)],
        tools=tools or [],
        system_prompt=system_prompt,
        temperature=temperature,
    )


def test_encode_responses_uses_input_not_messages() -> None:
    """encode_responses uses 'input', NOT 'messages'."""
    body = encode_responses(_make_req(), history=[])
    assert "input" in body
    assert "messages" not in body


def test_encode_responses_tool_choice_always_auto() -> None:
    """tool_choice is always 'auto' regardless of request."""
    body = encode_responses(
        _make_req(tools=[CanonicalTool(name="calc", description="math", parameters={})]),
        history=[],
    )
    assert body.get("tool_choice") == "auto", (
        "tool_choice must be 'auto' — 'required' is non-binding on /v1/responses"
    )


def test_encode_responses_flat_tool_shape() -> None:
    """tools use the flat Responses-API shape (not nested Chat Completions shape).

    Flat shape: {type, name, description, parameters}
    Nested shape (compat): {type, function: {name, description, parameters}}
    """
    tool = CanonicalTool(
        name="calculator",
        description="Evaluate math expressions",
        parameters={"type": "object", "properties": {"expr": {"type": "string"}}},
    )
    body = encode_responses(_make_req(tools=[tool]), history=[])

    tools = body.get("tools", [])
    assert len(tools) == 1
    t = tools[0]
    assert t["type"] == "function"
    assert t["name"] == "calculator", "name must be top-level on flat shape"
    assert t["description"] == "Evaluate math expressions"
    assert "parameters" in t
    # Must NOT be nested under a "function" key.
    assert "function" not in t, (
        "Responses-API uses flat tool shape; 'function' nested key is Chat Completions only"
    )


def test_encode_responses_system_prompt_as_instructions() -> None:
    """system_prompt is encoded as the top-level 'instructions' field."""
    body = encode_responses(
        _make_req(system_prompt="You are a helpful assistant."),
        history=[],
    )
    assert body.get("instructions") == "You are a helpful assistant."
    # Must NOT produce a system-role message in input.
    for item in body.get("input", []):
        assert item.get("role") != "system", (
            "system_prompt must go to 'instructions', not a system-role input item"
        )


def test_encode_responses_no_instructions_when_no_system_prompt() -> None:
    """When system_prompt is None, 'instructions' key is absent."""
    body = encode_responses(_make_req(), history=[])
    assert "instructions" not in body


def test_encode_responses_scalar_params() -> None:
    """temperature is passed through as a scalar param."""
    body = encode_responses(_make_req(temperature=0.7), history=[])
    assert body.get("temperature") == 0.7


def test_encode_responses_no_reasoning_field() -> None:
    """'reasoning' is NOT included in /v1/responses request body."""
    req = CanonicalChatRequest(
        model="test-model",
        input=[CanonicalInputBlock(type="text", content="hello")],
        reasoning="high",
    )
    body = encode_responses(req, history=[])
    assert "reasoning" not in body, (
        "'reasoning' must not be passed to /v1/responses (not a recognised field)"
    )


def test_encode_responses_history_in_input() -> None:
    """History messages are assembled into the 'input' array."""
    history = [
        CanonicalMessage(role="user", content="what is 2+2"),
        CanonicalMessage(role="assistant", content="4"),
    ]
    body = encode_responses(_make_req(), history=history)
    input_items = body["input"]
    contents = [item["content"] for item in input_items]
    assert "what is 2+2" in contents
    assert "4" in contents


def test_encode_responses_history_tool_result_call_id() -> None:
    """Tool result messages include call_id when CanonicalMessage.tool_call_id is set."""
    history = [
        CanonicalMessage(
            role="tool",
            content="47 * 53 = 2491",
            tool_call_id="call_abc123",
        ),
    ]
    body = encode_responses(_make_req(), history=history)
    tool_items = [item for item in body["input"] if item.get("role") == "tool"]
    assert len(tool_items) == 1
    assert tool_items[0].get("call_id") == "call_abc123"


def test_encode_responses_current_turn_appended() -> None:
    """The current turn (req.input) is appended as the last input item."""
    body = encode_responses(_make_req(input_text="test question"), history=[])
    last = body["input"][-1]
    assert last["role"] == "user"
    assert last["content"] == "test question"


def test_encode_responses_stream_true() -> None:
    """stream is always True (consistent with codebase convention)."""
    body = encode_responses(_make_req(), history=[])
    assert body.get("stream") is True


# ---------------------------------------------------------------------------
# _parse_responses_sse_chunk
# ---------------------------------------------------------------------------


def test_parse_responses_sse_chunk_basic() -> None:
    """Parses standard (event: / data:) SSE frames correctly."""
    raw = (
        b"event: response.created\n"
        b"data: {\"id\": \"resp_abc\"}\n"
        b"\n"
        b"event: response.output_text.delta\n"
        b"data: {\"delta\": \"hello\"}\n"
        b"\n"
    )
    events = _parse_responses_sse_chunk(raw)
    assert len(events) == 2
    assert events[0] == ("response.created", '{"id": "resp_abc"}')
    assert events[1] == ("response.output_text.delta", '{"delta": "hello"}')


def test_parse_responses_sse_chunk_empty() -> None:
    """Empty bytes yield no events."""
    assert _parse_responses_sse_chunk(b"") == []


def test_parse_responses_sse_chunk_no_trailing_blank() -> None:
    """Chunk without trailing blank line still captures the last event."""
    raw = b"event: response.completed\ndata: {}\n"
    events = _parse_responses_sse_chunk(raw)
    assert len(events) == 1
    assert events[0][0] == "response.completed"


# ---------------------------------------------------------------------------
# _ResponsesDecoderState.handle_event
# ---------------------------------------------------------------------------


def test_decoder_state_response_created_emits_chat_start() -> None:
    """response.created → chat.start with response_id."""
    state = _ResponsesDecoderState()
    events = state.handle_event("response.created", {"id": "resp_xyz", "status": "in_progress"})
    assert len(events) == 1
    assert events[0].type == "chat.start"
    assert events[0].response_id == "resp_xyz"


def test_decoder_state_response_in_progress_emits_nothing() -> None:
    """response.in_progress is a heartbeat — yields no canonical events."""
    state = _ResponsesDecoderState()
    events = state.handle_event("response.in_progress", {"status": "in_progress"})
    assert events == []


def test_decoder_state_output_text_delta_first_emits_start() -> None:
    """First response.output_text.delta → message.start + message.delta."""
    state = _ResponsesDecoderState()
    events = state.handle_event("response.output_text.delta", {"delta": "Hello"})
    types = [e.type for e in events]
    assert "message.start" in types
    assert "message.delta" in types
    delta_events = [e for e in events if e.type == "message.delta"]
    assert delta_events[0].content == "Hello"


def test_decoder_state_output_text_delta_subsequent_no_start() -> None:
    """Subsequent response.output_text.delta events do NOT re-emit message.start."""
    state = _ResponsesDecoderState()
    state.handle_event("response.output_text.delta", {"delta": "Hello"})
    events2 = state.handle_event("response.output_text.delta", {"delta": " world"})
    types = [e.type for e in events2]
    assert "message.start" not in types
    assert "message.delta" in types
    assert events2[-1].content == " world"


def test_decoder_state_reasoning_delta_emits_start_then_delta() -> None:
    """First response.reasoning_text.delta → reasoning.start + reasoning.delta."""
    state = _ResponsesDecoderState()
    events = state.handle_event("response.reasoning_text.delta", {"delta": "thinking..."})
    types = [e.type for e in events]
    assert "reasoning.start" in types
    assert "reasoning.delta" in types
    assert events[-1].content == "thinking..."


def test_decoder_state_response_completed_emits_chat_end() -> None:
    """response.completed → chat.end with response_id."""
    state = _ResponsesDecoderState()
    state.response_id = "resp_abc"
    events = state.handle_event(
        "response.completed",
        {"response": {"id": "resp_abc", "status": "completed"}},
    )
    assert len(events) == 1
    assert events[0].type == "chat.end"
    assert events[0].response_id == "resp_abc"


def test_decoder_state_message_item_done_emits_message_end() -> None:
    """response.output_item.done with type=message → message.end."""
    state = _ResponsesDecoderState()
    state.in_message = True  # simulate being in a message
    events = state.handle_event(
        "response.output_item.done",
        {"item": {"id": "msg_001", "type": "message", "status": "completed"}},
    )
    types = [e.type for e in events]
    assert "message.end" in types


def test_decoder_state_function_call_item_done_emits_tool_events() -> None:
    """response.output_item.done → tool_call.start + .arguments + .success."""
    state = _ResponsesDecoderState()
    state.items["fc_001"] = {"type": "function_call", "name": "calculator", "args_buf": ""}
    events = state.handle_event(
        "response.output_item.done",
        {
            "item": {
                "id": "fc_001",
                "type": "function_call",
                "name": "calculator",
                "arguments": '{"expr": "47*53"}',
                "call_id": "call_xyz",
                "status": "completed",
            },
        },
    )
    types = [e.type for e in events]
    assert "tool_call.start" in types
    assert "tool_call.arguments" in types
    assert "tool_call.success" in types

    # Verify call_id is populated on the CanonicalToolCall.
    start_events = [e for e in events if e.type == "tool_call.start"]
    assert start_events[0].tool_call is not None
    assert start_events[0].tool_call.call_id == "call_xyz", (
        "call_id must be populated from the function_call output item for round-trip"
    )
    assert start_events[0].tool_call.name == "calculator"

    args_events = [e for e in events if e.type == "tool_call.arguments"]
    assert args_events[0].tool_call is not None
    assert args_events[0].tool_call.arguments == {"expr": "47*53"}


def test_decoder_state_unknown_event_returns_empty() -> None:
    """Unknown response.* events yield no canonical events (forward-compatibility)."""
    state = _ResponsesDecoderState()
    events = state.handle_event("response.future_new_event", {"foo": "bar"})
    assert events == []


def test_decoder_state_reasoning_end_on_message_transition() -> None:
    """Transitioning from reasoning to message emits reasoning.end."""
    state = _ResponsesDecoderState()
    state.handle_event("response.reasoning_text.delta", {"delta": "reasoning"})
    # Now emit a text delta — should close reasoning first.
    events = state.handle_event("response.output_text.delta", {"delta": "answer"})
    types = [e.type for e in events]
    assert "reasoning.end" in types


# ---------------------------------------------------------------------------
# decode_responses (async generator)
# ---------------------------------------------------------------------------


def _make_responses_sse_bytes(
    response_id: str,
    text: str,
    reasoning: str | None = None,
) -> bytes:
    """Build a complete /v1/responses SSE stream as bytes for testing."""
    lines: list[str] = []

    def _event(event_type: str, data: dict) -> None:  # type: ignore[type-arg]
        lines.append(f"event: {event_type}")
        lines.append(f"data: {json.dumps(data)}")
        lines.append("")

    _event("response.created", {"id": response_id, "status": "in_progress"})
    _event("response.in_progress", {"id": response_id, "status": "in_progress"})

    if reasoning:
        _event("response.output_item.added", {"item": {"id": "rs_001", "type": "reasoning"}})
        _event("response.content_part.added", {"item_id": "rs_001"})
        _event("response.reasoning_text.delta", {"item_id": "rs_001", "delta": reasoning})
        _event("response.output_item.done", {"item": {"id": "rs_001", "type": "reasoning"}})

    item_id = "msg_001"
    _event("response.output_item.added", {"item": {"id": item_id, "type": "message"}})
    _event("response.content_part.added", {"item_id": item_id})
    _event("response.output_text.delta", {"item_id": item_id, "delta": text})
    _event("response.output_item.done", {"item": {"id": item_id, "type": "message"}})
    _event("response.completed", {"response": {"id": response_id}})

    return "\n".join(lines).encode()


def _mock_httpx_response(body_bytes: bytes, status_code: int = 200) -> AsyncMock:
    response = AsyncMock(spec=httpx.Response)
    response.status_code = status_code

    async def _aiter_bytes() -> AsyncIterator[bytes]:
        yield body_bytes

    response.aiter_bytes = _aiter_bytes
    response.aclose = AsyncMock()
    return response


@pytest.mark.asyncio()
async def test_decode_responses_simple_text() -> None:
    """Simple text response: correct canonical event sequence."""
    sse_bytes = _make_responses_sse_bytes("resp_001", "Hello world")
    stream = _mock_httpx_response(sse_bytes)

    events = [ev async for ev in decode_responses(stream)]
    types = [e.type for e in events]

    assert "chat.start" in types
    assert "message.start" in types
    assert "message.delta" in types
    assert "message.end" in types
    assert "chat.end" in types

    # response_id round-trips.
    start = next(e for e in events if e.type == "chat.start")
    assert start.response_id == "resp_001"
    end = next(e for e in events if e.type == "chat.end")
    assert end.response_id == "resp_001"


@pytest.mark.asyncio()
async def test_decode_responses_with_reasoning() -> None:
    """Reasoning blocks emit reasoning.start/delta/end before message events."""
    sse_bytes = _make_responses_sse_bytes("resp_002", "answer", reasoning="thinking step")
    stream = _mock_httpx_response(sse_bytes)

    events = [ev async for ev in decode_responses(stream)]
    types = [e.type for e in events]

    assert "reasoning.start" in types
    assert "reasoning.delta" in types
    assert "message.start" in types
    assert "message.delta" in types

    # reasoning.* must precede message.* (by index).
    r_start_idx = types.index("reasoning.start")
    m_start_idx = types.index("message.start")
    assert r_start_idx < m_start_idx


@pytest.mark.asyncio()
async def test_decode_responses_function_call() -> None:
    """function_call output item produces tool_call.start, .arguments, .success with call_id."""
    lines: list[str] = []

    def _event(event_type: str, data: dict) -> None:  # type: ignore[type-arg]
        lines.append(f"event: {event_type}")
        lines.append(f"data: {json.dumps(data)}")
        lines.append("")

    resp_id = "resp_fc"
    _event("response.created", {"id": resp_id})
    fc_id = "fc_001"
    _event(
        "response.output_item.added",
        {"item": {"id": fc_id, "type": "function_call", "name": "calculator"}},
    )
    _event("response.function_call_arguments.delta", {"item_id": fc_id, "delta": '{"expr":'})
    _event("response.function_call_arguments.delta", {"item_id": fc_id, "delta": ' "47*53"}'})
    _event("response.output_item.done", {
        "item": {
            "id": fc_id,
            "type": "function_call",
            "name": "calculator",
            "arguments": '{"expr": "47*53"}',
            "call_id": "call_round_trip",
        }
    })
    _event("response.completed", {"response": {"id": resp_id}})

    sse_bytes = "\n".join(lines).encode()
    stream = _mock_httpx_response(sse_bytes)

    events = [ev async for ev in decode_responses(stream)]
    types = [e.type for e in events]

    assert "tool_call.start" in types
    assert "tool_call.arguments" in types
    assert "tool_call.success" in types
    assert "chat.end" in types

    start_ev = next(e for e in events if e.type == "tool_call.start")
    assert start_ev.tool_call is not None
    assert start_ev.tool_call.call_id == "call_round_trip", (
        "call_id must be populated for the next-turn round-trip"
    )
    assert start_ev.tool_call.name == "calculator"

    args_ev = next(e for e in events if e.type == "tool_call.arguments")
    assert args_ev.tool_call is not None
    assert args_ev.tool_call.arguments == {"expr": "47*53"}


@pytest.mark.asyncio()
async def test_decode_responses_upstream_stream_error() -> None:
    """httpx.HTTPError mid-stream → canonical error event, generator terminates."""

    async def _aiter_bytes_raises() -> AsyncIterator[bytes]:
        yield b"event: response.created\ndata: {\"id\": \"resp_x\"}\n\n"
        raise httpx.ReadTimeout("timeout")

    stream = AsyncMock(spec=httpx.Response)
    stream.aiter_bytes = _aiter_bytes_raises

    events = [ev async for ev in decode_responses(stream)]

    error_events = [e for e in events if e.type == "error"]
    assert len(error_events) == 1
    assert error_events[0].error is not None
    assert error_events[0].error["code"] == "upstream_stream_error"


# ---------------------------------------------------------------------------
# call_id field on CanonicalToolCall
# ---------------------------------------------------------------------------


def test_canonical_tool_call_call_id_defaults_to_none() -> None:
    """CanonicalToolCall.call_id defaults to None for backwards compat."""
    tc = CanonicalToolCall(id="id1", name="tool", arguments={})
    assert tc.call_id is None


def test_canonical_tool_call_call_id_can_be_set() -> None:
    """CanonicalToolCall.call_id can be set for /v1/responses round-trip."""
    tc = CanonicalToolCall(id="id1", name="tool", arguments={}, call_id="call_xyz")
    assert tc.call_id == "call_xyz"


def test_canonical_tool_call_frozen_with_call_id() -> None:
    """CanonicalToolCall remains frozen (immutable) with call_id set."""
    from pydantic import ValidationError

    tc = CanonicalToolCall(id="id1", name="tool", arguments={}, call_id="call_x")
    # Pydantic frozen models raise ValidationError on direct attribute assignment.
    with pytest.raises((ValidationError, TypeError)):
        tc.call_id = "other"  # type: ignore[misc]
