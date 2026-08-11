# SPDX-License-Identifier: Apache-2.0
"""Integration tests for quota routes.

Tests:
- GET /api/quotas/me — auth required, returns QuotaResponse
- GET /api/admin/quotas — admin-only, returns list[QuotaSummary]
- PATCH /api/admin/quotas/{user_id} — admin upsert
- GET /api/admin/quotas — non-admin 403
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import metadata
from lmchat.middleware._bucket_store import InMemoryBucketStore
from lmchat.routes._dependencies import (
    get_models_service_dep,
)
from lmchat.services.auth_service import _reset_dummy_hash_cache
from lmchat.utils.hashing import hash_password

_LOW_N: int = 2**10


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _make_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: ANN401
    from lmchat.app import create_app
    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod

    db_url = f"sqlite+aiosqlite:///{tmp_path}/quota_route_test.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")

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


async def _engine_for(tmp_path: Path) -> AsyncEngine:
    db_url = f"sqlite+aiosqlite:///{tmp_path}/quota_route_test.db"
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
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_quota_me_requires_auth(test_client: TestClient):
    """GET /api/quotas/me returns 401 for unauthenticated requests."""
    response = test_client.get("/api/quotas/me")
    assert response.status_code == 401


@pytest.mark.anyio
async def test_quota_me_returns_defaults(
    tmp_path: Path, test_client: TestClient
):
    """GET /api/quotas/me returns default limits for a user with no quotas row."""
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    response = test_client.get("/api/quotas/me")
    assert response.status_code == 200
    data = response.json()
    assert data["tokens_per_day"] == 100_000
    assert data["requests_per_day"] == 1_000
    assert data["tokens_consumed_today"] == 0
    assert data["requests_consumed_today"] == 0
    assert "resets_at" in data


@pytest.mark.anyio
async def test_admin_quotas_requires_admin(
    tmp_path: Path, test_client: TestClient
):
    """GET /api/admin/quotas returns 403 for non-admin users."""
    await _insert_user(tmp_path, "bob", is_admin=False)
    _login(test_client, "bob")
    response = test_client.get("/api/admin/quotas")
    assert response.status_code == 403


@pytest.mark.anyio
async def test_admin_quotas_returns_list(
    tmp_path: Path, test_client: TestClient
):
    """GET /api/admin/quotas returns list[QuotaSummary] for admin."""
    await _insert_user(tmp_path, "admin_user", is_admin=True)
    _login(test_client, "admin_user")
    response = test_client.get("/api/admin/quotas")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)


@pytest.mark.anyio
async def test_admin_patch_quota(
    tmp_path: Path, test_client: TestClient
):
    """PATCH /api/admin/quotas/{user_id} updates quota for the target user."""
    uid = await _insert_user(tmp_path, "admin_user2", is_admin=True)
    _login(test_client, "admin_user2")
    response = test_client.patch(
        f"/api/admin/quotas/{uid}",
        data={"tokens_per_day": "50000", "requests_per_day": "500"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["tokens_per_day"] == 50_000
    assert data["requests_per_day"] == 500
    assert data["user_id"] == uid


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="time.tzset() is POSIX-only")
@pytest.mark.anyio
@pytest.mark.parametrize("tz_name", ["Etc/GMT-12", "Etc/GMT+12"])
async def test_quota_me_usage_uses_utc_today_not_local(
    tmp_path: Path,
    test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tz_name: str,
):
    """GET /api/quotas/me must read today's usage keyed by UTC, not host-local date.

    quota_usage rows are written keyed by ``DATE('now')`` (always UTC —
    SQLite's own clock, unaffected by the host's TZ). If the READ side
    derives "today" from ``date.today()`` (host-local), a host whose
    local calendar date differs from the UTC date reads the wrong row
    and reports 0 usage despite real consumption today.
    ``Etc/GMT-12`` / ``Etc/GMT+12`` are exact opposite offsets whose
    local-matches-UTC windows are complementary, so at least one of the
    two parametrized runs always exercises the divergence regardless of
    when this test executes.
    """
    monkeypatch.setenv("TZ", tz_name)
    time.tzset()
    try:
        username = f"carol_{tz_name.replace('/', '_').replace('+', 'p')}"
        uid = await _insert_user(tmp_path, username)
        eng = await _engine_for(tmp_path)
        try:
            async with eng.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO quota_usage (user_id, date, "
                        "tokens_consumed, requests_consumed) "
                        "VALUES (:uid, DATE('now'), 42, 3)"
                    ),
                    {"uid": uid},
                )
        finally:
            await eng.dispose()

        _login(test_client, username)
        response = test_client.get("/api/quotas/me")
        assert response.status_code == 200
        data = response.json()
        assert data["tokens_consumed_today"] == 42
        assert data["requests_consumed_today"] == 3
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time.tzset()
