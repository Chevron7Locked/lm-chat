# SPDX-License-Identifier: Apache-2.0
"""MCP integrations list routes for lm-chat.

Routes
------
GET  /api/integrations/available  — any authenticated user.
PUT  /api/integrations/available  — admin only (form-encoded body).

Wire contract
-------------
GET returns ``list[IntegrationEntry]`` (JSON array).
PUT accepts ``entries: str`` (JSON-encoded list of ``{value, sort_order}``
objects) as a URL-encoded form field, mirroring the existing form-encoded
mutation invariant used across the route layer (chats, prompts, etc.).
Returns the new ``list[IntegrationEntry]``.

Auth matrix
-----------
- Unauthenticated GET  → 401
- Non-admin GET        → 200 (any authenticated user sees the list)
- Unauthenticated PUT  → 401
- Non-admin PUT        → 403
- Admin PUT            → 200 (replaces list)
- Admin PUT > 30/min   → 429 + Retry-After
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Final

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from pydantic import ValidationError

from lmchat.logging import get_logger
from lmchat.routes._dependencies import (
    admin_rate_limit,
    require_admin,
    require_user,
)
from lmchat.services.auth_service import User
from lmchat.services.integrations_service import (
    IntegrationEntry,
    IntegrationSetEntry,
    IntegrationsService,
    _synthetic_id,
)

log = get_logger(__name__)

router = APIRouter()

_HTTP_400: Final[int] = 400
_HTTP_422: Final[int] = 422


# ---------------------------------------------------------------------------
# Dependency
# ---------------------------------------------------------------------------


def get_integrations_service_dep(request: Request) -> IntegrationsService:
    """Return the ``IntegrationsService`` from ``app.state.integrations_service``.

    Raises ``RuntimeError`` if unset — the lifespan must register it.
    Tests bypassing the lifespan must inject via ``app.dependency_overrides``.

    Args:
        request: The incoming FastAPI ``Request``.

    Returns:
        The singleton :class:`~lmchat.services.integrations_service.IntegrationsService`.

    Raises:
        RuntimeError: If ``app.state.integrations_service`` is unset.
    """
    svc = getattr(request.app.state, "integrations_service", None)
    if svc is None:
        raise RuntimeError(
            "app.state.integrations_service is unset — the FastAPI lifespan did not "
            "run, and no dependency_overrides entry exists for "
            "get_integrations_service_dep. Tests bypassing the lifespan must register "
            "an override; production code paths must use the lifespan."
        )
    return svc  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(
    "/api/integrations/available",
    response_model=list[IntegrationEntry],
    summary="List available MCP integrations",
    description=(
        "Returns the admin-supplied list of available MCP integration IDs, "
        "merged with any enabled MCP-Store-installed servers so that 1-click "
        "installs surface as composer pills automatically. "
        "Any authenticated user may call this endpoint to populate the chat "
        "composer's integrations picker. "
        "Source is DB table ``mcp_integrations_list`` when populated; "
        "falls back to ``LM_CHAT_AVAILABLE_INTEGRATIONS`` env var otherwise. "
        "Store-installed servers are appended after curated entries and are "
        "never pre-selected (``enabled_by_default=False``)."
    ),
    tags=["integrations"],
)
async def list_available(
    request: Request,
    svc: IntegrationsService = Depends(get_integrations_service_dep),
    _user: User = Depends(require_user),
) -> list[IntegrationEntry]:
    """Return the current list of available MCP integration IDs.

    Merges the curated integrations list with any enabled MCP-Store servers
    so that a 1-click install surfaces as a composer pill immediately, without
    requiring a separate admin step on the integrations page.

    Merge rules:
    - Curated entries (DB or env fallback) come first, in their existing
      order. These are always ``source="lmstudio"`` — LM Studio's own
      ``mcp.json`` servers, dispatched server-side (native endpoint mode).
    - For each store server that is ``enabled`` (and ``consented``), a
      synthetic ``source="store"`` entry for ``mcp/<slug>`` is appended
      with ``enabled_by_default=False`` (opt-in).
    - Store entries are NOT deduplicated against curated entries by value:
      the same ``mcp/<slug>`` can legitimately exist as BOTH an
      ``lmstudio`` entry and a ``store`` entry — they are different
      runtimes (LM Studio's native MCP dispatch vs. LMChat's client-side
      MCP-Store agentic host) for the same tool slug, and the chat
      composer selects the correct one by active endpoint mode (native vs.
      compat/cloud). Only duplicate store servers for the same slug are
      collapsed (within the store's own results).
    - Store entries are appended in slug-sorted order for stability.
    - If the store is unavailable on ``app.state`` the function degrades
      gracefully and returns just the curated list.

    Args:
        request: The incoming FastAPI ``Request`` (used to reach app.state).
        svc:     The integrations service dependency.
        _user:   The authenticated user (auth gate; value unused).

    Returns:
        List of :class:`~lmchat.services.integrations_service.IntegrationEntry`.
    """
    curated = await svc.list_available()

    # Graceful degradation: if the MCP-Store isn't wired up (e.g. some
    # test contexts that only override integrations_service), return just
    # the curated list rather than crashing.
    store = getattr(request.app.state, "mcp_server_store", None)
    if store is None:
        return curated

    try:
        store_servers = await store.list_all()
    except Exception:  # noqa: BLE001
        log.warning("integrations.list_available.store_read_failed")
        return curated

    # Track values already emitted as *store* entries so we only dedupe
    # WITHIN the store's own results (e.g. a defensive guard against the
    # store returning the same slug twice). We deliberately do NOT dedupe
    # against curated values here — see the merge-rules docstring above:
    # `(source, value)` is the real identity, not the bare value.
    seen_store_values: set[str] = set()

    # Determine the next sort_order after the curated block so store-added
    # entries sort consistently after them.
    next_sort = (max((e.sort_order for e in curated), default=-1) + 1)
    _epoch = datetime(1970, 1, 1, tzinfo=UTC)

    extra: list[IntegrationEntry] = []
    for server in sorted(store_servers, key=lambda s: s.slug):
        if not server.enabled:
            continue
        # consented is always True on install (install=consent in McpServerStore)
        # but guard it explicitly in case the field evolves.
        if not getattr(server, "consented", True):
            continue
        value = f"mcp/{server.slug}"
        if value in seen_store_values:
            continue
        seen_store_values.add(value)
        extra.append(
            IntegrationEntry(
                # Namespace the synthetic id derivation by system
                # ("store:" prefix) so a store entry never collides with
                # a same-slug curated synthetic entry (which hashes the
                # bare value) — distinct ids keep React keys distinct.
                id=_synthetic_id(f"store:{value}"),
                value=value,
                sort_order=next_sort,
                enabled_by_default=False,
                created_at=_epoch,
                updated_at=_epoch,
                source="store",
            )
        )
        next_sort += 1

    return curated + extra


@router.put(
    "/api/integrations/available",
    response_model=list[IntegrationEntry],
    summary="Set the MCP integrations list (admin)",
    description=(
        "Atomically replaces the DB-backed integrations list. "
        "Body: form-encoded ``entries`` field containing a JSON array of "
        "``{value: string, sort_order: int}`` objects. "
        "An empty array clears the DB list (falling back to env var). "
        "Admin-only; subject to the admin rate limit."
    ),
    tags=["integrations"],
)
async def set_available(
    entries: str = Form(
        ...,
        description=(
            "JSON-encoded list of {value, sort_order} objects. "
            "Example: '[{\"value\":\"mcp/searxng\",\"sort_order\":0}]'"
        ),
    ),
    svc: IntegrationsService = Depends(get_integrations_service_dep),
    _admin: User = Depends(require_admin),
    _rate: None = Depends(admin_rate_limit),
) -> list[IntegrationEntry]:
    """Replace the DB integrations list (admin only).

    Args:
        entries: JSON-encoded list of ``{value, sort_order}`` objects.
        svc:     The integrations service dependency.
        _admin:  The authenticated admin user (auth gate; value unused).
        _rate:   Admin rate-limit dependency (gate only; value unused).

    Returns:
        The new list of :class:`~lmchat.services.integrations_service.IntegrationEntry`.

    Raises:
        HTTPException: 400 if ``entries`` is not valid JSON.
        HTTPException: 422 if any entry fails Pydantic validation.
    """
    try:
        raw = json.loads(entries)
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(
            status_code=_HTTP_400,
            detail=f"entries must be a JSON array: {exc}",
        ) from exc

    if not isinstance(raw, list):
        raise HTTPException(
            status_code=_HTTP_400,
            detail="entries must be a JSON array (got non-list)",
        )

    try:
        parsed = [IntegrationSetEntry.model_validate(item) for item in raw]
    except ValidationError as exc:
        raise HTTPException(
            status_code=_HTTP_422,
            detail=f"invalid entry shape: {exc}",
        ) from exc

    log.info(
        "integrations.set_available",
        count=len(parsed),
        values=[e.value for e in parsed],
    )
    return await svc.set_available(parsed)
