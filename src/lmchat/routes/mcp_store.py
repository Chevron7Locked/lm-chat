# SPDX-License-Identifier: Apache-2.0
"""Admin routes for the MCP Store (Workstream C).

All endpoints are admin-only.  The store surfaces:
  - A curated catalog of known MCP servers (read-only, from catalog.py).
  - CRUD for the installed server registry (mcp_servers table).
  - Lightweight integration with McpHost for connected-status enrichment.

Route table
-----------
GET  /api/mcp-store/catalog           Curated catalog listing (no install side-effects).
GET  /api/mcp-store/servers           All installed servers with live connected status.
POST /api/mcp-store/servers           Install from catalog id OR custom BYO config.
DELETE /api/mcp-store/servers/{slug}  Disconnect, unregister, and delete.
"""
from __future__ import annotations

from typing import Annotated
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from lmchat.mcp import McpHost, McpServerConfig, split_secrets_for_transport
from lmchat.mcp.catalog import get_catalog, get_catalog_entry
from lmchat.routes._dependencies import require_admin
from lmchat.services.auth_service import User
from lmchat.services.mcp_server_store import McpServerSafeView, McpServerStore

router = APIRouter()


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class SecretSpec(BaseModel):
    key: str
    label: str
    required: bool


class CatalogEntryResponse(BaseModel):
    id: str
    name: str
    description: str
    transport: str
    command: str
    args: list[str]
    url: str
    secrets: list[SecretSpec]
    source: str
    trust: str


class McpServerResponse(BaseModel):
    id: int
    slug: str
    name: str
    transport: str
    command: str | None
    args: list[str] | None
    url: str | None
    secrets_set: list[str]
    enabled: bool
    source: str
    trust: str
    consented: bool
    connected: bool
    last_error: str | None = None
    tool_policy: list[str] = []


class InstallRequest(BaseModel):
    catalog_id: str | None = None
    slug: str | None = None
    name: str | None = None
    transport: str | None = None
    command: str | None = None
    args: list[str] | None = None
    url: str | None = None
    secrets: dict[str, str] | None = None
    tool_policy: list[str] | None = None


class PatchServerRequest(BaseModel):
    """Body for PATCH /api/mcp-store/servers/{slug}."""

    enabled: bool | None = None
    tool_policy: list[str] | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_store(request: Request) -> McpServerStore:
    return request.app.state.mcp_server_store  # type: ignore[no-any-return]


def _get_mcp_host(request: Request) -> McpHost:
    return request.app.state.mcp_host  # type: ignore[no-any-return]


def _catalog_to_response(entry: dict) -> CatalogEntryResponse:
    return CatalogEntryResponse(
        id=entry["id"],
        name=entry["name"],
        description=entry["description"],
        transport=entry["transport"],
        command=entry["command"],
        args=entry["args"],
        url=entry["url"],
        secrets=[SecretSpec(**s) for s in entry["secrets"]],
        source=entry["source"],
        trust=entry["trust"],
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/api/mcp-store/catalog", response_model=list[CatalogEntryResponse])
async def list_catalog(
    _user: Annotated[User, Depends(require_admin)],
) -> list[CatalogEntryResponse]:
    """Return the full curated MCP server catalog."""
    return [_catalog_to_response(entry) for entry in get_catalog()]


@router.get("/api/mcp-store/servers", response_model=list[McpServerResponse])
async def list_servers(
    request: Request,
    _user: Annotated[User, Depends(require_admin)],
) -> list[McpServerResponse]:
    """Return all installed MCP servers with live connected status."""
    store = _get_store(request)
    mcp_host = _get_mcp_host(request)
    connected_ids = set(mcp_host.connected_server_ids)

    views = await store.list_all()
    return [
        McpServerResponse(
            id=v.id,
            slug=v.slug,
            name=v.name,
            transport=v.transport,
            command=v.command,
            args=v.args,
            url=v.url,
            secrets_set=v.secrets_set,
            enabled=v.enabled,
            source=v.source,
            trust=v.trust,
            consented=v.consented,
            connected=v.slug in connected_ids,
            last_error=mcp_host.last_error(v.slug),
            tool_policy=v.tool_policy,
        )
        for v in views
    ]


@router.post("/api/mcp-store/servers", response_model=McpServerResponse)
async def install_server(
    request: Request,
    body: InstallRequest,
    _user: Annotated[User, Depends(require_admin)],
) -> McpServerResponse:
    """Install an MCP server from the catalog or as a custom BYO server.

    When ``catalog_id`` is provided the entry is looked up from the curated
    catalog and merged with any explicit overrides from ``body``.  For BYO
    installs ``slug``, ``name``, and ``transport`` are required.
    """
    store = _get_store(request)
    mcp_host = _get_mcp_host(request)

    # Resolve config: catalog entry takes precedence for structural fields;
    # body values override where explicitly provided.
    if body.catalog_id is not None:
        entry = get_catalog_entry(body.catalog_id)
        if entry is None:
            raise HTTPException(
                status_code=404,
                detail=f"Catalog entry not found: {body.catalog_id!r}",
            )
        slug = body.slug or entry["id"]
        name = body.name or entry["name"]
        transport = body.transport or entry["transport"]
        command = body.command or entry["command"] or None
        args = body.args if body.args is not None else entry["args"]
        url = body.url or entry["url"] or None
        source = entry["source"]
        trust = entry["trust"]
    else:
        # BYO: explicit fields required.
        slug = body.slug
        name = body.name
        transport = body.transport
        command = body.command
        args = body.args
        url = body.url
        source = "byo"
        trust = "byo"

    if not slug:
        raise HTTPException(status_code=400, detail="slug is required")
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if not transport:
        raise HTTPException(status_code=400, detail="transport is required")

    # Transport / URL validation (SSRF hardening).
    _valid_transports = {"stdio", "http", "sse"}
    if transport not in _valid_transports:
        raise HTTPException(
            status_code=400,
            detail="transport must be stdio, http, or sse",
        )
    if transport == "stdio":
        if not command:
            raise HTTPException(
                status_code=400,
                detail="command is required for stdio transport",
            )
    elif transport in {"http", "sse"}:
        _raw_url = url or ""
        _parsed_url = urlparse(_raw_url)
        if not _raw_url or _parsed_url.scheme not in ("http", "https"):
            raise HTTPException(
                status_code=400,
                detail="http/sse transport requires an http(s) url",
            )

    # Normalise empty strings to None for nullable columns.
    command = command or None
    url = url or None

    try:
        view = await store.install(
            slug=slug,
            name=name,
            transport=transport,
            command=command,
            args=args,
            url=url,
            secrets=body.secrets,
            source=source,
            trust=trust,
            tool_policy=body.tool_policy,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # Register in host _configs so it can be connected on demand (lazy; B1).
    # stdio secrets → child env; http/sse secrets → Authorization: Bearer header.
    _env, _headers = split_secrets_for_transport(transport, body.secrets or {})
    mcp_host._configs[slug] = McpServerConfig(
        server_id=slug,
        transport=transport,
        command=command or "",
        args=args or [],
        env=_env,
        url=url or "",
        headers=_headers,
    )

    return McpServerResponse(
        id=view.id,
        slug=view.slug,
        name=view.name,
        transport=view.transport,
        command=view.command,
        args=view.args,
        url=view.url,
        secrets_set=view.secrets_set,
        enabled=view.enabled,
        source=view.source,
        trust=view.trust,
        consented=view.consented,
        connected=False,
        last_error=mcp_host.last_error(slug),
        tool_policy=view.tool_policy,
    )


@router.delete("/api/mcp-store/servers/{slug}", status_code=204)
async def delete_server(
    slug: str,
    request: Request,
    _user: Annotated[User, Depends(require_admin)],
) -> None:
    """Disconnect, unregister, and delete an installed MCP server.

    Returns 404 when the slug is not installed.
    """
    store = _get_store(request)
    mcp_host = _get_mcp_host(request)

    # Verify the server exists before doing anything.
    view = await store.get(slug)
    if view is None:
        raise HTTPException(
            status_code=404, detail=f"MCP server not installed: {slug!r}"
        )

    # Disconnect from the host (no-op if not connected).
    await mcp_host.disconnect(slug)

    # Remove from host's config registry.
    mcp_host._configs.pop(slug, None)

    # Delete the DB row.
    await store.delete(slug)


def _view_to_response(
    view: McpServerSafeView,
    connected_ids: set[str],
    last_error: str | None = None,
) -> McpServerResponse:
    """Convert a :class:`McpServerSafeView` to a :class:`McpServerResponse`."""
    return McpServerResponse(
        id=view.id,
        slug=view.slug,
        name=view.name,
        transport=view.transport,
        command=view.command,
        args=view.args,
        url=view.url,
        secrets_set=view.secrets_set,
        enabled=view.enabled,
        source=view.source,
        trust=view.trust,
        consented=view.consented,
        connected=view.slug in connected_ids,
        last_error=last_error,
        tool_policy=view.tool_policy,
    )


@router.patch("/api/mcp-store/servers/{slug}", response_model=McpServerResponse)
async def patch_server(
    slug: str,
    request: Request,
    body: PatchServerRequest,
    _user: Annotated[User, Depends(require_admin)],
) -> McpServerResponse:
    """Update ``enabled`` and/or ``tool_policy`` for an installed server.

    When disabling, also disconnects the server from the MCP host so it
    stops receiving tool requests immediately.

    Returns 404 when the slug is not installed.
    """
    store = _get_store(request)
    mcp_host = _get_mcp_host(request)

    # Verify the server exists.
    existing = await store.get(slug)
    if existing is None:
        raise HTTPException(
            status_code=404, detail=f"MCP server not installed: {slug!r}"
        )

    view = None

    if body.enabled is not None:
        view = await store.update_enabled(slug, body.enabled)
        if not body.enabled:
            # Mirror disable → disconnect from host.
            await mcp_host.disconnect(slug)

    if body.tool_policy is not None:
        view = await store.update_tool_policy(slug, body.tool_policy)

    if view is None:
        # No fields were provided; return current state.
        view = await store._get_safe(slug)

    if view is None:
        raise HTTPException(
            status_code=404, detail=f"MCP server not installed: {slug!r}"
        )

    connected_ids = set(mcp_host.connected_server_ids)
    return _view_to_response(view, connected_ids, mcp_host.last_error(slug))


class _ToolEntry(BaseModel):
    name: str
    description: str
    denied: bool


class _ToolsResponse(BaseModel):
    slug: str
    connected: bool
    tools: list[_ToolEntry]
    error: str | None = None


@router.get(
    "/api/mcp-store/servers/{slug}/tools",
    response_model=_ToolsResponse,
)
async def get_server_tools(
    slug: str,
    request: Request,
    _user: Annotated[User, Depends(require_admin)],
) -> _ToolsResponse:
    """Return live tools for an installed server, annotated with denied status.

    Connects the server on demand (lazy; connect failures are non-fatal).
    Tools whose namespaced name appears in the row's ``tool_policy`` are
    returned with ``denied: true``.

    Returns 404 when the slug is not installed.
    """
    store = _get_store(request)
    mcp_host = _get_mcp_host(request)

    internal = await store.get(slug)
    if internal is None:
        raise HTTPException(
            status_code=404, detail=f"MCP server not installed: {slug!r}"
        )

    denied_set: set[str] = set(internal.tool_policy)

    # Connect on demand; failure is non-fatal.
    try:
        await mcp_host.connect(slug)
        connected = slug in set(mcp_host.connected_server_ids)
    except Exception as exc:  # noqa: BLE001
        return _ToolsResponse(
            slug=slug,
            connected=False,
            tools=[],
            error=mcp_host.last_error(slug) or str(exc),
        )

    if not connected:
        # last_error carries the real reason (e.g. the crashed server's own
        # stderr) when McpHost captured one; "connect returned False" is
        # only a last-resort fallback for the (rare) case it didn't.
        return _ToolsResponse(
            slug=slug,
            connected=False,
            tools=[],
            error=mcp_host.last_error(slug) or "connect returned False",
        )

    raw_tools = mcp_host.list_tools([slug])
    tools = [
        _ToolEntry(
            name=t.name,
            description=t.description or "",
            denied=t.name in denied_set,
        )
        for t in raw_tools
    ]

    return _ToolsResponse(slug=slug, connected=True, tools=tools)
