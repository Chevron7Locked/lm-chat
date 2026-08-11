# SPDX-License-Identifier: Apache-2.0
"""ModelProfile registry — per-model wire-knob quirks.

One row per known model family. Adding a model = appending one tuple to
``_PROFILES``. Adapted from an internal shared library's ``model_config.py``,
keeping the chat-path-relevant subset (``suppress_think_kwarg`` and
``defect_only_note`` are intentionally out of scope — those exist for a
multi-round tool loop that LMChat's web-chat surface doesn't drive).

The registry exists so per-model quirks for LM Studio integrations live in
ONE row, not scattered ``"<name>" in model_id`` conditionals. One file a
contributor can read to understand all model-family wire-knob decisions.

**Context-window discipline.** The ``context_window`` field on the
DEFAULT_PROFILE stays conservatively LOW (16K) — when a request arrives
for a model we haven't explicitly profiled, we assume small. This
drives the RAG budget calculation in
:func:`lmchat.services.rag_service.compute_rag_budget_chars` directly:
``window * 0.25 * 3.0 chars/token``. Explicit profiles override with
the actual window — they are the place to be generous, not the default.
The 0.25×3.0 fraction is correct for web-chat (system prompt + chat
history + user message + response budget all compete with retrieval —
unlike an agentic tool-loop eviction shape where the tool corpus owns the
entire window). The right correction was lowering DEFAULT, not changing
the fraction.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class ModelProfile:
    """Wire-knob defaults for a model family.

    Resolved by substring match against the request's ``model_id``.
    """

    #: Output budget to inject. ``None`` = leave unset (LM Studio default).
    max_tokens: int | None = None

    #: Hard ceiling on the seat's context window. Informational for now;
    #: a future finish=length+empty auto-retry path consumes it. Also
    #: drives ``rag_service.compute_rag_budget_chars`` — the fraction of
    #: the window allocated to retrieval. **Default kept conservatively
    #: LOW** (16K) so an unknown small-window model gets a safe RAG
    #: budget. A higher default would over-budget RAG on small seats,
    #: starving the response budget. Explicit profiles override with
    #: the actual window — they are the place to be generous, not the
    #: default.
    context_window: int = 16_384

    #: NAME of the sampler bundle to inject. Resolved against
    #: :mod:`lmchat.services.sampler_profiles`. ``"none"`` = inject nothing.
    #: Splitting "which samplers" from "what wire knobs" keeps both
    #: modules small — keeping "which samplers" and "what wire knobs" separate.
    sampler_family: str = "none"

    #: Strip prior-turn ``reasoning_content`` from the SENT history copy
    #: before posting to LM Studio. Persisted history (in the DB) keeps
    #: the field intact — only the wire payload strips. See §1.3.
    #: Native surface is unaffected (LM Studio carries history via
    #: ``previous_response_id``; LMChat doesn't send history at all).
    strip_reasoning_from_history: bool = False

    #: Substance-aware fold at ``chat.end`` (see :mod:`substance_fold`).
    #: Default ON because the only failure mode is over-conservatism;
    #: the user-visible bug we're closing fires when fold would have
    #: helped.
    substance_fold: bool = True

    #: Preemptive support for Mamba-hybrid / Cascade-class families whose
    #: chat template defines no ``tool`` role — tool results must be
    #: re-expressed as ``<tool_response>``-wrapped user messages
    #: (consecutive results merged into one user turn). Defaulted OFF; flip
    #: to True on the model row when the family ships in LMChat. Today no
    #: profile sets it — the field exists so wiring Nemotron later is one
    #: row change, never an `"<name>" in model_id` sprinkle. Mirrors
    #: Today no profile sets it — the field exists so wiring Nemotron later
    #: is one row change, never a ``"<name>" in model_id`` sprinkle.
    tool_results_as_user: bool = False


DEFAULT_PROFILE: Final[ModelProfile] = ModelProfile()


# ─── Known-quirk model families ─────────────────────────────────────────────


PROFILE_NEMOTRON_CASCADE_2: Final[ModelProfile] = ModelProfile(
    max_tokens=16_384,
    context_window=131_072,
    sampler_family="none",  # NVIDIA recommends temp 1.0; llama-server default
    strip_reasoning_from_history=True,
    substance_fold=True,
)


PROFILE_QWEN_DISTILL: Final[ModelProfile] = ModelProfile(
    # Qwen 3.5/3.6 thinking distills (122b, 35b-a3b, 9b) park final
    # answers in reasoning_content and degrade if prior
    # reasoning bytes ride forward.
    max_tokens=None,
    context_window=131_072,
    sampler_family="qwen_vendor",
    strip_reasoning_from_history=True,
    substance_fold=True,
)


PROFILE_DEEPSEEK_R1_DISTILL: Final[ModelProfile] = ModelProfile(
    # R1-distill chains degrade if prior reasoning_content rides forward
    # in history.
    max_tokens=None,
    context_window=131_072,
    sampler_family="none",
    strip_reasoning_from_history=True,
    substance_fold=True,
)


PROFILE_QWEN_POLARIS_9B: Final[ModelProfile] = ModelProfile(
    # Qwen3.5-9B-Polaris-HighIQ-Thinking — small local reasoning model.
    # 256K context window. Generous
    # ``max_tokens`` because thinking eats budget aggressively and the
    # silent-empty-content failure mode (substance_fold + auto-retry
    # safety net) needs room before the doubled-budget retry fires.
    # Standard Qwen chat template, no vendor sampler (only the 35b-a3b
    # has published per-task profiles), reasoning-content carried like
    # other thinking Qwens.
    max_tokens=16_384,
    context_window=262_144,
    sampler_family="none",
    strip_reasoning_from_history=True,
    substance_fold=True,
)


# Seat-substring → profile. First match wins; most-specific first.
# Substring matched against ``model_id.lower()``.
_PROFILES: Final[list[tuple[str, ModelProfile]]] = [
    ("nemotron-cascade-2", PROFILE_NEMOTRON_CASCADE_2),
    ("qwen3.5-122b-a10b", PROFILE_QWEN_DISTILL),
    ("qwen3.6-35b-a3b", PROFILE_QWEN_DISTILL),
    ("qwen3.5-9b-polaris", PROFILE_QWEN_POLARIS_9B),
    ("deepseek-r1-distill", PROFILE_DEEPSEEK_R1_DISTILL),
]


def resolve_profile(model_id: str | None) -> ModelProfile:
    """Return the :class:`ModelProfile` for ``model_id``.

    First substring match (against ``model_id.lower()``) wins, else
    :data:`DEFAULT_PROFILE`. ``None`` and ``""`` resolve to ``DEFAULT_PROFILE``.
    """
    low = (model_id or "").lower()
    for sub, profile in _PROFILES:
        if sub in low:
            return profile
    return DEFAULT_PROFILE


__all__ = [
    "ModelProfile",
    "DEFAULT_PROFILE",
    "PROFILE_NEMOTRON_CASCADE_2",
    "PROFILE_QWEN_DISTILL",
    "PROFILE_QWEN_POLARIS_9B",
    "PROFILE_DEEPSEEK_R1_DISTILL",
    "resolve_profile",
]
