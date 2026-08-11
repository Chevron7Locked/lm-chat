# SPDX-License-Identifier: Apache-2.0
"""P10a.2 — live-backend integration tests for /api/messages (2 endpoints).

Endpoints under test
--------------------
PATCH  /api/messages/{id}                   edit_message
DELETE /api/messages/{id}                   delete_message

Cross-cutting invariants
------------------------
- Unauthenticated → 401.
- Cross-user access → 404 (not 403, not 401).
- All mutation bodies are form-encoded.
"""
from __future__ import annotations

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
    assert resp.status_code == 201
    return dict(resp.json())


async def _append_message(
    client: httpx.AsyncClient,
    cookie: str,
    chat_id: int,
    role: str = "user",
    content: str = "test message",
) -> dict[str, Any]:
    resp = await client.post(
        f"/api/chats/{chat_id}/messages",
        data={"role": role, "content": content},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 201
    return dict(resp.json())


# ---------------------------------------------------------------------------
# PATCH /api/messages/{message_id}
# ---------------------------------------------------------------------------


async def test_edit_message_happy(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    chat = await _create_chat(client, cookie)
    msg = await _append_message(client, cookie, chat["id"], role="user", content="original")

    resp = await client.patch(
        f"/api/messages/{msg['id']}",
        data={"content": "edited content"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == "edited content"
    assert body["id"] == msg["id"]


async def test_edit_message_assistant_role_400(client: httpx.AsyncClient) -> None:
    """Editing a non-user-role message returns 400 (EditNotAllowedError)."""
    _, cookie = await register_and_login(client)
    chat = await _create_chat(client, cookie)
    msg = await _append_message(
        client, cookie, chat["id"], role="assistant", content="assistant reply"
    )

    resp = await client.patch(
        f"/api/messages/{msg['id']}",
        data={"content": "tampered"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 400


async def test_edit_message_not_found_404(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    resp = await client.patch(
        "/api/messages/999999",
        data={"content": "anything"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 404


async def test_edit_message_cross_user_403(client: httpx.AsyncClient) -> None:
    """P13l.1 contract: cross-user edits return 403 (not 404).

    The new endpoint distinguishes "you don't own this" (403) from
    "this message doesn't exist" (404) so the UI can render the right
    affordance.
    """
    _, cookie_a = await register_and_login(client)
    _, cookie_b = await register_and_login(client)
    chat = await _create_chat(client, cookie_a)
    msg = await _append_message(client, cookie_a, chat["id"])

    resp = await client.patch(
        f"/api/messages/{msg['id']}",
        data={"content": "hijack"},
        headers={"Cookie": f"lmchat_session={cookie_b}"},
    )
    assert resp.status_code == 403


async def test_edit_message_unauth(client: httpx.AsyncClient) -> None:
    resp = await client.patch("/api/messages/1", data={"content": "x"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# DELETE /api/messages/{message_id}
# ---------------------------------------------------------------------------


async def test_delete_message_happy(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    chat = await _create_chat(client, cookie)
    msg = await _append_message(client, cookie, chat["id"])

    resp = await client.delete(
        f"/api/messages/{msg['id']}",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 204


async def test_delete_message_not_found_404(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    resp = await client.delete(
        "/api/messages/999999",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 404


async def test_delete_message_cross_user_404(client: httpx.AsyncClient) -> None:
    _, cookie_a = await register_and_login(client)
    _, cookie_b = await register_and_login(client)
    chat = await _create_chat(client, cookie_a)
    msg = await _append_message(client, cookie_a, chat["id"])

    resp = await client.delete(
        f"/api/messages/{msg['id']}",
        headers={"Cookie": f"lmchat_session={cookie_b}"},
    )
    assert resp.status_code == 404


async def test_delete_message_unauth(client: httpx.AsyncClient) -> None:
    resp = await client.delete("/api/messages/1")
    assert resp.status_code == 401
