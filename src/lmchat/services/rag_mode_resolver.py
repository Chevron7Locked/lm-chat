# SPDX-License-Identifier: Apache-2.0
"""RAG-mode resolver — picks INLINE / HYBRID / FOCUSED per chat.

Three retrieval modes:

* **INLINE** — corpus small enough to inline into the system prompt; no
  embedding / FTS5 lookup. Used when the project's chunks fit under
  ``threshold`` tokens.
* **HYBRID** — corpus exceeds the threshold; fall through to the existing
  FTS5 + vector retrieval at ``rag_service.augment_prompt``. Also the
  default for legacy un-projected chats.
* **FOCUSED** — admin pinned a specific document via
  ``chats.settings.focused_document_id``; bypass retrieval and inject
  ordered chunks of THAT document via
  ``documents_service.get_document_chunks``.

The resolver does not touch the DB or call retrieve(); it only picks the
mode. ``rag_service.augment_prompt`` does the actual retrieval / inline /
focused branching.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from enum import StrEnum


class RagMode(StrEnum):
    """Three retrieval modes chosen per chat at augment time."""

    INLINE = "inline"
    HYBRID = "hybrid"
    FOCUSED = "focused"


@dataclass(frozen=True, slots=True)
class RagModeDecision:
    """Resolver output — the mode + supporting numbers (for logs)."""

    mode: RagMode
    project_corpus_tokens: int | None
    threshold_tokens: int | None
    focused_document_id: int | None


# Eligibility ratio for INLINE mode (threshold = ctx_window *
# inline_fraction). Measured empirically via the sweep harness at
# src/lmchat/services/d2_sweep.py; the formula shape is load-bearing, the
# constant is tuning.
_DEFAULT_INLINE_FRACTION: float = 0.061


# Fraction of the model's context window RAG content may occupy once a
# mode is chosen — backs rag_service.compute_rag_budget_chars /
# trim_rag_context_for_model and rag_inject_budget below. Distinct from
# _DEFAULT_INLINE_FRACTION above, which gates INLINE *eligibility*, a
# narrower question.
_RAG_CONTEXT_BUDGET_FRACTION: float = 0.25


def compute_rag_threshold(
    *,
    ctx_window: int,
    inline_fraction: float = _DEFAULT_INLINE_FRACTION,
    override: int | None = None,
) -> int:
    """Compute the per-chat RAG-mode threshold in tokens.

    Formula: ``threshold = ctx_window × inline_fraction``. A per-project
    override (``projects.rag_threshold``), when set, wins; NULL falls back
    to the formula.

    Args:
        ctx_window:      The active model's context window in tokens.
        inline_fraction: Dimensionless ratio, locked after the empirical
                         sweep.
        override:        Per-project override
                         (``projects.rag_threshold``) — when non-None
                         (including 0), skips the formula. 0 means "force
                         HYBRID" (threshold becomes 1); negative values
                         are clamped to 1.

    Returns:
        Threshold in tokens (always positive).
    """
    if override is not None:
        return max(1, int(override))
    if ctx_window <= 0:
        return 1
    return max(1, int(ctx_window * inline_fraction))


def rag_inject_budget(ctx_window: int) -> int:
    """Token budget for whole-corpus/whole-document RAG injection.

    Shared by the INLINE and FOCUSED injection loops in
    ``rag_service.augment_prompt``. Uses ``_RAG_CONTEXT_BUDGET_FRACTION``
    (the occupancy ratio) rather than ``_DEFAULT_INLINE_FRACTION`` (INLINE's
    narrower eligibility ratio) — INLINE corpora already fit under the
    tighter 0.061 threshold so this wider budget is a defensive re-check,
    not a new cap; FOCUSED has no upstream size check so this is its only
    backstop.

    Args:
        ctx_window: Active model's context window in tokens.

    Returns:
        Token budget. Returns ``sys.maxsize`` when ``ctx_window`` is
        unresolved (``<= 0``) — safe because
        ``rag_service.trim_rag_context_for_model`` always applies its own
        cap in that case too.
    """
    if ctx_window <= 0:
        return sys.maxsize
    return max(1, int(ctx_window * _RAG_CONTEXT_BUDGET_FRACTION))


def resolve_rag_mode(
    *,
    project_id: int | None,
    chat_settings: dict | None,
    ctx_window: int,
    project_corpus_tokens: int | None = None,
    inline_fraction: float = _DEFAULT_INLINE_FRACTION,
    project_rag_threshold_override: int | None = None,
) -> RagModeDecision:
    """Pick the retrieval mode for this chat.

    Priority:

    1. **FOCUSED** when the chat carries
       ``chats.settings.focused_document_id`` — admin-pinned doc bypasses
       retrieval regardless of project membership.
    2. **INLINE** when the chat is in a project AND the project's total
       corpus tokens fit under the resolved threshold.
    3. **HYBRID** otherwise — un-projected chats, large project corpora,
       or callers that don't supply ``project_corpus_tokens``.

    Args:
        project_id: Chat's ``project_id``, or None for un-projected.
        chat_settings: Chat's ``settings`` JSON. None is permitted.
        ctx_window: Active model's context window.
        project_corpus_tokens: Aggregated token count for the project's
            document chunks (see
            ``documents_service._estimate_project_corpus_tokens``). None
            means "caller doesn't know" → HYBRID.
        inline_fraction: Dimensionless ratio (see
            :func:`compute_rag_threshold`).
        project_rag_threshold_override: Per-project override
            (``projects.rag_threshold``). None → use the formula.

    Returns:
        :class:`RagModeDecision` carrying mode + supporting numbers.
    """
    # Step 1 — FOCUSED takes precedence regardless of project state.
    focused_id: int | None = None
    if chat_settings is not None:
        raw = chat_settings.get("focused_document_id")
        if isinstance(raw, int) and raw > 0:
            focused_id = raw

    if focused_id is not None:
        return RagModeDecision(
            mode=RagMode.FOCUSED,
            project_corpus_tokens=project_corpus_tokens,
            threshold_tokens=None,
            focused_document_id=focused_id,
        )

    # Step 2 — INLINE only when project + corpus known + ≤ threshold.
    threshold = compute_rag_threshold(
        ctx_window=ctx_window,
        inline_fraction=inline_fraction,
        override=project_rag_threshold_override,
    )
    if (
        project_id is not None
        and project_corpus_tokens is not None
        and project_corpus_tokens <= threshold
    ):
        return RagModeDecision(
            mode=RagMode.INLINE,
            project_corpus_tokens=project_corpus_tokens,
            threshold_tokens=threshold,
            focused_document_id=None,
        )

    # Step 3 — HYBRID fall-through (legacy un-projected + large
    # project corpora).
    return RagModeDecision(
        mode=RagMode.HYBRID,
        project_corpus_tokens=project_corpus_tokens,
        threshold_tokens=threshold,
        focused_document_id=None,
    )
