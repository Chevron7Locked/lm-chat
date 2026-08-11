# SPDX-License-Identifier: Apache-2.0
"""Schema + content tests for the curated MCP-store catalog."""
from __future__ import annotations

from lmchat.mcp.catalog import CATALOG, get_catalog, get_catalog_entry

_REQUIRED_KEYS = {
    "id",
    "name",
    "description",
    "transport",
    "command",
    "args",
    "url",
    "secrets",
    "source",
    "trust",
}


def test_catalog_ids_unique() -> None:
    ids = [e["id"] for e in CATALOG]
    assert len(ids) == len(set(ids)), f"duplicate catalog ids: {ids}"


def test_every_entry_has_required_shape() -> None:
    for e in CATALOG:
        assert _REQUIRED_KEYS <= set(e), f"{e.get('id')} missing {_REQUIRED_KEYS - set(e)}"
        assert e["transport"] in {"stdio", "http", "sse"}
        if e["transport"] == "stdio":
            assert e["command"], f"{e['id']} stdio entry has no command"
            assert e["command"] in {"npx", "uvx"}, f"{e['id']} odd command {e['command']}"
        else:
            assert e["url"], f"{e['id']} {e['transport']} entry has no url"
        assert isinstance(e["args"], list)
        for s in e["secrets"]:
            assert {"key", "label", "required"} <= set(s), f"{e['id']} bad secret {s}"
            assert isinstance(s["required"], bool)


def test_crawl4ai_present_and_sse() -> None:
    entry = get_catalog_entry("crawl4ai")
    assert entry is not None
    assert entry["transport"] == "sse"
    assert entry["url"].endswith("/mcp/sse")
    assert any(s["key"] == "CRAWL4AI_API_TOKEN" for s in entry["secrets"])


def test_popular_servers_present() -> None:
    ids = {e["id"] for e in get_catalog()}
    expected = {
        "git",
        "memory",
        "time",
        "notion",
        "supabase",
        "sentry",
        "figma",
        "brave-search",
        "tavily",
        "exa",
        "postgres",
        "slack",
    }
    assert expected <= ids, f"missing popular servers: {expected - ids}"
