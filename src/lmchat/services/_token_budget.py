# SPDX-License-Identifier: Apache-2.0
"""Shared token approximator + per-project prompt budget constant.

Single import surface for:

* ``approx_token_count(text)`` — the canonical token-budget
  approximator. Replaces every inline ``len(text) // 4`` call site
  across the service layer (streaming, ab_compare, document corpus
  estimation) so the CJK-correct UTF-8 byte heuristic is used
  consistently.
* ``PROJECT_PROMPT_TOKEN_BUDGET = 2000`` — budget for
  ``projects.system_prompt`` at create/update time. This constant
  lives here (utility-layer) rather than in ``projects_service``
  (service-layer) to avoid a
  ``rag_service → projects_service`` import cycle when the runtime
  truncation policy reads the same constant.

**Direction for new code**: any token-budget approximation MUST go
through ``approx_token_count``. Do NOT reintroduce inline
``len(text) // 4`` — the codepoint heuristic under-counts CJK and
other dense scripts by 2-4×.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Final, NamedTuple

# Per-project ``system_prompt`` budget in tokens.
# Enforced at WRITE TIME (POST/PATCH /api/projects → 422 if exceeded)
# so user-authored text is never silently truncated. Runtime
# truncation policy still applies to history / followups / RAG, but
# project_prompt never gets dropped.
PROJECT_PROMPT_TOKEN_BUDGET: Final[int] = 2000


def approx_token_count(text: str) -> int:
    """Approximate token count for *text*.

    Heuristic: ``max(1, len(text.encode('utf-8')) // 4)``. Uses the
    UTF-8 byte length rather than the codepoint count — a CJK
    codepoint is 3 bytes in UTF-8, so the codepoint heuristic
    under-counts 2-4× for dense scripts.

    A real BPE tokenizer would be more accurate but adds a heavy
    dependency for a guardrail; the byte heuristic is conservative
    enough that the budget gate refuses oversize prompts instead
    of silently letting CJK-heavy ones through.

    Returns at least 1 for any non-empty input; empty input returns 0.

    Args:
        text: The string to approximate.

    Returns:
        Estimated token count.
    """
    if not text:
        return 0
    return max(1, len(text.encode("utf-8")) // 4)


# ---------------------------------------------------------------------------
# Pre-flight context-budget gate
# ---------------------------------------------------------------------------

# Per-MCP-integration prompt cost FALLBACK. LM Studio expands each
# `mcp/<server>` entry server-side into a JSON-schema tool block before
# tokenisation. Empirically, typical MCP servers
# (searxng, context7, deepwiki, filesystem) sit in the 1.2k–1.8k token
# range. 1500 is the conservative midpoint — over-estimating triggers
# trimming earlier (safer) than under-estimating, which is the bug we're
# fixing (a small-context model + 9 integrations = ~16600 tokens,
# silent stream death).
#
# This flat guess is now used ONLY when
# a server's real schema can't be probed (not yet connected this turn /
# lookup failed) — see `estimate_context_budget`'s `integration_token_costs`
# param. When the caller (streaming_service._resolve_model_and_integrations_gate)
# has the server's tools cached in `mcp_host`, it tokenizes the ACTUAL
# advertised tool-schema JSON and passes that per-integration cost in,
# making would-overflow exact instead of a flat per-server guess.
MCP_INTEGRATION_SCHEMA_TOKENS: Final[int] = 1500


# Headroom reserved for the model's own response. The estimator counts
# only the INPUT tokens against `max_context_length`; without headroom
# the gate would let a prompt fill the entire context and leave nothing
# for the response. 2k tokens covers a typical reasoning answer + safety
# margin. Per-model tuning of _RESPONSE_HEADROOM_TOKENS is a possible
# future addition, out of scope here.
_RESPONSE_HEADROOM_TOKENS: Final[int] = 2000


class ContextBudget(NamedTuple):
    """Result of `estimate_context_budget`.

    Attributes:
        would_overflow: True if `estimated_total + headroom > max`.
        dropped: Integrations the estimator suggests dropping to fit.
                 Caller decides whether to actually drop them. Empty when
                 `would_overflow` is False OR when dropping all
                 integrations still wouldn't fit (caller fails fast).
        estimated_total: Total approximate input tokens with the kept
                         integrations.
        max: The model's `max_context_length`.
        max_with_headroom: `max - _RESPONSE_HEADROOM_TOKENS`. The gate
                           threshold the estimator actually uses.
    """

    would_overflow: bool
    dropped: list[str]
    estimated_total: int
    max: int
    max_with_headroom: int


def estimate_context_budget(
    *,
    system_prompt: str | None,
    input_text: str,
    integrations: list[str],
    max_context_length: int,
    integration_token_costs: Mapping[str, int] | None = None,
) -> ContextBudget:
    """Estimate input-token cost against a model's context window.

    Used pre-flight by the streaming service to decide whether to trim
    integrations before opening the upstream stream. The earlier
    gate only dropped integrations on
    NON-tool-trained models — tool-trained small-context models like
    qwen3-vl-8b sailed straight into the overflow. This estimator covers
    every tool-capable model.

    Per-integration cost: each integration's prompt
    cost comes from `integration_token_costs` when the caller has
    probed it (the server's tools are cached in `mcp_host` — see
    `streaming_service._resolve_model_and_integrations_gate`, which
    tokenizes the server's REAL advertised tool-schema JSON). Any
    integration missing from that mapping — including when the mapping
    itself is `None`, e.g. the server isn't connected yet or the probe
    failed — falls back to the flat `MCP_INTEGRATION_SCHEMA_TOKENS`
    guess.

    Strategy on `would_overflow`:
      - Trim integrations from the BACK of the list (lowest priority).
      - Each trim removes that integration's cost (probed or flat-guess
        fallback — see above).
      - Walk back to back until the remaining set fits, OR until the
        list is empty.
      - If even an empty integrations set overflows (system_prompt +
        input already too large), return `would_overflow=True,
        dropped=[]` — caller fails fast with an actionable error.

    Args:
        system_prompt: Top-level system prompt sent to LM Studio native.
        input_text: Concatenated text content from the current turn's
                    input blocks. Image data_url tokens are out of
                    scope — vision attachment is handled separately;
                    image tokens cost ~1700 each per LM Studio's wire
                    accounting and should be added once the FE sends them.
        integrations: User-selected MCP integrations (e.g.
                      ``[\"mcp/searxng\", \"mcp/filesystem\"]``).
        max_context_length: Model's loaded context window. 0 = unknown
                            (skip the gate; treat as non-overflowing).
        integration_token_costs: Optional probed per-integration token
                            cost, keyed by the full integration id (e.g.
                            ``\"mcp/searxng\"``). An integration absent
                            from this mapping uses
                            ``MCP_INTEGRATION_SCHEMA_TOKENS`` instead.

    Returns:
        ContextBudget tuple.
    """
    if max_context_length <= 0:
        # Unknown context (model not in cache, embedding model, etc.) —
        # don't second-guess. The gate is a hedge, not the only line of
        # defense. The in-body stall handler will catch
        # any actual overflow with a probe_for_error message.
        return ContextBudget(
            would_overflow=False,
            dropped=[],
            estimated_total=0,
            max=0,
            max_with_headroom=0,
        )

    max_with_headroom = max(1, max_context_length - _RESPONSE_HEADROOM_TOKENS)

    system_tokens = approx_token_count(system_prompt or "")
    input_tokens = approx_token_count(input_text)

    def _integration_cost(integration: str) -> int:
        if integration_token_costs is not None and integration in integration_token_costs:
            return integration_token_costs[integration]
        return MCP_INTEGRATION_SCHEMA_TOKENS

    integrations_tokens = sum(_integration_cost(i) for i in integrations)
    estimated_total = system_tokens + input_tokens + integrations_tokens

    if estimated_total <= max_with_headroom:
        return ContextBudget(
            would_overflow=False,
            dropped=[],
            estimated_total=estimated_total,
            max=max_context_length,
            max_with_headroom=max_with_headroom,
        )

    # Overflow — walk back from the end of the list, dropping integrations
    # until the remaining set fits OR the list is empty.
    kept = list(integrations)
    dropped: list[str] = []
    while kept and (
        system_tokens
        + input_tokens
        + sum(_integration_cost(i) for i in kept)
        > max_with_headroom
    ):
        dropped.append(kept.pop())

    final_total = (
        system_tokens + input_tokens + sum(_integration_cost(i) for i in kept)
    )

    return ContextBudget(
        would_overflow=final_total > max_with_headroom,
        dropped=dropped,
        estimated_total=final_total,
        max=max_context_length,
        max_with_headroom=max_with_headroom,
    )
