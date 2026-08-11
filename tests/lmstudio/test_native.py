# SPDX-License-Identifier: Apache-2.0
"""Tests for lmchat.lmstudio.native — /api/v1/chat encoder and SSE decoder.

LM Studio native adapter round-trip tests.
Covers:
- encode_native: minimal body, system_prompt top-level, tools+integrations
  collision warning, previous_response_id, reasoning, image input block.
- decode_native: fixture SSE stream → canonical events in order; tool_call.name
  payload mapping; error event; unknown event skipped.

SSE mock strategy: httpx.Response with a bytes body containing the full SSE
stream. decode_native uses aiter_bytes() which yields the body in chunks.
We use a sync-friendly AsyncByteStream wrapper to provide the content.
"""
from __future__ import annotations

import logging
import textwrap
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest

from lmchat.lmstudio.native import decode_native, encode_native
from lmchat.lmstudio.types import (
    CanonicalChatRequest,
    CanonicalEvent,
    CanonicalInputBlock,
    CanonicalTool,
)

# ---------------------------------------------------------------------------
# Logging fixture — routes structlog through stdlib so caplog can intercept.
# Without configure_logging(), structlog uses its built-in renderer that
# writes directly to stdout, which caplog cannot capture.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _route_structlog_for_caplog() -> Iterator[None]:
    """Configure structlog through stdlib logging for caplog capture.

    Mirrors the pattern established in tests/services/test_single_session_warning.py:
    call configure_logging(console=False, log_file=None) so structlog emits
    stdlib LogRecord objects that pytest's caplog fixture can intercept.
    The conftest reset_logging autouse fixture cleans up after each test.
    """
    from lmchat.logging import configure_logging

    configure_logging(console=False, log_file=None, level="DEBUG")
    yield

# ---------------------------------------------------------------------------
# Helpers for mocking httpx streaming responses
# ---------------------------------------------------------------------------


def _make_streaming_response(sse_body: str) -> httpx.Response:
    """Construct an httpx.Response whose body is the given SSE text.

    The response uses the default httpx transport (no real network). We set
    the body as bytes so aiter_bytes() yields it.
    """
    body_bytes = sse_body.encode("utf-8")
    return httpx.Response(
        status_code=200,
        headers={"content-type": "text/event-stream"},
        content=body_bytes,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _minimal_req() -> CanonicalChatRequest:
    """Minimal valid request: model + one text input block."""
    return CanonicalChatRequest(
        model="qwen3-8b",
        input=[CanonicalInputBlock(type="text", content="hi")],
    )


# ---------------------------------------------------------------------------
# encode_native tests
# ---------------------------------------------------------------------------


class TestEncodeNative:
    def test_minimal_body(self) -> None:
        """Minimal request produces model + input + stream keys."""
        body = encode_native(_minimal_req())
        assert body["model"] == "qwen3-8b"
        assert body["stream"] is True
        assert isinstance(body["input"], list)
        assert len(body["input"]) == 1
        assert body["input"][0] == {"type": "text", "content": "hi"}

    def test_minimal_body_no_extras(self) -> None:
        """Minimal request omits optional keys."""
        body = encode_native(_minimal_req())
        assert "system_prompt" not in body
        assert "previous_response_id" not in body
        assert "integrations" not in body
        assert "tools" not in body

    def test_system_prompt_is_top_level_string(self) -> None:
        """system_prompt is a top-level string, NOT a message block in input."""
        req = CanonicalChatRequest(
            model="m",
            input=[CanonicalInputBlock(type="text", content="hi")],
            system_prompt="You are helpful.",
        )
        body = encode_native(req)
        assert body["system_prompt"] == "You are helpful."
        # The input array must NOT contain a system-role message.
        for block in body["input"]:
            assert block.get("role") != "system"

    def test_input_blocks_have_no_role(self) -> None:
        """Native input blocks must not carry a role field."""
        req = CanonicalChatRequest(
            model="m",
            input=[CanonicalInputBlock(type="text", content="hello")],
        )
        body = encode_native(req)
        for block in body["input"]:
            assert "role" not in block

    def test_previous_response_id_threads_through(self) -> None:
        """previous_response_id is passed as a top-level field."""
        req = CanonicalChatRequest(
            model="m",
            input=[CanonicalInputBlock(type="text", content="x")],
            previous_response_id="resp_abc123",
        )
        body = encode_native(req)
        assert body["previous_response_id"] == "resp_abc123"

    def test_previous_response_id_suppresses_system_prompt(self) -> None:
        """2026-06-12 strict-template fix: when ``previous_response_id`` is
        set, ``system_prompt`` MUST be skipped — LM Studio's server-side
        state already has the prompt from turn 1, and strict Jinja
        templates (qwen3.6-35b observed live, line 85 raise) 400 with
        "Unable to generate parser for this template" when a second
        system message is appended on top of the rehydrated state.
        """
        req = CanonicalChatRequest(
            model="m",
            input=[CanonicalInputBlock(type="text", content="x")],
            system_prompt="You are helpful.",
            previous_response_id="resp_abc123",
        )
        body = encode_native(req)
        # The chain marker wins; the prompt is left out so LM Studio's
        # rehydrated state isn't shadowed.
        assert body["previous_response_id"] == "resp_abc123"
        assert "system_prompt" not in body, (
            "system_prompt MUST be skipped when previous_response_id is "
            "set or strict templates 400 — see streaming_service / "
            "native.py 2026-06-12 fix"
        )

    def test_first_turn_no_previous_response_id_keeps_system_prompt(
        self,
    ) -> None:
        """The first turn of a chain has no ``previous_response_id``;
        ``system_prompt`` MUST be sent so the model sees it. Pairs with
        the test above.
        """
        req = CanonicalChatRequest(
            model="m",
            input=[CanonicalInputBlock(type="text", content="x")],
            system_prompt="You are helpful.",
            # no previous_response_id — first turn of a new chain
        )
        body = encode_native(req)
        assert body["system_prompt"] == "You are helpful."
        assert "previous_response_id" not in body

    def test_reasoning_threads_through(self) -> None:
        """reasoning param is included when set."""
        req = CanonicalChatRequest(
            model="m",
            input=[CanonicalInputBlock(type="text", content="x")],
            reasoning="high",
        )
        body = encode_native(req)
        assert body["reasoning"] == "high"

    def test_temperature_and_top_p(self) -> None:
        """temperature and top_p pass through."""
        req = CanonicalChatRequest(
            model="m",
            input=[CanonicalInputBlock(type="text", content="x")],
            temperature=0.7,
            top_p=0.9,
        )
        body = encode_native(req)
        assert body["temperature"] == pytest.approx(0.7)
        assert body["top_p"] == pytest.approx(0.9)

    def test_integrations_only_no_tools_dropped_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """integrations alone: body includes integrations, no warning."""
        req = CanonicalChatRequest(
            model="m",
            input=[CanonicalInputBlock(type="text", content="x")],
            integrations=["mcp/searxng"],
        )
        with caplog.at_level(logging.WARNING, logger="lmchat.lmstudio.native"):
            body = encode_native(req)
        assert body["integrations"] == ["mcp/searxng"]
        assert "tools_dropped" not in caplog.text

    def test_integrations_and_tools_drops_tools_and_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """integrations + tools: tools are stripped and a WARNING is logged."""
        tool = CanonicalTool(name="my_fn", description="d", parameters={})
        req = CanonicalChatRequest(
            model="m",
            input=[CanonicalInputBlock(type="text", content="x")],
            integrations=["mcp/searxng"],
            tools=[tool],
        )
        with caplog.at_level(logging.WARNING, logger="lmchat.lmstudio.native"):
            body = encode_native(req)

        # tools must NOT appear in the outbound body
        assert "tools" not in body
        # integrations must be present
        assert body["integrations"] == ["mcp/searxng"]
        # structured-log warning must have fired
        assert "adapter.tools_dropped_for_integrations" in caplog.text

    def test_image_block_produces_correct_shape(self) -> None:
        """Image input block produces {"type":"image","data_url":"..."}."""
        req = CanonicalChatRequest(
            model="m",
            input=[CanonicalInputBlock(type="image", data_url="data:image/png;base64,abc")],
        )
        body = encode_native(req)
        assert body["input"] == [{"type": "image", "data_url": "data:image/png;base64,abc"}]

    def test_stream_false_passes_through(self) -> None:
        """stream=False is honoured."""
        req = CanonicalChatRequest(
            model="m",
            input=[CanonicalInputBlock(type="text", content="x")],
            stream=False,
        )
        body = encode_native(req)
        assert body["stream"] is False

    def test_none_params_not_included(self) -> None:
        """Generation params that are None are omitted from body."""
        body = encode_native(_minimal_req())
        for param in ("temperature", "top_p", "max_tokens", "reasoning"):
            assert param not in body


# ---------------------------------------------------------------------------
# decode_native tests
# ---------------------------------------------------------------------------

# A minimal chat turn SSE fixture (no tool calls, no reasoning).
_SIMPLE_CHAT_SSE = textwrap.dedent("""\
    event: chat.start
    data: {"type":"chat.start","model_instance_id":"inst_1","response_id":"resp_1"}

    event: prompt_processing.start
    data: {"type":"prompt_processing.start"}

    event: prompt_processing.progress
    data: {"type":"prompt_processing.progress","progress":0.5}

    event: prompt_processing.end
    data: {"type":"prompt_processing.end"}

    event: message.start
    data: {"type":"message.start"}

    event: message.delta
    data: {"type":"message.delta","content":"Hello"}

    event: message.delta
    data: {"type":"message.delta","content":" world"}

    event: message.delta
    data: {"type":"message.delta","content":"!"}

    event: message.end
    data: {"type":"message.end"}

    event: chat.end
    data: {"type":"chat.end","response_id":"resp_1"}

""")


async def _collect(response: httpx.Response) -> list[CanonicalEvent]:
    """Collect all events from decode_native into a list."""
    return [e async for e in decode_native(response)]


class TestDecodeNative:
    @pytest.mark.asyncio
    async def test_simple_chat_stream_order_and_types(self) -> None:
        """Fixture SSE stream produces canonical events in correct order."""
        resp = _make_streaming_response(_SIMPLE_CHAT_SSE)
        events = await _collect(resp)

        types = [e.type for e in events]
        assert types == [
            "chat.start",
            "prompt_processing.start",
            "prompt_processing.progress",
            "prompt_processing.end",
            "message.start",
            "message.delta",
            "message.delta",
            "message.delta",
            "message.end",
            "chat.end",
        ]

    @pytest.mark.asyncio
    async def test_chat_start_fields(self) -> None:
        """chat.start event carries model_instance_id and response_id."""
        resp = _make_streaming_response(_SIMPLE_CHAT_SSE)
        events = await _collect(resp)
        start = events[0]
        assert start.type == "chat.start"
        assert start.model_instance_id == "inst_1"
        assert start.response_id == "resp_1"

    @pytest.mark.asyncio
    async def test_message_delta_content(self) -> None:
        """message.delta events carry content."""
        resp = _make_streaming_response(_SIMPLE_CHAT_SSE)
        events = await _collect(resp)
        deltas = [e for e in events if e.type == "message.delta"]
        assert [e.content for e in deltas] == ["Hello", " world", "!"]

    @pytest.mark.asyncio
    async def test_chat_end_response_id(self) -> None:
        """chat.end carries response_id."""
        resp = _make_streaming_response(_SIMPLE_CHAT_SSE)
        events = await _collect(resp)
        end = events[-1]
        assert end.type == "chat.end"
        assert end.response_id == "resp_1"

    @pytest.mark.asyncio
    async def test_chat_end_response_id_nested_in_result(self) -> None:
        """LM Studio's live shape nests response_id in `result.response_id`.

        2026-06-02 wire probe against qwen3-vl-8b-instruct's streaming
        endpoint shows the chat.end event nests response_id inside
        ``result``. Without this decode every turn lost
        previous_response_id chaining, which produced the small-model
        context-loss bug (turn 3 answered "Do any have 3" with primary
        colors). The decoder must accept the nested shape.
        """
        sse = (
            'event: chat.end\n'
            'data: {"type":"chat.end","result":{"output":[],'
            '"response_id":"resp_nested_42"}}\n'
            '\n'
        )
        resp = _make_streaming_response(sse)
        events = await _collect(resp)
        end = events[-1]
        assert end.type == "chat.end"
        assert end.response_id == "resp_nested_42"

    @pytest.mark.asyncio
    async def test_prompt_processing_progress_value(self) -> None:
        """prompt_processing.progress carries the progress float."""
        resp = _make_streaming_response(_SIMPLE_CHAT_SSE)
        events = await _collect(resp)
        prog = next(e for e in events if e.type == "prompt_processing.progress")
        assert prog.progress == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_tool_call_name_maps_tool_name_to_canonical_name(self) -> None:
        """tool_call.name wire `tool_name` maps to CanonicalToolCall.name."""
        # Verbatim probe excerpt from docs/LM_STUDIO_HARNESS.md §"Event types".
        # The provider_info payload is split across a variable to satisfy line-length.
        _provider = '{"type":"plugin","plugin_id":"mcp/searxng"}'
        _name_data = (
            '{"type":"tool_call.name","tool_name":"search_web",'
            f'"provider_info":{_provider}}}'
        )
        sse = (
            "event: tool_call.start\n"
            'data: {"type":"tool_call.start"}\n\n'
            "event: tool_call.name\n"
            f"data: {_name_data}\n\n"
            "event: tool_call.success\n"
            'data: {"type":"tool_call.success","tool":"search_web",'
            '"arguments":{},"output":"result"}\n\n'
        )
        resp = _make_streaming_response(sse)
        events = await _collect(resp)

        tc_name_evt = next(e for e in events if e.type == "tool_call.name")
        assert tc_name_evt.tool_call is not None
        assert tc_name_evt.tool_call.name == "search_web"

    @pytest.mark.asyncio
    async def test_error_event_surfaces_error_dict(self) -> None:
        """error event carries error dict."""
        sse = textwrap.dedent("""\
            event: error
            data: {"error":{"code":"rate_limited","message":"slow down"}}

        """)
        resp = _make_streaming_response(sse)
        events = await _collect(resp)
        assert len(events) == 1
        assert events[0].type == "error"
        assert events[0].error is not None
        assert events[0].error["code"] == "rate_limited"

    @pytest.mark.asyncio
    async def test_unknown_event_is_skipped(self) -> None:
        """Unknown future event types are skipped without raising."""
        sse = textwrap.dedent("""\
            event: chat.start
            data: {"type":"chat.start","model_instance_id":"inst_1"}

            event: future.event.from.lm_studio_v99
            data: {"type":"future.event.from.lm_studio_v99","payload":"x"}

            event: message.start
            data: {"type":"message.start"}

        """)
        resp = _make_streaming_response(sse)
        events = await _collect(resp)
        types = [e.type for e in events]
        assert "future.event.from.lm_studio_v99" not in types
        assert "chat.start" in types
        assert "message.start" in types

    @pytest.mark.asyncio
    async def test_reasoning_delta_events(self) -> None:
        """reasoning.delta events carry content."""
        sse = textwrap.dedent("""\
            event: reasoning.start
            data: {"type":"reasoning.start"}

            event: reasoning.delta
            data: {"type":"reasoning.delta","content":"<think>step 1</think>"}

            event: reasoning.end
            data: {"type":"reasoning.end"}

        """)
        resp = _make_streaming_response(sse)
        events = await _collect(resp)
        assert [e.type for e in events] == [
            "reasoning.start", "reasoning.delta", "reasoning.end"
        ]
        delta = events[1]
        assert delta.content == "<think>step 1</think>"

    @pytest.mark.asyncio
    async def test_model_load_events(self) -> None:
        """model_load.* events are decoded (cold-start scenario)."""
        sse = textwrap.dedent("""\
            event: model_load.start
            data: {"type":"model_load.start"}

            event: model_load.progress
            data: {"type":"model_load.progress","progress":0.75}

            event: model_load.end
            data: {"type":"model_load.end"}

        """)
        resp = _make_streaming_response(sse)
        events = await _collect(resp)
        assert [e.type for e in events] == [
            "model_load.start", "model_load.progress", "model_load.end"
        ]
        prog = events[1]
        assert prog.progress == pytest.approx(0.75)

    @pytest.mark.asyncio
    async def test_malformed_json_in_data_skipped(self) -> None:
        """Malformed JSON in data field is skipped without raising."""
        sse = textwrap.dedent("""\
            event: message.start
            data: {"type":"message.start"}

            event: message.delta
            data: {not valid json}

            event: message.end
            data: {"type":"message.end"}

        """)
        resp = _make_streaming_response(sse)
        events = await _collect(resp)
        # The bad-JSON delta is skipped; start and end survive.
        types = [e.type for e in events]
        assert "message.start" in types
        assert "message.end" in types
        # message.delta should not appear (the only delta had bad JSON)
        assert "message.delta" not in types


# ---------------------------------------------------------------------------
# decode_native — mid-stream error handling
# ---------------------------------------------------------------------------


class _RaisingStream:
    """Stub httpx.Response whose aiter_lines raises mid-stream.

    Yields lines from a valid SSE block so the decoder gets to the inside of
    the loop, then raises an httpx.HTTPError on the next iteration. Verifies
    the decoder catches the error and emits a canonical error event before
    terminating cleanly.

    Note: decode_native uses aiter_lines(), not aiter_bytes(), so this stub
    implements aiter_lines(). Updated from the original aiter_bytes() version
    when decode_native was fixed to use aiter_lines() (bug fix 2026-05-27).
    """

    def __init__(self, *, first_lines: list[str], exc: BaseException) -> None:
        self._first_lines = first_lines
        self._exc = exc

    async def aiter_lines(self) -> AsyncIterator[str]:
        for line in self._first_lines:
            yield line
        raise self._exc


class TestDecodeNativeMidStreamError:
    """Stream-error → canonical error event terminator behavior."""

    @pytest.mark.asyncio
    async def test_connect_error_mid_stream_yields_error_event(self) -> None:
        """ConnectError raised by aiter_lines mid-stream surfaces as
        a canonical error event (not an uncaught exception)."""
        first_lines = [
            'event: chat.start',
            'data: {"type":"chat.start","response_id":"r1"}',
            '',  # blank line completes the event block
        ]
        stub = _RaisingStream(
            first_lines=first_lines,
            exc=httpx.ConnectError("Connection reset by peer"),
        )

        events: list = []
        async for ev in decode_native(stub):  # type: ignore[arg-type]
            events.append(ev)

        # chat.start was yielded before the error.
        types = [e.type for e in events]
        assert "chat.start" in types
        # An error event terminates the stream.
        assert events[-1].type == "error"
        assert events[-1].error is not None
        assert events[-1].error.get("code") == "upstream_stream_error"
        assert "ConnectError" in events[-1].error.get("message", "")

    @pytest.mark.asyncio
    async def test_read_timeout_mid_stream_yields_error_event(self) -> None:
        """ReadTimeout raised by aiter_lines mid-stream surfaces as
        a canonical error event."""
        first_lines = [
            'event: chat.start',
            'data: {"type":"chat.start","response_id":"r1"}',
            '',  # blank line completes the event block
        ]
        stub = _RaisingStream(
            first_lines=first_lines,
            exc=httpx.ReadTimeout("Read timed out"),
        )

        events: list = []
        async for ev in decode_native(stub):  # type: ignore[arg-type]
            events.append(ev)

        assert events[-1].type == "error"
        assert events[-1].error is not None
        assert events[-1].error.get("code") == "upstream_stream_error"


# ---------------------------------------------------------------------------
# decode_native — chunked TCP delivery regression (bug fix 2026-05-27)
# ---------------------------------------------------------------------------
# The original decode_native called aiter_bytes() and parsed each raw chunk
# of SSE bytes independently. When TCP delivered an SSE event's
# "event:" and "data:" lines in *separate* chunks (which is the normal case
# for a streaming response), the per-chunk parser reset its state between calls,
# so neither chunk produced a complete event → 0 events parsed. The fix
# switches to aiter_lines(), which reassembles lines internally and drives a
# single stateful SSE parser across the whole stream.
#
# These tests use a custom stub that exposes aiter_lines() and delivers the
# SSE lines in the split pattern that triggered the original bug.


class _SplitLineStream:
    """Stub that yields SSE lines one-at-a-time to simulate per-line delivery.

    In practice httpx.aiter_lines() internally reassembles lines from raw
    chunks. This stub exercises the parser with each line arriving as a
    separate yield — the minimal unit that would have broken the original
    chunk-based implementation.
    """

    def __init__(self, sse_body: str) -> None:
        # Split on newlines, keeping blank lines (they are the SSE block terminators).
        self._lines = sse_body.split("\n")

    async def aiter_lines(self) -> AsyncIterator[str]:
        for line in self._lines:
            yield line.rstrip("\r")


class TestDecodeNativeChunkedSplitRegression:
    """Regression suite for the aiter_bytes / chunk-split zero-event bug.

    Reproduces the exact SSE format emitted by LM Studio's native
    /api/v1/chat (confirmed by live curl probe 2026-05-27): chat.start →
    reasoning.start → reasoning.delta* → reasoning.end → message.start →
    message.delta* → prompt_processing.* → message.end → chat.end.

    All tests deliver each SSE line as a separate yield to maximally expose
    any parser-state-reset bug between "event:" and "data:" lines.
    """

    # Exact SSE format from live LM Studio probe 2026-05-27.
    _LIVE_FORMAT_SSE = (
        'event: chat.start\n'
        'data: {"type":"chat.start","model_instance_id":"qwen3.6-35b-a3b-test"}\n'
        '\n'
        'event: reasoning.start\n'
        'data: {"type":"reasoning.start"}\n'
        '\n'
        'event: reasoning.delta\n'
        'data: {"type":"reasoning.delta","content":"Thinking"}\n'
        '\n'
        'event: reasoning.delta\n'
        'data: {"type":"reasoning.delta","content":" Process"}\n'
        '\n'
        'event: reasoning.end\n'
        'data: {"type":"reasoning.end"}\n'
        '\n'
        'event: message.start\n'
        'data: {"type":"message.start"}\n'
        '\n'
        'event: message.delta\n'
        'data: {"type":"message.delta","content":"PONG"}\n'
        '\n'
        'event: prompt_processing.progress\n'
        'data: {"type":"prompt_processing.progress","progress":1}\n'
        '\n'
        'event: prompt_processing.end\n'
        'data: {"type":"prompt_processing.end"}\n'
        '\n'
        'event: message.end\n'
        'data: {"type":"message.end"}\n'
        '\n'
        'event: chat.end\n'
        'data: {"type":"chat.end","result":{"model_instance_id":"qwen3.6-35b-a3b-test"}}\n'
        '\n'
    )

    @pytest.mark.asyncio
    async def test_per_line_delivery_yields_nonzero_events(self) -> None:
        """Parser yields events even when each SSE line arrives individually.

        This is the direct regression test for the aiter_bytes chunk-split bug:
        the original code parsed each raw chunk independently, resetting
        parser state between the 'event:' and 'data:' lines, producing 0 events.
        """
        stub = _SplitLineStream(self._LIVE_FORMAT_SSE)
        events: list = []
        async for ev in decode_native(stub):  # type: ignore[arg-type]
            events.append(ev)

        assert len(events) > 0, (
            "decode_native yielded 0 events from a valid SSE stream delivered "
            "one line at a time — the chunk-split bug has regressed."
        )

    @pytest.mark.asyncio
    async def test_live_format_has_text_delta(self) -> None:
        """message.delta event with text content is parsed from live SSE format."""
        stub = _SplitLineStream(self._LIVE_FORMAT_SSE)
        events: list = []
        async for ev in decode_native(stub):  # type: ignore[arg-type]
            events.append(ev)

        text_deltas = [e for e in events if e.type == "message.delta"]
        assert len(text_deltas) >= 1, "No message.delta events found"
        assert text_deltas[0].content == "PONG"

    @pytest.mark.asyncio
    async def test_live_format_has_terminal_chat_end(self) -> None:
        """chat.end is the terminal event — stream closes cleanly."""
        stub = _SplitLineStream(self._LIVE_FORMAT_SSE)
        events: list = []
        async for ev in decode_native(stub):  # type: ignore[arg-type]
            events.append(ev)

        assert events[-1].type == "chat.end", (
            f"Expected terminal event to be chat.end, got {events[-1].type!r}. "
            "No terminal event means the frontend sees 'stream error'."
        )

    @pytest.mark.asyncio
    async def test_live_format_full_event_sequence(self) -> None:
        """All live-format native event types are parsed in correct order."""
        stub = _SplitLineStream(self._LIVE_FORMAT_SSE)
        events: list = []
        async for ev in decode_native(stub):  # type: ignore[arg-type]
            events.append(ev)

        types = [e.type for e in events]
        assert types == [
            "chat.start",
            "reasoning.start",
            "reasoning.delta",
            "reasoning.delta",
            "reasoning.end",
            "message.start",
            "message.delta",
            "prompt_processing.progress",
            "prompt_processing.end",
            "message.end",
            "chat.end",
        ]

    @pytest.mark.asyncio
    async def test_live_format_reasoning_deltas_have_content(self) -> None:
        """reasoning.delta events carry their content field."""
        stub = _SplitLineStream(self._LIVE_FORMAT_SSE)
        events: list = []
        async for ev in decode_native(stub):  # type: ignore[arg-type]
            events.append(ev)

        reasoning_deltas = [e for e in events if e.type == "reasoning.delta"]
        contents = [e.content for e in reasoning_deltas]
        assert contents == ["Thinking", " Process"]
