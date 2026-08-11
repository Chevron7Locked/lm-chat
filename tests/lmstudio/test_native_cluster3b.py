# SPDX-License-Identifier: Apache-2.0
"""Native decoder tests for stop_reason + real token stats."""
from __future__ import annotations

import json

import httpx
import pytest

from lmchat.lmstudio.native import decode_native
from lmchat.lmstudio.types import CanonicalChatRequest, CanonicalInputBlock

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _req() -> CanonicalChatRequest:
    return CanonicalChatRequest(
        model="test-model",
        input=[CanonicalInputBlock(type="text", content="hello")],
    )


def _make_response(sse_body: str) -> httpx.Response:
    return httpx.Response(
        status_code=200,
        headers={"content-type": "text/event-stream"},
        content=sse_body.encode("utf-8"),
    )


async def _collect(resp: httpx.Response) -> list:
    """Collect all CanonicalEvent objects from the response."""
    from lmchat.lmstudio.types import CanonicalEvent

    events: list[CanonicalEvent] = []
    async for ev in decode_native(resp):
        events.append(ev)
    return events


# ---------------------------------------------------------------------------
# stop_reason on chat.end
# ---------------------------------------------------------------------------


class TestChatEndStopReason:
    """chat.end events surface the model's stop_reason."""

    @pytest.mark.asyncio
    async def test_stop_reason_length_decoded(self) -> None:
        """chat.end with stop_reason='length' in result is surfaced as stop_reason='length'."""
        payload = json.dumps({
            "type": "chat.end",
            "result": {
                "response_id": "resp_abc",
                "stop_reason": "length",
                "output": [],
                "stats": {},
            },
        })
        sse = f"event: chat.end\ndata: {payload}\n\n"
        resp = _make_response(sse)
        events = await _collect(resp)

        end_evt = next(e for e in events if e.type == "chat.end")
        assert end_evt.stop_reason == "length"

    @pytest.mark.asyncio
    async def test_stop_reason_eosFound_decoded(self) -> None:
        """chat.end with stop_reason='eosFound' (normal completion) is decoded."""
        payload = json.dumps({
            "type": "chat.end",
            "result": {
                "response_id": "resp_xyz",
                "stop_reason": "eosFound",
                "output": [],
            },
        })
        sse = f"event: chat.end\ndata: {payload}\n\n"
        resp = _make_response(sse)
        events = await _collect(resp)

        end_evt = next(e for e in events if e.type == "chat.end")
        assert end_evt.stop_reason == "eosFound"

    @pytest.mark.asyncio
    async def test_stop_reason_absent_is_none(self) -> None:
        """chat.end with no stop_reason field results in stop_reason=None."""
        payload = json.dumps({
            "type": "chat.end",
            "result": {"response_id": "resp_old", "output": []},
        })
        sse = f"event: chat.end\ndata: {payload}\n\n"
        resp = _make_response(sse)
        events = await _collect(resp)

        end_evt = next(e for e in events if e.type == "chat.end")
        assert end_evt.stop_reason is None

    @pytest.mark.asyncio
    async def test_stop_reason_no_result_dict_is_none(self) -> None:
        """chat.end without a result dict (older LM Studio) gives stop_reason=None."""
        payload = json.dumps({"type": "chat.end", "response_id": "resp_legacy"})
        sse = f"event: chat.end\ndata: {payload}\n\n"
        resp = _make_response(sse)
        events = await _collect(resp)

        end_evt = next(e for e in events if e.type == "chat.end")
        assert end_evt.stop_reason is None

    @pytest.mark.asyncio
    async def test_response_id_still_decoded_alongside_stop_reason(self) -> None:
        """stop_reason addition does not break existing response_id decoding."""
        payload = json.dumps({
            "type": "chat.end",
            "result": {
                "response_id": "resp_coexist",
                "stop_reason": "length",
                "output": [],
            },
        })
        sse = f"event: chat.end\ndata: {payload}\n\n"
        resp = _make_response(sse)
        events = await _collect(resp)

        end_evt = next(e for e in events if e.type == "chat.end")
        assert end_evt.response_id == "resp_coexist"
        assert end_evt.stop_reason == "length"


# ---------------------------------------------------------------------------
# real token stats on chat.end
# ---------------------------------------------------------------------------


class TestChatEndRealTokenStats:
    """chat.end events surface real token stats from result.stats."""

    @pytest.mark.asyncio
    async def test_total_output_tokens_decoded(self) -> None:
        """chat.end carries total_output_tokens from result.stats."""
        payload = json.dumps({
            "type": "chat.end",
            "result": {
                "response_id": "resp_stats",
                "output": [],
                "stats": {
                    "input_tokens": 42,
                    "total_output_tokens": 18,
                    "tokens_per_second": 22.4,
                    "time_to_first_token_seconds": 0.31,
                },
            },
        })
        sse = f"event: chat.end\ndata: {payload}\n\n"
        resp = _make_response(sse)
        events = await _collect(resp)

        end_evt = next(e for e in events if e.type == "chat.end")
        assert end_evt.total_output_tokens == 18
        assert end_evt.tokens_per_second == pytest.approx(22.4)

    @pytest.mark.asyncio
    async def test_stats_absent_gives_none(self) -> None:
        """chat.end without stats block gives None for token stats fields."""
        payload = json.dumps({
            "type": "chat.end",
            "result": {"response_id": "resp_no_stats", "output": []},
        })
        sse = f"event: chat.end\ndata: {payload}\n\n"
        resp = _make_response(sse)
        events = await _collect(resp)

        end_evt = next(e for e in events if e.type == "chat.end")
        assert end_evt.total_output_tokens is None
        assert end_evt.tokens_per_second is None

    @pytest.mark.asyncio
    async def test_stop_reason_and_stats_coexist(self) -> None:
        """stop_reason + stats are decoded together from the same chat.end event."""
        payload = json.dumps({
            "type": "chat.end",
            "result": {
                "response_id": "resp_full",
                "stop_reason": "eosFound",
                "output": [],
                "stats": {
                    "total_output_tokens": 55,
                    "tokens_per_second": 31.2,
                },
            },
        })
        sse = f"event: chat.end\ndata: {payload}\n\n"
        resp = _make_response(sse)
        events = await _collect(resp)

        end_evt = next(e for e in events if e.type == "chat.end")
        assert end_evt.stop_reason == "eosFound"
        assert end_evt.total_output_tokens == 55
        assert end_evt.tokens_per_second == pytest.approx(31.2)
        assert end_evt.response_id == "resp_full"


# ---------------------------------------------------------------------------
# tool_call.success result threading
# ---------------------------------------------------------------------------


class TestToolCallSuccessResult:
    """tool_call.success carries output → CanonicalToolCall.result.

    Prior to this fix, native.py dropped the wire `output` field and
    CanonicalToolCall had no result field, so the FE's raw.result was always
    undefined and ToolCallCard showed no result on reload.
    """

    @pytest.mark.asyncio
    async def test_tool_call_success_result_decoded(self) -> None:
        """tool_call.success with output field surfaces as CanonicalToolCall.result."""
        payload = json.dumps({
            "type": "tool_call.success",
            "tool_call_id": "tc-abc",
            "tool": "search_web",
            "arguments": {"query": "lm studio"},
            "output": "LM Studio is a desktop application.",
        })
        sse = f"event: tool_call.success\ndata: {payload}\n\n"
        resp = _make_response(sse)
        events = await _collect(resp)

        success_evt = next(e for e in events if e.type == "tool_call.success")
        assert success_evt.tool_call is not None
        assert success_evt.tool_call.result == "LM Studio is a desktop application."

    @pytest.mark.asyncio
    async def test_tool_call_success_no_output_is_none(self) -> None:
        """tool_call.success without output field gives result=None (not a crash)."""
        payload = json.dumps({
            "type": "tool_call.success",
            "tool_call_id": "tc-xyz",
            "tool": "noop_tool",
            "arguments": {},
        })
        sse = f"event: tool_call.success\ndata: {payload}\n\n"
        resp = _make_response(sse)
        events = await _collect(resp)

        success_evt = next(e for e in events if e.type == "tool_call.success")
        assert success_evt.tool_call is not None
        assert success_evt.tool_call.result is None

    @pytest.mark.asyncio
    async def test_tool_call_failure_result_unchanged(self) -> None:
        """tool_call.failure continues to surface output via event.error (pre-existing path)."""
        payload = json.dumps({
            "type": "tool_call.failure",
            "tool_call_id": "tc-fail",
            "tool": "bad_tool",
            "arguments": {},
            "output": "Connection refused",
        })
        sse = f"event: tool_call.failure\ndata: {payload}\n\n"
        resp = _make_response(sse)
        events = await _collect(resp)

        fail_evt = next(e for e in events if e.type == "tool_call.failure")
        assert fail_evt.error is not None
        assert fail_evt.error["message"] == "Connection refused"
        # tool_call.failure still has result=None on CanonicalToolCall
        # (the result is in event.error, not event.tool_call.result)
        assert fail_evt.tool_call is not None
        assert fail_evt.tool_call.result is None
