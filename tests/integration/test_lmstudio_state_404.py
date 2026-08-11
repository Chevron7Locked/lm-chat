# SPDX-License-Identifier: Apache-2.0
"""404 regression guard — former ~/.lmstudio/ write/read surfaces must be gone.

The plugin management write path (routes/plugins.py + services/
plugins_service.py) and the McpService read path (routes/mcp.py + services/
mcp_service.py) were removed. Both paths accessed ~/.lmstudio/mcp.json directly,
violating the HTTP-first rule (all LM Studio access must go over HTTP, never the
local filesystem).

This test file asserts FOREVER that those routes return 404 — meaning the
route does not exist at all, not that it exists but requires auth (401). The
test pins the architectural decision in CI so that accidental re-implementation
of either surface immediately reddens the test suite.

Routes asserted 404 (not 401, not 403 — genuinely absent):
  Plugin write path:
    PUT /api/plugins
    POST /api/plugins
    GET /api/plugins
    GET /api/plugins/<id>
    DELETE /api/plugins/<id>

  McpService read path:
    GET /api/mcp/servers
    GET /api/mcp
    GET /api/mcp/anything

Test uses the same live-backend integration fixture as the rest of the
integration suite (tests/integration/conftest.py). A real authenticated session
is created to confirm the 404 is not masked by a 401.
"""
from __future__ import annotations

import httpx
import pytest

from tests.integration.conftest import register_and_login


@pytest.mark.asyncio
async def test_plugins_put_404(client: httpx.AsyncClient) -> None:
    """PUT /api/plugins must return 404 (route does not exist)."""
    _, cookie = await register_and_login(client)
    resp = await client.put(
        "/api/plugins",
        headers={"Cookie": f"lmchat_session={cookie}"},
        json={},
    )
    assert resp.status_code == 404, (
        f"PUT /api/plugins returned {resp.status_code}; expected 404. "
        "The plugin write surface was removed by P11a and must not be re-added."
    )


@pytest.mark.asyncio
async def test_plugins_post_404(client: httpx.AsyncClient) -> None:
    """POST /api/plugins must return 404 (route does not exist)."""
    _, cookie = await register_and_login(client)
    resp = await client.post(
        "/api/plugins",
        headers={"Cookie": f"lmchat_session={cookie}"},
        json={},
    )
    assert resp.status_code == 404, (
        f"POST /api/plugins returned {resp.status_code}; expected 404."
    )


@pytest.mark.asyncio
async def test_plugins_get_404(client: httpx.AsyncClient) -> None:
    """GET /api/plugins must return 404 (route does not exist)."""
    _, cookie = await register_and_login(client)
    resp = await client.get(
        "/api/plugins",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 404, (
        f"GET /api/plugins returned {resp.status_code}; expected 404."
    )


@pytest.mark.asyncio
async def test_plugins_get_by_id_404(client: httpx.AsyncClient) -> None:
    """GET /api/plugins/<id> must return 404 (route does not exist)."""
    _, cookie = await register_and_login(client)
    resp = await client.get(
        "/api/plugins/some-plugin-id",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 404, (
        f"GET /api/plugins/some-plugin-id returned {resp.status_code}; expected 404."
    )


@pytest.mark.asyncio
async def test_plugins_delete_404(client: httpx.AsyncClient) -> None:
    """DELETE /api/plugins/<id> must return 404 (route does not exist)."""
    _, cookie = await register_and_login(client)
    resp = await client.delete(
        "/api/plugins/some-plugin-id",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 404, (
        f"DELETE /api/plugins/some-plugin-id returned {resp.status_code}; expected 404."
    )


@pytest.mark.asyncio
async def test_mcp_servers_get_404(client: httpx.AsyncClient) -> None:
    """GET /api/mcp/servers must return 404 (route does not exist)."""
    _, cookie = await register_and_login(client)
    resp = await client.get(
        "/api/mcp/servers",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 404, (
        f"GET /api/mcp/servers returned {resp.status_code}; expected 404. "
        "The McpService read path was removed by P11a and must not be re-added."
    )


@pytest.mark.asyncio
async def test_mcp_root_get_404(client: httpx.AsyncClient) -> None:
    """GET /api/mcp must return 404 (route does not exist)."""
    _, cookie = await register_and_login(client)
    resp = await client.get(
        "/api/mcp",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 404, (
        f"GET /api/mcp returned {resp.status_code}; expected 404."
    )


@pytest.mark.asyncio
async def test_mcp_arbitrary_path_404(client: httpx.AsyncClient) -> None:
    """GET /api/mcp/<anything> must return 404 (route does not exist)."""
    _, cookie = await register_and_login(client)
    resp = await client.get(
        "/api/mcp/tools/list",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 404, (
        f"GET /api/mcp/tools/list returned {resp.status_code}; expected 404."
    )
