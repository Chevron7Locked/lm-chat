# SPDX-License-Identifier: Apache-2.0
"""Integration tests for params routes.

Tests for model parameters routes.

Routes:
  GET  /api/params/{model_id}          — requires auth
  POST /api/admin/params/invalidate    — requires admin
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.app import create_app
from lmchat.db.schema import metadata, users
from lmchat.middleware._bucket_store import InMemoryBucketStore
from lmchat.routes._dependencies import (
    get_default_session_store_dep,
    get_engine_dep,
    get_params_service_dep,
)
from lmchat.services.auth_service import _reset_dummy_hash_cache
from lmchat.services.params_service import ParamsService
from lmchat.session.sqlite_store import SQLiteSessionStore
from lmchat.utils.hashing import hash_password

_LOW_N: int = 2**10

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
    db_path = tmp_path / "test_routes_params.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}", pool_pre_ping=True)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture()
def params_svc() -> ParamsService:
    """Shared ParamsService for test isolation."""
    return ParamsService()


@pytest.fixture()
def test_client(
    db_engine: AsyncEngine,
    params_svc: ParamsService,
) -> Generator[TestClient]:
    """TestClient wired to per-test engine + shared ParamsService."""
    app = create_app()
    store = SQLiteSessionStore(engine=db_engine)
    app.dependency_overrides[get_engine_dep] = lambda: db_engine
    app.dependency_overrides[get_default_session_store_dep] = lambda: store
    app.dependency_overrides[get_params_service_dep] = lambda: params_svc

    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        yield client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _insert_user_async(engine: AsyncEngine, username: str, password: str) -> None:
    """Bypass the single-admin gate by inserting a user directly (async)."""
    from lmchat.db.schema import users as users_table

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
    asyncio.run(_insert_user_async(engine, username, password))


def _register_and_login(client: TestClient, *, engine: AsyncEngine | None = None) -> None:
    """Register and login a non-admin test user.

    Seeds a placeholder first so 'alice' is not the very first registered
    user (the bootstrap-admin grant would otherwise promote her to admin).
    """
    if engine is not None:
        _insert_user_direct(engine, "__placeholder__", "placeholder-pw")
        _insert_user_direct(engine, "alice", "correct-horse-battery")
    else:
        client.post(
            "/api/auth/register",
            data={"username": "__placeholder__", "password": "placeholder-pw"},
        )
        client.post("/api/auth/register", data={"username": "alice", "password": "correct-horse-battery"})
    client.post("/api/auth/login", data={"username": "alice", "password": "correct-horse-battery"})


async def _make_admin(engine: AsyncEngine, username: str) -> None:
    """Promote a user to admin."""
    async with engine.begin() as conn:
        await conn.execute(
            update(users).where(users.c.username == username).values(is_admin=True)
        )


# ---------------------------------------------------------------------------
# GET /api/params/{model_id}
# ---------------------------------------------------------------------------


def test_get_params_requires_auth(test_client: TestClient) -> None:
    """GET /api/params/X without session → 401."""
    resp = test_client.get("/api/params/test-model")
    assert resp.status_code == 401


def test_get_params_returns_empty_for_unknown_model(
    test_client: TestClient,
    db_engine: AsyncEngine,
) -> None:
    """GET /api/params/X for a model not in cache → 200 + empty rejected list."""
    _register_and_login(test_client, engine=db_engine)
    resp = test_client.get("/api/params/never-seen-model")
    assert resp.status_code == 200
    body = resp.json()
    assert body["model_id"] == "never-seen-model"
    assert body["rejected"] == []


@pytest.mark.asyncio()
async def test_get_params_returns_known_rejections(
    test_client: TestClient,
    params_svc: ParamsService,
    db_engine: AsyncEngine,
) -> None:
    """GET /api/params/X returns sorted rejected list when cache has data."""
    await params_svc.record_rejection(model_id="my-model", param="top_k")
    await params_svc.record_rejection(model_id="my-model", param="min_p")

    await _insert_user_async(db_engine, "__placeholder__", "placeholder-pw")
    await _insert_user_async(db_engine, "alice", "correct-horse-battery")
    test_client.post("/api/auth/login", data={"username": "alice", "password": "correct-horse-battery"})
    resp = test_client.get("/api/params/my-model")
    assert resp.status_code == 200
    body = resp.json()
    assert body["model_id"] == "my-model"
    # Sorted alphabetically
    assert body["rejected"] == ["min_p", "top_k"]


# ---------------------------------------------------------------------------
# POST /api/admin/params/invalidate
# ---------------------------------------------------------------------------


def test_invalidate_params_requires_auth(test_client: TestClient) -> None:
    """POST /api/admin/params/invalidate without session → 401."""
    resp = test_client.post("/api/admin/params/invalidate")
    assert resp.status_code == 401


def test_invalidate_params_requires_admin(test_client: TestClient, db_engine: AsyncEngine) -> None:
    """POST /api/admin/params/invalidate as non-admin → 403."""
    _register_and_login(test_client, engine=db_engine)
    resp = test_client.post("/api/admin/params/invalidate")
    assert resp.status_code == 403


@pytest.mark.asyncio()
async def test_invalidate_params_clears_specific_model(
    db_engine: AsyncEngine,
    params_svc: ParamsService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/admin/params/invalidate?model_id=X clears that model's cache."""
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.config import get_settings

    get_settings.cache_clear()

    await params_svc.record_rejection(model_id="model-a", param="top_k")
    await params_svc.record_rejection(model_id="model-b", param="min_p")

    app = create_app()
    store = SQLiteSessionStore(engine=db_engine)
    app.dependency_overrides[get_engine_dep] = lambda: db_engine
    app.dependency_overrides[get_default_session_store_dep] = lambda: store
    app.dependency_overrides[get_params_service_dep] = lambda: params_svc

    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        # Create admin user
        client.post("/api/auth/register", data={"username": "admin", "password": "correct-horse-battery"})
        await _make_admin(db_engine, "admin")
        client.post("/api/auth/login", data={"username": "admin", "password": "correct-horse-battery"})

        resp = client.post("/api/admin/params/invalidate?model_id=model-a")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["model_id"] == "model-a"

    # model-a cleared; model-b intact
    assert await params_svc.get_rejected(model_id="model-a") == frozenset()
    assert await params_svc.get_rejected(model_id="model-b") == frozenset({"min_p"})


@pytest.mark.asyncio()
async def test_invalidate_params_clears_all_when_no_model_id(
    db_engine: AsyncEngine,
    params_svc: ParamsService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/admin/params/invalidate (no model_id) clears entire cache."""
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.config import get_settings

    get_settings.cache_clear()

    await params_svc.record_rejection(model_id="model-a", param="top_k")
    await params_svc.record_rejection(model_id="model-b", param="min_p")

    app = create_app()
    store = SQLiteSessionStore(engine=db_engine)
    app.dependency_overrides[get_engine_dep] = lambda: db_engine
    app.dependency_overrides[get_default_session_store_dep] = lambda: store
    app.dependency_overrides[get_params_service_dep] = lambda: params_svc

    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.post("/api/auth/register", data={"username": "admin2", "password": "correct-horse-battery"})
        await _make_admin(db_engine, "admin2")
        client.post("/api/auth/login", data={"username": "admin2", "password": "correct-horse-battery"})

        resp = client.post("/api/admin/params/invalidate")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["model_id"] is None

    # Both cleared
    assert await params_svc.get_rejected(model_id="model-a") == frozenset()
    assert await params_svc.get_rejected(model_id="model-b") == frozenset()
