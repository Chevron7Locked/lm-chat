# SPDX-License-Identifier: Apache-2.0
"""Integration tests for GET /api/models and POST /api/admin/models/refresh.

Tests for the /api/models route.

Pattern: TestClient with real lifespan via context manager. Service
dependencies are overridden via ``app.dependency_overrides`` to return
test-scoped instances rather than requiring the lifespan to wire app.state.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI as _FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.app import create_app
from lmchat.db.schema import metadata, users
from lmchat.middleware._bucket_store import InMemoryBucketStore
from lmchat.routes._dependencies import (
    admin_rate_limit,
    get_default_session_store_dep,
    get_engine_dep,
    get_models_service_dep,
)
from lmchat.services.auth_service import _reset_dummy_hash_cache
from lmchat.services.models_service import (
    Capabilities,
    ModelInfo,
    ModelLoadResult,
    ModelsService,
    ModelUnloadResult,
    UnloadAllResult,
    UpstreamGatewayError,
    UpstreamModelError,
)
from lmchat.session.sqlite_store import SQLiteSessionStore

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LOW_N: int = 2**10
_LOW_COST: dict[str, int] = {"_hash_n": _LOW_N, "_hash_r": 8, "_hash_p": 1}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    """Required env vars + cache clearing."""
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.config import get_settings

    get_settings.cache_clear()
    _reset_dummy_hash_cache()
    yield
    get_settings.cache_clear()
    _reset_dummy_hash_cache()


@pytest.fixture()
async def db_engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    """Per-test SQLite engine with full schema."""
    db_path = tmp_path / "test_routes_models.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}", pool_pre_ping=True)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture()
def mock_models_service() -> MagicMock:
    """Mock ModelsService with controllable list_loaded/refresh."""
    svc = MagicMock(spec=ModelsService)

    _model = ModelInfo(
        key="test-model",
        capabilities=Capabilities(vision=False, trained_for_tool_use=True),
    )
    svc.list_loaded = AsyncMock(return_value=[_model])
    svc.refresh = AsyncMock(return_value=None)
    return svc


@pytest.fixture()
def test_client(
    db_engine: AsyncEngine,
    mock_models_service: MagicMock,
) -> Generator[TestClient]:
    """TestClient wired to per-test engine + mock ModelsService."""
    app = create_app()
    store = SQLiteSessionStore(engine=db_engine)

    app.dependency_overrides[get_engine_dep] = lambda: db_engine
    app.dependency_overrides[get_default_session_store_dep] = lambda: store
    app.dependency_overrides[get_models_service_dep] = lambda: mock_models_service

    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        # Clear the lifespan-created catalog so the route uses the overridden
        # get_models_service_dep (mock_models_service) via the legacy fallback.
        # The catalog merge layer is tested separately in test_model_catalog_routes.py.
        client.app.state.model_catalog = None  # type: ignore[attr-defined]
        yield client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _insert_user_async(engine: AsyncEngine, username: str, password: str) -> None:
    """Bypass the single-admin gate by inserting a user directly (async)."""

    from sqlalchemy import func, select, text

    from lmchat.db.schema import users as users_table
    from lmchat.utils.hashing import hash_password

    pw_hash = hash_password(password, n=_LOW_N, r=8, p=1)
    async with engine.begin() as conn:
        id_result = await conn.execute(
            select(func.coalesce(func.max(users_table.c.id), 0) + 1)
        )
        next_id = id_result.scalar()
        await conn.execute(
            text(
                "INSERT INTO users (id, username, password_hash)"
                " VALUES (:id, :u, :ph)"
            ),
            {"id": int(next_id or 1), "u": username, "ph": pw_hash},
        )


def _insert_user_direct(engine: AsyncEngine, username: str, password: str) -> None:
    """Bypass the single-admin gate by inserting the user directly (sync wrapper)."""
    import asyncio

    asyncio.run(_insert_user_async(engine, username, password))


def _register_and_login(
    client: TestClient,
    username: str = "alice",
    password: str = "correct-horse-battery",
    *,
    engine: AsyncEngine | None = None,
) -> None:
    """Register a user and log them in (sets session cookie on client)."""
    if engine is not None:
        _insert_user_direct(engine, username, password)
    else:
        client.post("/api/auth/register", data={"username": username, "password": password})
    client.post("/api/auth/login", data={"username": username, "password": password})


def _seed_placeholder(client: TestClient, *, engine: AsyncEngine | None = None) -> None:
    """Insert a throwaway user so the next user does not get the bootstrap-admin grant."""
    if engine is not None:
        _insert_user_direct(engine, "__placeholder__", "placeholder-pw")
    else:
        client.post(
            "/api/auth/register",
            data={"username": "__placeholder__", "password": "placeholder-pw"},
        )


async def _make_admin(engine: AsyncEngine, username: str) -> None:
    """Promote a user to admin by updating the DB directly."""
    from sqlalchemy import update

    async with engine.begin() as conn:
        await conn.execute(
            update(users).where(users.c.username == username).values(is_admin=True)
        )


# ---------------------------------------------------------------------------
# GET /api/models
# ---------------------------------------------------------------------------


def test_get_models_requires_auth(test_client: TestClient) -> None:
    """GET /api/models without a session cookie → 401."""
    resp = test_client.get("/api/models")
    assert resp.status_code == 401


def test_get_models_returns_list_when_authed(
    test_client: TestClient,
    mock_models_service: MagicMock,
) -> None:
    """GET /api/models with valid session → 200 + list of models."""
    _register_and_login(test_client)
    resp = test_client.get("/api/models")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 1
    assert body[0]["key"] == "test-model"


def test_get_models_calls_list_loaded(
    test_client: TestClient,
    mock_models_service: MagicMock,
) -> None:
    """GET /api/models delegates to models_service.list_loaded()."""
    _register_and_login(test_client)
    test_client.get("/api/models")
    mock_models_service.list_loaded.assert_awaited()


# ---------------------------------------------------------------------------
# POST /api/admin/models/refresh
# ---------------------------------------------------------------------------


def test_refresh_models_requires_auth(test_client: TestClient) -> None:
    """POST /api/admin/models/refresh without session → 401."""
    resp = test_client.post("/api/admin/models/refresh")
    assert resp.status_code == 401


def test_refresh_models_requires_admin(test_client: TestClient, db_engine: AsyncEngine) -> None:
    """POST /api/admin/models/refresh as non-admin user → 403."""
    _seed_placeholder(test_client, engine=db_engine)
    _register_and_login(test_client, engine=db_engine)
    resp = test_client.post("/api/admin/models/refresh")
    assert resp.status_code == 403


@pytest.mark.asyncio()
async def test_refresh_models_succeeds_for_admin(
    db_engine: AsyncEngine,
    mock_models_service: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/admin/models/refresh as admin → 200 + {ok, model_count}."""
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.config import get_settings

    get_settings.cache_clear()

    app = create_app()
    store = SQLiteSessionStore(engine=db_engine)
    app.dependency_overrides[get_engine_dep] = lambda: db_engine
    app.dependency_overrides[get_default_session_store_dep] = lambda: store
    app.dependency_overrides[get_models_service_dep] = lambda: mock_models_service

    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.post("/api/auth/register", data={"username": "admin", "password": "correct-horse-battery"})
        await _make_admin(db_engine, "admin")
        client.post("/api/auth/login", data={"username": "admin", "password": "correct-horse-battery"})

        resp = client.post("/api/admin/models/refresh")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert isinstance(body["model_count"], int)
        mock_models_service.refresh.assert_awaited()


# ---------------------------------------------------------------------------
# Helpers for lifecycle route tests
# ---------------------------------------------------------------------------


def _make_admin_app(
    db_engine: AsyncEngine,
    mock_svc: MagicMock,
) -> tuple[_FastAPI, SQLiteSessionStore]:
    """Build a FastAPI app with mock ModelsService; return (app, store)."""
    from lmchat.config import get_settings

    get_settings.cache_clear()
    app = create_app()
    store = SQLiteSessionStore(engine=db_engine)
    app.dependency_overrides[get_engine_dep] = lambda: db_engine
    app.dependency_overrides[get_default_session_store_dep] = lambda: store
    app.dependency_overrides[get_models_service_dep] = lambda: mock_svc
    return app, store


# ---------------------------------------------------------------------------
# POST /api/models/load
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_load_model_requires_admin(
    db_engine: AsyncEngine,
    mock_models_service: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/models/load as non-admin → 403."""
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.config import get_settings

    get_settings.cache_clear()

    app, store = _make_admin_app(db_engine, mock_models_service)
    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        await _insert_user_async(db_engine, "__placeholder__", "placeholder-pw")
        await _insert_user_async(db_engine, "nonadmin", "correct-horse-battery")
        client.post("/api/auth/login", data={"username": "nonadmin", "password": "correct-horse-battery"})
        resp = client.post("/api/models/load", data={"model": "some-model"})
        assert resp.status_code == 403


@pytest.mark.asyncio()
async def test_load_model_success(
    db_engine: AsyncEngine,
    mock_models_service: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/models/load as admin with valid model → 200."""
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.config import get_settings

    get_settings.cache_clear()

    mock_models_service.load_model = AsyncMock(
        return_value=ModelLoadResult(
            instance_id="qwen3-8b:2",
            load_time_seconds=10.32,
            status="loaded",
        )
    )

    app, store = _make_admin_app(db_engine, mock_models_service)
    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.post("/api/auth/register", data={"username": "admin", "password": "correct-horse-battery"})
        await _make_admin(db_engine, "admin")
        client.post("/api/auth/login", data={"username": "admin", "password": "correct-horse-battery"})

        resp = client.post("/api/models/load", data={"model": "qwen3-8b"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["instance_id"] == "qwen3-8b:2"
        assert body["model"] == "qwen3-8b"
        assert body["status"] == "loaded"


@pytest.mark.asyncio()
async def test_load_model_upstream_4xx_returns_same_status(
    db_engine: AsyncEngine,
    mock_models_service: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/models/load with upstream 404 → our 404."""
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.config import get_settings

    get_settings.cache_clear()

    mock_models_service.load_model = AsyncMock(
        side_effect=UpstreamModelError(404, "model_not_found", "Model not found")
    )

    app, store = _make_admin_app(db_engine, mock_models_service)
    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.post("/api/auth/register", data={"username": "admin", "password": "correct-horse-battery"})
        await _make_admin(db_engine, "admin")
        client.post("/api/auth/login", data={"username": "admin", "password": "correct-horse-battery"})

        resp = client.post("/api/models/load", data={"model": "nonexistent"})
        assert resp.status_code == 404


@pytest.mark.asyncio()
async def test_load_model_upstream_4xx_detail_structure(
    db_engine: AsyncEngine,
    mock_models_service: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/models/load with upstream 400 → structured {message, type, code, param} detail.

    Uses a 400 (not 404) so the SPA-fallback handler does not intercept the
    response (the app's 404 handler serves the SPA shell for browser navigation;
    non-404 4xx pass through the default JSON error handler).
    """
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.config import get_settings

    get_settings.cache_clear()

    mock_models_service.load_model = AsyncMock(
        side_effect=UpstreamModelError(
            400,
            "invalid_request",
            "Missing required field 'model'",
            code="missing_required_parameter",
            param="model",
        )
    )

    app, store = _make_admin_app(db_engine, mock_models_service)
    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.post("/api/auth/register", data={"username": "admin", "password": "correct-horse-battery"})
        await _make_admin(db_engine, "admin")
        client.post("/api/auth/login", data={"username": "admin", "password": "correct-horse-battery"})

        resp = client.post("/api/models/load", data={"model": "some-model"})
        assert resp.status_code == 400
        body = resp.json()
        # Fix 2: structured error detail preserves all upstream fields.
        assert body["detail"]["message"] == "Missing required field 'model'"
        assert body["detail"]["type"] == "invalid_request"
        assert body["detail"]["code"] == "missing_required_parameter"
        assert body["detail"]["param"] == "model"


@pytest.mark.asyncio()
async def test_load_model_upstream_5xx_returns_502(
    db_engine: AsyncEngine,
    mock_models_service: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/models/load with upstream 503 → our 502."""
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.config import get_settings

    get_settings.cache_clear()

    mock_models_service.load_model = AsyncMock(
        side_effect=UpstreamGatewayError(503, "upstream error")
    )

    app, store = _make_admin_app(db_engine, mock_models_service)
    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.post("/api/auth/register", data={"username": "admin", "password": "correct-horse-battery"})
        await _make_admin(db_engine, "admin")
        client.post("/api/auth/login", data={"username": "admin", "password": "correct-horse-battery"})

        resp = client.post("/api/models/load", data={"model": "some-model"})
        assert resp.status_code == 502


@pytest.mark.asyncio()
async def test_load_model_rejects_json_body(
    db_engine: AsyncEngine,
    mock_models_service: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/models/load with JSON body → 422 (form-encoded invariant)."""
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.config import get_settings

    get_settings.cache_clear()

    mock_models_service.load_model = AsyncMock(
        return_value=ModelLoadResult(instance_id="x", status="loaded")
    )

    app, store = _make_admin_app(db_engine, mock_models_service)
    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.post("/api/auth/register", data={"username": "admin", "password": "correct-horse-battery"})
        await _make_admin(db_engine, "admin")
        client.post("/api/auth/login", data={"username": "admin", "password": "correct-horse-battery"})

        resp = client.post(
            "/api/models/load",
            json={"model": "qwen3-8b"},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/models/unload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_unload_model_requires_admin(
    db_engine: AsyncEngine,
    mock_models_service: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/models/unload as non-admin → 403."""
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.config import get_settings

    get_settings.cache_clear()

    app, store = _make_admin_app(db_engine, mock_models_service)
    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        await _insert_user_async(db_engine, "__placeholder__", "placeholder-pw")
        await _insert_user_async(db_engine, "nonadmin2", "correct-horse-battery")
        client.post("/api/auth/login", data={"username": "nonadmin2", "password": "correct-horse-battery"})
        resp = client.post("/api/models/unload", data={"instance_id": "inst-1"})
        assert resp.status_code == 403


@pytest.mark.asyncio()
async def test_unload_single_instance_success(
    db_engine: AsyncEngine,
    mock_models_service: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/models/unload with instance_id → 200 + {ok, unloaded}."""
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.config import get_settings

    get_settings.cache_clear()

    mock_models_service.unload_instance = AsyncMock(
        return_value=ModelUnloadResult(instance_id="qwen3-8b")
    )

    app, store = _make_admin_app(db_engine, mock_models_service)
    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.post("/api/auth/register", data={"username": "admin", "password": "correct-horse-battery"})
        await _make_admin(db_engine, "admin")
        client.post("/api/auth/login", data={"username": "admin", "password": "correct-horse-battery"})

        resp = client.post(
            "/api/models/unload",
            data={"instance_id": "qwen3-8b"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["unloaded"] == ["qwen3-8b"]


@pytest.mark.asyncio()
async def test_unload_all_success(
    db_engine: AsyncEngine,
    mock_models_service: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/models/unload with all=true → 200 + unloaded list."""
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.config import get_settings

    get_settings.cache_clear()

    mock_models_service.unload_all_instances = AsyncMock(
        return_value=UnloadAllResult(
            succeeded=["qwen3-8b", "qwen3-8b:2"],
            failed=[],
        )
    )

    app, store = _make_admin_app(db_engine, mock_models_service)
    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.post("/api/auth/register", data={"username": "admin", "password": "correct-horse-battery"})
        await _make_admin(db_engine, "admin")
        client.post("/api/auth/login", data={"username": "admin", "password": "correct-horse-battery"})

        resp = client.post(
            "/api/models/unload",
            data={"all": "true", "model": "qwen3-8b"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert len(body["unloaded"]) == 2
        assert body["failed"] == []


@pytest.mark.asyncio()
async def test_unload_requires_instance_id_or_all(
    db_engine: AsyncEngine,
    mock_models_service: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/models/unload without instance_id or all → 400."""
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.config import get_settings

    get_settings.cache_clear()

    app, store = _make_admin_app(db_engine, mock_models_service)
    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.post("/api/auth/register", data={"username": "admin", "password": "correct-horse-battery"})
        await _make_admin(db_engine, "admin")
        client.post("/api/auth/login", data={"username": "admin", "password": "correct-horse-battery"})

        # No instance_id, no all — should 400.
        resp = client.post("/api/models/unload", data={})
        assert resp.status_code == 400


@pytest.mark.asyncio()
async def test_unload_upstream_4xx_returns_same_status(
    db_engine: AsyncEngine,
    mock_models_service: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/models/unload with upstream 404 → our 404."""
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.config import get_settings

    get_settings.cache_clear()

    mock_models_service.unload_instance = AsyncMock(
        side_effect=UpstreamModelError(404, "model_not_found", "not loaded")
    )

    app, store = _make_admin_app(db_engine, mock_models_service)
    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.post("/api/auth/register", data={"username": "admin", "password": "correct-horse-battery"})
        await _make_admin(db_engine, "admin")
        client.post("/api/auth/login", data={"username": "admin", "password": "correct-horse-battery"})

        resp = client.post(
            "/api/models/unload",
            data={"instance_id": "nonexistent"},
        )
        assert resp.status_code == 404


@pytest.mark.asyncio()
async def test_unload_model_rejects_json_body(
    db_engine: AsyncEngine,
    mock_models_service: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/models/unload with JSON body → 400 (form-encoded invariant).

    The unload route has optional form fields (all default to None/False), so a
    JSON body doesn't cause a 422.  Instead, the route logic rejects the request
    with 400 because no usable form fields are present.
    """
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.config import get_settings

    get_settings.cache_clear()

    mock_models_service.unload_instance = AsyncMock(
        return_value=ModelUnloadResult(instance_id="x")
    )

    app, store = _make_admin_app(db_engine, mock_models_service)
    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.post("/api/auth/register", data={"username": "admin", "password": "correct-horse-battery"})
        await _make_admin(db_engine, "admin")
        client.post("/api/auth/login", data={"username": "admin", "password": "correct-horse-battery"})

        resp = client.post(
            "/api/models/unload",
            json={"instance_id": "qwen3-8b"},
        )
        # Optional form fields → JSON body is ignored → handler returns 400 (no
        # instance_id or all=true in form fields).
        assert resp.status_code == 400


@pytest.mark.asyncio()
async def test_download_model_rejects_json_body(
    db_engine: AsyncEngine,
    mock_models_service: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/models/download with JSON body → 422 (form-encoded invariant)."""
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.config import get_settings

    get_settings.cache_clear()

    from lmchat.services.models_service import ModelDownloadResult

    mock_models_service.download_model = AsyncMock(
        return_value=ModelDownloadResult(status="ok")
    )

    app, store = _make_admin_app(db_engine, mock_models_service)
    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.post("/api/auth/register", data={"username": "admin", "password": "correct-horse-battery"})
        await _make_admin(db_engine, "admin")
        client.post("/api/auth/login", data={"username": "admin", "password": "correct-horse-battery"})

        resp = client.post(
            "/api/models/download",
            json={"model": "bartowski/Phi-3.5-mini-instruct-GGUF/file.gguf"},
        )
        assert resp.status_code == 422


@pytest.mark.asyncio()
async def test_unload_all_partial_failure_returns_both_lists(
    db_engine: AsyncEngine,
    mock_models_service: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/models/unload all=true with mid-batch failure → 200 + both lists."""
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.config import get_settings

    get_settings.cache_clear()

    mock_models_service.unload_all_instances = AsyncMock(
        return_value=UnloadAllResult(
            succeeded=["qwen3-8b"],
            failed=[("qwen3-8b:2", "LM Studio 404: [model_not_found] not loaded")],
        )
    )

    app, store = _make_admin_app(db_engine, mock_models_service)
    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.post("/api/auth/register", data={"username": "admin", "password": "correct-horse-battery"})
        await _make_admin(db_engine, "admin")
        client.post("/api/auth/login", data={"username": "admin", "password": "correct-horse-battery"})

        resp = client.post(
            "/api/models/unload",
            data={"all": "true", "model": "qwen3-8b"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["unloaded"] == ["qwen3-8b"]
        assert len(body["failed"]) == 1
        assert body["failed"][0]["instance_id"] == "qwen3-8b:2"
        assert "not loaded" in body["failed"][0]["error"]


# ---------------------------------------------------------------------------
# P12c — rate-limit 429 tests for lifecycle routes
# ---------------------------------------------------------------------------


def _make_exhausted_rate_limit_app(
    db_engine: AsyncEngine,
    mock_svc: MagicMock,
) -> tuple[_FastAPI, SQLiteSessionStore]:
    """Build app with admin_rate_limit overridden to always return 429."""
    from fastapi import HTTPException as _HTTPException

    from lmchat.config import get_settings

    get_settings.cache_clear()
    app = create_app()
    store = SQLiteSessionStore(engine=db_engine)
    app.dependency_overrides[get_engine_dep] = lambda: db_engine
    app.dependency_overrides[get_default_session_store_dep] = lambda: store
    app.dependency_overrides[get_models_service_dep] = lambda: mock_svc
    # Override rate-limit dependency to simulate an exhausted bucket.

    async def _always_429() -> None:
        raise _HTTPException(
            status_code=429,
            detail="Admin rate limit exceeded. Retry after 60s.",
            headers={"Retry-After": "60"},
        )

    app.dependency_overrides[admin_rate_limit] = _always_429
    return app, store


@pytest.mark.asyncio()
async def test_load_model_rate_limit_exhausted_returns_429(
    db_engine: AsyncEngine,
    mock_models_service: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/models/load with exhausted rate limit → 429 + Retry-After."""
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.config import get_settings

    get_settings.cache_clear()

    app, store = _make_exhausted_rate_limit_app(db_engine, mock_models_service)
    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.post("/api/auth/register", data={"username": "admin", "password": "correct-horse-battery"})
        await _make_admin(db_engine, "admin")
        client.post("/api/auth/login", data={"username": "admin", "password": "correct-horse-battery"})

        resp = client.post("/api/models/load", data={"model": "any-model"})
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers


@pytest.mark.asyncio()
async def test_unload_model_rate_limit_exhausted_returns_429(
    db_engine: AsyncEngine,
    mock_models_service: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/models/unload with exhausted rate limit → 429 + Retry-After."""
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.config import get_settings

    get_settings.cache_clear()

    app, store = _make_exhausted_rate_limit_app(db_engine, mock_models_service)
    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.post("/api/auth/register", data={"username": "admin", "password": "correct-horse-battery"})
        await _make_admin(db_engine, "admin")
        client.post("/api/auth/login", data={"username": "admin", "password": "correct-horse-battery"})

        resp = client.post("/api/models/unload", data={"instance_id": "inst-1"})
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers


@pytest.mark.asyncio()
async def test_download_model_rate_limit_exhausted_returns_429(
    db_engine: AsyncEngine,
    mock_models_service: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/models/download with exhausted rate limit → 429 + Retry-After."""
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.config import get_settings

    get_settings.cache_clear()

    app, store = _make_exhausted_rate_limit_app(db_engine, mock_models_service)
    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.post("/api/auth/register", data={"username": "admin", "password": "correct-horse-battery"})
        await _make_admin(db_engine, "admin")
        client.post("/api/auth/login", data={"username": "admin", "password": "correct-horse-battery"})

        resp = client.post("/api/models/download", data={"model": "publisher/model/file.gguf"})
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers


@pytest.mark.asyncio()
async def test_refresh_models_rate_limit_exhausted_returns_429(
    db_engine: AsyncEngine,
    mock_models_service: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/admin/models/refresh with exhausted rate limit → 429 + Retry-After."""
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.config import get_settings

    get_settings.cache_clear()

    app, store = _make_exhausted_rate_limit_app(db_engine, mock_models_service)
    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.post("/api/auth/register", data={"username": "admin", "password": "correct-horse-battery"})
        await _make_admin(db_engine, "admin")
        client.post("/api/auth/login", data={"username": "admin", "password": "correct-horse-battery"})

        resp = client.post("/api/admin/models/refresh")
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers
