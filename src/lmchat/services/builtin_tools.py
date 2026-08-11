# SPDX-License-Identifier: Apache-2.0
"""Builtin tool registry — app-executed tools the model can call directly.

Distinct from MCP tools (``lmchat.mcp.host``), which proxy calls out to
external MCP server processes: builtin tools run in-process against
services already held on ``app.state``. First (and so far only) builtin
tool: ``web_search``, backed by ``WebSearchService``.

Design
------
Each registered tool is a (``CanonicalTool`` descriptor, executor) pair,
held in a ``BuiltinToolRegistry``. An executor is a plain async callable:
it takes the parsed tool-call ``arguments`` dict plus a
``BuiltinToolContext`` carrying whatever services it needs. Services are
threaded through explicitly — never imported as a global singleton — so
callers build the context from ``app.state`` per request, and tests can
substitute fakes.

Executors never raise: every failure mode (missing service, bad args,
backend exception) is caught and turned into a short, readable string the
model can read as the tool result — the same non-fatal-degradation contract
``McpHost.call_tool()`` follows for MCP tools.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from lmchat.lmstudio.types import CanonicalTool
from lmchat.logging import get_logger
from lmchat.services.web_search_service import WebSearchService, WebSearchUnavailable

log = get_logger(__name__)

__all__ = [
    "BUILTIN_TOOL_REGISTRY",
    "WEB_SEARCH_TOOL",
    "BuiltinToolContext",
    "BuiltinToolEntry",
    "BuiltinToolExecutor",
    "BuiltinToolRegistry",
]

# Clamp so a misbehaving (or adversarial) tool call can't ask for an
# unreasonable number of results.
_WEB_SEARCH_MIN_TOP_N = 1
_WEB_SEARCH_MAX_TOP_N = 10
_WEB_SEARCH_DEFAULT_TOP_N = 5


@dataclass(frozen=True)
class BuiltinToolContext:
    """Resources a builtin-tool executor may reach for.

    Built per-request from ``app.state`` by the caller — never held as a
    module-level singleton. A future builtin tool that needs a different
    service adds a field here.
    """

    web_search_service: WebSearchService | None = None


BuiltinToolExecutor = Callable[[dict[str, Any], BuiltinToolContext], Awaitable[str]]


@dataclass(frozen=True)
class BuiltinToolEntry:
    """A registered builtin tool: the descriptor advertised to the model
    plus the executor that runs it."""

    tool: CanonicalTool
    executor: BuiltinToolExecutor


def _coerce_top_n(raw: Any) -> int:
    """Best-effort coercion of the ``top_n`` argument to a clamped int.

    Anything that doesn't cleanly coerce (missing, wrong type, non-numeric
    string) falls back to the default rather than failing the whole call.
    """
    try:
        top_n = int(raw)
    except (TypeError, ValueError):
        return _WEB_SEARCH_DEFAULT_TOP_N
    return max(_WEB_SEARCH_MIN_TOP_N, min(top_n, _WEB_SEARCH_MAX_TOP_N))


async def _web_search_executor(arguments: dict[str, Any], ctx: BuiltinToolContext) -> str:
    """Execute the ``web_search`` builtin tool.

    Never raises: every failure path (no service configured, bad args, a
    transport-level ``WebSearchUnavailable``, or any other exception) is
    caught and turned into a short, readable string instead, so the
    tool-call loop can always feed a result back to the model.
    """
    service = ctx.web_search_service
    if service is None:
        return "web search is not available: no search backend is configured."

    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        return "web search failed: a non-empty 'query' argument is required."

    top_n = _coerce_top_n(arguments.get("top_n", _WEB_SEARCH_DEFAULT_TOP_N))

    try:
        results = await service.search(query, top_n=top_n)
    except WebSearchUnavailable as exc:
        log.warning("builtin_tools.web_search.unavailable", error=str(exc))
        return f"web search failed: {exc}"
    except Exception as exc:  # noqa: BLE001 — executor contract: never raise
        log.warning("builtin_tools.web_search.error", error=str(exc))
        return f"web search failed: {exc}"

    if not results:
        return f"No web search results found for: {query}"

    lines = [f"Web search results for: {query}"]
    lines.extend(
        f"{i}. {r.title} — {r.url} — {r.snippet}" for i, r in enumerate(results, start=1)
    )
    return "\n".join(lines)


WEB_SEARCH_TOOL = CanonicalTool(
    name="web_search",
    description=(
        "Search the live web for current information. Use this when you "
        "need up-to-date facts, news, or anything not covered by your "
        "training data."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query.",
            },
            "top_n": {
                "type": "integer",
                "description": "Maximum number of results to return (default 5).",
            },
        },
        "required": ["query"],
    },
)


class BuiltinToolRegistry:
    """Lookup of builtin tool name -> (descriptor, executor).

    A second builtin tool is added by constructing another
    ``BuiltinToolEntry`` and including it in ``entries`` (or adding it to
    ``_DEFAULT_ENTRIES`` below for the process-wide default registry).
    """

    def __init__(self, entries: dict[str, BuiltinToolEntry] | None = None) -> None:
        self._entries: dict[str, BuiltinToolEntry] = (
            dict(entries) if entries is not None else dict(_DEFAULT_ENTRIES)
        )

    def get(self, name: str) -> BuiltinToolEntry | None:
        """Look up a registered builtin tool by name, or None if unknown."""
        return self._entries.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self._entries

    def tools(self) -> list[CanonicalTool]:
        """All registered tool descriptors, in registration order."""
        return [entry.tool for entry in self._entries.values()]

    def subset(self, names: Iterable[str]) -> BuiltinToolRegistry:
        """A new registry holding only the given (registered) tool names.

        Names not registered here are silently skipped rather than raising —
        callers build *names* per-turn from independent gates (e.g.
        ``web_search`` only when its service is configured), so an inactive
        tool simply not being in this registry's default set is the
        expected, non-error case.
        """
        return BuiltinToolRegistry(
            entries={
                name: entry for name in names if (entry := self._entries.get(name)) is not None
            }
        )


_DEFAULT_ENTRIES: dict[str, BuiltinToolEntry] = {
    "web_search": BuiltinToolEntry(tool=WEB_SEARCH_TOOL, executor=_web_search_executor),
}

# Process-wide default registry. Safe to share: it holds no per-request
# state — services are threaded through BuiltinToolContext at call time,
# never stored on the registry itself.
BUILTIN_TOOL_REGISTRY = BuiltinToolRegistry()
