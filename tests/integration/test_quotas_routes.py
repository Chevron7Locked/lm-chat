# SPDX-License-Identifier: Apache-2.0
"""P10a.4 — live-backend integration tests for quota routes.

Routes covered
--------------
1.  GET   /api/quotas/me               — current user quota + usage
2.  GET   /api/admin/quotas            — list all quota rows (admin-only)
3.  PATCH /api/admin/quotas/{user_id}  — upsert quota (admin-only)

Cross-cutting invariants
------------------------
- Unauthenticated → 401.
- Non-admin on admin routes → 403.
- Admin querying unknown user_id for PATCH → creates/upserts (no 404 for
  PATCH because set_quota is an upsert; the admin list only shows rows that
  exist).
- PATCH body is form-encoded.
- 429 enforcement: set requests_per_day=1, then send a second request that
  hits a quota-checked path.
"""
from __future__ import annotations

import secrets
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration.conftest import (
    make_admin,
    register_admin_and_login,
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
    """Return isolated client + admin user dict + session cookie."""
    base_url = live_servers["base_url"]
    c = httpx.AsyncClient(base_url=base_url, follow_redirects=True, timeout=15.0)
    uname = f"adm_{secrets.token_hex(6)}"
    user, _ = await register_and_login(c, uname)
    await make_admin(db_engine, user["id"])
    login_resp = await c.post(
        "/api/auth/login",
        data={"username": uname, "password": "integration-pw-OK"},
    )
    assert login_resp.status_code == 200
    cookie = login_resp.cookies.get("lmchat_session") or ""
    c.cookies.set("lmchat_session", cookie)
    return c, user, cookie


# ---------------------------------------------------------------------------
# GET /api/quotas/me
# ---------------------------------------------------------------------------


async def test_get_my_quota_unauth_401(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/quotas/me")
    assert resp.status_code == 401


async def test_get_my_quota_happy(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    resp = await client.get(
        "/api/quotas/me",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "tokens_per_day" in body
    assert "requests_per_day" in body
    assert "tokens_consumed_today" in body
    assert "requests_consumed_today" in body
    assert "resets_at" in body
    # Defaults: 100 000 tokens, 1 000 requests
    assert isinstance(body["tokens_per_day"], int)
    assert isinstance(body["requests_per_day"], int)
    # resets_at must be an ISO-8601 string
    resets_at: str = body["resets_at"]
    assert "T" in resets_at, f"resets_at not ISO-8601: {resets_at}"


# ---------------------------------------------------------------------------
# GET /api/admin/quotas
# ---------------------------------------------------------------------------


async def test_list_quotas_unauth_401(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/admin/quotas")
    assert resp.status_code == 401


async def test_list_quotas_nonadmin_403(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    resp = await client.get(
        "/api/admin/quotas",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 403


async def test_list_quotas_admin_happy(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, cookie = await register_admin_and_login(client, db_engine)
    resp = await client.get(
        "/api/admin/quotas",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)  # Invariant 3


async def test_list_quotas_shows_explicit_rows(
    client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    live_servers: dict[str, Any],
) -> None:
    """After patching a user's quota, that user appears in the admin list."""
    adm_c, _, adm_cookie = await _make_fresh_admin(live_servers, db_engine)
    # Create a regular user.
    reg_user, _ = await register_and_login(client)
    # Patch that user's quota.
    await adm_c.patch(
        f"/api/admin/quotas/{reg_user['id']}",
        data={"tokens_per_day": "9999", "requests_per_day": "77"},
    )
    # The user should now appear in the list.
    list_resp = await adm_c.get("/api/admin/quotas")
    assert list_resp.status_code == 200
    ids = [r["user_id"] for r in list_resp.json()]
    assert reg_user["id"] in ids


# ---------------------------------------------------------------------------
# PATCH /api/admin/quotas/{user_id}
# ---------------------------------------------------------------------------


async def test_patch_quota_unauth_401(client: httpx.AsyncClient) -> None:
    resp = await client.patch(
        "/api/admin/quotas/1",
        data={"tokens_per_day": "100", "requests_per_day": "10"},
    )
    assert resp.status_code == 401


async def test_patch_quota_nonadmin_403(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    reg_user2, _ = await register_and_login(client)
    resp = await client.patch(
        f"/api/admin/quotas/{reg_user2['id']}",
        data={"tokens_per_day": "100", "requests_per_day": "10"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 403


async def test_patch_quota_admin_happy(
    client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    live_servers: dict[str, Any],
) -> None:
    adm_c, _, _ = await _make_fresh_admin(live_servers, db_engine)
    reg_user, _ = await register_and_login(client)
    resp = await adm_c.patch(
        f"/api/admin/quotas/{reg_user['id']}",
        data={"tokens_per_day": "50000", "requests_per_day": "500"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == reg_user["id"]
    assert body["tokens_per_day"] == 50000
    assert body["requests_per_day"] == 500


async def test_patch_quota_missing_fields_422(
    client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    live_servers: dict[str, Any],
) -> None:
    adm_c, _, _ = await _make_fresh_admin(live_servers, db_engine)
    reg_user, _ = await register_and_login(client)
    resp = await adm_c.patch(
        f"/api/admin/quotas/{reg_user['id']}",
        data={"tokens_per_day": "100"},  # missing requests_per_day
    )
    assert resp.status_code == 422


async def test_patch_quota_reflected_in_me(
    client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    live_servers: dict[str, Any],
) -> None:
    """After admin PATCH, /api/quotas/me reflects the new limits."""
    adm_c, _, _ = await _make_fresh_admin(live_servers, db_engine)
    reg_user, reg_cookie = await register_and_login(client)
    await adm_c.patch(
        f"/api/admin/quotas/{reg_user['id']}",
        data={"tokens_per_day": "12345", "requests_per_day": "222"},
    )
    me_resp = await client.get(
        "/api/quotas/me",
        headers={"Cookie": f"lmchat_session={reg_cookie}"},
    )
    assert me_resp.status_code == 200
    body = me_resp.json()
    assert body["tokens_per_day"] == 12345
    assert body["requests_per_day"] == 222


# ---------------------------------------------------------------------------
# 429 quota enforcement
# ---------------------------------------------------------------------------


async def test_quota_429_on_exceeded_requests(
    client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    live_servers: dict[str, Any],
) -> None:
    """Set requests_per_day=1, then verify the second request returns 429."""
    adm_c, _, _ = await _make_fresh_admin(live_servers, db_engine)
    reg_user, reg_cookie = await register_and_login(client)

    # Set quota to 1 request/day.
    patch_resp = await adm_c.patch(
        f"/api/admin/quotas/{reg_user['id']}",
        data={"tokens_per_day": "100000", "requests_per_day": "1"},
    )
    assert patch_resp.status_code == 200

    # First quota-checked request: should succeed (quota consumed).
    # NOTE: /api/quotas/me is now in _SKIP_EXACT (self-introspection
    # doesn't count toward quota — fixed after P12 EOP r2 admin-bootstrap
    # audit). Use /api/chats (DB-only, no external dep) as the
    # quota-checked endpoint for this test.
    first = await client.get(
        "/api/chats",
        headers={"Cookie": f"lmchat_session={reg_cookie}"},
    )
    assert first.status_code == 200

    # Second request: quota exhausted → 429.
    second = await client.get(
        "/api/chats",
        headers={"Cookie": f"lmchat_session={reg_cookie}"},
    )
    assert second.status_code == 429
    assert "retry-after" in second.headers
