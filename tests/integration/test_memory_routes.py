# SPDX-License-Identifier: Apache-2.0
"""P10a.3 — live-backend integration tests for /api/memory (5 endpoints).

Endpoints under test
--------------------
POST   /api/memory/pin              pin_insight
DELETE /api/memory/pin/{insight_id} unpin_insight
GET    /api/memory/pins             list_pins
POST   /api/memory/reindex          start_reindex  (admin-only)
GET    /api/memory/reindex/status   get_reindex_status (admin-only)

Cross-cutting invariants
------------------------
- Unauthenticated → 401 on every route.
- Cross-user mutation (User B on User A's insight) → 404, not 403.
- GET /api/memory/pins is user-scoped: User B cannot see User A's pins.
- Admin endpoints → 403 for non-admin callers.
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.integration.conftest import (
    register_admin_and_login,
    register_and_login,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _pin(
    client: httpx.AsyncClient,
    cookie: str,
    text: str = "remember this",
) -> dict[str, Any]:
    """POST /api/memory/pin and assert 201."""
    resp = await client.post(
        "/api/memory/pin",
        data={"text": text},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 201, f"pin failed: {resp.text}"
    return dict(resp.json())


# ---------------------------------------------------------------------------
# POST /api/memory/pin
# ---------------------------------------------------------------------------


async def test_pin_insight_happy(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    resp = await client.post(
        "/api/memory/pin",
        data={"text": "my important note"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert "id" in body
    assert body["text"] == "my important note"
    assert "user_id" in body
    assert "created_at" in body


async def test_pin_insight_unauth(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/memory/pin", data={"text": "no auth"})
    assert resp.status_code == 401


async def test_pin_insight_missing_text_422(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    resp = await client.post(
        "/api/memory/pin",
        data={},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 422


async def test_pin_insight_idempotent(client: httpx.AsyncClient) -> None:
    """Pinning the same text twice returns the existing row (idempotent)."""
    _, cookie = await register_and_login(client)
    first = await _pin(client, cookie, "idempotent note xyz123")
    second = await _pin(client, cookie, "idempotent note xyz123")
    # Same PK returned — dedup in service layer.
    assert first["id"] == second["id"]


# ---------------------------------------------------------------------------
# DELETE /api/memory/pin/{insight_id}
# ---------------------------------------------------------------------------


async def test_unpin_insight_happy(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    insight = await _pin(client, cookie, "to be deleted")

    resp = await client.delete(
        f"/api/memory/pin/{insight['id']}",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 204

    # Confirm no longer listed.
    list_resp = await client.get(
        "/api/memory/pins",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    ids = [i["id"] for i in list_resp.json()]
    assert insight["id"] not in ids


async def test_unpin_insight_not_found_404(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    resp = await client.delete(
        "/api/memory/pin/999999",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 404


async def test_unpin_insight_cross_user_404(client: httpx.AsyncClient) -> None:
    """User B cannot unpin User A's insight — must receive 404, not 403."""
    _, cookie_a = await register_and_login(client)
    _, cookie_b = await register_and_login(client)

    insight = await _pin(client, cookie_a, "user a private note")

    resp = await client.delete(
        f"/api/memory/pin/{insight['id']}",
        headers={"Cookie": f"lmchat_session={cookie_b}"},
    )
    assert resp.status_code == 404

    # Verify the insight still exists for User A (not silently deleted).
    list_resp = await client.get(
        "/api/memory/pins",
        headers={"Cookie": f"lmchat_session={cookie_a}"},
    )
    ids = [i["id"] for i in list_resp.json()]
    assert insight["id"] in ids


async def test_unpin_insight_unauth(client: httpx.AsyncClient) -> None:
    resp = await client.delete("/api/memory/pin/1")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/memory/pins
# ---------------------------------------------------------------------------


async def test_list_pins_happy(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    await _pin(client, cookie, "pin alpha list")
    await _pin(client, cookie, "pin beta list")

    resp = await client.get(
        "/api/memory/pins",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    texts = {i["text"] for i in body}
    assert "pin alpha list" in texts
    assert "pin beta list" in texts


async def test_list_pins_unauth(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/memory/pins")
    assert resp.status_code == 401


async def test_list_pins_isolation(client: httpx.AsyncClient) -> None:
    """User B's pin list must not contain User A's pins."""
    _, cookie_a = await register_and_login(client)
    _, cookie_b = await register_and_login(client)

    await _pin(client, cookie_a, "user a secret note isolation")

    resp = await client.get(
        "/api/memory/pins",
        headers={"Cookie": f"lmchat_session={cookie_b}"},
    )
    assert resp.status_code == 200
    texts = {i["text"] for i in resp.json()}
    assert "user a secret note isolation" not in texts


# ---------------------------------------------------------------------------
# POST /api/memory/reindex  (admin-only)
# ---------------------------------------------------------------------------


async def test_reindex_admin_happy(
    client: httpx.AsyncClient,
    db_engine: object,
) -> None:
    _, cookie = await register_admin_and_login(client, db_engine)  # type: ignore[arg-type]
    resp = await client.post(
        "/api/memory/reindex",
        data={"embedding_model_id": "stub-model-q4"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["ok"] is True
    assert body["embedding_model_id"] == "stub-model-q4"
    assert body["task_status"] == "started"


async def test_reindex_non_admin_403(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    resp = await client.post(
        "/api/memory/reindex",
        data={"embedding_model_id": "stub-model-q4"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 403


async def test_reindex_unauth(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/memory/reindex",
        data={"embedding_model_id": "stub-model-q4"},
    )
    assert resp.status_code == 401


async def test_reindex_missing_embedding_model_id_422(
    client: httpx.AsyncClient,
    db_engine: object,
) -> None:
    _, cookie = await register_admin_and_login(client, db_engine)  # type: ignore[arg-type]
    resp = await client.post(
        "/api/memory/reindex",
        data={},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/memory/reindex/status  (admin-only)
# ---------------------------------------------------------------------------


async def test_reindex_status_admin_happy(
    client: httpx.AsyncClient,
    db_engine: object,
) -> None:
    _, cookie = await register_admin_and_login(client, db_engine)  # type: ignore[arg-type]
    resp = await client.get(
        "/api/memory/reindex/status",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    # state is one of the known ReindexState values
    assert body["state"] in {"idle", "running", "completed", "failed"}
    assert "processed" in body
    assert "total" in body
    assert "elapsed_s" in body


async def test_reindex_status_non_admin_403(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    resp = await client.get(
        "/api/memory/reindex/status",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 403


async def test_reindex_status_unauth(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/memory/reindex/status")
    assert resp.status_code == 401
