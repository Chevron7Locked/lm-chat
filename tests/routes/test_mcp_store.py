# SPDX-License-Identifier: Apache-2.0
"""Integration tests for the MCP Store admin routes (Workstream C).

Covers:
- GET /api/mcp-store/catalog: 200 admin; 401 unauthenticated; 403 non-admin.
- GET /api/mcp-store/servers: 200 admin with empty list.
- POST /api/mcp-store/servers: 200 install from catalog; 404 unknown catalog_id.
- DELETE /api/mcp-store/servers/{slug}: 204 success; 404 not installed.

Tests use a real SQLite DB + full lifespan (mirrors test_providers.py), with
mcp_host and mcp_server_store mocked on app.state for fast, isolated runs.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import metadata
from lmchat.middleware._bucket_store import InMemoryBucketStore
from lmchat.routes._dependencies import get_models_service_dep
from lmchat.services.auth_service import _reset_dummy_hash_cache
from lmchat.services.mcp_server_store import McpServerInternalView, McpServerSafeView
from lmchat.utils.hashing import hash_password

_LOW_N: int = 2**10


# ---------------------------------------------------------------------------
# App / DB helpers (mirrors test_providers.py)
# ---------------------------------------------------------------------------


def _make_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: ANN401
    from lmchat.app import create_app
    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod

    db_url = f"sqlite+aiosqlite:///{tmp_path}/mcp_store_route_test.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    monkeypatch.setenv("LM_STUDIO_BASE_URL", "http://env.example:1234")
    monkeypatch.setenv("LM_STUDIO_API_KEY", "env-api-key")
    monkeypatch.setenv("LM_STUDIO_DEFAULT_MODEL", "env-model")

    get_settings.cache_clear()
    engine_mod.dispose_engine()
    _reset_dummy_hash_cache()

    app = create_app()

    stub_models = AsyncMock()
    stub_models.list_loaded = AsyncMock(return_value=[])
    stub_models.refresh = AsyncMock(return_value=None)
    app.dependency_overrides[get_models_service_dep] = lambda: stub_models

    return app


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch):
    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod

    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    get_settings.cache_clear()
    _reset_dummy_hash_cache()
    yield
    get_settings.cache_clear()
    _reset_dummy_hash_cache()
    engine_mod.dispose_engine()


@pytest.fixture()
def test_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    app = _make_app(tmp_path, monkeypatch)

    # Mock mcp_host with the interface the routes need.
    mock_host = MagicMock()
    mock_host.connected_server_ids = []
    mock_host._configs = {}
    mock_host.disconnect = AsyncMock(return_value=None)
    mock_host.last_error = MagicMock(return_value=None)

    # Mock mcp_server_store for fast, isolated tests.
    mock_store = AsyncMock()
    mock_store.list_all = AsyncMock(return_value=[])
    mock_store.install = AsyncMock(
        return_value=McpServerSafeView(
            id=1,
            slug="github",
            name="GitHub",
            transport="stdio",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-github"],
            url=None,
            secrets_set=["GITHUB_TOKEN"],
            enabled=True,
            source="official",
            trust="curated",
            consented=True,
            connected=False,
            tool_policy=[],
        )
    )
    mock_store.get = AsyncMock(return_value=None)
    mock_store.delete = AsyncMock(return_value=None)
    mock_store.update_enabled = AsyncMock(return_value=None)
    mock_store.update_tool_policy = AsyncMock(return_value=None)
    mock_store._get_safe = AsyncMock(return_value=None)

    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.mcp_host = mock_host  # type: ignore[attr-defined]
        client.app.state.mcp_server_store = mock_store  # type: ignore[attr-defined]
        yield client


async def _engine_for(tmp_path: Path) -> AsyncEngine:
    db_url = f"sqlite+aiosqlite:///{tmp_path}/mcp_store_route_test.db"
    eng = create_async_engine(db_url, pool_pre_ping=True)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return eng


async def _insert_user(
    tmp_path: Path,
    username: str,
    is_admin: bool = False,
) -> int:
    pw_hash = hash_password("test-pw", n=_LOW_N, r=8, p=1)
    eng = await _engine_for(tmp_path)
    try:
        async with eng.begin() as conn:
            await conn.execute(
                text(
                    "INSERT OR IGNORE INTO users (username, password_hash, is_admin)"
                    " VALUES (:u, :pw, :admin)"
                ),
                {"u": username, "pw": pw_hash, "admin": 1 if is_admin else 0},
            )
            row = (
                await conn.execute(
                    text("SELECT id FROM users WHERE username = :u"), {"u": username}
                )
            ).fetchone()
            return int(row[0])  # type: ignore[index]
    finally:
        await eng.dispose()


def _login(client: TestClient, username: str, password: str = "test-pw") -> None:
    resp = client.post(
        "/api/auth/login",
        data={"username": username, "password": password},
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_catalog_unauthenticated_401(test_client: TestClient) -> None:
    """GET /api/mcp-store/catalog without auth returns 401."""
    resp = test_client.get("/api/mcp-store/catalog")
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_catalog_list_200(
    tmp_path: Path, test_client: TestClient
) -> None:
    """GET /api/mcp-store/catalog returns 200 with a list of entries for admin."""
    await _insert_user(tmp_path, "alice", is_admin=True)
    _login(test_client, "alice")

    resp = test_client.get("/api/mcp-store/catalog")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) > 0, "Catalog should not be empty"

    # Spot-check first entry has required fields.
    first = data[0]
    for field in (
        "id", "name", "description", "transport", "command",
        "args", "secrets", "source", "trust",
    ):
        assert field in first, f"Missing field {field!r} in catalog entry"


@pytest.mark.asyncio
async def test_catalog_non_admin_403(
    tmp_path: Path, test_client: TestClient
) -> None:
    """GET /api/mcp-store/catalog returns 403 for non-admin."""
    await _insert_user(tmp_path, "bob", is_admin=False)
    _login(test_client, "bob")

    resp = test_client.get("/api/mcp-store/catalog")
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_servers_list_empty_200(
    tmp_path: Path, test_client: TestClient
) -> None:
    """GET /api/mcp-store/servers returns 200 with empty list when nothing installed."""
    await _insert_user(tmp_path, "alice", is_admin=True)
    _login(test_client, "alice")

    resp = test_client.get("/api/mcp-store/servers")
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


@pytest.mark.asyncio
async def test_install_from_catalog_200(
    tmp_path: Path, test_client: TestClient
) -> None:
    """POST /api/mcp-store/servers with catalog_id returns 200 and installed entry."""
    await _insert_user(tmp_path, "alice", is_admin=True)
    _login(test_client, "alice")

    resp = test_client.post(
        "/api/mcp-store/servers",
        json={
            "catalog_id": "github",
            "secrets": {"GITHUB_TOKEN": "ghp_test_token"},
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["slug"] == "github"
    assert data["name"] == "GitHub"
    assert "GITHUB_TOKEN" in data["secrets_set"]
    assert data["connected"] is False


@pytest.mark.asyncio
async def test_install_missing_catalog_id_404(
    tmp_path: Path, test_client: TestClient
) -> None:
    """POST /api/mcp-store/servers with an unknown catalog_id returns 404."""
    await _insert_user(tmp_path, "alice", is_admin=True)
    _login(test_client, "alice")

    resp = test_client.post(
        "/api/mcp-store/servers",
        json={"catalog_id": "nonexistent-server-xyz"},
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_delete_204(tmp_path: Path, test_client: TestClient) -> None:
    """DELETE /api/mcp-store/servers/{slug} returns 204 when installed."""
    await _insert_user(tmp_path, "alice", is_admin=True)
    _login(test_client, "alice")

    # Make get() return a valid internal view so delete can proceed.
    test_client.app.state.mcp_server_store.get = AsyncMock(  # type: ignore[attr-defined]
        return_value=McpServerInternalView(
            id=1,
            slug="github",
            name="GitHub",
            transport="stdio",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-github"],
            url=None,
        )
    )

    resp = test_client.delete("/api/mcp-store/servers/github")
    assert resp.status_code == 204, resp.text


@pytest.mark.asyncio
async def test_delete_nonexistent_404(
    tmp_path: Path, test_client: TestClient
) -> None:
    """DELETE /api/mcp-store/servers/{slug} returns 404 when not installed."""
    await _insert_user(tmp_path, "alice", is_admin=True)
    _login(test_client, "alice")

    # store.get is already mocked to return None.
    resp = test_client.delete("/api/mcp-store/servers/nonexistent")
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_admin_only_servers_403(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Non-admin user gets 403 on /api/mcp-store/servers."""
    await _insert_user(tmp_path, "bob", is_admin=False)
    _login(test_client, "bob")

    assert test_client.get("/api/mcp-store/servers").status_code == 403


# ---------------------------------------------------------------------------
# B4 tests — PATCH servers/{slug}, GET servers/{slug}/tools, tool_policy field
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_includes_tool_policy(
    tmp_path: Path, test_client: TestClient
) -> None:
    """POST /api/mcp-store/servers: response includes tool_policy field."""
    await _insert_user(tmp_path, "alice", is_admin=True)
    _login(test_client, "alice")

    resp = test_client.post(
        "/api/mcp-store/servers",
        json={
            "catalog_id": "github",
            "secrets": {"GITHUB_TOKEN": "ghp_test"},
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "tool_policy" in data, "tool_policy missing from install response"
    assert data["tool_policy"] == []


@pytest.mark.asyncio
async def test_patch_enable_disable(
    tmp_path: Path, test_client: TestClient
) -> None:
    """PATCH /api/mcp-store/servers/{slug} enable/disable: calls update_enabled."""
    from lmchat.services.mcp_server_store import McpServerSafeView as _SafeView

    await _insert_user(tmp_path, "alice", is_admin=True)
    _login(test_client, "alice")

    disabled_view = _SafeView(
        id=1,
        slug="github",
        name="GitHub",
        transport="stdio",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-github"],
        url=None,
        secrets_set=[],
        enabled=False,
        source="official",
        trust="curated",
        consented=True,
        connected=False,
        tool_policy=[],
    )
    from lmchat.services.mcp_server_store import McpServerInternalView as _InternalView

    existing_internal = _InternalView(
        id=1,
        slug="github",
        name="GitHub",
        transport="stdio",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-github"],
        url=None,
        tool_policy=[],
    )
    test_client.app.state.mcp_server_store.get = AsyncMock(  # type: ignore[attr-defined]
        return_value=existing_internal
    )
    test_client.app.state.mcp_server_store.update_enabled = AsyncMock(  # type: ignore[attr-defined]
        return_value=disabled_view
    )

    resp = test_client.patch("/api/mcp-store/servers/github", json={"enabled": False})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["enabled"] is False
    # Disconnect should have been called.
    test_client.app.state.mcp_host.disconnect.assert_called_once_with("github")  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_patch_tool_policy(
    tmp_path: Path, test_client: TestClient
) -> None:
    """PATCH /api/mcp-store/servers/{slug} tool_policy: calls update_tool_policy."""
    from lmchat.services.mcp_server_store import McpServerInternalView as _InternalView
    from lmchat.services.mcp_server_store import McpServerSafeView as _SafeView

    await _insert_user(tmp_path, "alice", is_admin=True)
    _login(test_client, "alice")

    existing_internal = _InternalView(
        id=1,
        slug="firecrawl",
        name="Firecrawl",
        transport="stdio",
        command=None,
        args=None,
        url=None,
        tool_policy=[],
    )
    updated_view = _SafeView(
        id=1,
        slug="firecrawl",
        name="Firecrawl",
        transport="stdio",
        command=None,
        args=None,
        url=None,
        secrets_set=[],
        enabled=True,
        source="byo",
        trust="byo",
        consented=True,
        connected=False,
        tool_policy=["firecrawl_scrape"],
    )
    test_client.app.state.mcp_server_store.get = AsyncMock(  # type: ignore[attr-defined]
        return_value=existing_internal
    )
    test_client.app.state.mcp_server_store.update_tool_policy = AsyncMock(  # type: ignore[attr-defined]
        return_value=updated_view
    )

    resp = test_client.patch(
        "/api/mcp-store/servers/firecrawl",
        json={"tool_policy": ["firecrawl_scrape"]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["tool_policy"] == ["firecrawl_scrape"]


@pytest.mark.asyncio
async def test_patch_not_installed_404(
    tmp_path: Path, test_client: TestClient
) -> None:
    """PATCH /api/mcp-store/servers/{slug} returns 404 when not installed."""
    await _insert_user(tmp_path, "alice", is_admin=True)
    _login(test_client, "alice")

    # store.get returns None (default mock).
    resp = test_client.patch(
        "/api/mcp-store/servers/nonexistent", json={"enabled": True}
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_get_tools_marks_denied(
    tmp_path: Path, test_client: TestClient
) -> None:
    """GET /api/mcp-store/servers/{slug}/tools marks denied tools correctly."""
    from lmchat.lmstudio.types import CanonicalTool
    from lmchat.services.mcp_server_store import McpServerInternalView as _InternalView

    await _insert_user(tmp_path, "alice", is_admin=True)
    _login(test_client, "alice")

    internal = _InternalView(
        id=1,
        slug="firecrawl",
        name="Firecrawl",
        transport="stdio",
        command=None,
        args=None,
        url=None,
        tool_policy=["firecrawl_scrape"],
    )
    test_client.app.state.mcp_server_store.get = AsyncMock(  # type: ignore[attr-defined]
        return_value=internal
    )

    # Mock the host to return connected + two tools.
    mock_host = test_client.app.state.mcp_host  # type: ignore[attr-defined]
    mock_host.connect = AsyncMock(return_value=True)
    mock_host.connected_server_ids = ["firecrawl"]
    mock_host.list_tools = MagicMock(  # type: ignore[attr-defined]
        return_value=[
            CanonicalTool(
                name="firecrawl_scrape",
                description="Scrape a URL",
                parameters={"type": "object", "properties": {}},
            ),
            CanonicalTool(
                name="firecrawl_map",
                description="Map a domain",
                parameters={"type": "object", "properties": {}},
            ),
        ]
    )

    resp = test_client.get("/api/mcp-store/servers/firecrawl/tools")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["slug"] == "firecrawl"
    assert data["connected"] is True
    tools_by_name = {t["name"]: t for t in data["tools"]}
    assert tools_by_name["firecrawl_scrape"]["denied"] is True
    assert tools_by_name["firecrawl_map"]["denied"] is False


@pytest.mark.asyncio
async def test_get_tools_not_installed_404(
    tmp_path: Path, test_client: TestClient
) -> None:
    """GET /api/mcp-store/servers/{slug}/tools returns 404 when not installed."""
    await _insert_user(tmp_path, "alice", is_admin=True)
    _login(test_client, "alice")

    # store.get returns None (default).
    resp = test_client.get("/api/mcp-store/servers/nonexistent/tools")
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_get_tools_connect_failure(
    tmp_path: Path, test_client: TestClient
) -> None:
    """GET /api/mcp-store/servers/{slug}/tools: connect failure → connected:false, error."""
    from lmchat.services.mcp_server_store import McpServerInternalView as _InternalView

    await _insert_user(tmp_path, "alice", is_admin=True)
    _login(test_client, "alice")

    internal = _InternalView(
        id=1,
        slug="github",
        name="GitHub",
        transport="stdio",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-github"],
        url=None,
        tool_policy=[],
    )
    test_client.app.state.mcp_server_store.get = AsyncMock(  # type: ignore[attr-defined]
        return_value=internal
    )
    mock_host = test_client.app.state.mcp_host  # type: ignore[attr-defined]
    mock_host.connect = AsyncMock(side_effect=RuntimeError("npx not found"))

    resp = test_client.get("/api/mcp-store/servers/github/tools")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["connected"] is False
    assert data["tools"] == []
    assert "npx not found" in (data.get("error") or "")


# ---------------------------------------------------------------------------
# FIX B — transport / URL validation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_stdio_without_command_400(
    tmp_path: Path, test_client: TestClient
) -> None:
    """POST /api/mcp-store/servers: BYO stdio without command → 400."""
    await _insert_user(tmp_path, "alice", is_admin=True)
    _login(test_client, "alice")

    resp = test_client.post(
        "/api/mcp-store/servers",
        json={
            "slug": "my-mcp",
            "name": "My MCP",
            "transport": "stdio",
            # command intentionally omitted
        },
    )
    assert resp.status_code == 400, resp.text
    assert "command is required" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_install_http_without_url_400(
    tmp_path: Path, test_client: TestClient
) -> None:
    """POST /api/mcp-store/servers: http transport without url → 400."""
    await _insert_user(tmp_path, "alice", is_admin=True)
    _login(test_client, "alice")

    resp = test_client.post(
        "/api/mcp-store/servers",
        json={
            "slug": "remote-mcp",
            "name": "Remote MCP",
            "transport": "http",
            # url intentionally omitted
        },
    )
    assert resp.status_code == 400, resp.text
    assert "http(s) url" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_install_http_with_file_scheme_400(
    tmp_path: Path, test_client: TestClient
) -> None:
    """POST /api/mcp-store/servers: http transport with file:// url → 400 (SSRF block)."""
    await _insert_user(tmp_path, "alice", is_admin=True)
    _login(test_client, "alice")

    resp = test_client.post(
        "/api/mcp-store/servers",
        json={
            "slug": "evil-mcp",
            "name": "Evil MCP",
            "transport": "http",
            "url": "file:///etc/passwd",
        },
    )
    assert resp.status_code == 400, resp.text
    assert "http(s) url" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_install_invalid_transport_400(
    tmp_path: Path, test_client: TestClient
) -> None:
    """POST /api/mcp-store/servers: unknown transport string → 400."""
    await _insert_user(tmp_path, "alice", is_admin=True)
    _login(test_client, "alice")

    resp = test_client.post(
        "/api/mcp-store/servers",
        json={
            "slug": "weird-mcp",
            "name": "Weird MCP",
            "transport": "websocket",
        },
    )
    assert resp.status_code == 400, resp.text
    assert "transport must be" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_install_http_with_valid_https_url_200(
    tmp_path: Path, test_client: TestClient
) -> None:
    """POST /api/mcp-store/servers: http transport with https url passes validation → 200."""
    await _insert_user(tmp_path, "alice", is_admin=True)
    _login(test_client, "alice")

    # mock_store.install already returns a valid SafeView; just check the route
    # lets the request through the validation gate.
    resp = test_client.post(
        "/api/mcp-store/servers",
        json={
            "slug": "firecrawl-byo",
            "name": "Firecrawl BYO",
            "transport": "http",
            "url": "https://api.firecrawl.dev",
        },
    )
    assert resp.status_code == 200, resp.text
