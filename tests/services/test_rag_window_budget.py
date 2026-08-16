# SPDX-License-Identifier: Apache-2.0
"""Tests for window-aware RAG context trimming.

Mirrors the reference implementation ``eviction.py`` budget-shape tests where applicable;
diverges where LMChat's web-chat single-turn shape demands.

2026-08-15: ``compute_rag_budget_chars``/``trim_rag_context_for_model`` no
longer take a ``model_id`` and no longer fall back to a per-model-family
static table (``ModelProfile.context_window`` was removed entirely — LM
Chat is a public app, not a fleet roster; a name-matched table silently
mis-sizes every model without an explicit row). The budget is driven
SOLELY by the caller's live-probed ``ctx_window``
(``ModelsService.get_max_context_length`` — provider-agnostic), with a
fixed numeric floor for the genuinely-unresolved case.
"""
from __future__ import annotations

from lmchat.services.model_profile import (
    DEFAULT_PROFILE,
    PROFILE_NEMOTRON_CASCADE_2,
    PROFILE_QWEN_DISTILL,
    PROFILE_QWEN_POLARIS_9B,
)
from lmchat.services.rag_service import (
    _UNKNOWN_CTX_WINDOW_FLOOR_TOKENS,
    compute_rag_budget_chars,
    trim_rag_context_for_model,
)


class TestComputeBudget:
    """The budget is proportional to the caller-supplied live ``ctx_window``
    — never to a model name."""

    def test_unresolved_window_uses_the_fixed_floor(self) -> None:
        # None means "no probe result at all" (e.g. RAG ran with no
        # embedding client wired this turn).
        budget = compute_rag_budget_chars(None)
        assert budget == int(_UNKNOWN_CTX_WINDOW_FLOOR_TOKENS * 0.25 * 3.0)

    def test_zero_or_negative_window_also_uses_the_floor(self) -> None:
        # 0/negative both mean "the probe ran and came back unresolved" —
        # ModelsService.get_max_context_length's own "unknown" contract.
        assert compute_rag_budget_chars(0) == compute_rag_budget_chars(None)
        assert compute_rag_budget_chars(-1) == compute_rag_budget_chars(None)

    def test_a_large_live_window_produces_a_proportionally_larger_budget(
        self,
    ) -> None:
        # 262_144 is a real loaded_context_length shape reported by LM
        # Studio for an unprofiled model — no static table entry needed or
        # consulted; the number alone drives the budget.
        live_window = 262_144
        budget = compute_rag_budget_chars(live_window)
        assert budget == int(live_window * 0.25 * 3.0)

        floor_budget = compute_rag_budget_chars(None)
        # Proportional, not coincidental: the ratio between the two
        # budgets matches the ratio between the two windows (16x).
        assert budget == floor_budget * (live_window // _UNKNOWN_CTX_WINDOW_FLOOR_TOKENS)
        assert budget > floor_budget

    def test_a_small_live_window_produces_a_proportionally_smaller_budget(
        self,
    ) -> None:
        small_window = 4_096
        budget = compute_rag_budget_chars(small_window)
        assert budget == int(small_window * 0.25 * 3.0)
        assert budget < compute_rag_budget_chars(None)


class TestTrim:
    """The trim is a hard char cap on the context block, driven solely by
    the caller-supplied ``ctx_window`` — no model_id parameter."""

    def test_empty_block_returns_empty(self) -> None:
        trimmed, original, fired = trim_rag_context_for_model("", None)
        assert trimmed == ""
        assert original == 0
        assert fired is False

    def test_under_budget_passes_through(self) -> None:
        block = "## Memory\n short retrieval\n"
        trimmed, original, fired = trim_rag_context_for_model(block, None)
        assert trimmed == block
        assert original == len(block)
        assert fired is False

    def test_over_budget_trims_and_appends_marker(self) -> None:
        # Floor budget ≈ 12_288 chars; build a block well over that.
        block = "X" * 50_000
        trimmed, original, fired = trim_rag_context_for_model(block, None)
        assert fired is True
        assert original == 50_000
        assert len(trimmed) <= len(block)
        # Marker is appended so the admin can SEE in logs / chat dev console
        # that retrieval was capped rather than silently shortened.
        assert "[…retrieval truncated to fit model window]" in trimmed

    def test_large_live_window_handles_much_larger_blocks(self) -> None:
        # A 262_144-token live window → ~196K char budget. A 50K block
        # passes through untouched — proves the live number alone (no
        # model name) is what unlocks the larger budget.
        block = "X" * 50_000
        trimmed, original, fired = trim_rag_context_for_model(block, 262_144)
        assert fired is False
        assert trimmed == block
        assert original == 50_000

    def test_trim_with_live_window_keeps_a_block_the_unresolved_floor_would_cut(
        self,
    ) -> None:
        # Sits between the floor budget (~12_288 chars) and the live-window
        # budget (~196_608 chars): trimmed when unresolved, passed through
        # untouched once a live window is supplied.
        block = "X" * 50_000

        floor_trimmed, _, floor_fired = trim_rag_context_for_model(block, None)
        assert floor_fired is True
        assert len(floor_trimmed) < len(block)

        live_trimmed, _, live_fired = trim_rag_context_for_model(block, 262_144)
        assert live_fired is False
        assert live_trimmed == block


class TestModelProfileFieldAddition:
    """``tool_results_as_user`` field added to ModelProfile (preemptive)."""

    def test_default_is_false(self) -> None:
        assert DEFAULT_PROFILE.tool_results_as_user is False

    def test_all_current_profiles_default_to_false(self) -> None:
        # All current ModelProfile entries leave tool_results_as_user False —
        # no LMChat-wired family today needs the <tool_response> rewrap.
        # The field exists so wiring Nemotron later is one row change.
        assert PROFILE_NEMOTRON_CASCADE_2.tool_results_as_user is False
        assert PROFILE_QWEN_DISTILL.tool_results_as_user is False
        assert PROFILE_QWEN_POLARIS_9B.tool_results_as_user is False


class TestPolaris9bExplicitProfile:
    """The 9b polaris is explicitly profiled for its WIRE-KNOB quirks
    (sampler/history/max_tokens) — NOT for its context window, which
    ``ModelProfile`` no longer carries at all (2026-08-15: see
    ``TestComputeBudget``/``TestTrim`` above for how the RAG budget path
    gets its window instead — always a live probe, never this table)."""

    def test_polaris_resolves_to_explicit_profile_not_default(self) -> None:
        from lmchat.services.model_profile import resolve_profile

        profile = resolve_profile("qwen3.5-9b-polaris-highiq-thinking-i1")
        assert profile is PROFILE_QWEN_POLARIS_9B

    def test_polaris_strips_reasoning_from_history(self) -> None:
        # Thinking-class Qwen → same discipline as the distills.
        assert PROFILE_QWEN_POLARIS_9B.strip_reasoning_from_history is True

    def test_polaris_has_substance_fold_on(self) -> None:
        assert PROFILE_QWEN_POLARIS_9B.substance_fold is True

    def test_polaris_has_explicit_max_tokens(self) -> None:
        # Thinking-heavy → output budget must be set; otherwise reasoning
        # eats it and content comes back empty.
        assert PROFILE_QWEN_POLARIS_9B.max_tokens == 16_384

    def test_polaris_sampler_family_none(self) -> None:
        # Only the 35b-a3b has vendor per-task profiles; the 9b gets none.
        assert PROFILE_QWEN_POLARIS_9B.sampler_family == "none"
