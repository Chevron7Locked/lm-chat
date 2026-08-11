# SPDX-License-Identifier: Apache-2.0
"""Capability-aware legend appended to the assembled system prompt.

LM Chat ships its own modes, slash commands, and per-chat tools, but the
model has no way to know that unless it's told. This module renders a
compact reference block — a menu the model self-navigates, not a script it
gets pushed through.

Two kinds of capability, and the distinction is load-bearing:

- **OFFER** — user-invoked; the model can only *suggest* these, never call
  them: the sub-agent preset modes (mirrors ``web/src/lib/presets.ts``) and
  the chat-feature slash commands.
- **DO** — the model calls these directly: whatever tools are enabled for
  the current chat.

Every row stays one line. This block rides on every turn, so it stays
cheap and factual — no "you should proactively ..." language. An earlier
incident showed that eager injected directives measurably degrade local
model reasoning; this is a menu, not a nudge.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from lmchat.mcp.catalog import get_catalog_entry

# Mirrors the first sentence of builtin_tools.WEB_SEARCH_TOOL's tool-call
# description. Kept as a literal (not sliced from that constant) so a
# future rewrite of the tool-call description doesn't silently reshape
# this block, and vice versa.
_WEB_SEARCH_DESCRIPTION = "Search the live web for current information."

_NONE_ENABLED_LINE = (
    "- (none enabled — the user can add tools with the composer's tool picker)"
)


@dataclass(frozen=True, slots=True)
class CapabilityRow:
    """One legend line: a slash command plus a terse, situation-first
    description of when it fits."""

    command: str
    description: str


# OFFER — sub-agent preset modes. Order and wording mirror the locked
# legend rendering; commands match the slashCmd values in
# web/src/lib/presets.ts. "general" has no distinct slash worth suggesting
# here — it's the silent default, not a mode to reach for.
MODES: tuple[CapabilityRow, ...] = (
    CapabilityRow(
        "/research", "deep, multi-step research; runs in a clean-context sub-agent"
    ),
    CapabilityRow("/architect", "design a system or plan before building"),
    CapabilityRow("/code", "focused coding in a clean-context sub-agent"),
    CapabilityRow("/write", "long-form / creative writing"),
    CapabilityRow("/analyze", "structured data / document analysis"),
)

# OFFER — stable chat-feature slash commands.
FEATURES: tuple[CapabilityRow, ...] = (
    CapabilityRow("/compare", "run two models on the same prompt, side by side"),
    CapabilityRow("/compact", "condense a long conversation to reclaim context"),
    CapabilityRow("/prompt", "insert a saved prompt from the library"),
    CapabilityRow("/memory <text>", "pin a durable fact to remember"),
)


def _tool_description(tool_id: str, catalog: Callable[[str], dict | None]) -> str:
    """Friendly one-liner for an enabled tool id, falling back to its slug.

    ``web_search`` is an app-executed builtin, not an MCP catalog entry, so
    it's special-cased first. Everything else is looked up by stripping the
    ``mcp/`` namespace prefix and querying the curated MCP Store catalog
    (``lmchat.mcp.catalog``) — the same catalog curated ``mcp/<slug>``
    integration ids are named after. A custom/BYO id with no catalog entry
    falls back to the prefix-stripped slug (matching the display label).
    """
    if tool_id == "web_search":
        return _WEB_SEARCH_DESCRIPTION
    entry = catalog(tool_id.removeprefix("mcp/"))
    description = entry.get("description") if entry else None
    return description or tool_id.removeprefix("mcp/")


def render_capability_legend(
    *,
    enabled_tools: list[str],
    catalog: Callable[[str], dict | None] = get_catalog_entry,
) -> str:
    """Render the ``[Capabilities]`` reference block.

    Args:
        enabled_tools: Tool ids enabled for this turn (e.g.
            ``["mcp/searxng"]``). Deduplicated while preserving order;
            empty renders the none-enabled fallback line.
        catalog: ``id -> catalog entry dict`` lookup, defaulting to the
            real MCP Store catalog (``lmchat.mcp.catalog.get_catalog_entry``).
            Overridable in tests.

    Returns:
        The self-contained legend block, ready to concatenate onto the
        system prompt.
    """
    lines = [
        "[Capabilities]",
        "Reference — reach for one of these only when it clearly fits the "
        "task; most turns need none. Compose freely.",
        "",
        "Suggest to the user (they run these):",
    ]
    for row in (*MODES, *FEATURES):
        lines.append(f"- {row.command} — {row.description}")
    lines.append("")
    lines.append("Tools you can call directly:")
    deduped_tools = list(dict.fromkeys(enabled_tools))
    if deduped_tools:
        for tool_id in deduped_tools:
            display = tool_id.removeprefix("mcp/")
            lines.append(f"- {display} — {_tool_description(tool_id, catalog)}")
    else:
        lines.append(_NONE_ENABLED_LINE)
    lines.append("")
    lines.append(
        "- Memory is automatic: LM Chat distills durable facts from the "
        "conversation on its own and recalls them in later chats. There is "
        "no save-memory tool to call — if the user asks you to remember "
        "something, just acknowledge it; the app persists it."
    )
    lines.append(
        "- The LM Chat guide is at /docs; relevant sections are added to "
        "your context automatically when you ask about the app."
    )
    return "\n".join(lines)
