# SPDX-License-Identifier: Apache-2.0
"""Integration tests for admin routes.

Tests for the admin routes.

Uses FastAPI TestClient with a real per-test SQLite DB.  The global engine
is redirected to the per-test DB (same pattern as test_streaming.py) so
AuthMiddleware, route handlers, and service code all share one DB.

Tests
-----
- test_admin_users_list_requires_admin      — non-admin → 403
- test_admin_users_list_requires_auth       — no session → 401
- test_admin_users_list_returns_pagination  — limit+offset paginates correctly
- test_admin_role_toggle_audit_logged       — audit row written on role change
- test_admin_role_toggle_404_missing_user   — 404 on unknown user
- test_admin_revoke_sessions_calls_store    — sessions deleted + audit row
- test_admin_revoke_sessions_404_unknown    — 404 for unknown user
- test_admin_audit_log_paginated            — returns paginated rows
- test_admin_audit_log_event_filter         — event= query param filters rows
- test_admin_debug_no_secrets_returned      — /api/debug safe response
- test_admin_debug_requires_admin           — non-admin → 403
- test_admin_rate_limit_429_after_threshold — 429 after bucket exhausted
"""
from __future__ import annotations

from collections.abc import Generator, Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import audit_log, metadata, sessions, users
from lmchat.middleware._bucket_store import InMemoryBucketStore
from lmchat.routes._dependencies import (
    admin_rate_limit,
    get_models_service_dep,
)
from lmchat.services.auth_service import _reset_dummy_hash_cache
from lmchat.utils.hashing import hash_password

_LOW_N: int = 2**10


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _make_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:  # noqa: ANN401
    """Build the app with per-test DB isolation.

    Redirects the global engine (used by AuthMiddleware) to the per-test DB
    by setting DATABASE_URL env var and resetting the engine singleton.
    """
    from lmchat.app import create_app
    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod

    db_url = f"sqlite+aiosqlite:///{tmp_path}/admin_route_test.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    monkeypatch.setenv("LM_CHAT_ADMIN_RATE_LIMIT_PER_MIN", "30")

    get_settings.cache_clear()
    engine_mod.dispose_engine()
    _reset_dummy_hash_cache()

    app = create_app()

    stub_models = AsyncMock()
    stub_models.list_loaded = AsyncMock(return_value=[MagicMock(), MagicMock()])
    stub_models.refresh = AsyncMock(return_value=None)
    app.dependency_overrides[get_models_service_dep] = lambda: stub_models

    # Bypass admin_rate_limit for most tests (restored in rate-limit test).
    # The override must have a FastAPI-compatible signature (no **kwargs) so
    # FastAPI doesn't interpret the kwargs as query params.
    async def _noop_rl() -> None:
        return None

    app.dependency_overrides[admin_rate_limit] = _noop_rl

    return app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Clear settings cache and dummy hash cache around each test."""
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
def test_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[TestClient]:
    """TestClient with a per-test DB and stub models service."""
    app = _make_app(tmp_path, monkeypatch)

    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        yield client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _engine_for(tmp_path: Path) -> AsyncEngine:
    """Return the per-test async engine (mirroring the DATABASE_URL set by _make_app)."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/admin_route_test.db"
    eng = create_async_engine(db_url, pool_pre_ping=True)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return eng


async def _insert_user(
    tmp_path: Path,
    username: str,
    password: str = "test-pw",
    is_admin: bool = False,
) -> int:
    """Insert a user at low-cost scrypt into the test DB."""
    pw_hash = hash_password(password, n=_LOW_N, r=8, p=1)
    eng = await _engine_for(tmp_path)
    try:
        async with eng.begin() as conn:
            id_result = await conn.execute(
                select(func.coalesce(func.max(users.c.id), 0) + 1)
            )
            next_id = int(id_result.scalar() or 1)
            await conn.execute(
                text(
                    "INSERT INTO users (id, username, password_hash, is_admin)"
                    " VALUES (:id, :u, :ph, :ia)"
                ),
                {"id": next_id, "u": username, "ph": pw_hash, "ia": int(is_admin)},
            )
    finally:
        await eng.dispose()
    return next_id


async def _count_audit(tmp_path: Path, event: str) -> int:
    """Count audit_log rows for *event* in the test DB."""
    eng = await _engine_for(tmp_path)
    try:
        async with eng.connect() as conn:
            result = await conn.execute(
                select(func.count())
                .select_from(audit_log)
                .where(audit_log.c.event == event)
            )
            return int(result.scalar() or 0)
    finally:
        await eng.dispose()


def _login(client: TestClient, username: str, password: str = "test-pw") -> None:
    """POST /api/auth/login and assert success."""
    resp = client.post(
        "/api/auth/login",
        data={"username": username, "password": password},
    )
    assert resp.status_code == 200, f"Login failed ({resp.status_code}): {resp.text}"


async def _setup_admin(
    tmp_path: Path, client: TestClient
) -> tuple[int, int]:
    """Create admin + regular user; log in as admin.

    Returns:
        (admin_id, regular_user_id)
    """
    admin_id = await _insert_user(tmp_path, "admin_u", "admin_pw", is_admin=True)
    user_id = await _insert_user(tmp_path, "regular_u", "reg_pw", is_admin=False)
    _login(client, "admin_u", "admin_pw")
    return admin_id, user_id


# ---------------------------------------------------------------------------
# Tests: access control
# ---------------------------------------------------------------------------


async def test_admin_users_list_requires_admin(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Non-admin user receives 403."""
    await _insert_user(tmp_path, "nonadmin", is_admin=False)
    _login(test_client, "nonadmin")
    resp = test_client.get("/api/admin/users")
    assert resp.status_code == 403, resp.text


async def test_admin_users_list_requires_auth(test_client: TestClient) -> None:
    """Unauthenticated caller receives 401."""
    test_client.cookies.clear()
    resp = test_client.get("/api/admin/users")
    assert resp.status_code == 401, (
        f"Expected 401, got {resp.status_code}: {resp.text}"
    )


# ---------------------------------------------------------------------------
# Tests: user list pagination
# ---------------------------------------------------------------------------


async def test_admin_users_list_returns_pagination(
    tmp_path: Path, test_client: TestClient
) -> None:
    """GET /api/admin/users paginates and excludes secret fields."""
    await _setup_admin(tmp_path, test_client)
    # Insert 3 extras so total = 5.
    for i in range(3):
        await _insert_user(tmp_path, f"extra_{i}")

    resp_p1 = test_client.get("/api/admin/users?limit=2&offset=0")
    assert resp_p1.status_code == 200, resp_p1.text
    page1 = resp_p1.json()
    assert len(page1) == 2

    resp_p2 = test_client.get("/api/admin/users?limit=2&offset=2")
    assert resp_p2.status_code == 200, resp_p2.text
    page2 = resp_p2.json()
    assert len(page2) == 2

    for row in page1 + page2:
        assert "password_hash" not in row
        assert "totp_secret" not in row
        assert "id" in row
        assert "username" in row
        assert "is_admin" in row


# ---------------------------------------------------------------------------
# Tests: role toggle
# ---------------------------------------------------------------------------


async def test_admin_role_toggle_audit_logged(
    tmp_path: Path, test_client: TestClient
) -> None:
    """POST role sets is_admin and writes an audit row."""
    admin_id, user_id = await _setup_admin(tmp_path, test_client)

    before = await _count_audit(tmp_path, "admin.users.role_changed")

    resp = test_client.post(
        f"/api/admin/users/{user_id}/role",
        data={"is_admin": "true"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["is_admin"] is True
    assert "password_hash" not in body
    assert "totp_secret" not in body

    after = await _count_audit(tmp_path, "admin.users.role_changed")
    assert after == before + 1


async def test_admin_role_toggle_survives_audit_write_failure_and_logs_error(
    tmp_path: Path, test_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST role still succeeds (200) even when the audit write fails.

    A failed ``admin.users.role_changed`` audit
    write is escalated to an ERROR log + metric increment instead of the
    old best-effort WARNING — but the role change itself (already applied
    to the DB) is never blocked or rolled back (proceed-but-loud).
    """
    from lmchat.services import audit_service

    admin_id, user_id = await _setup_admin(tmp_path, test_client)

    async def _boom(**_kwargs: object) -> None:
        raise RuntimeError("simulated DB write failure")

    monkeypatch.setattr(audit_service, "write_audit_event", _boom)

    error_events: list[str] = []
    original_error = audit_service.log.error

    def _spy_error(event: str, **kwargs: object) -> None:
        error_events.append(event)
        original_error(event, **kwargs)

    monkeypatch.setattr(audit_service.log, "error", _spy_error)

    before = audit_service.AUDIT_WRITE_FAILURES_CRITICAL.labels(
        event="admin.users.role_changed"
    )._value.get()

    resp = test_client.post(
        f"/api/admin/users/{user_id}/role",
        data={"is_admin": "true"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_admin"] is True

    after = audit_service.AUDIT_WRITE_FAILURES_CRITICAL.labels(
        event="admin.users.role_changed"
    )._value.get()
    assert after == before + 1
    assert error_events == [
        "critical audit write failed — compliance record incomplete"
    ]


async def test_admin_role_toggle_404_missing_user(
    tmp_path: Path, test_client: TestClient
) -> None:
    """POST role returns 404 for non-existent user."""
    await _setup_admin(tmp_path, test_client)
    resp = test_client.post("/api/admin/users/9999/role", data={"is_admin": "true"})
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# Tests: revoke sessions
# ---------------------------------------------------------------------------


async def test_admin_revoke_sessions_calls_session_store(
    tmp_path: Path, test_client: TestClient
) -> None:
    """POST revoke-sessions deletes target's sessions and writes audit row."""
    admin_id, user_id = await _setup_admin(tmp_path, test_client)

    eng = await _engine_for(tmp_path)
    try:
        async with eng.begin() as conn:
            await conn.execute(
                insert(sessions).values(
                    id="fake-session-xyz",
                    user_id=user_id,
                    expires_at=datetime.utcnow() + timedelta(days=1),
                )
            )
    finally:
        await eng.dispose()

    resp = test_client.post(f"/api/admin/users/{user_id}/revoke-sessions")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"

    eng2 = await _engine_for(tmp_path)
    try:
        async with eng2.connect() as conn:
            remaining = int(
                (
                    await conn.execute(
                        select(func.count())
                        .select_from(sessions)
                        .where(sessions.c.user_id == user_id)
                    )
                ).scalar()
                or 0
            )
    finally:
        await eng2.dispose()

    assert remaining == 0, "Expected 0 sessions after revoke"
    assert await _count_audit(tmp_path, "admin.users.sessions_revoked") >= 1


async def test_admin_revoke_sessions_404_unknown(
    tmp_path: Path, test_client: TestClient
) -> None:
    """POST revoke-sessions returns 404 for non-existent user."""
    await _setup_admin(tmp_path, test_client)
    resp = test_client.post("/api/admin/users/9999/revoke-sessions")
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# Tests: audit-log
# ---------------------------------------------------------------------------


async def test_admin_audit_log_paginated(
    tmp_path: Path, test_client: TestClient
) -> None:
    """GET /api/admin/audit-log returns AuditLogPage with items + total + limit + offset."""
    await _setup_admin(tmp_path, test_client)

    resp = test_client.get("/api/admin/audit-log?limit=5&offset=0")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "items" in body
    assert "total" in body
    assert "limit" in body
    assert "offset" in body
    assert isinstance(body["items"], list)
    assert len(body["items"]) <= 5
    assert isinstance(body["total"], int)
    assert body["limit"] == 5
    assert body["offset"] == 0

    if body["items"]:
        row = body["items"][0]
        assert "id" in row
        assert "event" in row
        assert "created_at" in row

    resp2 = test_client.get("/api/admin/audit-log?limit=5&offset=9999")
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert body2["items"] == []
    # total reflects the full table count even when offset overshoots; this
    # endpoint itself emits admin.audit_log.viewed on every call (auditing
    # the audit log itself), so total grows by 1 between the two requests.
    assert body2["total"] >= body["total"]


async def test_admin_audit_log_event_filter(
    tmp_path: Path, test_client: TestClient
) -> None:
    """GET /api/admin/audit-log?event= filters to matching rows only."""
    await _setup_admin(tmp_path, test_client)

    resp = test_client.get("/api/admin/audit-log?event=auth.login.success")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    for row in body["items"]:
        assert row["event"] == "auth.login.success"


# ---------------------------------------------------------------------------
# Tests: debug endpoint
# ---------------------------------------------------------------------------


async def test_admin_debug_no_secrets_returned(
    tmp_path: Path, test_client: TestClient
) -> None:
    """GET /api/debug returns sanitised info — no secrets or session tokens."""
    await _setup_admin(tmp_path, test_client)

    resp = test_client.get("/api/debug")
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert "version" in data
    assert "db_dialect" in data
    assert data["db_dialect"] == "sqlite"

    forbidden = {
        "lm_studio_api_key",
        "lm_chat_secret",
        "password_hash",
        "totp_secret",
    }
    for field in forbidden:
        assert field not in data, f"Secret field '{field}' leaked in /api/debug"


async def test_admin_debug_requires_admin(
    tmp_path: Path, test_client: TestClient
) -> None:
    """GET /api/debug returns 403 for non-admin users."""
    await _insert_user(tmp_path, "nonadmin2", is_admin=False)
    _login(test_client, "nonadmin2")

    resp = test_client.get("/api/debug")
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# Tests: rate limiting
# ---------------------------------------------------------------------------


async def test_admin_rate_limit_429_after_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Admin endpoints return 429 once the token bucket is exhausted.

    Sets burst=1 so the second request is rate-limited.
    """
    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod

    db_url = f"sqlite+aiosqlite:///{tmp_path}/admin_rate_limit_test.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    monkeypatch.setenv("LM_CHAT_ADMIN_RATE_LIMIT_PER_MIN", "1")

    get_settings.cache_clear()
    engine_mod.dispose_engine()
    _reset_dummy_hash_cache()

    from lmchat.app import create_app

    app = create_app()

    stub_models = AsyncMock()
    stub_models.list_loaded = AsyncMock(return_value=[])
    stub_models.refresh = AsyncMock(return_value=None)
    app.dependency_overrides[get_models_service_dep] = lambda: stub_models
    # Do NOT override admin_rate_limit — we want the real dep to fire.

    with TestClient(app, raise_server_exceptions=True) as client:
        admin_buckets = InMemoryBucketStore()
        client.app.state.admin_buckets = admin_buckets  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]

        # Insert and log in as admin.
        pw_hash = hash_password("rl_pw", n=_LOW_N, r=8, p=1)
        eng = create_async_engine(db_url, pool_pre_ping=True)
        try:
            async with eng.begin() as conn:
                await conn.run_sync(metadata.create_all)
                await conn.execute(
                    text(
                        "INSERT INTO users (id, username, password_hash, is_admin)"
                        " VALUES (1, 'rl_admin', :ph, 1)"
                    ),
                    {"ph": pw_hash},
                )
        finally:
            await eng.dispose()

        login_resp = client.post(
            "/api/auth/login",
            data={"username": "rl_admin", "password": "rl_pw"},
        )
        assert login_resp.status_code == 200, login_resp.text

        # With burst=1, the first request to any admin route depletes the bucket.
        # The second request must return 429.
        resp1 = client.get("/api/admin/users")
        resp2 = client.get("/api/admin/users")

        statuses = {resp1.status_code, resp2.status_code}
        assert 429 in statuses, (
            f"Expected a 429 in {statuses}; burst=1 should exhaust after first request"
        )

    get_settings.cache_clear()
    engine_mod.dispose_engine()
