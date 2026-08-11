# SPDX-License-Identifier: Apache-2.0
"""Unit tests for per-tool failure-streak detection.

Tests:
- Three consecutive failures: emits tool_call.failure_streak_warning on third.
- Fail-then-success-then-fail (reset): two failures, one success, two more
  failures — counter resets at success, no warning until 3 NEW failures.
- Per-tool isolation: 2 failures of tool A + 2 failures of tool B do NOT
  trigger — streak is per-tool, not global.
- Success-then-failures: one success up front, then 3 failures trigger.
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


def _tool_stream(*tool_sequences: list[CanonicalEvent]) -> list[CanonicalEvent]:
    """Build a full stream: chat.start → [tool sequences] → chat.end."""
    evs: list[CanonicalEvent] = [_ev("chat.start")]
    for seq in tool_sequences:
        evs.extend(seq)
    evs.append(_ev("chat.end"))
    return evs


async def _events_from(events: list[CanonicalEvent]) -> AsyncIterator[CanonicalEvent]:
    for ev in events:
        yield ev


def _make_client(events: list[CanonicalEvent]) -> LmstudioStreamingClient:
    adapter = MagicMock()
    adapter.stream_chat = MagicMock(return_value=_events_from(events))
    return LmstudioStreamingClient(adapter=adapter)


async def _collect(
    client: LmstudioStreamingClient, callback=None
) -> list[CanonicalEvent]:
    req = _make_request()
    return [ev async for ev in client.stream(request=req, on_tool_call=callback)]


def _get_streak_counter(tool_name: str) -> float:
    """Read TOOL_CALL_FAILURE_STREAKS_TOTAL{tool_name=...} from the Prometheus registry."""
    for metric in REGISTRY.collect():
        if metric.name == "lmchat_tool_call_failure_streaks":
            for sample in metric.samples:
                if (
                    sample.name == "lmchat_tool_call_failure_streaks_total"
                    and sample.labels.get("tool_name") == tool_name
                ):
                    return sample.value
    return 0.0


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_three_consecutive_failures_emits_warning() -> None:
    """Third consecutive failure of the same tool emits tool_call.failure_streak_warning."""
    args = {"q": "failing query"}
    seq1 = _tc_sequence("search_web", args, "tool_call.failure", id_="c1")
    seq2 = _tc_sequence("search_web", args, "tool_call.failure", id_="c2")
    seq3 = _tc_sequence("search_web", args, "tool_call.failure", id_="c3")

    events = _tool_stream(seq1, seq2, seq3)
    callback = MagicMock()
    client = _make_client(events)

    before = _get_streak_counter("search_web")
    result = await _collect(client, callback=callback)
    after = _get_streak_counter("search_web")

    # Counter incremented once (threshold reached on 3rd failure).
    assert after == before + 1.0, f"Counter: before={before} after={after}"

    # Exactly one warning event in the output.
    warnings = [ev for ev in result if ev.type == "tool_call.failure_streak_warning"]
    assert len(warnings) == 1, f"Expected 1 warning, got {len(warnings)}"

    # Warning carries the completing tool_call.
    assert warnings[0].tool_call is not None
    assert warnings[0].tool_call.name == "search_web"

    # Warning error payload encodes the streak count.
    assert warnings[0].error is not None
    assert warnings[0].error["code"] == "tool_failure_streak"
    assert warnings[0].error["streak"] == 3


@pytest.mark.asyncio
async def test_fail_success_fail_resets_streak() -> None:
    """Success in the middle resets the streak; 2 new failures after do not trigger."""
    args = {"q": "query"}
    fail1 = _tc_sequence("search_web", args, "tool_call.failure", id_="c1")
    fail2 = _tc_sequence("search_web", args, "tool_call.failure", id_="c2")
    success = _tc_sequence("search_web", args, "tool_call.success", id_="c3")
    fail3 = _tc_sequence("search_web", args, "tool_call.failure", id_="c4")
    fail4 = _tc_sequence("search_web", args, "tool_call.failure", id_="c5")

    # 2 failures → success (resets) → 2 more failures: total = 2 post-reset, no trigger.
    events = _tool_stream(fail1, fail2, success, fail3, fail4)
    callback = MagicMock()
    client = _make_client(events)

    result = await _collect(client, callback=callback)

    warnings = [ev for ev in result if ev.type == "tool_call.failure_streak_warning"]
    assert len(warnings) == 0, (
        f"False positive: streak warning after reset. Events: {[ev.type for ev in result]}"
    )


@pytest.mark.asyncio
async def test_per_tool_isolation_no_cross_tool_trigger() -> None:
    """2 failures of tool_a + 2 failures of tool_b do not trigger — streak is per-tool."""
    args = {"q": "query"}
    a_fail1 = _tc_sequence("tool_alpha", args, "tool_call.failure", id_="a1")
    a_fail2 = _tc_sequence("tool_alpha", args, "tool_call.failure", id_="a2")
    b_fail1 = _tc_sequence("tool_beta", args, "tool_call.failure", id_="b1")
    b_fail2 = _tc_sequence("tool_beta", args, "tool_call.failure", id_="b2")

    events = _tool_stream(a_fail1, b_fail1, a_fail2, b_fail2)
    callback = MagicMock()
    client = _make_client(events)

    result = await _collect(client, callback=callback)

    warnings = [ev for ev in result if ev.type == "tool_call.failure_streak_warning"]
    assert len(warnings) == 0, (
        f"Cross-tool false positive. Events: {[ev.type for ev in result]}"
    )


@pytest.mark.asyncio
async def test_success_then_three_failures_triggers() -> None:
    """One success up front, then 3 failures should still trigger (success doesn't pollute)."""
    args = {"q": "query"}
    success = _tc_sequence("search_web", args, "tool_call.success", id_="c0")
    fail1 = _tc_sequence("search_web", args, "tool_call.failure", id_="c1")
    fail2 = _tc_sequence("search_web", args, "tool_call.failure", id_="c2")
    fail3 = _tc_sequence("search_web", args, "tool_call.failure", id_="c3")

    events = _tool_stream(success, fail1, fail2, fail3)
    callback = MagicMock()
    client = _make_client(events)

    before = _get_streak_counter("search_web")
    result = await _collect(client, callback=callback)
    after = _get_streak_counter("search_web")

    assert after == before + 1.0, f"Counter should increment: before={before} after={after}"

    warnings = [ev for ev in result if ev.type == "tool_call.failure_streak_warning"]
    assert len(warnings) == 1, f"Expected 1 warning, got {len(warnings)}"
