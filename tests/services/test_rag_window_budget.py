# SPDX-License-Identifier: Apache-2.0
"""Tests for window-aware RAG context trimming.

Mirrors the reference implementation ``eviction.py`` budget-shape tests where applicable;
diverges where LMChat's web-chat single-turn shape demands.
"""
from __future__ import annotations

from lmchat.services.model_profile import (
    DEFAULT_PROFILE,
    PROFILE_NEMOTRON_CASCADE_2,
    PROFILE_QWEN_DISTILL,
    PROFILE_QWEN_POLARIS_9B,
)
from lmchat.services.rag_service import (
    compute_rag_budget_chars,
    trim_rag_context_for_model,
)


class TestComputeBudget:
    """The budget is proportional to ``ModelProfile.context_window``."""

    def test_default_profile_uses_conservative_window(self) -> None:
        # DEFAULT is 16K tokens (lowered from 32K to keep unknown
        # small-window models safe);
        # budget = 16K * 0.25 * 3.0 chars/tok = ~12K chars.
        budget = compute_rag_budget_chars("unknown-model-id")
        # Roughly: floor(16384 * 0.25 * 3.0) = 12288.
        assert budget == int(DEFAULT_PROFILE.context_window * 0.25 * 3.0)

    def test_polaris_9b_uses_full_256k_window(self) -> None:
        budget = compute_rag_budget_chars("qwen3.5-9b-polaris-highiq-thinking-i1")
        # 262144 tokens * 0.25 * 3.0 chars/tok ≈ 196K chars.
        assert budget == int(PROFILE_QWEN_POLARIS_9B.context_window * 0.25 * 3.0)

    def test_qwen_distill_131k(self) -> None:
        budget = compute_rag_budget_chars("qwen3.5-122b-a10b-claude-distill-v2-i1")
        assert budget == int(PROFILE_QWEN_DISTILL.context_window * 0.25 * 3.0)

    def test_nemotron_131k(self) -> None:
        budget = compute_rag_budget_chars("nemotron-cascade-2-30b-a3b")
        assert budget == int(PROFILE_NEMOTRON_CASCADE_2.context_window * 0.25 * 3.0)

    def test_none_model_id_falls_to_default(self) -> None:
        budget = compute_rag_budget_chars(None)
        assert budget == int(DEFAULT_PROFILE.context_window * 0.25 * 3.0)

    def test_empty_model_id_falls_to_default(self) -> None:
        assert compute_rag_budget_chars("") == compute_rag_budget_chars(None)


class TestTrim:
    """The trim is a hard char cap on the context block."""

    def test_empty_block_returns_empty(self) -> None:
        trimmed, original, fired = trim_rag_context_for_model("", "anything")
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
        # DEFAULT budget ≈ 24576 chars; build a block well over that.
        block = "X" * 50_000
        trimmed, original, fired = trim_rag_context_for_model(block, None)
        assert fired is True
        assert original == 50_000
        assert len(trimmed) <= len(block)
        # Marker is appended so the admin can SEE in logs / chat dev console
        # that retrieval was capped rather than silently shortened.
        assert "[…retrieval truncated to fit model window]" in trimmed

    def test_polaris_9b_handles_much_larger_blocks(self) -> None:
        # 9b polaris has a 256K window → ~196K char budget. A 50K block
        # passes through untouched.
        block = "X" * 50_000
        trimmed, original, fired = trim_rag_context_for_model(
            block, "qwen3.5-9b-polaris-highiq-thinking-i1"
        )
        assert fired is False
        assert trimmed == block
        assert original == 50_000


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
    """The 9b polaris is now explicitly profiled."""

    def test_polaris_resolves_to_explicit_profile_not_default(self) -> None:
        from lmchat.services.model_profile import resolve_profile

        # The 9b polaris was hitting DEFAULT_PROFILE before this entry,
        # which (now lowered to 16K) is far too small
        # for its actual 256K window.
        profile = resolve_profile("qwen3.5-9b-polaris-highiq-thinking-i1")
        assert profile is PROFILE_QWEN_POLARIS_9B
        assert profile.context_window == 262_144

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
