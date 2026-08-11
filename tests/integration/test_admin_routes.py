# SPDX-License-Identifier: Apache-2.0
"""Live-backend integration tests for admin routes.

P10a.1 — covers all 7 endpoints in routes/admin.py against a live
uvicorn instance (not the ASGI TestClient).

Endpoints covered
-----------------
1.  GET  /api/admin/users
2.  POST /api/admin/users/{user_id}/role
3.  POST /api/admin/users/{user_id}/revoke-sessions
4.  GET  /api/admin/audit-log
5.  GET  /api/debug
6.  POST /api/admin/models/refresh
7.  GET  /api/admin/users/count

Each endpoint has at least one happy-path and one error-path test.
Admin 403 (non-admin authenticated) and 401 (unauthenticated) are
covered for every admin endpoint.
"""
from __future__ import annotations

import secrets
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration.conftest import (
    make_admin,
    register_and_login,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_fresh_admin(
    live_servers: dict[str, Any],
    db_engine: AsyncEngine,
) -> tuple[httpx.AsyncClient, dict[str, Any], str]:
    """Return an isolated async client + admin user dict + cookie.

    Each call creates a new admin user in a fresh isolated client so tests
    do not share session state with the module-scoped ``client`` fixture.
    """
    base_url = live_servers["base_url"]
    c = httpx.AsyncClient(base_url=base_url, follow_redirects=True, timeout=15.0)
    uname = f"adm_{secrets.token_hex(6)}"
    user, cookie = await register_and_login(c, uname)
    await make_admin(db_engine, user["id"])
    # Re-login so the session is marked after admin promotion.
    login_resp = await c.post(
        "/api/auth/login",
        data={"username": uname, "password": "integration-pw-OK"},
    )
    assert login_resp.status_code == 200, f"admin re-login failed: {login_resp.text}"
    cookie = login_resp.cookies.get("lmchat_session") or ""
    c.cookies.set("lmchat_session", cookie)
    return c, user, cookie


async def _make_fresh_regular(
    live_servers: dict[str, Any],
) -> tuple[httpx.AsyncClient, dict[str, Any], str]:
    """Return an isolated async client + regular (non-admin) user dict + cookie."""
    base_url = live_servers["base_url"]
    c = httpx.AsyncClient(base_url=base_url, follow_redirects=True, timeout=15.0)
    uname = f"reg_{secrets.token_hex(6)}"
    user, cookie = await register_and_login(c, uname)
    c.cookies.set("lmchat_session", cookie)
    return c, user, cookie


# ---------------------------------------------------------------------------
# GET /api/admin/users
# ---------------------------------------------------------------------------


async def test_admin_users_list_happy(
    live_servers: dict[str, Any],
    db_engine: AsyncEngine,
) -> None:
    """GET /admin/users as admin → 200, list of UserResponse objects."""
    adm_c, adm_user, _ = await _make_fresh_admin(live_servers, db_engine)
    try:
        # Paginate through all users (limit=200 pages) until the admin is found.
        # The integration server accumulates users across the entire test session,
        # so the newly created admin may have a high ID beyond the first page.
        target_id = adm_user["id"]
        found = False
        offset = 0
        page_limit = 200
        all_rows: list[dict] = []
        while True:
            resp = await adm_c.get(f"/api/admin/users?limit={page_limit}&offset={offset}")
            assert resp.status_code == 200, resp.text
            page = resp.json()
            assert isinstance(page, list)
            all_rows.extend(page)
            if target_id in [u["id"] for u in page]:
                found = True
                break
            if len(page) < page_limit:
                # Last page — exhausted all users without finding the admin.
                break
            offset += page_limit
        assert found, (
            f"Admin user id={target_id} not found in any page of /api/admin/users"
        )
        # Secret columns must be absent from every returned row.
        for row in all_rows:
            assert "password_hash" not in row
            assert "totp_secret" not in row
            assert "id" in row
            assert "username" in row
            assert "is_admin" in row
    finally:
        await adm_c.aclose()


async def test_admin_users_list_403_non_admin(
    live_servers: dict[str, Any],
) -> None:
    """GET /admin/users as a non-admin user → 403."""
    reg_c, _, _ = await _make_fresh_regular(live_servers)
    try:
        resp = await reg_c.get("/api/admin/users")
        assert resp.status_code == 403, resp.text
    finally:
        await reg_c.aclose()


async def test_admin_users_list_401_unauthed(
    live_servers: dict[str, Any],
) -> None:
    """GET /admin/users without any session → 401."""
    base_url = live_servers["base_url"]
    async with httpx.AsyncClient(
        base_url=base_url, follow_redirects=True, timeout=15.0
    ) as anon:
        resp = await anon.get("/api/admin/users")
        assert resp.status_code == 401, resp.text


async def test_admin_users_list_pagination(
    live_servers: dict[str, Any],
    db_engine: AsyncEngine,
) -> None:
    """GET /admin/users?limit=2 returns at most 2 rows."""
    adm_c, _, _ = await _make_fresh_admin(live_servers, db_engine)
    try:
        # Register a few more users to ensure there are enough rows.
        for _ in range(3):
            uname = f"pag_{secrets.token_hex(4)}"
            await adm_c.post(
                "/api/auth/register",
                data={"username": uname, "password": "pag-pw"},
            )
        resp = await adm_c.get("/api/admin/users?limit=2&offset=0")
        assert resp.status_code == 200
        assert len(resp.json()) <= 2
    finally:
        await adm_c.aclose()


# ---------------------------------------------------------------------------
# POST /api/admin/users/{user_id}/role
# ---------------------------------------------------------------------------


async def test_admin_set_role_happy(
    live_servers: dict[str, Any],
    db_engine: AsyncEngine,
) -> None:
    """POST /admin/users/{id}/role → 200, UserResponse with updated is_admin."""
    adm_c, _, _ = await _make_fresh_admin(live_servers, db_engine)
    reg_c, reg_user, _ = await _make_fresh_regular(live_servers)
    try:
        resp = await adm_c.post(
            f"/api/admin/users/{reg_user['id']}/role",
            data={"is_admin": "true"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["id"] == reg_user["id"]
        assert body["is_admin"] is True
        # Secret columns absent.
        assert "password_hash" not in body
        assert "totp_secret" not in body
    finally:
        await adm_c.aclose()
        await reg_c.aclose()


async def test_admin_set_role_403_non_admin(
    live_servers: dict[str, Any],
    db_engine: AsyncEngine,
) -> None:
    """POST /admin/users/{id}/role as non-admin → 403."""
    reg_c, reg_user, _ = await _make_fresh_regular(live_servers)
    reg2_c, reg2_user, _ = await _make_fresh_regular(live_servers)
    try:
        resp = await reg_c.post(
            f"/api/admin/users/{reg2_user['id']}/role",
            data={"is_admin": "true"},
        )
        assert resp.status_code == 403, resp.text
    finally:
        await reg_c.aclose()
        await reg2_c.aclose()


async def test_admin_set_role_401_unauthed(
    live_servers: dict[str, Any],
) -> None:
    """POST /admin/users/{id}/role without session → 401."""
    base_url = live_servers["base_url"]
    async with httpx.AsyncClient(
        base_url=base_url, follow_redirects=True, timeout=15.0
    ) as anon:
        resp = await anon.post(
            "/api/admin/users/1/role",
            data={"is_admin": "true"},
        )
        assert resp.status_code == 401, resp.text


async def test_admin_set_role_404_unknown_user(
    live_servers: dict[str, Any],
    db_engine: AsyncEngine,
) -> None:
    """POST /admin/users/99999/role where user doesn't exist → 404."""
    adm_c, _, _ = await _make_fresh_admin(live_servers, db_engine)
    try:
        resp = await adm_c.post(
            "/api/admin/users/99999/role",
            data={"is_admin": "true"},
        )
        assert resp.status_code == 404, resp.text
    finally:
        await adm_c.aclose()


# ---------------------------------------------------------------------------
# POST /api/admin/users/{user_id}/revoke-sessions
# ---------------------------------------------------------------------------


async def test_admin_revoke_sessions_happy(
    live_servers: dict[str, Any],
    db_engine: AsyncEngine,
) -> None:
    """POST /admin/users/{id}/revoke-sessions → 200 + session invalidated."""
    adm_c, _, _ = await _make_fresh_admin(live_servers, db_engine)
    reg_c, reg_user, _ = await _make_fresh_regular(live_servers)
    try:
        # Verify regular user's session works.
        me_before = await reg_c.get("/api/auth/me")
        assert me_before.status_code == 200, me_before.text

        # Admin revokes.
        revoke_resp = await adm_c.post(
            f"/api/admin/users/{reg_user['id']}/revoke-sessions",
        )
        assert revoke_resp.status_code == 200, revoke_resp.text
        assert revoke_resp.json()["status"] == "ok"

        # The regular user's cookie is now invalid.
        me_after = await reg_c.get("/api/auth/me")
        assert me_after.status_code == 401, (
            f"Expected 401 after session revocation, got {me_after.status_code}"
        )
    finally:
        await adm_c.aclose()
        await reg_c.aclose()


async def test_admin_revoke_sessions_403_non_admin(
    live_servers: dict[str, Any],
) -> None:
    """POST /admin/users/{id}/revoke-sessions as non-admin → 403."""
    reg_c, reg_user, _ = await _make_fresh_regular(live_servers)
    try:
        resp = await reg_c.post(
            f"/api/admin/users/{reg_user['id']}/revoke-sessions",
        )
        assert resp.status_code == 403, resp.text
    finally:
        await reg_c.aclose()


async def test_admin_revoke_sessions_401_unauthed(
    live_servers: dict[str, Any],
) -> None:
    """POST /admin/users/{id}/revoke-sessions without session → 401."""
    base_url = live_servers["base_url"]
    async with httpx.AsyncClient(
        base_url=base_url, follow_redirects=True, timeout=15.0
    ) as anon:
        resp = await anon.post("/api/admin/users/1/revoke-sessions")
        assert resp.status_code == 401, resp.text


async def test_admin_revoke_sessions_404_unknown_user(
    live_servers: dict[str, Any],
    db_engine: AsyncEngine,
) -> None:
    """POST /admin/users/99999/revoke-sessions where user doesn't exist → 404."""
    adm_c, _, _ = await _make_fresh_admin(live_servers, db_engine)
    try:
        resp = await adm_c.post("/api/admin/users/99999/revoke-sessions")
        assert resp.status_code == 404, resp.text
    finally:
        await adm_c.aclose()


# ---------------------------------------------------------------------------
# GET /api/admin/audit-log
# ---------------------------------------------------------------------------


async def test_admin_audit_log_happy(
    live_servers: dict[str, Any],
    db_engine: AsyncEngine,
) -> None:
    """GET /admin/audit-log as admin → 200 + AuditLogPage shape."""
    adm_c, _, _ = await _make_fresh_admin(live_servers, db_engine)
    try:
        resp = await adm_c.get("/api/admin/audit-log")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "items" in body
        assert "total" in body
        assert "limit" in body
        assert "offset" in body
        assert isinstance(body["items"], list)
    finally:
        await adm_c.aclose()


async def test_admin_audit_log_403_non_admin(
    live_servers: dict[str, Any],
) -> None:
    """GET /admin/audit-log as non-admin → 403."""
    reg_c, _, _ = await _make_fresh_regular(live_servers)
    try:
        resp = await reg_c.get("/api/admin/audit-log")
        assert resp.status_code == 403, resp.text
    finally:
        await reg_c.aclose()


async def test_admin_audit_log_401_unauthed(
    live_servers: dict[str, Any],
) -> None:
    """GET /admin/audit-log without session → 401."""
    base_url = live_servers["base_url"]
    async with httpx.AsyncClient(
        base_url=base_url, follow_redirects=True, timeout=15.0
    ) as anon:
        resp = await anon.get("/api/admin/audit-log")
        assert resp.status_code == 401, resp.text


async def test_admin_audit_log_event_filter(
    live_servers: dict[str, Any],
    db_engine: AsyncEngine,
) -> None:
    """GET /admin/audit-log?event=<name> filters to that event type."""
    adm_c, _, _ = await _make_fresh_admin(live_servers, db_engine)
    try:
        # Trigger an auth.register event by registering a user.
        uname = f"af_{secrets.token_hex(4)}"
        await adm_c.post(
            "/api/auth/register",
            data={"username": uname, "password": "audit-filter-pw"},
        )
        resp = await adm_c.get("/api/admin/audit-log?event=auth.register")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Every returned item should match the filter.
        for item in body["items"]:
            assert item["event"] == "auth.register"
    finally:
        await adm_c.aclose()


# ---------------------------------------------------------------------------
# GET /api/debug
# ---------------------------------------------------------------------------


async def test_debug_happy(
    live_servers: dict[str, Any],
    db_engine: AsyncEngine,
) -> None:
    """GET /api/debug as admin → 200 + safe diagnostic dict (no secrets)."""
    adm_c, _, _ = await _make_fresh_admin(live_servers, db_engine)
    try:
        resp = await adm_c.get("/api/debug")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "version" in body
        assert "db_dialect" in body
        assert "model_count" in body
        assert "stream_bucket_count" in body
        assert "admin_bucket_count" in body
        # Confirm no secrets leaked.
        body_str = str(body)
        assert "password" not in body_str.lower()
        assert "secret" not in body_str.lower()
        assert "token" not in body_str.lower()
    finally:
        await adm_c.aclose()


async def test_debug_403_non_admin(
    live_servers: dict[str, Any],
) -> None:
    """GET /api/debug as non-admin → 403."""
    reg_c, _, _ = await _make_fresh_regular(live_servers)
    try:
        resp = await reg_c.get("/api/debug")
        assert resp.status_code == 403, resp.text
    finally:
        await reg_c.aclose()


async def test_debug_401_unauthed(
    live_servers: dict[str, Any],
) -> None:
    """GET /api/debug without session → 401."""
    base_url = live_servers["base_url"]
    async with httpx.AsyncClient(
        base_url=base_url, follow_redirects=True, timeout=15.0
    ) as anon:
        resp = await anon.get("/api/debug")
        assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# POST /api/admin/models/refresh
# ---------------------------------------------------------------------------


async def test_admin_models_refresh_happy(
    live_servers: dict[str, Any],
    db_engine: AsyncEngine,
) -> None:
    """POST /admin/models/refresh as admin → 200 + {ok, model_count}."""
    adm_c, _, _ = await _make_fresh_admin(live_servers, db_engine)
    try:
        resp = await adm_c.post("/api/admin/models/refresh")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("ok") is True
        assert "model_count" in body
        assert isinstance(body["model_count"], int)
    finally:
        await adm_c.aclose()


async def test_admin_models_refresh_403_non_admin(
    live_servers: dict[str, Any],
) -> None:
    """POST /admin/models/refresh as non-admin → 403."""
    reg_c, _, _ = await _make_fresh_regular(live_servers)
    try:
        resp = await reg_c.post("/api/admin/models/refresh")
        assert resp.status_code == 403, resp.text
    finally:
        await reg_c.aclose()


async def test_admin_models_refresh_401_unauthed(
    live_servers: dict[str, Any],
) -> None:
    """POST /admin/models/refresh without session → 401."""
    base_url = live_servers["base_url"]
    async with httpx.AsyncClient(
        base_url=base_url, follow_redirects=True, timeout=15.0
    ) as anon:
        resp = await anon.post("/api/admin/models/refresh")
        assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# GET /api/admin/users/count
# ---------------------------------------------------------------------------


async def test_admin_users_count_happy(
    live_servers: dict[str, Any],
    db_engine: AsyncEngine,
) -> None:
    """GET /admin/users/count as admin → 200 + {count: int}."""
    adm_c, _, _ = await _make_fresh_admin(live_servers, db_engine)
    try:
        resp = await adm_c.get("/api/admin/users/count")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "count" in body
        assert isinstance(body["count"], int)
        assert body["count"] >= 1  # at least the admin user
    finally:
        await adm_c.aclose()


async def test_admin_users_count_403_non_admin(
    live_servers: dict[str, Any],
) -> None:
    """GET /admin/users/count as non-admin → 403."""
    reg_c, _, _ = await _make_fresh_regular(live_servers)
    try:
        resp = await reg_c.get("/api/admin/users/count")
        assert resp.status_code == 403, resp.text
    finally:
        await reg_c.aclose()


async def test_admin_users_count_401_unauthed(
    live_servers: dict[str, Any],
) -> None:
    """GET /admin/users/count without session → 401."""
    base_url = live_servers["base_url"]
    async with httpx.AsyncClient(
        base_url=base_url, follow_redirects=True, timeout=15.0
    ) as anon:
        resp = await anon.get("/api/admin/users/count")
        assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# Cross-cutting: audit log rows produced by admin operations
# ---------------------------------------------------------------------------


async def test_admin_role_change_produces_audit_row(
    live_servers: dict[str, Any],
    db_engine: AsyncEngine,
) -> None:
    """POST /admin/users/{id}/role writes an audit row to audit_log."""
    from sqlalchemy import select, text

    from lmchat.db.schema import audit_log

    adm_c, _, _ = await _make_fresh_admin(live_servers, db_engine)
    reg_c, reg_user, _ = await _make_fresh_regular(live_servers)
    try:
        # Count before.
        async with db_engine.connect() as conn:
            before = int(
                (
                    await conn.execute(
                        select(text("COUNT(*)"))
                        .select_from(audit_log)
                        .where(audit_log.c.event == "admin.users.role_changed")
                    )
                ).scalar()
                or 0
            )

        await adm_c.post(
            f"/api/admin/users/{reg_user['id']}/role",
            data={"is_admin": "false"},
        )

        async with db_engine.connect() as conn:
            after = int(
                (
                    await conn.execute(
                        select(text("COUNT(*)"))
                        .select_from(audit_log)
                        .where(audit_log.c.event == "admin.users.role_changed")
                    )
                ).scalar()
                or 0
            )

        assert after == before + 1
    finally:
        await adm_c.aclose()
        await reg_c.aclose()
