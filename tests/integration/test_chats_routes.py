# SPDX-License-Identifier: Apache-2.0
"""P10a.2 — live-backend integration tests for /api/chats (9 endpoints).

Endpoints under test
--------------------
POST   /api/chats                  create_chat
GET    /api/chats                  list_chats
PATCH  /api/chats/reorder          reorder_chats
GET    /api/chats/{id}             get_chat
PATCH  /api/chats/{id}             patch_chat
DELETE /api/chats/{id}             delete_chat
POST   /api/chats/{id}/fork        fork_chat
POST   /api/chats/{id}/compact     compact_chat
POST   /api/chats/{id}/messages    append_message

Cross-cutting invariants
------------------------
- Unauthenticated → 401 on every route.
- Cross-user access (User B on User A's chat) → 404, not 403.
- All mutation bodies are form-encoded (application/x-www-form-urlencoded).
- List endpoints return arrays and satisfy Invariant 3 (arrays, not objects).
"""
from __future__ import annotations

import secrets
from typing import Any

import httpx
import pytest

from tests.integration.conftest import register_and_login

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _create_chat(
    client: httpx.AsyncClient,
    cookie: str,
    title: str = "test chat",
) -> dict[str, Any]:
    resp = await client.post(
        "/api/chats",
        data={"title": title},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 201, f"create_chat failed: {resp.text}"
    return dict(resp.json())


# ---------------------------------------------------------------------------
# POST /api/chats
# ---------------------------------------------------------------------------


async def test_create_chat_happy(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    resp = await client.post(
        "/api/chats",
        data={"title": "My first chat"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["title"] == "My first chat"
    assert "id" in body
    assert "user_id" in body
    assert "created_at" in body
    assert "updated_at" in body
    assert body["pinned"] is False
    assert body["folder"] is None


async def test_create_chat_unauth(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/chats", data={"title": "no auth"})
    assert resp.status_code == 401


async def test_create_chat_missing_title(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    resp = await client.post(
        "/api/chats",
        data={},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/chats
# ---------------------------------------------------------------------------


async def test_list_chats_happy(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    await _create_chat(client, cookie, "alpha")
    await _create_chat(client, cookie, "beta")

    resp = await client.get(
        "/api/chats",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    # Invariant 3: top-level array
    assert isinstance(body, list)
    titles = {c["title"] for c in body}
    assert "alpha" in titles
    assert "beta" in titles


async def test_list_chats_folder_filter(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    chat = await _create_chat(client, cookie, "filtered")
    await client.patch(
        f"/api/chats/{chat['id']}",
        data={"folder": "work"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    resp = await client.get(
        "/api/chats?folder=work",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert any(c["title"] == "filtered" for c in body)


async def test_list_chats_unauth(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/chats")
    assert resp.status_code == 401


async def test_list_chats_isolation(client: httpx.AsyncClient) -> None:
    """User B's list must not contain User A's chats."""
    _, cookie_a = await register_and_login(client)
    _, cookie_b = await register_and_login(client)
    tag = secrets.token_hex(8)
    await _create_chat(client, cookie_a, f"private-{tag}")

    resp = await client.get(
        "/api/chats",
        headers={"Cookie": f"lmchat_session={cookie_b}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert not any(f"private-{tag}" in c["title"] for c in body)


# ---------------------------------------------------------------------------
# PATCH /api/chats/reorder
# ---------------------------------------------------------------------------


async def test_reorder_happy(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    chat = await _create_chat(client, cookie)

    resp = await client.patch(
        "/api/chats/reorder",
        data={"chat_id": str(chat["id"]), "display_order": "0"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


async def test_reorder_with_folder(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    chat = await _create_chat(client, cookie)

    resp = await client.patch(
        "/api/chats/reorder",
        data={
            "chat_id": str(chat["id"]),
            "folder": "projects",
            "display_order": "0",
        },
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200


async def test_reorder_cross_user_404(client: httpx.AsyncClient) -> None:
    _, cookie_a = await register_and_login(client)
    _, cookie_b = await register_and_login(client)
    chat = await _create_chat(client, cookie_a)

    resp = await client.patch(
        "/api/chats/reorder",
        data={"chat_id": str(chat["id"]), "display_order": "0"},
        headers={"Cookie": f"lmchat_session={cookie_b}"},
    )
    assert resp.status_code == 404


async def test_reorder_unauth(client: httpx.AsyncClient) -> None:
    resp = await client.patch(
        "/api/chats/reorder",
        data={"chat_id": "1", "display_order": "0"},
    )
    assert resp.status_code == 401


async def test_reorder_negative_display_order_422(client: httpx.AsyncClient) -> None:
    """display_order has ge=0 constraint — negative value → 422."""
    _, cookie = await register_and_login(client)
    chat = await _create_chat(client, cookie)

    resp = await client.patch(
        "/api/chats/reorder",
        data={"chat_id": str(chat["id"]), "display_order": "-1"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/chats/{chat_id}
# ---------------------------------------------------------------------------


async def test_get_chat_happy(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    created = await _create_chat(client, cookie, "get me")

    resp = await client.get(
        f"/api/chats/{created['id']}",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == created["id"]
    assert body["title"] == "get me"
    # messages key present (ChatWithMessagesResponse)
    assert "messages" in body
    assert isinstance(body["messages"], list)


async def test_get_chat_cross_user_404(client: httpx.AsyncClient) -> None:
    _, cookie_a = await register_and_login(client)
    _, cookie_b = await register_and_login(client)
    chat = await _create_chat(client, cookie_a)

    resp = await client.get(
        f"/api/chats/{chat['id']}",
        headers={"Cookie": f"lmchat_session={cookie_b}"},
    )
    assert resp.status_code == 404


async def test_get_chat_not_found_404(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    resp = await client.get(
        "/api/chats/999999",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 404


async def test_get_chat_unauth(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/chats/1")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# PATCH /api/chats/{chat_id}
# ---------------------------------------------------------------------------


async def test_patch_chat_rename(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    chat = await _create_chat(client, cookie, "original title")

    resp = await client.patch(
        f"/api/chats/{chat['id']}",
        data={"title": "new title"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200
    assert resp.json()["title"] == "new title"


async def test_patch_chat_pin(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    chat = await _create_chat(client, cookie)

    resp = await client.patch(
        f"/api/chats/{chat['id']}",
        data={"pinned": "true"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200
    assert resp.json()["pinned"] is True


async def test_patch_chat_settings_rag_enabled(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    chat = await _create_chat(client, cookie)

    resp = await client.patch(
        f"/api/chats/{chat['id']}",
        data={"rag_enabled": "true"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200
    assert resp.json()["settings"]["rag_enabled"] is True


async def test_patch_chat_settings_reasoning_effort(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    chat = await _create_chat(client, cookie)

    resp = await client.patch(
        f"/api/chats/{chat['id']}",
        data={"reasoning_effort": "high"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200
    assert resp.json()["settings"]["reasoning_effort"] == "high"


async def test_patch_chat_settings_ab_compare_invalid_json_422(
    client: httpx.AsyncClient,
) -> None:
    _, cookie = await register_and_login(client)
    chat = await _create_chat(client, cookie)

    resp = await client.patch(
        f"/api/chats/{chat['id']}",
        data={"ab_compare": "not-json"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 422


async def test_patch_chat_cross_user_404(client: httpx.AsyncClient) -> None:
    _, cookie_a = await register_and_login(client)
    _, cookie_b = await register_and_login(client)
    chat = await _create_chat(client, cookie_a)

    resp = await client.patch(
        f"/api/chats/{chat['id']}",
        data={"title": "hijack"},
        headers={"Cookie": f"lmchat_session={cookie_b}"},
    )
    assert resp.status_code == 404


async def test_patch_chat_unauth(client: httpx.AsyncClient) -> None:
    resp = await client.patch("/api/chats/1", data={"title": "no auth"})
    assert resp.status_code == 401


async def test_patch_chat_model_id_persists(client: httpx.AsyncClient) -> None:
    """PATCH model_id persists and GET list + detail both return it."""
    _, cookie = await register_and_login(client)
    chat = await _create_chat(client, cookie, "model-id persistence test")

    # New chat has no model_id.
    assert chat.get("model_id") is None

    model = "qwen3.6-35b-a3b-uncensored-heretic-native-mtp-preserved-i1"

    # PATCH to set model_id.
    patch_resp = await client.patch(
        f"/api/chats/{chat['id']}",
        data={"model_id": model},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert patch_resp.status_code == 200, patch_resp.text
    assert patch_resp.json()["model_id"] == model

    # GET /api/chats list returns model_id.
    list_resp = await client.get(
        "/api/chats",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert list_resp.status_code == 200
    chats_list = list_resp.json()
    matching = [c for c in chats_list if c["id"] == chat["id"]]
    assert matching, "created chat missing from list"
    assert matching[0]["model_id"] == model

    # GET /api/chats/{id} detail also returns model_id.
    detail_resp = await client.get(
        f"/api/chats/{chat['id']}",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert detail_resp.status_code == 200
    assert detail_resp.json()["model_id"] == model


# ---------------------------------------------------------------------------
# DELETE /api/chats/{chat_id}
# ---------------------------------------------------------------------------


async def test_delete_chat_happy(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    chat = await _create_chat(client, cookie, "to delete")

    resp = await client.delete(
        f"/api/chats/{chat['id']}",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 204

    # Confirm it's gone
    get_resp = await client.get(
        f"/api/chats/{chat['id']}",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert get_resp.status_code == 404


async def test_delete_chat_cross_user_404(client: httpx.AsyncClient) -> None:
    _, cookie_a = await register_and_login(client)
    _, cookie_b = await register_and_login(client)
    chat = await _create_chat(client, cookie_a)

    resp = await client.delete(
        f"/api/chats/{chat['id']}",
        headers={"Cookie": f"lmchat_session={cookie_b}"},
    )
    assert resp.status_code == 404


async def test_delete_chat_not_found_404(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    resp = await client.delete(
        "/api/chats/999999",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 404


async def test_delete_chat_unauth(client: httpx.AsyncClient) -> None:
    resp = await client.delete("/api/chats/1")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/chats/{chat_id}/fork
# ---------------------------------------------------------------------------


async def test_fork_chat_happy(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    chat = await _create_chat(client, cookie, "source chat")

    # Append a message so there's something to fork at
    msg_resp = await client.post(
        f"/api/chats/{chat['id']}/messages",
        data={"role": "user", "content": "hello"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert msg_resp.status_code == 201
    msg_id = msg_resp.json()["id"]

    fork_resp = await client.post(
        f"/api/chats/{chat['id']}/fork",
        data={"at_message_id": str(msg_id)},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert fork_resp.status_code == 201
    forked = fork_resp.json()
    assert forked["id"] != chat["id"]
    assert "title" in forked


async def test_fork_chat_cross_user_404(client: httpx.AsyncClient) -> None:
    _, cookie_a = await register_and_login(client)
    _, cookie_b = await register_and_login(client)
    chat = await _create_chat(client, cookie_a)

    resp = await client.post(
        f"/api/chats/{chat['id']}/fork",
        data={"at_message_id": "1"},
        headers={"Cookie": f"lmchat_session={cookie_b}"},
    )
    assert resp.status_code == 404


async def test_fork_chat_not_found_404(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    resp = await client.post(
        "/api/chats/999999/fork",
        data={"at_message_id": "1"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 404


async def test_fork_chat_missing_at_message_id_422(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    chat = await _create_chat(client, cookie)

    resp = await client.post(
        f"/api/chats/{chat['id']}/fork",
        data={},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 422


async def test_fork_chat_unauth(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/chats/1/fork", data={"at_message_id": "1"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/chats/{chat_id}/compact
# ---------------------------------------------------------------------------


async def test_compact_chat_happy(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    chat = await _create_chat(client, cookie, "compact me")

    resp = await client.post(
        f"/api/chats/{chat['id']}/compact",
        data={"target_tokens": "4000"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "chat_id" in body
    assert "removed_message_ids" in body
    assert isinstance(body["removed_message_ids"], list)
    assert "remaining_token_count" in body
    assert "original_token_count" in body
    assert body["chat_id"] == chat["id"]


async def test_compact_chat_cross_user_404(client: httpx.AsyncClient) -> None:
    _, cookie_a = await register_and_login(client)
    _, cookie_b = await register_and_login(client)
    chat = await _create_chat(client, cookie_a)

    resp = await client.post(
        f"/api/chats/{chat['id']}/compact",
        data={"target_tokens": "4000"},
        headers={"Cookie": f"lmchat_session={cookie_b}"},
    )
    assert resp.status_code == 404


async def test_compact_chat_not_found_404(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    resp = await client.post(
        "/api/chats/999999/compact",
        data={"target_tokens": "4000"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 404


async def test_compact_chat_target_too_low_422(client: httpx.AsyncClient) -> None:
    """target_tokens=1 is below the invariant minimum when the chat has messages.

    The compact service computes invariant_minimum = invariant_token_sum * 1.1.
    An empty chat has no invariant tokens (minimum = 0), so we must add a
    message long enough that its token count * 1.1 > 1 before calling compact.
    """
    _, cookie = await register_and_login(client)
    chat = await _create_chat(client, cookie)

    # Add a user message long enough that its tokens * 1.1 >> 1
    long_content = "word " * 50  # ~50 tokens; invariant_minimum >> 1
    await client.post(
        f"/api/chats/{chat['id']}/messages",
        data={"role": "user", "content": long_content},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )

    resp = await client.post(
        f"/api/chats/{chat['id']}/compact",
        data={"target_tokens": "1"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 422


async def test_compact_chat_unauth(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/chats/1/compact", data={"target_tokens": "4000"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/chats/{chat_id}/messages (append_message)
# ---------------------------------------------------------------------------


async def test_append_message_happy(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    chat = await _create_chat(client, cookie)

    resp = await client.post(
        f"/api/chats/{chat['id']}/messages",
        data={"role": "user", "content": "Hello world"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["role"] == "user"
    assert body["content"] == "Hello world"
    assert body["chat_id"] == chat["id"]
    assert "id" in body


async def test_append_message_with_optional_fields(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    chat = await _create_chat(client, cookie)

    resp = await client.post(
        f"/api/chats/{chat['id']}/messages",
        data={
            "role": "user",
            "content": "Hello",
            "reasoning_content": "<think>let me think</think>",
            "model_id": "stub-model-q4",
        },
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["reasoning_content"] == "<think>let me think</think>"


async def test_append_message_invalid_role_422(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    chat = await _create_chat(client, cookie)

    resp = await client.post(
        f"/api/chats/{chat['id']}/messages",
        data={"role": "bogus_role", "content": "hi"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 422


async def test_append_message_cross_user_404(client: httpx.AsyncClient) -> None:
    _, cookie_a = await register_and_login(client)
    _, cookie_b = await register_and_login(client)
    chat = await _create_chat(client, cookie_a)

    resp = await client.post(
        f"/api/chats/{chat['id']}/messages",
        data={"role": "user", "content": "hijack"},
        headers={"Cookie": f"lmchat_session={cookie_b}"},
    )
    assert resp.status_code == 404


async def test_append_message_not_found_404(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    resp = await client.post(
        "/api/chats/999999/messages",
        data={"role": "user", "content": "hi"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 404


async def test_append_message_unauth(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/chats/1/messages",
        data={"role": "user", "content": "hi"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# PATCH /api/chats/{id} — per-chat override clear-to-inherit.
# FastAPI coerces an empty form value to None on an optional param, so a field
# sent EMPTY (clear) was indistinguishable from OMITTED — every clear silently
# failed (numeric fields additionally 422'd on the "null" the rail serialises).
# The route now reads the raw form to recover the present-but-empty signal.
# ---------------------------------------------------------------------------


async def test_numeric_rail_override_set_then_clear(
    client: httpx.AsyncClient,
) -> None:
    _, cookie = await register_and_login(client)
    chat = await _create_chat(client, cookie)
    hdr = {"Cookie": f"lmchat_session={cookie}"}
    cid = chat["id"]

    # Set a real value → stored.
    resp = await client.patch(
        f"/api/chats/{cid}", data={"temperature": "0.7"}, headers=hdr
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["settings"]["temperature"] == 0.7

    # Clear via "null" — the string the rail serialises the JS `null` to. 200,
    # NOT a 422, and the override is gone.
    resp = await client.patch(
        f"/api/chats/{cid}", data={"temperature": "null"}, headers=hdr
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["settings"].get("temperature") is None

    # Clear via a present-but-empty value also works (belt-and-suspenders).
    await client.patch(f"/api/chats/{cid}", data={"top_k": "40"}, headers=hdr)
    resp = await client.patch(f"/api/chats/{cid}", data={"top_k": ""}, headers=hdr)
    assert resp.status_code == 200, resp.text
    assert resp.json()["settings"].get("top_k") is None


async def test_perchat_string_fields_clear_via_empty(
    client: httpx.AsyncClient,
) -> None:
    """reasoning / repeat_warning_cut_k / system_prompt clear on a present-but-
    empty submission (the raw-form path recovers what FastAPI swallowed)."""
    _, cookie = await register_and_login(client)
    chat = await _create_chat(client, cookie)
    hdr = {"Cookie": f"lmchat_session={cookie}"}
    cid = chat["id"]

    for key, val in (
        ("reasoning", "high"),
        ("reasoning_effort", "high"),
        ("active_preset", "general"),
        ("repeat_warning_cut_k", "25"),
        ("system_prompt", "hello"),
    ):
        resp = await client.patch(f"/api/chats/{cid}", data={key: val}, headers=hdr)
        assert resp.status_code == 200, resp.text
        assert resp.json()["settings"].get(key) is not None, f"{key} did not persist"

        resp = await client.patch(f"/api/chats/{cid}", data={key: ""}, headers=hdr)
        assert resp.status_code == 200, resp.text
        assert resp.json()["settings"].get(key) is None, f"{key} did not clear"

    # Enum/id fields also clear via the "null" the rail may serialise (they
    # never carry a literal "null" value, unlike free-text system_prompt).
    await client.patch(f"/api/chats/{cid}", data={"reasoning": "low"}, headers=hdr)
    resp = await client.patch(
        f"/api/chats/{cid}", data={"reasoning": "null"}, headers=hdr
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["settings"].get("reasoning") is None


async def test_system_prompt_literal_null_is_preserved(
    client: httpx.AsyncClient,
) -> None:
    """A literal "null" is a VALID system prompt — it must be stored, not
    treated as a clear sentinel (free text ≠ numeric fields)."""
    _, cookie = await register_and_login(client)
    chat = await _create_chat(client, cookie)
    resp = await client.patch(
        f"/api/chats/{chat['id']}",
        data={"system_prompt": "null"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["settings"].get("system_prompt") == "null"


async def test_numeric_rail_override_rejects_non_numeric(
    client: httpx.AsyncClient,
) -> None:
    _, cookie = await register_and_login(client)
    chat = await _create_chat(client, cookie)
    resp = await client.patch(
        f"/api/chats/{chat['id']}",
        data={"max_tokens": "not-a-number"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 422, resp.text
