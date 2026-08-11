# SPDX-License-Identifier: Apache-2.0
"""Tests for lmchat.lmstudio.compat — /v1/chat/completions encoder and SSE decoder.

Covers the LM Studio compat encoder and decoder.
Covers:
- assemble_compat_messages: system_prompt, history tool_calls, tool-role messages,
  image input blocks.
- encode_compat: tools array shape, reasoning/integrations dropped, max_output_tokens.
- decode_compat: content deltas → message events; reasoning_content transitions;
  tool_calls → tool_call.start + tool_call.arguments; finish_reason="tool_calls" →
  tool_call.success; finish_reason="stop" → message.end + chat.end; [DONE] cleanly.
"""
from __future__ import annotations

import json

import httpx
import pytest

from lmchat.lmstudio.compat import assemble_compat_messages, decode_compat, encode_compat
from lmchat.lmstudio.types import (
    CanonicalChatRequest,
    CanonicalEvent,
    CanonicalInputBlock,
    CanonicalMessage,
    CanonicalTool,
    CanonicalToolCall,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_compat_response(sse_body: str) -> httpx.Response:
    """Construct an httpx.Response with the given OpenAI-style SSE body."""
    return httpx.Response(
        status_code=200,
        headers={"content-type": "text/event-stream"},
        content=sse_body.encode("utf-8"),
    )


def _compat_delta_chunk(
    *,
    content: str | None = None,
    reasoning_content: str | None = None,
    tool_calls: list[dict] | None = None,  # type: ignore[type-arg]
    finish_reason: str | None = None,
) -> str:
    """Build a single OpenAI-style SSE data chunk."""
    delta: dict = {}  # type: ignore[type-arg]
    if content is not None:
        delta["content"] = content
    if reasoning_content is not None:
        delta["reasoning_content"] = reasoning_content
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    obj = {
        "choices": [
            {
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ]
    }
    return f"data: {json.dumps(obj)}\n\n"


async def _collect(response: httpx.Response) -> list[CanonicalEvent]:
    """Collect all events from decode_compat into a list."""
    return [e async for e in decode_compat(response)]


# ---------------------------------------------------------------------------
# assemble_compat_messages tests
# ---------------------------------------------------------------------------


class TestAssembleCompatMessages:
    def test_system_prompt_prepends(self) -> None:
        """system_prompt is prepended as role="system"."""
        req = CanonicalChatRequest(
            model="m",
            system_prompt="Be concise.",
            input=[CanonicalInputBlock(type="text", content="Hello")],
        )
        msgs = assemble_compat_messages(req, history=[])
        assert msgs[0] == {"role": "system", "content": "Be concise."}

    def test_no_system_prompt_no_system_message(self) -> None:
        """Absent system_prompt produces no system message."""
        req = CanonicalChatRequest(
            model="m",
            input=[CanonicalInputBlock(type="text", content="Hi")],
        )
        msgs = assemble_compat_messages(req, history=[])
        assert all(m["role"] != "system" for m in msgs)

    def test_history_appended(self) -> None:
        """History messages appear between system and current turn."""
        req = CanonicalChatRequest(
            model="m",
            system_prompt="sys",
            input=[CanonicalInputBlock(type="text", content="latest")],
        )
        history = [
            CanonicalMessage(role="user", content="old question"),
            CanonicalMessage(role="assistant", content="old answer"),
        ]
        msgs = assemble_compat_messages(req, history=history)
        assert msgs[0]["role"] == "system"
        assert msgs[1] == {"role": "user", "content": "old question"}
        assert msgs[2] == {"role": "assistant", "content": "old answer"}
        assert msgs[3] == {"role": "user", "content": "latest"}

    def test_assistant_tool_calls_produce_openai_shape(self) -> None:
        """Assistant message with tool_calls produces OpenAI-style tool_calls array."""
        tc = CanonicalToolCall(id="call_abc", name="calculator", arguments={"expr": "2+2"})
        history = [CanonicalMessage(role="assistant", content=None, tool_calls=[tc])]
        req = CanonicalChatRequest(
            model="m",
            input=[CanonicalInputBlock(type="text", content="hi")],
        )
        msgs = assemble_compat_messages(req, history=history)
        asst = msgs[0]
        assert asst["role"] == "assistant"
        assert "tool_calls" in asst
        tc_wire = asst["tool_calls"][0]
        assert tc_wire["id"] == "call_abc"
        assert tc_wire["type"] == "function"
        assert tc_wire["function"]["name"] == "calculator"
        # arguments must be a JSON-encoded string per the harness
        parsed = json.loads(tc_wire["function"]["arguments"])
        assert parsed == {"expr": "2+2"}

    def test_tool_role_message_includes_tool_call_id(self) -> None:
        """tool-role history message includes tool_call_id."""
        history = [
            CanonicalMessage(role="tool", content="42", tool_call_id="call_abc"),
        ]
        req = CanonicalChatRequest(
            model="m",
            input=[CanonicalInputBlock(type="text", content="x")],
        )
        msgs = assemble_compat_messages(req, history=history)
        tool_msg = msgs[0]
        assert tool_msg["role"] == "tool"
        assert tool_msg["content"] == "42"
        assert tool_msg["tool_call_id"] == "call_abc"

    def test_image_block_produces_multipart_content(self) -> None:
        """Image input block produces multipart content array per OpenAI spec."""
        req = CanonicalChatRequest(
            model="m",
            input=[
                CanonicalInputBlock(type="text", content="describe this"),
                CanonicalInputBlock(type="image", data_url="data:image/png;base64,abc"),
            ],
        )
        msgs = assemble_compat_messages(req, history=[])
        user_msg = msgs[-1]
        assert isinstance(user_msg["content"], list)
        assert user_msg["content"][0] == {"type": "text", "text": "describe this"}
        assert user_msg["content"][1] == {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,abc"},
        }

    def test_text_only_produces_string_content(self) -> None:
        """Text-only input produces a single string (not a list)."""
        req = CanonicalChatRequest(
            model="m",
            input=[
                CanonicalInputBlock(type="text", content="Hello "),
                CanonicalInputBlock(type="text", content="world"),
            ],
        )
        msgs = assemble_compat_messages(req, history=[])
        user_msg = msgs[-1]
        assert user_msg["content"] == "Hello world"

    def test_current_turn_is_last_user_message(self) -> None:
        """The current turn from req.input is the last message with role=user."""
        req = CanonicalChatRequest(
            model="m",
            input=[CanonicalInputBlock(type="text", content="current turn")],
        )
        history = [CanonicalMessage(role="user", content="prior")]
        msgs = assemble_compat_messages(req, history=history)
        assert msgs[-1]["role"] == "user"
        assert msgs[-1]["content"] == "current turn"


# ---------------------------------------------------------------------------
# encode_compat tests
# ---------------------------------------------------------------------------


class TestEncodeCompat:
    def test_minimal_body(self) -> None:
        """Minimal request produces model + messages + stream."""
        req = CanonicalChatRequest(
            model="qwen3-8b",
            input=[CanonicalInputBlock(type="text", content="hi")],
        )
        body = encode_compat(req, history=[])
        assert body["model"] == "qwen3-8b"
        assert body["stream"] is True
        assert isinstance(body["messages"], list)
        assert body["messages"][-1]["content"] == "hi"

    def test_tools_array_shape(self) -> None:
        """tools array uses OpenAI function-call envelope."""
        tool = CanonicalTool(
            name="calc",
            description="Evaluate an expression.",
            parameters={"type": "object", "properties": {"expr": {"type": "string"}}},
        )
        req = CanonicalChatRequest(
            model="m",
            input=[CanonicalInputBlock(type="text", content="hi")],
            tools=[tool],
        )
        body = encode_compat(req, history=[])
        assert "tools" in body
        wire_tool = body["tools"][0]
        assert wire_tool["type"] == "function"
        assert wire_tool["function"]["name"] == "calc"
        assert wire_tool["function"]["description"] == "Evaluate an expression."

    def test_reasoning_dropped(self) -> None:
        """reasoning is native-only; must NOT appear in compat body."""
        req = CanonicalChatRequest(
            model="m",
            input=[CanonicalInputBlock(type="text", content="hi")],
            reasoning="high",
        )
        body = encode_compat(req, history=[])
        assert "reasoning" not in body

    def test_integrations_dropped(self) -> None:
        """integrations is native-only; must NOT appear in compat body."""
        req = CanonicalChatRequest(
            model="m",
            input=[CanonicalInputBlock(type="text", content="hi")],
            integrations=["mcp/searxng"],
        )
        body = encode_compat(req, history=[])
        assert "integrations" not in body

    def test_max_output_tokens_passes_through(self) -> None:
        """max_output_tokens is included in compat body."""
        req = CanonicalChatRequest(
            model="m",
            input=[CanonicalInputBlock(type="text", content="hi")],
            max_output_tokens=1024,
        )
        body = encode_compat(req, history=[])
        assert body["max_output_tokens"] == 1024

    def test_compat_scalar_params(self) -> None:
        """temperature, top_p, top_k, min_p, repeat_penalty, max_tokens, store pass through."""
        req = CanonicalChatRequest(
            model="m",
            input=[CanonicalInputBlock(type="text", content="hi")],
            temperature=0.5,
            top_p=0.8,
            top_k=50,
            min_p=0.1,
            repeat_penalty=1.2,
            max_tokens=256,
            store=True,
        )
        body = encode_compat(req, history=[])
        assert body["temperature"] == pytest.approx(0.5)
        assert body["top_p"] == pytest.approx(0.8)
        assert body["top_k"] == 50
        assert body["min_p"] == pytest.approx(0.1)
        assert body["repeat_penalty"] == pytest.approx(1.2)
        assert body["max_tokens"] == 256
        assert body["store"] is True

    def test_no_tools_no_tools_key(self) -> None:
        """Empty tools list means 'tools' key is absent from body."""
        req = CanonicalChatRequest(
            model="m",
            input=[CanonicalInputBlock(type="text", content="hi")],
        )
        body = encode_compat(req, history=[])
        assert "tools" not in body


# ---------------------------------------------------------------------------
# decode_compat tests
# ---------------------------------------------------------------------------


class TestDecodeCompat:
    @pytest.mark.asyncio
    async def test_content_deltas_produce_start_delta_end(self) -> None:
        """First content chunk → message.start + message.delta; stop → message.end + chat.end."""
        sse = (
            _compat_delta_chunk(content="Hello")
            + _compat_delta_chunk(content=" world")
            + _compat_delta_chunk(finish_reason="stop")
            + "data: [DONE]\n\n"
        )
        events = await _collect(_make_compat_response(sse))
        types = [e.type for e in events]
        assert types == [
            "message.start",
            "message.delta",
            "message.delta",
            "message.end",
            "chat.end",
        ]
        assert events[1].content == "Hello"
        assert events[2].content == " world"

    @pytest.mark.asyncio
    async def test_done_terminates_cleanly(self) -> None:
        """[DONE] terminates the stream without error."""
        sse = (
            _compat_delta_chunk(content="ok")
            + _compat_delta_chunk(finish_reason="stop")
            + "data: [DONE]\n\n"
        )
        events = await _collect(_make_compat_response(sse))
        assert "chat.end" in [e.type for e in events]

    @pytest.mark.asyncio
    async def test_reasoning_content_sequence(self) -> None:
        """reasoning_content chunks → reasoning.start / delta / end transitions."""
        sse = (
            _compat_delta_chunk(reasoning_content="<think>step 1</think>")
            + _compat_delta_chunk(reasoning_content="<think>step 2</think>")
            + _compat_delta_chunk(content="Answer: 42")
            + _compat_delta_chunk(finish_reason="stop")
            + "data: [DONE]\n\n"
        )
        events = await _collect(_make_compat_response(sse))
        types = [e.type for e in events]
        assert "reasoning.start" in types
        assert "reasoning.delta" in types
        assert "reasoning.end" in types
        # reasoning.end should come before message.start
        ri_end = types.index("reasoning.end")
        msg_start = types.index("message.start")
        assert ri_end < msg_start

    @pytest.mark.asyncio
    async def test_tool_call_name_produces_start(self) -> None:
        """First tool_calls[].function.name delta → tool_call.start."""
        sse = (
            _compat_delta_chunk(
                tool_calls=[{
                    "index": 0,
                    "id": "call_1",
                    "function": {"name": "calculator", "arguments": ""},
                }]
            )
            + _compat_delta_chunk(
                tool_calls=[{
                    "index": 0,
                    "function": {"arguments": '{"expr":'},
                }]
            )
            + _compat_delta_chunk(
                tool_calls=[{
                    "index": 0,
                    "function": {"arguments": '"2+2"}'},
                }]
            )
            + _compat_delta_chunk(finish_reason="tool_calls")
            + "data: [DONE]\n\n"
        )
        events = await _collect(_make_compat_response(sse))
        types = [e.type for e in events]
        assert "tool_call.start" in types
        start_evt = next(e for e in events if e.type == "tool_call.start")
        assert start_evt.tool_call is not None
        assert start_evt.tool_call.name == "calculator"

    @pytest.mark.asyncio
    async def test_tool_call_arguments_emitted_once_parseable(self) -> None:
        """Cumulative arguments → tool_call.arguments once JSON-parseable."""
        sse = (
            _compat_delta_chunk(
                tool_calls=[{
                    "index": 0,
                    "id": "call_1",
                    "function": {"name": "calc", "arguments": ""},
                }]
            )
            + _compat_delta_chunk(
                tool_calls=[{"index": 0, "function": {"arguments": '{"expr":'}}]
            )
            + _compat_delta_chunk(
                tool_calls=[{"index": 0, "function": {"arguments": '"1+1"}'}}]
            )
            + _compat_delta_chunk(finish_reason="tool_calls")
            + "data: [DONE]\n\n"
        )
        events = await _collect(_make_compat_response(sse))
        args_evts = [e for e in events if e.type == "tool_call.arguments"]
        assert len(args_evts) == 1
        assert args_evts[0].tool_call is not None
        assert args_evts[0].tool_call.arguments == {"expr": "1+1"}

    @pytest.mark.asyncio
    async def test_finish_reason_tool_calls_produces_success(self) -> None:
        """finish_reason='tool_calls' → tool_call.success (no tool_call.end)."""
        sse = (
            _compat_delta_chunk(
                tool_calls=[{
                    "index": 0,
                    "id": "call_1",
                    "function": {"name": "fn", "arguments": "{}"},
                }]
            )
            + _compat_delta_chunk(finish_reason="tool_calls")
            + "data: [DONE]\n\n"
        )
        events = await _collect(_make_compat_response(sse))
        types = [e.type for e in events]
        assert "tool_call.success" in types
        assert "tool_call.end" not in types  # must not exist

    @pytest.mark.asyncio
    async def test_finish_reason_length_produces_chat_end(self) -> None:
        """finish_reason='length' also produces message.end + chat.end."""
        sse = (
            _compat_delta_chunk(content="truncated")
            + _compat_delta_chunk(finish_reason="length")
            + "data: [DONE]\n\n"
        )
        events = await _collect(_make_compat_response(sse))
        types = [e.type for e in events]
        assert "message.end" in types
        assert "chat.end" in types

    @pytest.mark.asyncio
    async def test_finish_reason_length_sets_stop_reason(self) -> None:
        """Continue-chip closeout: chat.end carries stop_reason='length' on truncation."""
        sse = (
            _compat_delta_chunk(content="truncated")
            + _compat_delta_chunk(finish_reason="length")
            + "data: [DONE]\n\n"
        )
        events = await _collect(_make_compat_response(sse))
        chat_end = next(e for e in events if e.type == "chat.end")
        assert chat_end.stop_reason == "length"

    @pytest.mark.asyncio
    async def test_finish_reason_stop_sets_stop_reason(self) -> None:
        """Continue-chip closeout: natural completion carries stop_reason='stop'."""
        sse = (
            _compat_delta_chunk(content="done")
            + _compat_delta_chunk(finish_reason="stop")
            + "data: [DONE]\n\n"
        )
        events = await _collect(_make_compat_response(sse))
        chat_end = next(e for e in events if e.type == "chat.end")
        assert chat_end.stop_reason == "stop"
        # Non-terminal events never carry a stop_reason.
        assert all(
            e.stop_reason is None for e in events if e.type != "chat.end"
        )

    @pytest.mark.asyncio
    async def test_repeated_finish_reason_emits_chat_end_once(self) -> None:
        """Regression (live OpenRouter 2026-06-18): a provider that echoes a
        non-null finish_reason across two SSE chunks must still yield exactly
        one terminal chat.end (and one message.end). Observed live sequence:
        message.start, delta, delta, message.end, chat.end, chat.end.
        """
        sse = (
            _compat_delta_chunk(content="done")
            + _compat_delta_chunk(finish_reason="stop")
            + _compat_delta_chunk(finish_reason="stop")  # provider repeats it
            + "data: [DONE]\n\n"
        )
        events = await _collect(_make_compat_response(sse))
        types = [e.type for e in events]
        assert types.count("chat.end") == 1, types
        assert types.count("message.end") == 1, types
        # P2 fix: [DONE] without a preceding finish_reason now synthesises
        # chat.end so the draft finalises. The block below confirms the
        # synthesised terminal events are emitted exactly once.
        sse = (
            _compat_delta_chunk(content="part1")
            + _compat_delta_chunk(content="part2")
            + "data: [DONE]\n\n"
        )
        events = await _collect(_make_compat_response(sse))
        types = [e.type for e in events]
        assert types.count("message.delta") == 2
        # P2 fix: message.end and chat.end ARE now synthesised on [DONE].
        assert types.count("message.end") == 1, (
            f"P2: expected synthesised message.end on [DONE]-only stream; got {types}"
        )
        assert types.count("chat.end") == 1, (
            f"P2: expected synthesised chat.end on [DONE]-only stream; got {types}"
        )

    @pytest.mark.asyncio
    async def test_malformed_json_chunk_skipped(self) -> None:
        """Malformed JSON data value is skipped without raising."""
        sse = (
            _compat_delta_chunk(content="ok")
            + "data: {not valid json}\n\n"
            + _compat_delta_chunk(finish_reason="stop")
            + "data: [DONE]\n\n"
        )
        events = await _collect(_make_compat_response(sse))
        # Should still get the good events.
        types = [e.type for e in events]
        assert "message.start" in types
        assert "chat.end" in types

    @pytest.mark.asyncio
    async def test_empty_choices_skipped(self) -> None:
        """Chunks with empty choices list are skipped (no content/delta events).

        P2 fix: [DONE] now synthesises a terminal chat.end even when the only
        preceding chunks had empty choices — the provider sent nothing useful
        but the draft must still finalise.
        """
        obj = {"choices": []}
        sse = f"data: {json.dumps(obj)}\n\n" + "data: [DONE]\n\n"
        events = await _collect(_make_compat_response(sse))
        types = [e.type for e in events]
        # No content events from the empty-choices chunk.
        assert "message.start" not in types
        assert "message.delta" not in types
        # P2 fix: one synthesised chat.end so the draft finalises.
        assert types.count("chat.end") == 1, (
            f"Expected synthesised chat.end after empty-choices + [DONE]; got {types}"
        )

    @pytest.mark.asyncio
    async def test_reasoning_end_emitted_on_finish_stop(self) -> None:
        """If still in reasoning at finish_reason=stop, reasoning.end is emitted."""
        sse = (
            _compat_delta_chunk(reasoning_content="<think>thinking</think>")
            + _compat_delta_chunk(finish_reason="stop")
            + "data: [DONE]\n\n"
        )
        events = await _collect(_make_compat_response(sse))
        types = [e.type for e in events]
        assert "reasoning.end" in types
        assert "chat.end" in types


# ---------------------------------------------------------------------------
# decode_compat — mid-stream error handling
# ---------------------------------------------------------------------------


class _RaisingCompatStream:
    """Stub httpx.Response whose aiter_bytes raises mid-stream.

    Yields one valid OpenAI-style delta chunk, then raises an httpx.HTTPError.
    Verifies the decoder catches the error and emits a canonical error event
    before terminating cleanly.
    """

    def __init__(self, *, first_chunk: bytes, exc: BaseException) -> None:
        self._first_chunk = first_chunk
        self._exc = exc

    async def aiter_bytes(self):  # noqa: ANN201
        yield self._first_chunk
        raise self._exc


class TestDecodeCompatMidStreamError:
    """Compat decoder stream-error → canonical error event terminator."""

    @pytest.mark.asyncio
    async def test_connect_error_mid_stream_yields_error_event(self) -> None:
        """ConnectError raised by aiter_bytes mid-stream surfaces as
        a canonical error event."""
        # A minimal OpenAI-style delta with content.
        first_chunk = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        stub = _RaisingCompatStream(
            first_chunk=first_chunk,
            exc=httpx.ConnectError("connection reset"),
        )

        events: list = []
        async for ev in decode_compat(stub):  # type: ignore[arg-type]
            events.append(ev)

        # First chunk produced message.start + message.delta.
        types = [e.type for e in events]
        assert "message.start" in types
        # Last event is a canonical error.
        assert events[-1].type == "error"
        assert events[-1].error is not None
        assert events[-1].error.get("code") == "upstream_stream_error"
        assert "ConnectError" in events[-1].error.get("message", "")

    @pytest.mark.asyncio
    async def test_read_timeout_mid_stream_yields_error_event(self) -> None:
        """ReadTimeout raised by aiter_bytes mid-stream surfaces as
        a canonical error event."""
        first_chunk = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        stub = _RaisingCompatStream(
            first_chunk=first_chunk,
            exc=httpx.ReadTimeout("read timed out"),
        )

        events: list = []
        async for ev in decode_compat(stub):  # type: ignore[arg-type]
            events.append(ev)

        assert events[-1].type == "error"
        assert events[-1].error is not None
        assert events[-1].error.get("code") == "upstream_stream_error"


# ---------------------------------------------------------------------------
# decode_compat — [DONE] without finish_reason must still emit chat.end (P2)
# ---------------------------------------------------------------------------


class TestDecodeCompatDoneWithoutFinishReason:
    """P2 fix: [DONE] with no preceding finish_reason chunk must synthesise chat.end."""

    @pytest.mark.asyncio
    async def test_done_without_finish_reason_emits_exactly_one_chat_end(self) -> None:
        """SSE stream: content deltas + [DONE] with NO finish_reason chunk.

        Before the fix, the stream exhausted without chat.end and the draft
        was stuck. After the fix, exactly one chat.end is synthesised.
        """
        sse = (
            _compat_delta_chunk(content="Hello")
            + _compat_delta_chunk(content=" world")
            + "data: [DONE]\n\n"  # no finish_reason chunk at all
        )
        events = await _collect(_make_compat_response(sse))
        types = [e.type for e in events]
        # Must contain exactly one chat.end.
        assert types.count("chat.end") == 1, (
            f"Expected exactly 1 chat.end; got types={types}"
        )

    @pytest.mark.asyncio
    async def test_done_without_finish_reason_does_not_double_emit_chat_end(self) -> None:
        """When a finish_reason=stop chunk already fired, [DONE] must not double-emit."""
        sse = (
            _compat_delta_chunk(content="done")
            + _compat_delta_chunk(finish_reason="stop")
            + "data: [DONE]\n\n"
        )
        events = await _collect(_make_compat_response(sse))
        types = [e.type for e in events]
        assert types.count("chat.end") == 1, (
            f"Expected exactly 1 chat.end (no double-emit); got types={types}"
        )

    @pytest.mark.asyncio
    async def test_loop_exhausted_without_done_emits_chat_end(self) -> None:
        """Stream ends without [DONE] and without finish_reason — chat.end still fires."""
        sse = (
            _compat_delta_chunk(content="abrupt end")
            # no finish_reason chunk, no [DONE]
        )
        events = await _collect(_make_compat_response(sse))
        types = [e.type for e in events]
        assert types.count("chat.end") == 1, (
            f"Expected 1 chat.end after loop exhaustion; got types={types}"
        )
