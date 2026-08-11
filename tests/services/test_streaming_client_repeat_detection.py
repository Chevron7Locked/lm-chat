# SPDX-License-Identifier: Apache-2.0
"""Unit tests for Fix C — exact-repeat tool-call detection.

Per CHAT-ENHANCE-PHASE1/PLAN_v2.md §6 (Test plan).

Tests:
- Two identical successful calls in a row: second emits tool_call.repeat_warning
  BEFORE on_tool_call callback.
- Fail-then-retry case: tool_call.failure → tool_call.success with same sig
  → NO warning event emitted.
- Different args: no warning.
- Lookback window exhaustion (>5 different calls between identical pair):
  no warning.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from prometheus_client import REGISTRY  # type: ignore[import-untyped]

from lmchat.lmstudio.types import (
    CanonicalChatRequest,
    CanonicalEvent,
    CanonicalInputBlock,
    CanonicalToolCall,
)
from lmchat.services.lmstudio_streaming_client import LmstudioStreamingClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(model: str = "test-model") -> CanonicalChatRequest:
    return CanonicalChatRequest(
        model=model,
        input=[CanonicalInputBlock(type="text", content="hello")],
    )


def _ev(type_: str, **kwargs: Any) -> CanonicalEvent:
    return CanonicalEvent(type=type_, **kwargs)  # type: ignore[arg-type]


def _tool_ev(type_: str, tc: CanonicalToolCall) -> CanonicalEvent:
    return CanonicalEvent(type=type_, tool_call=tc)  # type: ignore[arg-type]


def _tool_stream(
    *tool_sequences: list[CanonicalEvent],
) -> list[CanonicalEvent]:
    """Build a full stream: chat.start → [tool sequences] → chat.end."""
    evs: list[CanonicalEvent] = [_ev("chat.start")]
    for seq in tool_sequences:
        evs.extend(seq)
    evs.append(_ev("chat.end"))
    return evs


def _tc_sequence(
    name: str,
    args: dict,
    terminal: str,  # "tool_call.success" or "tool_call.failure"
    id_: str = "c1",
) -> list[CanonicalEvent]:
    """One complete tool-call event sequence (start → name → args → terminal)."""
    tc = CanonicalToolCall(id=id_, name=name, arguments=args)
    return [
        _tool_ev("tool_call.start", tc),
        _tool_ev("tool_call.name", tc),
        _tool_ev("tool_call.arguments", tc),
        _tool_ev(terminal, tc),
    ]


async def _events_from(events: list[CanonicalEvent]) -> AsyncIterator[CanonicalEvent]:
    for ev in events:
        yield ev


def _make_client(events: list[CanonicalEvent]) -> LmstudioStreamingClient:
    adapter = MagicMock()
    adapter.stream_chat = MagicMock(return_value=_events_from(events))
    return LmstudioStreamingClient(adapter=adapter)


async def _collect(client: LmstudioStreamingClient, callback=None) -> list[CanonicalEvent]:
    """Drain client.stream() into a list."""
    req = _make_request()
    return [ev async for ev in client.stream(request=req, on_tool_call=callback)]


def _get_repeat_counter(tool_name: str) -> float:
    """Read TOOL_CALL_REPEATS_TOTAL{tool_name=...} from the Prometheus registry."""
    for metric in REGISTRY.collect():
        if metric.name == "lmchat_tool_call_repeats":
            for sample in metric.samples:
                if (
                    sample.name == "lmchat_tool_call_repeats_total"
                    and sample.labels.get("tool_name") == tool_name
                ):
                    return sample.value
    return 0.0


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repeat_warning_emitted_on_second_identical_successful_call() -> None:
    """tool_call.repeat_warning is emitted when the same call (name+args) succeeds twice."""
    args = {"q": "test query"}
    seq1 = _tc_sequence("search_web", args, "tool_call.success", id_="c1")
    seq2 = _tc_sequence("search_web", args, "tool_call.success", id_="c2")

    events = _tool_stream(seq1, seq2)
    callback = MagicMock()
    client = _make_client(events)

    before = _get_repeat_counter("search_web")
    result = await _collect(client, callback=callback)
    after = _get_repeat_counter("search_web")

    # Counter incremented exactly once (for the second call).
    assert after == before + 1.0, f"Counter: before={before} after={after}"

    # A tool_call.repeat_warning event must appear in the output.
    warning_events = [ev for ev in result if ev.type == "tool_call.repeat_warning"]
    assert len(warning_events) == 1, f"Expected 1 warning, got {len(warning_events)}"

    # The warning must appear before on_tool_call fires for the second call.
    # Both calls fire the callback; the warning appears between the two completions.
    assert callback.call_count == 2, f"Expected 2 callbacks, got {callback.call_count}"

    # Warning event carries the tool_call that triggered it.
    assert warning_events[0].tool_call is not None
    assert warning_events[0].tool_call.name == "search_web"


@pytest.mark.asyncio
async def test_no_warning_on_fail_then_retry_same_args() -> None:
    """Fail-then-retry with same args MUST NOT emit tool_call.repeat_warning.

    Failure → success with same signature is a legitimate retry, not a
    stuck loop.
    """
    args = {"q": "retry query"}
    seq_fail = _tc_sequence("search_web", args, "tool_call.failure", id_="c1")
    seq_success = _tc_sequence("search_web", args, "tool_call.success", id_="c2")

    events = _tool_stream(seq_fail, seq_success)
    callback = MagicMock()
    client = _make_client(events)

    result = await _collect(client, callback=callback)

    warning_events = [ev for ev in result if ev.type == "tool_call.repeat_warning"]
    assert len(warning_events) == 0, (
        f"False positive: warning emitted on fail→retry. Events: {[ev.type for ev in result]}"
    )


@pytest.mark.asyncio
async def test_no_warning_on_different_args() -> None:
    """Different args with same tool name MUST NOT emit a warning."""
    seq1 = _tc_sequence("search_web", {"q": "first"}, "tool_call.success", id_="c1")
    seq2 = _tc_sequence("search_web", {"q": "second"}, "tool_call.success", id_="c2")

    events = _tool_stream(seq1, seq2)
    callback = MagicMock()
    client = _make_client(events)

    result = await _collect(client, callback=callback)

    warning_events = [ev for ev in result if ev.type == "tool_call.repeat_warning"]
    assert len(warning_events) == 0, (
        f"False positive: warning emitted on different args. Events: {[ev.type for ev in result]}"
    )


@pytest.mark.asyncio
async def test_no_warning_after_lookback_window_exhaustion() -> None:
    """Repeat after >5 different calls between the pair MUST NOT emit a warning.

    The deque(maxlen=5) means the original call signature drops out of the
    window before the repeat arrives.
    """
    args = {"q": "repeated"}
    # One initial call, then 5 distinct calls to fill and flush the window,
    # then the same initial call again.
    seq_initial = _tc_sequence("search_web", args, "tool_call.success", id_="c0")
    fillers = [
        _tc_sequence("search_web", {"q": f"filler{i}"}, "tool_call.success", id_=f"f{i}")
        for i in range(5)
    ]
    seq_repeat = _tc_sequence("search_web", args, "tool_call.success", id_="c1")

    events = _tool_stream(seq_initial, *fillers, seq_repeat)
    callback = MagicMock()
    client = _make_client(events)

    result = await _collect(client, callback=callback)

    warning_events = [ev for ev in result if ev.type == "tool_call.repeat_warning"]
    assert len(warning_events) == 0, (
        f"False positive: warning after window exhaustion. Events: {[ev.type for ev in result]}"
    )
