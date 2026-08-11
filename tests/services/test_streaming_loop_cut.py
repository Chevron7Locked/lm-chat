# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the pure loop-cut decision helper (stream_chat decomp, final cut).

``_decide_loop_cut`` / ``_LoopCutDecision`` (streaming_service.py) were
extracted from the inline loop-cut predicate + reason derivation formerly
duplicated between the "loop-cut predicate" and the loop-cut terminal inside
``StreamingService._run_persist_and_yield``. The helper is pure — no ``self``,
no I/O, no yield — so its three thresholds (client-advisory early cut,
consecutive-identical backstop, per-turn backstop) can be tested directly
without driving a full streaming turn. See ``test_streaming_tool_loop_cap.py``
for the end-to-end integration coverage of the same behavior.
"""
from __future__ import annotations

import pytest

from lmchat.services import streaming_service as ss
from lmchat.services.streaming_service import _decide_loop_cut, _LoopCutDecision


@pytest.fixture(autouse=True)
def _small_thresholds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin small, deterministic thresholds for these tests.

    Decouples the tests from the env-configurable defaults (50 / 5) and from
    any ``LM_CHAT_MAX_*`` env vars set elsewhere in the test session.
    """
    monkeypatch.setattr(ss, "_MAX_IDENTICAL_TOOL_ROUNDS", 3)
    monkeypatch.setattr(ss, "_MAX_TOOL_ROUNDS_PER_TURN", 10)


# ---------------------------------------------------------------------------
# No trigger
# ---------------------------------------------------------------------------


def test_no_trigger_no_cut() -> None:
    """None of the three conditions fire -> no cut, both reasons None."""
    decision = _decide_loop_cut(
        early_cut_reason=None,
        event_type="tool_call.success",
        consecutive_identical_rounds=1,
        turn_tool_rounds=2,
    )
    assert decision == _LoopCutDecision(
        should_cut=False, cut_reason=None, effective_cut=None
    )


def test_backstops_ignore_non_tool_call_events() -> None:
    """The two service-local backstops only evaluate on tool_call.success /
    tool_call.failure -- an otherwise-tripping count on another event.type
    does NOT cut."""
    decision = _decide_loop_cut(
        early_cut_reason=None,
        event_type="tool_call.arguments",
        consecutive_identical_rounds=99,
        turn_tool_rounds=99,
    )
    assert decision.should_cut is False
    assert decision.cut_reason is None
    assert decision.effective_cut is None


# ---------------------------------------------------------------------------
# Client-advisory early cut
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reason", ["repeat_loop", "failure_streak"])
def test_advisory_reason_cuts_on_any_event_type(reason: str) -> None:
    """The client-advisory reason cuts on ANY event.type (not just
    success/failure) and cut_reason/effective_cut both echo it verbatim."""
    decision = _decide_loop_cut(
        early_cut_reason=reason,
        event_type="tool_call.arguments",  # deliberately NOT success/failure
        consecutive_identical_rounds=0,
        turn_tool_rounds=0,
    )
    assert decision.should_cut is True
    assert decision.cut_reason == reason
    assert decision.effective_cut == reason


def test_advisory_reason_takes_priority_over_backstops() -> None:
    """When the advisory reason AND a backstop would both fire, cut_reason
    reflects the advisory reason (checked first, mirroring the original
    inline if/elif/else chain) and is NOT renormalized by the
    identical-rounds effective_cut step."""
    decision = _decide_loop_cut(
        early_cut_reason="failure_streak",
        event_type="tool_call.success",
        consecutive_identical_rounds=5,  # also over the identical-rounds max
        turn_tool_rounds=1,
    )
    assert decision.should_cut is True
    assert decision.cut_reason == "failure_streak"
    assert decision.effective_cut == "failure_streak"


def test_unknown_advisory_reason_falls_back_to_tool_loop_cap() -> None:
    """Any non-None early_cut_reason other than the two known values maps to
    "tool_loop_cap" (mirrors the original inline if/elif/else chain -- in
    practice only "repeat_loop"/"failure_streak" are ever assigned upstream,
    but the fallback branch is part of the preserved behavior)."""
    decision = _decide_loop_cut(
        early_cut_reason="something_else",
        event_type="tool_call.success",
        consecutive_identical_rounds=0,
        turn_tool_rounds=0,
    )
    assert decision.should_cut is True
    assert decision.cut_reason == "tool_loop_cap"
    assert decision.effective_cut == "tool_loop_cap"


# ---------------------------------------------------------------------------
# Consecutive-identical backstop
# ---------------------------------------------------------------------------


def test_consecutive_identical_below_max_no_cut() -> None:
    decision = _decide_loop_cut(
        early_cut_reason=None,
        event_type="tool_call.success",
        consecutive_identical_rounds=2,  # < max (3)
        turn_tool_rounds=1,
    )
    assert decision.should_cut is False


def test_consecutive_identical_at_max_cuts() -> None:
    """consecutive_identical_rounds == the max cuts (predicate uses >=)."""
    decision = _decide_loop_cut(
        early_cut_reason=None,
        event_type="tool_call.success",
        consecutive_identical_rounds=3,  # == _MAX_IDENTICAL_TOOL_ROUNDS (3)
        turn_tool_rounds=1,
    )
    assert decision.should_cut is True
    assert decision.cut_reason == "tool_loop_cap"
    assert decision.effective_cut == "repeat_loop"


def test_consecutive_identical_over_max_cuts() -> None:
    decision = _decide_loop_cut(
        early_cut_reason=None,
        event_type="tool_call.failure",
        consecutive_identical_rounds=7,  # > max
        turn_tool_rounds=1,
    )
    assert decision.should_cut is True
    assert decision.cut_reason == "tool_loop_cap"
    assert decision.effective_cut == "repeat_loop"


def test_consecutive_identical_backstop_disabled_at_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A <= 0 threshold disables the identical-rounds backstop entirely
    (matches the module constant's documented override semantics)."""
    monkeypatch.setattr(ss, "_MAX_IDENTICAL_TOOL_ROUNDS", 0)
    decision = _decide_loop_cut(
        early_cut_reason=None,
        event_type="tool_call.success",
        consecutive_identical_rounds=999,
        turn_tool_rounds=1,
    )
    assert decision.should_cut is False


# ---------------------------------------------------------------------------
# Per-turn pathological backstop
# ---------------------------------------------------------------------------


def test_turn_tool_rounds_at_max_no_cut() -> None:
    """turn_tool_rounds == the cap does NOT cut (predicate uses strict >)."""
    decision = _decide_loop_cut(
        early_cut_reason=None,
        event_type="tool_call.success",
        consecutive_identical_rounds=0,
        turn_tool_rounds=10,  # == cap, not > cap
    )
    assert decision.should_cut is False


def test_turn_tool_rounds_over_max_cuts() -> None:
    """turn_tool_rounds strictly > the per-turn cap cuts, with the
    identical-rounds backstop NOT tripped -> effective_cut stays
    "tool_loop_cap" (no repeat_loop renormalization)."""
    decision = _decide_loop_cut(
        early_cut_reason=None,
        event_type="tool_call.success",
        consecutive_identical_rounds=0,
        turn_tool_rounds=11,  # > _MAX_TOOL_ROUNDS_PER_TURN (10)
    )
    assert decision.should_cut is True
    assert decision.cut_reason == "tool_loop_cap"
    assert decision.effective_cut == "tool_loop_cap"


def test_turn_tool_rounds_over_max_and_identical_also_over_renormalizes(
) -> None:
    """When the per-turn backstop fires the cut but the identical-rounds
    count is ALSO over its max, effective_cut is renormalized to
    "repeat_loop" (the terminal-content policy nuance)."""
    decision = _decide_loop_cut(
        early_cut_reason=None,
        event_type="tool_call.failure",
        consecutive_identical_rounds=4,  # also over the identical max (3)
        turn_tool_rounds=11,  # > per-turn cap (10)
    )
    assert decision.should_cut is True
    assert decision.cut_reason == "tool_loop_cap"
    assert decision.effective_cut == "repeat_loop"


def test_per_turn_backstop_disabled_at_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """A <= 0 threshold disables the per-turn backstop entirely."""
    monkeypatch.setattr(ss, "_MAX_TOOL_ROUNDS_PER_TURN", 0)
    decision = _decide_loop_cut(
        early_cut_reason=None,
        event_type="tool_call.success",
        consecutive_identical_rounds=0,
        turn_tool_rounds=999,
    )
    assert decision.should_cut is False
