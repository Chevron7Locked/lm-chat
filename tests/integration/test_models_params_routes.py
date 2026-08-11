# SPDX-License-Identifier: Apache-2.0
"""P10a.4 — live-backend integration tests for models and params routes.

Routes covered
--------------
1.  GET  /api/models                  — list models (auth-gated)
2.  POST /api/admin/models/refresh    — refresh model cache (admin-only)
3.  GET  /api/params/{model_id}       — get rejected params for a model (auth-gated)
4.  POST /api/admin/params/invalidate — clear params cache (admin-only)

Cross-cutting invariants
------------------------
- Unauthenticated → 401 on every route.
- Non-admin on admin routes → 403.
- List endpoints return bare arrays (Invariant 3).
- models: returns list[ModelInfo]; includes the stub-model-q4 from the stub.
- params: model not in cache returns empty rejected list.
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
# GET /api/models
# ---------------------------------------------------------------------------


async def test_list_models_unauth_401(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/models")
    assert resp.status_code == 401


async def test_list_models_happy(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    resp = await client.get(
        "/api/models",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)  # Invariant 3
    # The stub LM Studio serves stub-model-q4 — at least one model expected.
    assert len(body) >= 1
    # Each model entry should carry ModelInfo.key.
    model = body[0]
    assert "key" in model


async def test_list_models_returns_stub_model(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    resp = await client.get(
        "/api/models",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    keys = [m.get("key", "") for m in body]
    assert any("stub-model-q4" in k for k in keys)


# ---------------------------------------------------------------------------
# POST /api/admin/models/refresh
# ---------------------------------------------------------------------------


async def test_refresh_models_unauth_401(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/admin/models/refresh")
    assert resp.status_code == 401


async def test_refresh_models_nonadmin_403(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    resp = await client.post(
        "/api/admin/models/refresh",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 403


async def test_refresh_models_admin_happy(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, cookie = await register_admin_and_login(client, db_engine)
    resp = await client.post(
        "/api/admin/models/refresh",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "model_count" in body
    assert isinstance(body["model_count"], int)


# ---------------------------------------------------------------------------
# GET /api/params/{model_id}
# ---------------------------------------------------------------------------


async def test_get_params_unauth_401(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/params/some-model")
    assert resp.status_code == 401


async def test_get_params_unknown_model_empty(client: httpx.AsyncClient) -> None:
    """Model not in cache → rejected list is empty (not 404)."""
    _, cookie = await register_and_login(client)
    resp = await client.get(
        "/api/params/unknown-model-xyz",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["model_id"] == "unknown-model-xyz"
    assert body["rejected"] == []


async def test_get_params_known_model_returns_sorted(
    client: httpx.AsyncClient,
    live_servers: dict[str, Any],
) -> None:
    """If cache has entries, they come back sorted alphabetically."""
    _, cookie = await register_and_login(client)
    app = live_servers["app"]
    svc = app.state.params_service
    # Seed the cache directly.
    model_id = "stub-model-q4"
    await svc.record_rejection(model_id=model_id, param="temperature")
    await svc.record_rejection(model_id=model_id, param="top_p")
    resp = await client.get(
        f"/api/params/{model_id}",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["model_id"] == model_id
    assert "temperature" in body["rejected"]
    assert "top_p" in body["rejected"]
    assert body["rejected"] == sorted(body["rejected"])


# ---------------------------------------------------------------------------
# POST /api/admin/params/invalidate
# ---------------------------------------------------------------------------


async def test_invalidate_params_unauth_401(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/admin/params/invalidate")
    assert resp.status_code == 401


async def test_invalidate_params_nonadmin_403(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    resp = await client.post(
        "/api/admin/params/invalidate",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 403


async def test_invalidate_params_all_admin_happy(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Invalidate entire cache (no model_id query param)."""
    _, cookie = await register_admin_and_login(client, db_engine)
    resp = await client.post(
        "/api/admin/params/invalidate",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["model_id"] is None


async def test_invalidate_params_single_model(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Invalidate one model's cache via ?model_id= query param."""
    _, cookie = await register_admin_and_login(client, db_engine)
    resp = await client.post(
        "/api/admin/params/invalidate?model_id=stub-model-q4",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["model_id"] == "stub-model-q4"


async def test_invalidate_params_clears_cache(
    client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    live_servers: dict[str, Any],
) -> None:
    """After invalidation, the model no longer has rejected params."""
    app = live_servers["app"]
    svc = app.state.params_service
    model_id = "stub-model-to-clear"
    await svc.record_rejection(model_id=model_id, param="some_param")

    _, adm_cookie = await register_admin_and_login(client, db_engine)
    await client.post(
        f"/api/admin/params/invalidate?model_id={model_id}",
        headers={"Cookie": f"lmchat_session={adm_cookie}"},
    )

    _, reg_cookie = await register_and_login(client)
    check_resp = await client.get(
        f"/api/params/{model_id}",
        headers={"Cookie": f"lmchat_session={reg_cookie}"},
    )
    assert check_resp.status_code == 200
    assert check_resp.json()["rejected"] == []
