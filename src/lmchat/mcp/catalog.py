# SPDX-License-Identifier: Apache-2.0
"""Curated MCP server catalog for LMChat's MCP Store.

Each entry describes a known, pre-vetted MCP server that the admin can
install with one click.  The catalog is intentionally pure Python (no DB,
no I/O) so it can be imported cheaply and served from a GET endpoint with
zero latency.

Catalog entry schema
---------------------
    id          Stable slug (e.g. "github"). Never changes after publication.
    name        Human-readable display name.
    description One-sentence description shown in the UI.
    transport   "stdio" | "http" | "sse"
    command     Executable for stdio transport ("npx" or "uvx").
    args        CLI arguments passed to ``command``.
    url         Endpoint URL for http/sse transport; empty for stdio.
    secrets     List of {"key", "label", "required"} dicts describing env vars
                the server needs.  Values are NOT stored here; only metadata.
    source      Always "official" for curated catalog entries.
    trust       Always "curated" for curated catalog entries.
"""
from __future__ import annotations

CATALOG: list[dict] = [
    {
        "id": "github",
        "name": "GitHub",
        "description": "Read and write GitHub repos, issues, PRs, and more via the GitHub API.",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "url": "",
        "secrets": [
            {
                "key": "GITHUB_TOKEN",
                "label": "GitHub Personal Access Token",
                "required": True,
            }
        ],
        "source": "official",
        "trust": "curated",
    },
    {
        "id": "filesystem",
        "name": "Filesystem",
        "description": "Expose local filesystem paths to the model for reading and writing files.",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@rustmcp/rust-mcp-filesystem@latest"],
        "url": "",
        "secrets": [],
        "source": "official",
        "trust": "curated",
    },
    {
        "id": "fetch",
        "name": "Fetch",
        "description": "Fetch and parse web pages or raw URLs for the model to read.",
        "transport": "stdio",
        "command": "uvx",
        "args": ["mcp-server-fetch"],
        "url": "",
        "secrets": [],
        "source": "official",
        "trust": "curated",
    },
    {
        "id": "sequential-thinking",
        "name": "Sequential Thinking",
        "description": "Structured multi-step reasoning tool for complex problem decomposition.",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
        "url": "",
        "secrets": [],
        "source": "official",
        "trust": "curated",
    },
    {
        "id": "context7",
        "name": "Context7",
        "description": "Fetch up-to-date library docs and code examples from Context7's index.",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@upstash/context7-mcp"],
        "url": "",
        "secrets": [
            {
                "key": "CONTEXT7_API_KEY",
                "label": "Context7 API Key",
                "required": True,
            }
        ],
        "source": "official",
        "trust": "curated",
    },
    {
        "id": "deepwiki",
        "name": "DeepWiki",
        "description": "Query deep documentation and wiki-style knowledge bases via DeepWiki.",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "mcp-deepwiki@latest"],
        "url": "",
        "secrets": [],
        "source": "official",
        "trust": "curated",
    },
    {
        "id": "firecrawl",
        "name": "Firecrawl",
        "description": "Crawl and scrape websites into clean Markdown for model ingestion.",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "firecrawl-mcp"],
        "url": "",
        "secrets": [
            {
                "key": "FIRECRAWL_API_KEY",
                "label": "Firecrawl API Key",
                "required": True,
            },
            {
                "key": "FIRECRAWL_API_URL",
                "label": "Firecrawl API URL (self-hosted)",
                "required": False,
            },
        ],
        "source": "official",
        "trust": "curated",
    },
    {
        "id": "searxng",
        "name": "SearXNG",
        "description": "Privacy-respecting web search via a self-hosted SearXNG instance.",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "searxng-mcp"],
        "url": "",
        "secrets": [
            {
                "key": "SEARXNG_SERVER_URL",
                "label": "SearXNG Server URL",
                "required": True,
            }
        ],
        "source": "official",
        "trust": "curated",
    },
    {
        "id": "playwright",
        "name": "Playwright",
        "description": "Headless browser automation for web interaction and screenshot capture.",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@playwright/mcp@latest", "--headless"],
        "url": "",
        "secrets": [],
        "source": "official",
        "trust": "curated",
    },
    {
        "id": "wolfram",
        "name": "Wolfram Alpha",
        "description": "Query Wolfram Alpha for computational answers, math, and factual lookups.",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "wolfram-mcp"],
        "url": "",
        "secrets": [
            {
                "key": "WOLFRAM_APP_ID",
                "label": "Wolfram App ID",
                "required": True,
            }
        ],
        "source": "official",
        "trust": "curated",
    },
    {
        "id": "crawl4ai",
        "name": "Crawl4AI",
        "description": "Self-hosted crawler/scraper: URL to Markdown, plus crawl and screenshot.",
        # Crawl4AI's built-in MCP is SSE. LMChat's own host connects directly
        # (the API token below is sent as an Authorization: Bearer header).
        "transport": "sse",
        "command": "",
        "args": [],
        "url": "http://localhost:11235/mcp/sse",
        "secrets": [
            {
                "key": "CRAWL4AI_API_TOKEN",
                "label": "Crawl4AI API Token (sent as Bearer auth)",
                "required": True,
            }
        ],
        "source": "official",
        "trust": "curated",
    },
    {
        "id": "git",
        "name": "Git",
        "description": "Read, search, and manipulate local Git repositories.",
        "transport": "stdio",
        "command": "uvx",
        "args": ["mcp-server-git"],
        "url": "",
        "secrets": [],
        "source": "official",
        "trust": "curated",
    },
    {
        "id": "memory",
        "name": "Memory",
        "description": "Persistent knowledge-graph memory across sessions.",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-memory"],
        "url": "",
        "secrets": [],
        "source": "official",
        "trust": "curated",
    },
    {
        "id": "time",
        "name": "Time",
        "description": "Current time and timezone conversion utilities.",
        "transport": "stdio",
        "command": "uvx",
        "args": ["mcp-server-time"],
        "url": "",
        "secrets": [],
        "source": "official",
        "trust": "curated",
    },
    {
        "id": "notion",
        "name": "Notion",
        "description": "Query and edit Notion pages, databases, and blocks.",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@notionhq/notion-mcp-server"],
        "url": "",
        "secrets": [
            {
                "key": "NOTION_TOKEN",
                "label": "Notion Integration Token",
                "required": True,
            }
        ],
        "source": "official",
        "trust": "curated",
    },
    {
        "id": "supabase",
        "name": "Supabase",
        "description": "Query Supabase projects — database, docs, and edge functions (read-only).",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@supabase/mcp-server-supabase@latest", "--read-only"],
        "url": "",
        "secrets": [
            {
                "key": "SUPABASE_ACCESS_TOKEN",
                "label": "Supabase Personal Access Token",
                "required": True,
            }
        ],
        "source": "official",
        "trust": "curated",
    },
    {
        "id": "sentry",
        "name": "Sentry",
        "description": "Inspect Sentry issues, events, and projects.",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@sentry/mcp-server@latest"],
        "url": "",
        "secrets": [
            {
                "key": "SENTRY_ACCESS_TOKEN",
                "label": "Sentry User Auth Token",
                "required": True,
            },
            {
                "key": "SENTRY_HOST",
                "label": "Sentry Host (self-hosted only, e.g. sentry.example.com)",
                "required": False,
            },
        ],
        "source": "official",
        "trust": "curated",
    },
    {
        "id": "figma",
        "name": "Figma",
        "description": "Pull Figma file and frame layout/design data into the model (Framelink).",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "figma-developer-mcp", "--stdio"],
        "url": "",
        "secrets": [
            {
                "key": "FIGMA_API_KEY",
                "label": "Figma Personal Access Token",
                "required": True,
            }
        ],
        "source": "official",
        "trust": "curated",
    },
    {
        "id": "brave-search",
        "name": "Brave Search",
        "description": "Web, local, image, video, and news search via the Brave Search API.",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@brave/brave-search-mcp-server", "--transport", "stdio"],
        "url": "",
        "secrets": [
            {
                "key": "BRAVE_API_KEY",
                "label": "Brave Search API Key",
                "required": True,
            }
        ],
        "source": "official",
        "trust": "curated",
    },
    {
        "id": "tavily",
        "name": "Tavily",
        "description": "AI-optimized web search and page extraction via the Tavily API.",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "tavily-mcp@latest"],
        "url": "",
        "secrets": [
            {
                "key": "TAVILY_API_KEY",
                "label": "Tavily API Key",
                "required": True,
            }
        ],
        "source": "official",
        "trust": "curated",
    },
    {
        "id": "exa",
        "name": "Exa Search",
        "description": "Neural/semantic web search and content retrieval via the Exa API.",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "exa-mcp-server"],
        "url": "",
        "secrets": [
            {
                "key": "EXA_API_KEY",
                "label": "Exa API Key",
                "required": True,
            }
        ],
        "source": "official",
        "trust": "curated",
    },
    {
        "id": "postgres",
        "name": "Postgres Pro",
        "description": "Query, inspect, and tune a PostgreSQL database (read-only).",
        "transport": "stdio",
        "command": "uvx",
        "args": ["postgres-mcp", "--access-mode=restricted"],
        "url": "",
        "secrets": [
            {
                "key": "DATABASE_URI",
                "label": "Postgres Connection URI (postgresql://user:pass@host:5432/db)",
                "required": True,
            }
        ],
        "source": "official",
        "trust": "curated",
    },
    {
        "id": "slack",
        "name": "Slack",
        "description": "Read and post to Slack channels, DMs, and threads.",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "slack-mcp-server@latest", "--transport", "stdio"],
        "url": "",
        "secrets": [
            {
                "key": "SLACK_MCP_XOXP_TOKEN",
                "label": "Slack User OAuth Token (xoxp-…)",
                "required": True,
            }
        ],
        "source": "official",
        "trust": "curated",
    },
]

# Index by id for O(1) lookup.
_CATALOG_INDEX: dict[str, dict] = {entry["id"]: entry for entry in CATALOG}


def get_catalog() -> list[dict]:
    """Return the full curated catalog.

    Returns:
        List of catalog entry dicts (not copies — treat as read-only).
    """
    return CATALOG


def get_catalog_entry(id: str) -> dict | None:  # noqa: A002
    """Return a single catalog entry by stable id, or None.

    Args:
        id: The stable slug (e.g. ``"github"``).

    Returns:
        Catalog entry dict or ``None`` when not found.
    """
    return _CATALOG_INDEX.get(id)
