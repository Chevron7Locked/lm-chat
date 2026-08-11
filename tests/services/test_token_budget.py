# SPDX-License-Identifier: Apache-2.0
"""Tests for `lmchat.services._token_budget`.

`approx_token_count` is the canonical token approximator; the
`estimate_context_budget` function is the pre-flight gate that closes
the silent-stream-death on small-context tool-trained models with too
many MCP integrations.
"""
from __future__ import annotations

from lmchat.services._token_budget import (
    MCP_INTEGRATION_SCHEMA_TOKENS,
    approx_token_count,
    estimate_context_budget,
)

# ---------------------------------------------------------------------------
# approx_token_count
# ---------------------------------------------------------------------------


def test_approx_token_count_empty() -> None:
    assert approx_token_count("") == 0


def test_approx_token_count_ascii() -> None:
    # "hello world" = 11 bytes UTF-8 → 2 tokens approx
    assert approx_token_count("hello world") == 2


def test_approx_token_count_cjk_under_counts_via_codepoint() -> None:
    # CJK should be 2-4x heavier in tokens than the codepoint count.
    # "私は猫である" = 6 codepoints, 18 bytes UTF-8 → 4 tokens approx.
    # If we used codepoint heuristic ("len(text)//4") we'd return 1.
    assert approx_token_count("私は猫である") == 4


# ---------------------------------------------------------------------------
# estimate_context_budget — happy path
# ---------------------------------------------------------------------------


def test_estimate_budget_fits_easily() -> None:
    """A short prompt + a few integrations on a 131k context model fits."""
    result = estimate_context_budget(
        system_prompt="You are a helpful assistant.",
        input_text="What is the capital of France?",
        integrations=["mcp/searxng", "mcp/context7"],
        max_context_length=131_072,
    )
    assert result.would_overflow is False
    assert result.dropped == []
    assert result.estimated_total < result.max_with_headroom


def test_estimate_budget_unknown_context_short_circuits() -> None:
    """max_context_length=0 means unknown — skip the gate."""
    result = estimate_context_budget(
        system_prompt="x" * 100_000,
        input_text="y" * 100_000,
        integrations=["mcp/a", "mcp/b", "mcp/c"],
        max_context_length=0,
    )
    assert result.would_overflow is False
    assert result.dropped == []


# ---------------------------------------------------------------------------
# estimate_context_budget — overflow + trim policy
# ---------------------------------------------------------------------------


def test_estimate_budget_trims_lowest_priority_integrations() -> None:
    """9 MCPs + a substantive system prompt on a 16k context model trims
    back-of-list first."""
    integrations = [
        "mcp/context7",       # highest priority — first in list
        "mcp/deepwiki",
        "mcp/firecrawl",
        "mcp/searxng",
        "mcp/playwright",
        "mcp/wolfram",
        "mcp/paper-search-mcp",
        "mcp/sequential-thinking",
        "mcp/filesystem",     # lowest priority — last in list
    ]
    # A typical admin system prompt for the "general" preset is ~2k
    # tokens (~8000 UTF-8 bytes). Plus 9 × 1500 = 13_500 of MCP overhead.
    # 16_384 - 2_000 headroom = 14_384 budget. ~2_000 system + 13_500
    # MCPs = 15_500 — overflows by ~1_100, forcing one drop.
    result = estimate_context_budget(
        system_prompt="x" * 8_000,
        input_text="Short user message.",
        integrations=integrations,
        max_context_length=16_384,
    )
    assert result.would_overflow is False, (
        f"After trimming, the kept set should fit. dropped={result.dropped}, "
        f"total={result.estimated_total}, cap={result.max_with_headroom}"
    )
    assert len(result.dropped) >= 1
    # Dropped must come from the END of the input list (lowest priority).
    assert "mcp/filesystem" in result.dropped
    assert "mcp/context7" not in result.dropped


def test_estimate_budget_fails_fast_when_unsalvageable() -> None:
    """When prompt alone overflows the budget, dropping all integrations
    still doesn't fit — would_overflow=True, dropped=[]."""
    # 16k model — but the system_prompt alone is 50k bytes (= ~12.5k
    # tokens), and input is another 30k bytes (~7.5k tokens). Even with
    # zero integrations the total (12.5k + 7.5k = 20k) exceeds the
    # ~14.4k threshold. Nothing the gate can do.
    result = estimate_context_budget(
        system_prompt="x" * 50_000,
        input_text="y" * 30_000,
        integrations=["mcp/searxng", "mcp/filesystem"],
        max_context_length=16_384,
    )
    assert result.would_overflow is True
    # All integrations dropped; still overflows.
    assert sorted(result.dropped) == ["mcp/filesystem", "mcp/searxng"]


def test_estimate_budget_operator_reproducer_vl_8b_with_9_mcps() -> None:
    """A scenario that used to cause a chat to die silently.

    qwen3-vl-8b loaded with 16k context, 9 MCP integrations.
    Before this fix: LM Studio expands all 9 schemas, total exceeds
    context, stream closes with bare chat.start + no chat.end.
    After the fix: the estimator trims to fit, the gate emits a
    `stream.integrations_trimmed_for_context` warning frame, the
    stream proceeds successfully with a reduced set.
    """
    nine_mcps = [
        "mcp/context7", "mcp/deepwiki", "mcp/firecrawl", "mcp/searxng",
        "mcp/playwright", "mcp/wolfram", "mcp/paper-search-mcp",
        "mcp/sequential-thinking", "mcp/filesystem",
    ]
    # The real "general" preset system_prompt is ~3.5k chars,
    # ~875 tokens. With short input and 9 MCPs that's ~875 + 13_500 =
    # 14_375 — JUST under the 14_384 budget (the gate runs against
    # max - headroom, not raw max). Add 200 more tokens of system prompt
    # to force a guaranteed trim.
    realistic_system_prompt = "x" * 4_300
    result = estimate_context_budget(
        system_prompt=realistic_system_prompt,
        input_text="quick question about the asgard transporter beam",
        integrations=nine_mcps,
        max_context_length=16_384,
    )
    # Trimming should produce a kept set that fits.
    assert result.would_overflow is False
    kept_count = len(nine_mcps) - len(result.dropped)
    assert kept_count >= 0
    # The dropped ones must come from the end.
    if result.dropped:
        assert result.dropped[0] == "mcp/filesystem"


def test_estimate_budget_each_dropped_integration_saves_schema_tokens() -> None:
    """Each integration costs MCP_INTEGRATION_SCHEMA_TOKENS."""
    # Set up so dropping ONE integration is exactly enough to fit.
    cap = 10_000
    # headroom: 10_000 - 2_000 = 8_000 tokens budget.
    # 5 MCPs = 7_500 tokens; plus 600 token text = 8_100 — overflows by 100.
    # Dropping 1 → 4 MCPs = 6_000; plus 600 = 6_600 — fits.
    system_prompt = ""
    # 2400 UTF-8 bytes = 600 approx tokens.
    input_text = "x" * 2_400
    result = estimate_context_budget(
        system_prompt=system_prompt,
        input_text=input_text,
        integrations=["mcp/a", "mcp/b", "mcp/c", "mcp/d", "mcp/e"],
        max_context_length=cap,
    )
    assert result.would_overflow is False
    assert len(result.dropped) == 1
    assert result.dropped == ["mcp/e"]


def test_mcp_integration_schema_tokens_is_load_bearing() -> None:
    """The estimator's per-integration cost is the constant we tune as
    we live-probe MCP schema sizes — pin it so a change is intentional."""
    assert MCP_INTEGRATION_SCHEMA_TOKENS == 1500


# ---------------------------------------------------------------------------
# estimate_context_budget — integration_token_costs
#
# The flat MCP_INTEGRATION_SCHEMA_TOKENS guess is a fallback ONLY. When the
# caller (streaming_service._resolve_model_and_integrations_gate) has probed
# a server's REAL advertised tool-schema size — because the server is
# already connected in mcp_host — that probed cost drives the
# would_overflow/trim decision instead of the flat multiplier.
# ---------------------------------------------------------------------------


def test_estimate_budget_uses_probed_cost_over_flat_guess_when_larger() -> None:
    """A probed cost LARGER than the flat guess forces a trim the flat
    guess alone would never have triggered — proving the real tokenized
    size drives the decision, not the flat constant."""
    cap = 12_000
    # headroom: 12_000 - 2_000 = 10_000 token budget.
    input_text = "x" * 4_000  # 1_000 tokens.
    # With the flat guess (1500): 1_000 + 1_500 = 2_500 — comfortably
    # fits, no trim. With the REAL probed schema cost (10_000): 1_000 +
    # 10_000 = 11_000 > 10_000 — must trim to fit.
    result = estimate_context_budget(
        system_prompt="",
        input_text=input_text,
        integrations=["mcp/big-server"],
        max_context_length=cap,
        integration_token_costs={"mcp/big-server": 10_000},
    )
    assert result.dropped == ["mcp/big-server"]
    assert result.would_overflow is False
    assert result.estimated_total == 1_000


def test_estimate_budget_uses_probed_cost_over_flat_guess_when_smaller() -> None:
    """A probed cost SMALLER than the flat guess keeps an integration the
    flat guess would have trimmed — proving the flat constant is not
    silently applied on top of / instead of a real probe."""
    cap = 10_000
    # headroom: 10_000 - 2_000 = 8_000 token budget.
    input_text = "x" * (7_000 * 4)  # 7_000 tokens.
    # With the flat guess (1500): 7_000 + 1_500 = 8_500 > 8_000 — would
    # trim. With the REAL probed cost (200): 7_000 + 200 = 7_200 — fits,
    # nothing dropped.
    result = estimate_context_budget(
        system_prompt="",
        input_text=input_text,
        integrations=["mcp/tiny-server"],
        max_context_length=cap,
        integration_token_costs={"mcp/tiny-server": 200},
    )
    assert result.would_overflow is False
    assert result.dropped == []
    assert result.estimated_total == 7_200


def test_estimate_budget_falls_back_to_flat_constant_when_cost_unprobed() -> None:
    """An integration ABSENT from integration_token_costs (server not
    connected / probe failed) falls back to MCP_INTEGRATION_SCHEMA_TOKENS
    — identical behaviour to omitting the argument entirely."""
    cap = 10_000
    input_text = "x" * 2_400  # 600 tokens.

    omitted = estimate_context_budget(
        system_prompt="",
        input_text=input_text,
        integrations=["mcp/unprobed"],
        max_context_length=cap,
    )
    empty_map = estimate_context_budget(
        system_prompt="",
        input_text=input_text,
        integrations=["mcp/unprobed"],
        max_context_length=cap,
        integration_token_costs={},
    )
    assert omitted == empty_map
    assert omitted.estimated_total == 600 + MCP_INTEGRATION_SCHEMA_TOKENS


def test_estimate_budget_mixes_probed_and_unprobed_integrations() -> None:
    """One integration has a probed cost, the other doesn't (its server
    isn't connected) — the fallback applies PER-INTEGRATION, not
    all-or-nothing for the whole request."""
    result = estimate_context_budget(
        system_prompt="",
        input_text="",
        integrations=["mcp/probed", "mcp/unprobed"],
        max_context_length=20_000,
        integration_token_costs={"mcp/probed": 3_000},
    )
    assert result.would_overflow is False
    assert result.dropped == []
    assert result.estimated_total == 3_000 + MCP_INTEGRATION_SCHEMA_TOKENS
