# SPDX-License-Identifier: Apache-2.0
"""Integration tests for the MCP integrations routes.

Tests:
- GET /api/integrations/available — 401 unauthenticated
- GET /api/integrations/available — 200 authenticated (env fallback)
- GET /api/integrations/available — 200 authenticated (DB populated)
- PUT /api/integrations/available — 401 unauthenticated
- PUT /api/integrations/available — 403 non-admin
- PUT /api/integrations/available — 200 admin sets list
- PUT /api/integrations/available — 400 bad JSON
- PUT /api/integrations/available — 400 non-list JSON
- PUT /api/integrations/available — 422 invalid entry shape
- PUT /api/integrations/available — 429 admin rate limit (mock)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import metadata
from lmchat.middleware._bucket_store import InMemoryBucketStore
from lmchat.routes._dependencies import get_models_service_dep
from lmchat.services.auth_service import _reset_dummy_hash_cache
from lmchat.services.mcp_server_store import McpServerSafeView
from lmchat.utils.hashing import hash_password

_LOW_N: int = 2**10


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _make_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    env_integrations: str = "",
) -> Any:  # noqa: ANN401
    from lmchat.app import create_app
    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod

    db_url = f"sqlite+aiosqlite:///{tmp_path}/int_route_test.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    # Disable ~/.lmstudio/mcp.json file discovery — the dev machine's
    # config would otherwise bleed into hermetic tests that assert on
    # an empty integrations list. Tests that want file discovery should
    # construct the service directly with `local_mcp_config=` instead.
    monkeypatch.setenv("LM_CHAT_LOCAL_MCP_DISCOVERY_ENABLED", "false")
    if env_integrations:
        monkeypatch.setenv("LM_CHAT_AVAILABLE_INTEGRATIONS", env_integrations)

    get_settings.cache_clear()
    engine_mod.dispose_engine()
    _reset_dummy_hash_cache()

    app = create_app()

    stub_models = AsyncMock()
    stub_models.list_loaded = AsyncMock(return_value=[])
    stub_models.refresh = AsyncMock(return_value=None)
    app.dependency_overrides[get_models_service_dep] = lambda: stub_models

    return app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        yield client


@pytest.fixture()
def test_client_with_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Client with LM_CHAT_AVAILABLE_INTEGRATIONS set to 2 entries."""
    app = _make_app(tmp_path, monkeypatch, env_integrations="mcp/searxng,mcp/filesystem")
    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        yield client


async def _engine_for(tmp_path: Path) -> AsyncEngine:
    db_url = f"sqlite+aiosqlite:///{tmp_path}/int_route_test.db"
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
                    "INSERT OR IGNORE INTO users (username, password_hash, is_admin) "
                    "VALUES (:u, :pw, :admin)"
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
# GET /api/integrations/available — auth matrix
# ---------------------------------------------------------------------------


def test_list_available_requires_auth(test_client: TestClient) -> None:
    """GET /api/integrations/available → 401 for unauthenticated."""
    resp = test_client.get("/api/integrations/available")
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_list_available_authenticated_empty(
    tmp_path: Path, test_client: TestClient
) -> None:
    """GET /api/integrations/available → 200 for authenticated user, empty list."""
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    resp = test_client.get("/api/integrations/available")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.anyio
async def test_list_available_env_fallback(
    tmp_path: Path, test_client_with_env: TestClient
) -> None:
    """GET returns env-fallback list when DB is empty and env is set."""
    await _insert_user(tmp_path, "alice-env")
    _login(test_client_with_env, "alice-env")
    resp = test_client_with_env.get("/api/integrations/available")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    values = [e["value"] for e in data]
    assert "mcp/searxng" in values
    assert "mcp/filesystem" in values


# ---------------------------------------------------------------------------
# PUT /api/integrations/available — auth matrix
# ---------------------------------------------------------------------------


def test_put_available_requires_auth(test_client: TestClient) -> None:
    """PUT /api/integrations/available → 401 for unauthenticated."""
    resp = test_client.put(
        "/api/integrations/available",
        data={"entries": json.dumps([])},
    )
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_put_available_requires_admin(
    tmp_path: Path, test_client: TestClient
) -> None:
    """PUT /api/integrations/available → 403 for non-admin user."""
    await _insert_user(tmp_path, "regular-user", is_admin=False)
    _login(test_client, "regular-user")
    resp = test_client.put(
        "/api/integrations/available",
        data={"entries": json.dumps([])},
    )
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_put_available_admin_sets_list(
    tmp_path: Path, test_client: TestClient
) -> None:
    """PUT /api/integrations/available → 200 for admin, sets the list."""
    await _insert_user(tmp_path, "admin-user", is_admin=True)
    _login(test_client, "admin-user")

    entries = [
        {"value": "mcp/searxng", "sort_order": 0},
        {"value": "mcp/filesystem", "sort_order": 1},
    ]
    resp = test_client.put(
        "/api/integrations/available",
        data={"entries": json.dumps(entries)},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    values = [e["value"] for e in data]
    assert "mcp/searxng" in values
    assert "mcp/filesystem" in values

    # Verify GET returns the new list.
    get_resp = test_client.get("/api/integrations/available")
    assert get_resp.status_code == 200
    get_data = get_resp.json()
    assert len(get_data) == 2


@pytest.mark.anyio
async def test_put_available_empty_clears_list(
    tmp_path: Path, test_client: TestClient
) -> None:
    """PUT with empty array clears the DB list."""
    await _insert_user(tmp_path, "admin-clear", is_admin=True)
    _login(test_client, "admin-clear")

    # First populate.
    test_client.put(
        "/api/integrations/available",
        data={"entries": json.dumps([{"value": "mcp/temp", "sort_order": 0}])},
    )
    # Then clear.
    resp = test_client.put(
        "/api/integrations/available",
        data={"entries": json.dumps([])},
    )
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# PUT error paths
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_put_available_bad_json(
    tmp_path: Path, test_client: TestClient
) -> None:
    """PUT with invalid JSON → 400."""
    await _insert_user(tmp_path, "admin-badjson", is_admin=True)
    _login(test_client, "admin-badjson")
    resp = test_client.put(
        "/api/integrations/available",
        data={"entries": "not-valid-json{{{"},
    )
    assert resp.status_code == 400
    assert "entries must be a JSON array" in resp.json()["detail"]


@pytest.mark.anyio
async def test_put_available_non_list_json(
    tmp_path: Path, test_client: TestClient
) -> None:
    """PUT with JSON that is not a list → 400."""
    await _insert_user(tmp_path, "admin-nonlist", is_admin=True)
    _login(test_client, "admin-nonlist")
    resp = test_client.put(
        "/api/integrations/available",
        data={"entries": json.dumps({"value": "mcp/test"})},
    )
    assert resp.status_code == 400
    assert "non-list" in resp.json()["detail"]


@pytest.mark.anyio
async def test_put_available_invalid_entry_shape(
    tmp_path: Path, test_client: TestClient
) -> None:
    """PUT with wrong entry shape → 422."""
    await _insert_user(tmp_path, "admin-badshape", is_admin=True)
    _login(test_client, "admin-badshape")
    resp = test_client.put(
        "/api/integrations/available",
        data={"entries": json.dumps([{"wrong_field": 99}])},
    )
    # Pydantic treats missing required 'value' as validation error → 422
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_put_available_rate_limit(
    tmp_path: Path, test_client: TestClient
) -> None:
    """PUT /api/integrations/available → 429 when admin rate limit is exhausted."""
    await _insert_user(tmp_path, "admin-rl", is_admin=True)
    _login(test_client, "admin-rl")

    # Exhaust the admin bucket for this user.
    from lmchat.middleware._bucket_store import InMemoryBucketStore

    exhausted_store = InMemoryBucketStore()
    user_id_row = None
    from sqlalchemy.ext.asyncio import create_async_engine as _cae
    db_url = f"sqlite+aiosqlite:///{tmp_path}/int_route_test.db"
    eng = _cae(db_url)
    async with eng.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT id FROM users WHERE username = 'admin-rl'")
            )
        ).fetchone()
        user_id_row = row[0] if row else 1
    await eng.dispose()

    # Pre-exhaust by consuming all tokens with a very low rate/burst.
    key = f"admin:{user_id_row}"
    # Drain every available token (burst=1, rate≈0 → immediate exhaustion).
    allowed, _ = await exhausted_store.consume(key, rate=0.001, burst=1)
    assert allowed  # first consume succeeds
    _allowed2, _ = await exhausted_store.consume(key, rate=0.001, burst=1)

    # Inject the exhausted bucket store.
    test_client.app.state.admin_buckets = exhausted_store  # type: ignore[attr-defined]
    resp = test_client.put(
        "/api/integrations/available",
        data={"entries": json.dumps([])},
    )
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers


# ---------------------------------------------------------------------------
# GET /api/integrations/available — MCP-Store server merge
# ---------------------------------------------------------------------------


def _make_mock_store(servers: list[McpServerSafeView]) -> AsyncMock:
    """Return an AsyncMock McpServerStore that returns *servers* from list_all."""
    mock_store = AsyncMock()
    mock_store.list_all = AsyncMock(return_value=servers)
    return mock_store


@pytest.fixture()
def test_client_with_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Client with a mock MCP-Store containing one enabled server ('fetch')."""
    app = _make_app(tmp_path, monkeypatch)
    fetch_server = McpServerSafeView(
        id=42,
        slug="fetch",
        name="Fetch",
        transport="stdio",
        command="uvx",
        args=["mcp-server-fetch"],
        url=None,
        secrets_set=[],
        enabled=True,
        source="official",
        trust="curated",
        consented=True,
        connected=False,
        tool_policy=[],
    )
    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.mcp_server_store = _make_mock_store([fetch_server])  # type: ignore[attr-defined]
        yield client


@pytest.mark.anyio
async def test_list_available_includes_store_server(
    tmp_path: Path, test_client_with_store: TestClient
) -> None:
    """GET /api/integrations/available merges an enabled store server as mcp/<slug>.

    Verifies:
    - ``mcp/fetch`` appears in the response (store-installed, enabled).
    - ``enabled_by_default`` is False for the store-added entry (opt-in).
    """
    await _insert_user(tmp_path, "alice-store")
    _login(test_client_with_store, "alice-store")
    resp = test_client_with_store.get("/api/integrations/available")
    assert resp.status_code == 200
    data = resp.json()
    values = [e["value"] for e in data]
    assert "mcp/fetch" in values, f"expected mcp/fetch in {values}"

    # The store-added entry must be opt-in (not pre-selected).
    fetch_entry = next(e for e in data if e["value"] == "mcp/fetch")
    assert fetch_entry["enabled_by_default"] is False


@pytest.mark.anyio
async def test_list_available_curated_still_present_with_store(
    tmp_path: Path,
    test_client_with_store: TestClient,
) -> None:
    """Curated DB entries are still returned alongside store-added entries."""
    await _insert_user(tmp_path, "admin-curated", is_admin=True)
    _login(test_client_with_store, "admin-curated")

    # Add a curated entry via PUT first.
    entries = [{"value": "mcp/searxng", "sort_order": 0}]
    put_resp = test_client_with_store.put(
        "/api/integrations/available",
        data={"entries": json.dumps(entries)},
    )
    assert put_resp.status_code == 200

    resp = test_client_with_store.get("/api/integrations/available")
    assert resp.status_code == 200
    data = resp.json()
    values = [e["value"] for e in data]
    assert "mcp/searxng" in values, f"curated entry missing: {values}"
    assert "mcp/fetch" in values, f"store entry missing: {values}"


@pytest.mark.anyio
async def test_list_available_store_slug_coexists_with_curated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store server whose slug is already curated coexists as BOTH entries.

    ``mcp/<slug>`` legitimately identifies two different runtimes: LM
    Studio's own ``mcp.json`` server (``source="lmstudio"``, dispatched
    server-side) and LMChat's MCP-Store server (``source="store"``,
    client-side agentic host). The dedup must be scoped to
    ``(source, value)``, not the bare value — so both entries must be
    present, not collapsed into one.
    """
    # Build the app with "mcp/fetch" in the curated env fallback.
    app = _make_app(
        tmp_path, monkeypatch, env_integrations="mcp/fetch,mcp/filesystem"
    )
    fetch_server = McpServerSafeView(
        id=42,
        slug="fetch",
        name="Fetch",
        transport="stdio",
        command="uvx",
        args=["mcp-server-fetch"],
        url=None,
        secrets_set=[],
        enabled=True,
        source="official",
        trust="curated",
        consented=True,
        connected=False,
        tool_policy=[],
    )
    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.mcp_server_store = _make_mock_store([fetch_server])  # type: ignore[attr-defined]

        await _insert_user(tmp_path, "alice-dedup")
        _login(client, "alice-dedup")
        resp = client.get("/api/integrations/available")
        assert resp.status_code == 200
        data = resp.json()
        # Both the lmstudio (curated) and store entries for mcp/fetch
        # must be present — one of each source.
        sources = {e["source"] for e in data if e["value"] == "mcp/fetch"}
        assert sources == {"lmstudio", "store"}, (
            f"expected both sources present for mcp/fetch: {sources}"
        )


@pytest.mark.anyio
async def test_list_available_store_entry_tagged_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store-added mcp/fetch entry is tagged source=='store'; curated entries are 'lmstudio'."""
    # Distinct slug in curated vs. store so we can assert both sources
    # independently of the coexistence behavior tested elsewhere.
    app = _make_app(tmp_path, monkeypatch, env_integrations="mcp/searxng")
    fetch_server = McpServerSafeView(
        id=42,
        slug="fetch",
        name="Fetch",
        transport="stdio",
        command="uvx",
        args=["mcp-server-fetch"],
        url=None,
        secrets_set=[],
        enabled=True,
        source="official",
        trust="curated",
        consented=True,
        connected=False,
        tool_policy=[],
    )
    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.mcp_server_store = _make_mock_store([fetch_server])  # type: ignore[attr-defined]

        await _insert_user(tmp_path, "alice-source-tag")
        _login(client, "alice-source-tag")
        resp = client.get("/api/integrations/available")
        assert resp.status_code == 200
        data = resp.json()

        fetch_entry = next(e for e in data if e["value"] == "mcp/fetch")
        assert fetch_entry["source"] == "store"

        curated_entries = [e for e in data if e["value"] != "mcp/fetch"]
        assert curated_entries, "expected at least one curated entry to compare against"
        assert all(e["source"] == "lmstudio" for e in curated_entries)


@pytest.mark.anyio
async def test_list_available_compat_has_store_source_when_all_slugs_overlap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard: compat/cloud mode (source=='store' filter) is never empty.

    When the curated (env-fallback) list and the store's servers name the
    exact same slug, a naive bare-value dedup collapses everything to
    ``source="lmstudio"`` and leaves zero ``source="store"`` entries — which
    is exactly the bug that made the compat-mode composer show ZERO MCP
    servers (it filters to ``source==="store"``).
    """
    app = _make_app(tmp_path, monkeypatch, env_integrations="mcp/fetch")
    fetch_server = McpServerSafeView(
        id=42,
        slug="fetch",
        name="Fetch",
        transport="stdio",
        command="uvx",
        args=["mcp-server-fetch"],
        url=None,
        secrets_set=[],
        enabled=True,
        source="official",
        trust="curated",
        consented=True,
        connected=False,
        tool_policy=[],
    )
    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.mcp_server_store = _make_mock_store([fetch_server])  # type: ignore[attr-defined]

        await _insert_user(tmp_path, "alice-compat")
        _login(client, "alice-compat")
        resp = client.get("/api/integrations/available")
        assert resp.status_code == 200
        data = resp.json()
        assert any(e["source"] == "store" for e in data), (
            f"compat mode would be empty — no store entry in {data}"
        )


@pytest.mark.anyio
async def test_list_available_synthetic_ids_distinct_across_systems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-slug env-fallback (synthetic, negative-id) curated entry and a
    store entry must get DISTINCT ids — otherwise they collide as React keys.
    """
    app = _make_app(tmp_path, monkeypatch, env_integrations="mcp/fetch")
    fetch_server = McpServerSafeView(
        id=42,
        slug="fetch",
        name="Fetch",
        transport="stdio",
        command="uvx",
        args=["mcp-server-fetch"],
        url=None,
        secrets_set=[],
        enabled=True,
        source="official",
        trust="curated",
        consented=True,
        connected=False,
        tool_policy=[],
    )
    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.mcp_server_store = _make_mock_store([fetch_server])  # type: ignore[attr-defined]

        await _insert_user(tmp_path, "alice-ids")
        _login(client, "alice-ids")
        resp = client.get("/api/integrations/available")
        assert resp.status_code == 200
        data = resp.json()
        ids = [e["id"] for e in data if e["value"] == "mcp/fetch"]
        assert len(ids) == 2, f"expected 2 mcp/fetch entries: {data}"
        assert ids[0] != ids[1], f"synthetic ids collided across systems: {ids}"


@pytest.mark.anyio
async def test_list_available_disabled_store_server_excluded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A disabled store server must NOT appear in /api/integrations/available."""
    app = _make_app(tmp_path, monkeypatch)
    disabled_server = McpServerSafeView(
        id=99,
        slug="disabled-tool",
        name="Disabled Tool",
        transport="stdio",
        command="uvx",
        args=["disabled-tool"],
        url=None,
        secrets_set=[],
        enabled=False,  # ← disabled
        source="official",
        trust="curated",
        consented=True,
        connected=False,
        tool_policy=[],
    )
    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.mcp_server_store = _make_mock_store([disabled_server])  # type: ignore[attr-defined]

        await _insert_user(tmp_path, "alice-disabled")
        _login(client, "alice-disabled")
        resp = client.get("/api/integrations/available")
        assert resp.status_code == 200
        values = [e["value"] for e in resp.json()]
        assert "mcp/disabled-tool" not in values, f"disabled server must not appear: {values}"


@pytest.mark.anyio
async def test_list_available_no_store_degrades_gracefully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When mcp_server_store is absent from app.state, GET returns curated list only."""
    app = _make_app(
        tmp_path, monkeypatch, env_integrations="mcp/searxng"
    )
    with TestClient(app, raise_server_exceptions=True) as client:
        from starlette.applications import Starlette

        starlette_app = cast(Starlette, client.app)
        starlette_app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        starlette_app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        # Explicitly remove mcp_server_store to simulate missing state.
        if hasattr(starlette_app.state, "mcp_server_store"):
            del starlette_app.state.mcp_server_store

        await _insert_user(tmp_path, "alice-nostore")
        _login(client, "alice-nostore")
        resp = client.get("/api/integrations/available")
        assert resp.status_code == 200
        values = [e["value"] for e in resp.json()]
        assert "mcp/searxng" in values
