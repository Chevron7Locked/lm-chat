# SPDX-License-Identifier: Apache-2.0
"""RAG-mode resolver: INLINE / HYBRID / FOCUSED.

The threshold is derived from a hybrid sweep → ratio → formula. Tests pin:

* ``compute_rag_threshold`` formula: ``ctx_window × inline_fraction``;
  per-project override wins.
* ``resolve_rag_mode`` priority:
  1. FOCUSED when ``chat_settings.focused_document_id`` is set
     (regardless of project state).
  2. INLINE when project + corpus known + ≤ threshold.
  3. HYBRID fall-through.
"""

from __future__ import annotations

from lmchat.services.rag_mode_resolver import (
    RagMode,
    RagModeDecision,
    compute_rag_threshold,
    resolve_rag_mode,
)

# ─── compute_rag_threshold ───────────────────────────────────────────────


def test_compute_threshold_default_formula_at_anchor() -> None:
    """Anchor model (131k ctx, default inline_fraction 0.061) →
    ≈ 8000 token threshold (the sweep-winning value the ratio was
    derived from)."""
    threshold = compute_rag_threshold(ctx_window=131_000)
    assert 7000 < threshold < 9000, threshold


def test_compute_threshold_scales_with_ctx_window() -> None:
    """8K-ctx model → ~488 tokens; 262K-ctx → ~16k tokens. Ratio
    transfers across the admin's fleet."""
    t_8k = compute_rag_threshold(ctx_window=8_000)
    t_262k = compute_rag_threshold(ctx_window=262_000)
    assert 400 < t_8k < 600, t_8k
    assert 15_000 < t_262k < 17_000, t_262k


def test_compute_threshold_per_project_override_wins() -> None:
    """A non-NULL ``projects.rag_threshold`` overrides the formula."""
    assert compute_rag_threshold(ctx_window=131_000, override=4000) == 4000
    assert compute_rag_threshold(ctx_window=8_000, override=1000) == 1000


def test_compute_threshold_override_zero_forces_hybrid() -> None:
    """Override of 0 means 'force HYBRID' — clamped to 1 so any
    non-empty corpus exceeds the threshold. Pre-fix this silently
    fell back to the formula."""
    assert compute_rag_threshold(ctx_window=131_000, override=0) == 1
    assert compute_rag_threshold(ctx_window=8_000, override=0) == 1


def test_compute_threshold_negative_override_clamped_to_one() -> None:
    """Negative overrides clamp to 1 rather than producing a
    nonsensical negative threshold."""
    assert compute_rag_threshold(ctx_window=131_000, override=-5) == 1


def test_compute_threshold_zero_ctx_returns_one() -> None:
    """Degenerate input (ctx_window <= 0) returns 1 — no NaN, no
    negative threshold."""
    assert compute_rag_threshold(ctx_window=0) == 1
    assert compute_rag_threshold(ctx_window=-1) == 1


# ─── resolve_rag_mode — FOCUSED priority ─────────────────────────────────


def test_focused_short_circuits_even_with_project_id() -> None:
    """FOCUSED wins over INLINE/HYBRID even when project_id is set
    and corpus is small enough to inline."""
    decision = resolve_rag_mode(
        project_id=42,
        chat_settings={"focused_document_id": 7},
        ctx_window=131_000,
        project_corpus_tokens=100,
    )
    assert decision.mode == RagMode.FOCUSED
    assert decision.focused_document_id == 7


def test_focused_works_without_project_id() -> None:
    """A focused-doc bypass works for un-projected chats too."""
    decision = resolve_rag_mode(
        project_id=None,
        chat_settings={"focused_document_id": 99},
        ctx_window=131_000,
    )
    assert decision.mode == RagMode.FOCUSED
    assert decision.focused_document_id == 99


def test_focused_ignored_when_id_is_invalid() -> None:
    """Negative/zero/non-int focused_document_id is ignored — falls
    through to INLINE/HYBRID rules."""
    for bad in (0, -1, "7", None, [], {}):
        decision = resolve_rag_mode(
            project_id=None,
            chat_settings={"focused_document_id": bad},
            ctx_window=131_000,
        )
        assert decision.mode != RagMode.FOCUSED, f"bad={bad!r}"


# ─── resolve_rag_mode — INLINE vs HYBRID ─────────────────────────────────


def test_inline_when_project_and_small_corpus(  ) -> None:
    """Project corpus ≤ threshold → INLINE."""
    decision = resolve_rag_mode(
        project_id=42,
        chat_settings={},
        ctx_window=131_000,
        project_corpus_tokens=4_000,  # ≤ 8k threshold
    )
    assert decision.mode == RagMode.INLINE


def test_hybrid_when_project_and_large_corpus() -> None:
    """Project corpus > threshold → HYBRID."""
    decision = resolve_rag_mode(
        project_id=42,
        chat_settings={},
        ctx_window=131_000,
        project_corpus_tokens=20_000,  # > 8k threshold
    )
    assert decision.mode == RagMode.HYBRID


def test_hybrid_when_no_project_id() -> None:
    """Un-projected chats always HYBRID (preserves legacy
    behavior)."""
    decision = resolve_rag_mode(
        project_id=None,
        chat_settings={},
        ctx_window=131_000,
        project_corpus_tokens=4_000,
    )
    assert decision.mode == RagMode.HYBRID


def test_hybrid_when_corpus_size_unknown() -> None:
    """Caller doesn't supply ``project_corpus_tokens`` →
    HYBRID by default (can't decide to INLINE without the number)."""
    decision = resolve_rag_mode(
        project_id=42,
        chat_settings={},
        ctx_window=131_000,
        project_corpus_tokens=None,
    )
    assert decision.mode == RagMode.HYBRID


def test_per_project_override_can_force_hybrid_for_small_corpus() -> None:
    """If the admin wants to force HYBRID even for a small corpus
    they can set ``projects.rag_threshold=1``; the formula returns
    the override and the corpus exceeds it."""
    decision = resolve_rag_mode(
        project_id=42,
        chat_settings={},
        ctx_window=131_000,
        project_corpus_tokens=4_000,
        project_rag_threshold_override=1,  # tiny threshold
    )
    assert decision.mode == RagMode.HYBRID


def test_decision_carries_supporting_numbers() -> None:
    """RagModeDecision exposes the numbers the caller needs to log
    or surface via the /rag_mode endpoint."""
    decision = resolve_rag_mode(
        project_id=42,
        chat_settings={},
        ctx_window=131_000,
        project_corpus_tokens=4_000,
    )
    assert isinstance(decision, RagModeDecision)
    assert decision.project_corpus_tokens == 4_000
    assert decision.threshold_tokens is not None and decision.threshold_tokens > 0
    assert decision.focused_document_id is None
