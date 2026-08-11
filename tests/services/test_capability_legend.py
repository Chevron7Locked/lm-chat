# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the capability-aware system-prompt legend.

Covers the render function in isolation (no StreamingService, no DB):
modes + features are always present, tools reflect the enabled list via
the catalog lookup, the empty-tools fallback line renders, and the
OFFER/DO framing survives.
"""
from __future__ import annotations

import pytest

from lmchat.services.capability_legend import (
    FEATURES,
    MODES,
    render_capability_legend,
)


def _fake_catalog(entry_id: str) -> dict | None:
    if entry_id == "searxng":
        return {"description": "Privacy-respecting web search."}
    return None


def test_all_modes_present_with_slash_and_description() -> None:
    legend = render_capability_legend(enabled_tools=[], catalog=_fake_catalog)
    for row in MODES:
        assert f"- {row.command} — {row.description}" in legend


def test_all_features_present_with_slash_and_description() -> None:
    legend = render_capability_legend(enabled_tools=[], catalog=_fake_catalog)
    for row in FEATURES:
        assert f"- {row.command} — {row.description}" in legend


def test_offer_and_do_headers_present() -> None:
    legend = render_capability_legend(enabled_tools=[], catalog=_fake_catalog)
    assert "Suggest to the user (they run these):" in legend
    assert "Tools you can call directly:" in legend


def test_conservative_framing_line_present() -> None:
    legend = render_capability_legend(enabled_tools=[], catalog=_fake_catalog)
    assert (
        "reach for one of these only when it clearly fits the task; "
        "most turns need none" in legend
    )


def test_docs_pointer_present() -> None:
    legend = render_capability_legend(enabled_tools=[], catalog=_fake_catalog)
    assert (
        "- The LM Chat guide is at /docs; relevant sections are added to "
        "your context automatically when you ask about the app." in legend
    )


def test_empty_tools_renders_none_enabled_fallback() -> None:
    legend = render_capability_legend(enabled_tools=[], catalog=_fake_catalog)
    assert "(none enabled — the user can add tools with the composer's tool picker)" in legend


def test_enabled_tool_uses_catalog_description() -> None:
    legend = render_capability_legend(
        enabled_tools=["mcp/searxng"], catalog=_fake_catalog
    )
    assert "- searxng — Privacy-respecting web search." in legend
    assert "none enabled" not in legend


def test_enabled_tool_without_catalog_entry_falls_back_to_slug() -> None:
    legend = render_capability_legend(
        enabled_tools=["mcp/some-byo-server"], catalog=_fake_catalog
    )
    # The mcp/ prefix is stripped for display on both sides — no double prefix.
    assert "- some-byo-server — some-byo-server" in legend
    assert "mcp/some-byo-server" not in legend


def test_web_search_uses_builtin_description_not_catalog() -> None:
    # "web_search" must never hit the catalog lookup — it isn't an MCP
    # integration id, and a catalog that raised on unknown ids would break
    # this path if it were queried.
    def _raising_catalog(entry_id: str) -> dict | None:
        raise AssertionError(f"catalog should not be queried for {entry_id!r}")

    legend = render_capability_legend(
        enabled_tools=["web_search"], catalog=_raising_catalog
    )
    assert "- web_search — Search the live web for current information." in legend


def test_multiple_enabled_tools_all_rendered_in_order() -> None:
    legend = render_capability_legend(
        enabled_tools=["mcp/searxng", "web_search"], catalog=_fake_catalog
    )
    searxng_idx = legend.find("- searxng —")
    web_search_idx = legend.find("- web_search —")
    assert searxng_idx >= 0 and web_search_idx >= 0
    assert searxng_idx < web_search_idx


def test_duplicate_enabled_tools_deduplicated() -> None:
    legend = render_capability_legend(
        enabled_tools=["mcp/searxng", "mcp/searxng"], catalog=_fake_catalog
    )
    assert legend.count("- searxng —") == 1


def test_default_catalog_resolves_known_mcp_store_ids() -> None:
    # Default `catalog` param wires the real MCP Store catalog — sanity
    # check a well-known id resolves to a real (non-bare-id) description
    # without the caller having to pass a stub.
    legend = render_capability_legend(enabled_tools=["mcp/filesystem"])
    assert "- filesystem — " in legend
    assert "- filesystem — filesystem" not in legend
    assert "mcp/filesystem" not in legend


def test_legend_opens_with_capabilities_header() -> None:
    legend = render_capability_legend(enabled_tools=[], catalog=_fake_catalog)
    assert legend.startswith("[Capabilities]\n")


def test_web_search_only_using_default_catalog_emits_description_row() -> None:
    # The minimal builtin_web_search=True render case: enabled_tools carries
    # ONLY "web_search" (no MCP integrations), against the real default
    # catalog (no stub). Pins the exact DO row streaming_service.py's
    # openai_compat branch produces when it appends "web_search" to
    # _enabled_tools.
    legend = render_capability_legend(enabled_tools=["web_search"])
    assert "- web_search — Search the live web for current information." in legend
    assert "(none enabled" not in legend


def test_render_does_not_accept_none_enabled_tools() -> None:
    """``enabled_tools`` is typed ``list[str]``, not optional. The None ->
    [] normalization happens at the call site
    (``StreamingService._assemble_system_prompt``: ``list(payload.payload
    .integrations or [])``), never inside this function — passing ``None``
    directly raises rather than silently rendering the fallback line. This
    pins that boundary so the call site can't drop the normalization
    without a render-level test noticing the contract changed."""
    with pytest.raises(TypeError):
        render_capability_legend(enabled_tools=None)  # type: ignore[arg-type]
