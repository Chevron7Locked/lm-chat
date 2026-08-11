# SPDX-License-Identifier: Apache-2.0
"""Integration tests for analytics routes.

Covers:
- GET /api/analytics/me — 200 for authed user, 401 for unauthenticated.
- GET /api/analytics/system — 200 for admin, 403 for non-admin, 401 unauthed.
- Response shapes match AnalyticsResponse / SystemAnalyticsResponse.
"""
from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: F401

from lmchat.app import create_app
from lmchat.middleware._bucket_store import InMemoryBucketStore
from lmchat.routes.analytics import _get_analytics_service
from lmchat.services.analytics_service import (
    AnalyticsService,
    SystemAnalytics,
    TopModel,
    UserAnalytics,
)
from lmchat.services.auth_service import _reset_dummy_hash_cache

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Generator[None]:
    db_url = f"sqlite+aiosqlite:///{tmp_path}/analytics_test.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod
    get_settings.cache_clear()
    engine_mod.dispose_engine()
    _reset_dummy_hash_cache()
    yield
    get_settings.cache_clear()
    engine_mod.dispose_engine()
    _reset_dummy_hash_cache()



def _make_user_analytics() -> UserAnalytics:
    return UserAnalytics(
        total_messages=5,
        total_chats=2,
        messages_last_7_days=3,
        top_models=[TopModel(model_id="llama-3", count=4)],
    )


def _make_system_analytics() -> SystemAnalytics:
    return SystemAnalytics(
        total_users=10,
        total_chats=50,
        total_messages=200,
        messages_last_7_days=30,
        top_models=[TopModel(model_id="llama-3", count=100)],
    )


@pytest.fixture()
def mock_analytics_service() -> MagicMock:
    svc = MagicMock(spec=AnalyticsService)
    svc.user_stats = AsyncMock(return_value=_make_user_analytics())
    svc.system_stats = AsyncMock(return_value=_make_system_analytics())
    return svc


@pytest.fixture()
def test_client(
    mock_analytics_service: MagicMock,
) -> Generator[TestClient]:
    app = create_app()
    app.dependency_overrides[_get_analytics_service] = lambda: mock_analytics_service

    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        yield client


def _insert_user_sync(
    client: TestClient,
    username: str,
    password: str = "correct-horse-battery",
) -> None:
    """Insert a user directly into the DB via the app's async engine.

    Bypasses the single-admin registration gate — used to seed extra users
    in tests that need a non-admin identity.
    """
    import asyncio

    from sqlalchemy import func, select, text

    from lmchat.utils.hashing import hash_password

    engine = client.app.state.engine  # type: ignore[attr-defined]
    pw_hash = hash_password(password, n=2**10, r=8, p=1)

    async def _do() -> None:
        from lmchat.db.schema import users as users_table

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

    asyncio.run(_do())


def _register_and_login(client: TestClient, username: str = "alice", password: str = "correct-horse-battery") -> None:
    """Insert the user directly (bypass the single-admin gate) then log in."""
    _insert_user_sync(client, username, password)
    client.post("/api/auth/login", data={"username": username, "password": password})


def _seed_placeholder(client: TestClient) -> None:
    """Insert a throwaway user so the next user does not get the bootstrap-admin grant.

    The first user inserted via the app (or directly) gets is_admin=True via
    auth_service.register. This placeholder is the first user, so the next
    registered non-admin test user does not get auto-promoted.
    """
    _insert_user_sync(client, "__placeholder__", "placeholder-pw")


def _make_admin(client: TestClient, username: str = "alice") -> None:
    """Promote a user to admin via the admin API (using an existing admin)."""
    # Use a raw SQL approach: look up the user id from the session DB and then
    # use the engine from the app state to promote them.
    import asyncio

    from sqlalchemy import update as sa_update

    from lmchat.db.schema import users as users_table

    engine = client.app.state.engine  # type: ignore[attr-defined]

    async def _update() -> None:
        async with engine.begin() as conn:
            await conn.execute(
                sa_update(users_table)
                .where(users_table.c.username == username)
                .values(is_admin=True)
            )

    asyncio.run(_update())


# ---------------------------------------------------------------------------
# GET /api/analytics/me
# ---------------------------------------------------------------------------


def test_me_analytics_200(test_client: TestClient) -> None:
    """Authed user gets their analytics."""
    _register_and_login(test_client)
    resp = test_client.get("/api/analytics/me")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_messages"] == 5
    assert data["total_chats"] == 2
    assert data["messages_last_7_days"] == 3
    assert len(data["top_models"]) == 1
    assert data["top_models"][0]["model_id"] == "llama-3"


def test_me_analytics_401_unauthenticated(test_client: TestClient) -> None:
    """Unauthenticated requests to /api/analytics/me return 401."""
    resp = test_client.get("/api/analytics/me")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/analytics/system
# ---------------------------------------------------------------------------


def test_system_analytics_200_admin(test_client: TestClient) -> None:
    """Admin user gets system analytics."""
    _register_and_login(test_client, "admin_user")
    _make_admin(test_client, "admin_user")
    resp = test_client.get("/api/analytics/system")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_users"] == 10
    assert data["total_chats"] == 50
    assert data["total_messages"] == 200


def test_system_analytics_403_non_admin(test_client: TestClient) -> None:
    """Non-admin user gets 403 on /api/analytics/system."""
    _seed_placeholder(test_client)
    _register_and_login(test_client, "regular_user")
    resp = test_client.get("/api/analytics/system")
    assert resp.status_code == 403


def test_system_analytics_401_unauthenticated(test_client: TestClient) -> None:
    """Unauthenticated request to /api/analytics/system returns 401."""
    resp = test_client.get("/api/analytics/system")
    assert resp.status_code == 401
