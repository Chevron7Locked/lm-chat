# SPDX-License-Identifier: Apache-2.0
"""Live-backend integration tests for the MCP integrations routes.

Uses the existing uvicorn fixture from conftest.py — the stub LM Studio
handles model/chat calls; the integrations service uses its own DB table.

Covers:
- GET /api/integrations/available → 401 unauthenticated
- GET /api/integrations/available → 200 authenticated user (empty list)
- PUT /api/integrations/available → 401 unauthenticated
- PUT /api/integrations/available → 403 non-admin
- Admin sets list via PUT; GET reflects new state
- PUT with empty array clears the DB list; GET returns empty
- Full lifecycle: admin sets 2 entries → user retrieves them
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tests.integration.conftest import (
    register_admin_and_login,
    register_and_login,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# GET /api/integrations/available
# ---------------------------------------------------------------------------


async def test_list_available_unauth_401(client: httpx.AsyncClient) -> None:
    """Unauthenticated GET → 401."""
    resp = await client.get("/api/integrations/available")
    assert resp.status_code == 401


async def test_list_available_auth_empty(client: httpx.AsyncClient) -> None:
    """Authenticated user can GET the list; empty by default."""
    _, cookie = await register_and_login(client)
    resp = await client.get(
        "/api/integrations/available",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200
    # DB is empty and no env default in the integration test server.
    assert isinstance(resp.json(), list)


# ---------------------------------------------------------------------------
# PUT /api/integrations/available
# ---------------------------------------------------------------------------


async def test_put_available_unauth_401(client: httpx.AsyncClient) -> None:
    """Unauthenticated PUT → 401."""
    resp = await client.put(
        "/api/integrations/available",
        data={"entries": json.dumps([])},
    )
    assert resp.status_code == 401


async def test_put_available_non_admin_403(client: httpx.AsyncClient) -> None:
    """Non-admin PUT → 403."""
    _, cookie = await register_and_login(client)
    resp = await client.put(
        "/api/integrations/available",
        data={"entries": json.dumps([])},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Full admin lifecycle
# ---------------------------------------------------------------------------


async def test_admin_sets_list_user_retrieves(
    client: httpx.AsyncClient,
    db_engine: Any,
) -> None:
    """Admin sets 2 entries; regular user retrieves them via GET."""
    # Set up admin.
    _, admin_cookie = await register_admin_and_login(client, db_engine)
    # Set up regular user.
    _, user_cookie = await register_and_login(client)

    entries = [
        {"value": "mcp/searxng", "sort_order": 0},
        {"value": "mcp/filesystem", "sort_order": 1},
    ]

    # Admin writes.
    put_resp = await client.put(
        "/api/integrations/available",
        data={"entries": json.dumps(entries)},
        headers={"Cookie": f"lmchat_session={admin_cookie}"},
    )
    assert put_resp.status_code == 200
    put_data = put_resp.json()
    assert len(put_data) == 2
    values_set = {e["value"] for e in put_data}
    assert values_set == {"mcp/searxng", "mcp/filesystem"}

    # User reads.
    get_resp = await client.get(
        "/api/integrations/available",
        headers={"Cookie": f"lmchat_session={user_cookie}"},
    )
    assert get_resp.status_code == 200
    get_data = get_resp.json()
    assert len(get_data) == 2
    values_get = {e["value"] for e in get_data}
    assert values_get == {"mcp/searxng", "mcp/filesystem"}


async def test_admin_clears_list(
    client: httpx.AsyncClient,
    db_engine: Any,
) -> None:
    """Admin can clear the list with an empty PUT; GET returns empty."""
    _, admin_cookie = await register_admin_and_login(client, db_engine)

    # Populate first.
    await client.put(
        "/api/integrations/available",
        data={"entries": json.dumps([{"value": "mcp/temporary", "sort_order": 0}])},
        headers={"Cookie": f"lmchat_session={admin_cookie}"},
    )

    # Then clear.
    clear_resp = await client.put(
        "/api/integrations/available",
        data={"entries": json.dumps([])},
        headers={"Cookie": f"lmchat_session={admin_cookie}"},
    )
    assert clear_resp.status_code == 200
    assert clear_resp.json() == []

    # GET reflects empty state.
    get_resp = await client.get(
        "/api/integrations/available",
        headers={"Cookie": f"lmchat_session={admin_cookie}"},
    )
    assert get_resp.status_code == 200
    assert get_resp.json() == []


async def test_response_shape(
    client: httpx.AsyncClient,
    db_engine: Any,
) -> None:
    """Response entries include all required fields per IntegrationEntry schema."""
    _, admin_cookie = await register_admin_and_login(client, db_engine)

    entries = [{"value": "mcp/shape-test", "sort_order": 0}]
    put_resp = await client.put(
        "/api/integrations/available",
        data={"entries": json.dumps(entries)},
        headers={"Cookie": f"lmchat_session={admin_cookie}"},
    )
    assert put_resp.status_code == 200
    data = put_resp.json()
    assert len(data) == 1
    entry = data[0]
    # Verify all required fields are present.
    for field in ("id", "value", "sort_order", "created_at", "updated_at"):
        assert field in entry, f"missing field: {field}"
    assert entry["value"] == "mcp/shape-test"
    assert entry["id"] > 0
    assert entry["sort_order"] == 0
