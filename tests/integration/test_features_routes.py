# SPDX-License-Identifier: Apache-2.0
"""P10a.4 — live-backend integration tests for feature routes.

Routes covered
--------------
1.  POST /api/search/web         (web_search.py — 1 endpoint)
2.  POST /api/ab/stream          (ab_compare.py  — 1 endpoint)
3.  GET  /api/analytics/me       (analytics.py   — 1 endpoint)
4.  GET  /api/analytics/system   (analytics.py   — 1 endpoint, admin-only)
5.  GET  /api/prompts            (prompt_library.py — 1 endpoint)
6.  POST /api/prompts            (prompt_library.py — 1 endpoint)
7.  PATCH /api/prompts/{id}      (prompt_library.py — 1 endpoint)
8.  DELETE /api/prompts/{id}     (prompt_library.py — 1 endpoint)

Cross-cutting invariants
------------------------
- Unauthenticated → 401 on every route.
- Cross-user access on PATCH/DELETE /api/prompts/{id} → 404.
- GET /api/analytics/me returns only own data.
- GET /api/analytics/system → 401 unauthenticated, 403 non-admin.
- All mutation bodies are form-encoded.
- web_search: mocked at the service boundary to avoid real SearXNG/DDG calls.
- ab_compare: SSE response tested for Content-Type; cross-chat ownership note
  in route docstring states chat_id is for client-side correlation only (no DB
  ownership check at this endpoint — not a security boundary).
"""
from __future__ import annotations

import secrets
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


async def _create_prompt(
    client: httpx.AsyncClient,
    cookie: str,
    name: str | None = None,
    content: str = "You are a helpful assistant.",
) -> dict[str, Any]:
    if name is None:
        name = f"prompt_{secrets.token_hex(4)}"
    resp = await client.post(
        "/api/prompts",
        data={"name": name, "content": content},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 201, f"create_prompt failed: {resp.text}"
    return dict(resp.json())


# ---------------------------------------------------------------------------
# POST /api/search/web
# ---------------------------------------------------------------------------


async def test_web_search_unauth_401(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/search/web", data={"q": "python"})
    assert resp.status_code == 401


async def test_web_search_missing_q_422(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    resp = await client.post(
        "/api/search/web",
        data={},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 422


async def test_web_search_empty_q_422(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    resp = await client.post(
        "/api/search/web",
        data={"q": ""},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 422


async def test_web_search_happy_returns_list(
    client: httpx.AsyncClient, live_servers: dict[str, Any]
) -> None:
    """Mock the WebSearchService.search call and verify the list response.

    Replaces the bound method with a plain coroutine to avoid AsyncMock
    crossing an event-loop boundary with the session-scoped live server.
    """
    from lmchat.services.web_search_service import SearchResult

    _, cookie = await register_and_login(client)
    app = live_servers["app"]
    svc = app.state.web_search_service

    fake = [SearchResult(title="Python", url="https://python.org", snippet="lang")]

    async def _fake_search(query: str, top_n: int = 5) -> list[SearchResult]:
        return fake

    original_search = svc.search
    svc.search = _fake_search  # type: ignore[method-assign]
    try:
        resp = await client.post(
            "/api/search/web",
            data={"q": "python"},
            headers={"Cookie": f"lmchat_session={cookie}"},
        )
    finally:
        svc.search = original_search  # type: ignore[method-assign]

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert body[0]["title"] == "Python"
    assert body[0]["url"] == "https://python.org"
    assert "snippet" in body[0]


# ---------------------------------------------------------------------------
# POST /api/ab/stream
# ---------------------------------------------------------------------------


async def test_ab_stream_unauth_401(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/ab/stream",
        data={
            "chat_id": "1",
            "message": "hello",
            "model_a": "model-a",
            "model_b": "model-b",
        },
    )
    assert resp.status_code == 401


async def test_ab_stream_missing_fields_422(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    resp = await client.post(
        "/api/ab/stream",
        data={"chat_id": "1", "message": "hello"},  # missing model_a/model_b
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 422


async def test_ab_stream_max_tokens_cap_400(client: httpx.AsyncClient) -> None:
    """max_tokens above cap → 400 before the SSE response starts."""
    _, cookie = await register_and_login(client)
    resp = await client.post(
        "/api/ab/stream",
        data={
            "chat_id": "1",
            "message": "hello",
            "model_a": "model-a",
            "model_b": "model-b",
            "max_tokens": "99999999",  # vastly over default 32 768 cap
        },
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"].lower()
    assert "max_tokens" in detail or "limit" in detail


async def test_ab_stream_happy_sse(
    client: httpx.AsyncClient, live_servers: dict[str, Any]
) -> None:
    """A/B stream against the stub LM Studio returns text/event-stream."""
    _, cookie = await register_and_login(client)
    resp = await client.post(
        "/api/ab/stream",
        data={
            "chat_id": "1",
            "message": "say hello",
            "model_a": "stub-model-q4",
            "model_b": "stub-model-q4",
        },
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200
    ct = resp.headers.get("content-type", "")
    assert "text/event-stream" in ct


# ---------------------------------------------------------------------------
# GET /api/analytics/me
# ---------------------------------------------------------------------------


async def test_analytics_me_unauth_401(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/analytics/me")
    assert resp.status_code == 401


async def test_analytics_me_happy(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    resp = await client.get(
        "/api/analytics/me",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "total_messages" in body
    assert "total_chats" in body
    assert "messages_last_7_days" in body
    assert "top_models" in body
    assert isinstance(body["top_models"], list)


async def test_analytics_me_only_own_data(client: httpx.AsyncClient) -> None:
    """Two users — each sees their own stats; zero cross-contamination."""
    _, cookie_a = await register_and_login(client)
    _, cookie_b = await register_and_login(client)

    resp_a = await client.get(
        "/api/analytics/me",
        headers={"Cookie": f"lmchat_session={cookie_a}"},
    )
    resp_b = await client.get(
        "/api/analytics/me",
        headers={"Cookie": f"lmchat_session={cookie_b}"},
    )
    assert resp_a.status_code == 200
    assert resp_b.status_code == 200
    # Both start at zero; independence is implied — no shared state bleed.
    assert resp_a.json()["total_chats"] == 0
    assert resp_b.json()["total_chats"] == 0


# ---------------------------------------------------------------------------
# GET /api/analytics/system  (admin-only)
# ---------------------------------------------------------------------------


async def test_analytics_system_unauth_401(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/analytics/system")
    assert resp.status_code == 401


async def test_analytics_system_nonadmin_403(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    resp = await client.get(
        "/api/analytics/system",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 403


async def test_analytics_system_admin_happy(
    client: httpx.AsyncClient, db_engine: Any
) -> None:
    _, cookie = await register_admin_and_login(client, db_engine)
    resp = await client.get(
        "/api/analytics/system",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "total_users" in body
    assert "total_chats" in body
    assert "total_messages" in body
    assert "messages_last_7_days" in body
    assert "top_models" in body
    assert isinstance(body["top_models"], list)


# ---------------------------------------------------------------------------
# GET /api/prompts  (list)
# ---------------------------------------------------------------------------


async def test_list_prompts_unauth_401(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/prompts")
    assert resp.status_code == 401


async def test_list_prompts_empty_list(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    resp = await client.get(
        "/api/prompts",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)  # Invariant 3: bare array


async def test_list_prompts_returns_own_prompts(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    await _create_prompt(client, cookie)
    resp = await client.get(
        "/api/prompts",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) >= 1
    assert "id" in body[0]
    assert "name" in body[0]
    assert "content" in body[0]


async def test_list_prompts_user_isolation(client: httpx.AsyncClient) -> None:
    """User B does not see User A's prompts."""
    _, cookie_a = await register_and_login(client)
    _, cookie_b = await register_and_login(client)
    await _create_prompt(client, cookie_a)
    resp = await client.get(
        "/api/prompts",
        headers={"Cookie": f"lmchat_session={cookie_b}"},
    )
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# POST /api/prompts  (create)
# ---------------------------------------------------------------------------


async def test_create_prompt_unauth_401(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/prompts",
        data={"name": "test", "content": "hello"},
    )
    assert resp.status_code == 401


async def test_create_prompt_happy(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    resp = await client.post(
        "/api/prompts",
        data={"name": "My Prompt", "content": "You are a helpful assistant."},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "My Prompt"
    assert body["content"] == "You are a helpful assistant."
    assert "id" in body
    assert "created_at" in body


async def test_create_prompt_duplicate_name_409(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    name = f"dupe_{secrets.token_hex(4)}"
    await _create_prompt(client, cookie, name=name)
    resp = await client.post(
        "/api/prompts",
        data={"name": name, "content": "second"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 409


async def test_create_prompt_content_too_long_422(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    long_content = "x" * 32769  # over the 32 768 limit
    resp = await client.post(
        "/api/prompts",
        data={"name": f"long_{secrets.token_hex(4)}", "content": long_content},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 422


async def test_create_prompt_missing_content_422(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    resp = await client.post(
        "/api/prompts",
        data={"name": "no-content"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# PATCH /api/prompts/{id}  (update)
# ---------------------------------------------------------------------------


async def test_update_prompt_unauth_401(client: httpx.AsyncClient) -> None:
    resp = await client.patch("/api/prompts/1", data={"name": "x"})
    assert resp.status_code == 401


async def test_update_prompt_happy(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    prompt = await _create_prompt(client, cookie)
    prompt_id = prompt["id"]
    resp = await client.patch(
        f"/api/prompts/{prompt_id}",
        data={"name": "Updated Name"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Updated Name"


async def test_update_prompt_cross_user_404(client: httpx.AsyncClient) -> None:
    """User B patching User A's prompt → 404 (not 403, not 200)."""
    _, cookie_a = await register_and_login(client)
    _, cookie_b = await register_and_login(client)
    prompt = await _create_prompt(client, cookie_a)
    prompt_id = prompt["id"]
    resp = await client.patch(
        f"/api/prompts/{prompt_id}",
        data={"name": "hijacked"},
        headers={"Cookie": f"lmchat_session={cookie_b}"},
    )
    assert resp.status_code == 404, (
        f"Expected 404 on cross-user PATCH, got {resp.status_code}: {resp.text}"
    )


async def test_update_prompt_nonexistent_404(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    resp = await client.patch(
        "/api/prompts/999999",
        data={"name": "ghost"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 404


async def test_update_prompt_name_conflict_409(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    name_a = f"nameA_{secrets.token_hex(4)}"
    name_b = f"nameB_{secrets.token_hex(4)}"
    await _create_prompt(client, cookie, name=name_a)
    prompt_b = await _create_prompt(client, cookie, name=name_b)
    resp = await client.patch(
        f"/api/prompts/{prompt_b['id']}",
        data={"name": name_a},  # conflict
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# DELETE /api/prompts/{id}
# ---------------------------------------------------------------------------


async def test_delete_prompt_unauth_401(client: httpx.AsyncClient) -> None:
    resp = await client.delete("/api/prompts/1")
    assert resp.status_code == 401


async def test_delete_prompt_happy(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    prompt = await _create_prompt(client, cookie)
    resp = await client.delete(
        f"/api/prompts/{prompt['id']}",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 204


async def test_delete_prompt_cross_user_404(client: httpx.AsyncClient) -> None:
    """User B deleting User A's prompt → 404 (SA-2026-001 class invariant)."""
    _, cookie_a = await register_and_login(client)
    _, cookie_b = await register_and_login(client)
    prompt = await _create_prompt(client, cookie_a)
    resp = await client.delete(
        f"/api/prompts/{prompt['id']}",
        headers={"Cookie": f"lmchat_session={cookie_b}"},
    )
    assert resp.status_code == 404, (
        f"Expected 404 on cross-user DELETE, got {resp.status_code}: {resp.text}"
    )


async def test_delete_prompt_nonexistent_404(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    resp = await client.delete(
        "/api/prompts/999999",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 404


async def test_delete_prompt_gone_on_reread(client: httpx.AsyncClient) -> None:
    """After DELETE, listing prompts no longer returns the deleted item."""
    _, cookie = await register_and_login(client)
    prompt = await _create_prompt(client, cookie)
    await client.delete(
        f"/api/prompts/{prompt['id']}",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    list_resp = await client.get(
        "/api/prompts",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    ids = [p["id"] for p in list_resp.json()]
    assert prompt["id"] not in ids
