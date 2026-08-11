# SPDX-License-Identifier: Apache-2.0
"""Tests for the builtin tool registry — registry lookup, the ``web_search``
CanonicalTool descriptor, and its executor's formatting/error handling.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from lmchat.services.builtin_tools import (
    BUILTIN_TOOL_REGISTRY,
    WEB_SEARCH_TOOL,
    BuiltinToolContext,
    BuiltinToolRegistry,
    _web_search_executor,
)
from lmchat.services.web_search_service import SearchResult, WebSearchUnavailable

# ─── Registry ──────────────────────────────────────────────────────────────


def test_registry_lookup_returns_web_search_descriptor_and_executor() -> None:
    entry = BUILTIN_TOOL_REGISTRY.get("web_search")
    assert entry is not None
    assert entry.tool is WEB_SEARCH_TOOL
    assert entry.executor is _web_search_executor


def test_registry_unknown_tool_returns_none() -> None:
    assert BUILTIN_TOOL_REGISTRY.get("does_not_exist") is None
    assert "does_not_exist" not in BUILTIN_TOOL_REGISTRY


def test_registry_contains_and_tools_listing() -> None:
    assert "web_search" in BUILTIN_TOOL_REGISTRY
    tools = BUILTIN_TOOL_REGISTRY.tools()
    assert WEB_SEARCH_TOOL in tools


def test_registry_custom_entries_are_isolated_from_default() -> None:
    """A registry built with its own entries doesn't see the default set."""
    custom = BuiltinToolRegistry(entries={})
    assert custom.get("web_search") is None
    assert BUILTIN_TOOL_REGISTRY.get("web_search") is not None


def test_registry_subset_keeps_only_named_tools() -> None:
    subset = BUILTIN_TOOL_REGISTRY.subset(["web_search"])
    assert "web_search" in subset
    assert subset.tools() == [WEB_SEARCH_TOOL]


def test_registry_subset_silently_skips_unregistered_names() -> None:
    subset = BUILTIN_TOOL_REGISTRY.subset(["web_search", "does_not_exist"])
    assert list(subset.tools()) == [WEB_SEARCH_TOOL]


def test_registry_subset_empty_names_yields_empty_registry() -> None:
    subset = BUILTIN_TOOL_REGISTRY.subset([])
    assert subset.tools() == []
    assert "web_search" not in subset


# ─── web_search CanonicalTool descriptor ──────────────────────────────────


def test_web_search_tool_descriptor_shape() -> None:
    assert WEB_SEARCH_TOOL.name == "web_search"
    assert WEB_SEARCH_TOOL.description  # non-empty, human-readable
    params = WEB_SEARCH_TOOL.parameters
    assert params["type"] == "object"
    assert "query" in params["properties"]
    assert params["properties"]["query"]["type"] == "string"
    assert params["required"] == ["query"]


# ─── web_search executor ──────────────────────────────────────────────────


def _make_service(**overrides: object) -> MagicMock:
    service = MagicMock()
    service.search = AsyncMock(**overrides)
    return service


@pytest.mark.asyncio
async def test_executor_formats_results_as_compact_numbered_list() -> None:
    results = [
        SearchResult(title="Alpha", url="https://a.example.com", snippet="Alpha snippet"),
        SearchResult(title="Beta", url="https://b.example.com", snippet="Beta snippet"),
    ]
    service = _make_service(return_value=results)
    ctx = BuiltinToolContext(web_search_service=service)

    out = await _web_search_executor({"query": "test query"}, ctx)

    assert "test query" in out
    assert "1. Alpha — https://a.example.com — Alpha snippet" in out
    assert "2. Beta — https://b.example.com — Beta snippet" in out
    service.search.assert_awaited_once_with("test query", top_n=5)


@pytest.mark.asyncio
async def test_executor_top_n_is_passed_through_and_clamped() -> None:
    service = _make_service(return_value=[])
    ctx = BuiltinToolContext(web_search_service=service)

    await _web_search_executor({"query": "q", "top_n": 3}, ctx)
    service.search.assert_awaited_once_with("q", top_n=3)

    service2 = _make_service(return_value=[])
    ctx2 = BuiltinToolContext(web_search_service=service2)
    await _web_search_executor({"query": "q", "top_n": 999}, ctx2)
    service2.search.assert_awaited_once_with("q", top_n=10)


@pytest.mark.asyncio
async def test_executor_empty_results_returns_readable_string() -> None:
    service = _make_service(return_value=[])
    ctx = BuiltinToolContext(web_search_service=service)

    out = await _web_search_executor({"query": "nothing here"}, ctx)

    assert isinstance(out, str)
    assert "nothing here" in out
    assert "no" in out.lower() or "0" in out


@pytest.mark.asyncio
async def test_executor_search_unavailable_returns_string_not_raise() -> None:
    service = _make_service(side_effect=WebSearchUnavailable("all backends down"))
    ctx = BuiltinToolContext(web_search_service=service)

    out = await _web_search_executor({"query": "q"}, ctx)

    assert isinstance(out, str)
    assert "failed" in out.lower()
    assert "all backends down" in out


@pytest.mark.asyncio
async def test_executor_generic_exception_returns_string_not_raise() -> None:
    service = _make_service(side_effect=RuntimeError("boom"))
    ctx = BuiltinToolContext(web_search_service=service)

    out = await _web_search_executor({"query": "q"}, ctx)

    assert isinstance(out, str)
    assert "failed" in out.lower()
    assert "boom" in out


@pytest.mark.asyncio
async def test_executor_missing_service_returns_clear_string() -> None:
    ctx = BuiltinToolContext(web_search_service=None)

    out = await _web_search_executor({"query": "q"}, ctx)

    assert isinstance(out, str)
    assert "not available" in out.lower()


@pytest.mark.asyncio
async def test_executor_missing_query_argument_does_not_call_service() -> None:
    service = _make_service(return_value=[])
    ctx = BuiltinToolContext(web_search_service=service)

    out = await _web_search_executor({}, ctx)

    assert isinstance(out, str)
    assert "failed" in out.lower()
    service.search.assert_not_awaited()


@pytest.mark.asyncio
async def test_executor_blank_query_argument_does_not_call_service() -> None:
    service = _make_service(return_value=[])
    ctx = BuiltinToolContext(web_search_service=service)

    out = await _web_search_executor({"query": "   "}, ctx)

    assert isinstance(out, str)
    assert "failed" in out.lower()
    service.search.assert_not_awaited()
