# SPDX-License-Identifier: Apache-2.0
"""P10a.3 — live-backend integration tests for GET /api/search.

Endpoint under test
-------------------
GET /api/search   search_messages (scope: messages | chats | memory | all)

Cross-cutting invariants
------------------------
- Unauthenticated → 401.
- All results scoped to the authenticated user — cross-user data never leaks.
- FTS5 special operators (OR, AND, *) in the query must not error (they are
  passed to the underlying service; robustness to user-supplied operators is
  a correctness requirement, not a 422 condition).

Scope coverage
--------------
- scope=messages  (default) — basic FTS5 / ILIKE hit + empty result.
- scope=chats     — chat title substring search.
- scope=memory    — pinned insight substring search.
- scope=all       — fan-out across all three scopes.

Note on scope=messages / FTS5
-------------------------------
The test database uses SQLite so the FTS5 code path is active.  The
message_service.search() implementation handles FTS5 query sanitisation
internally; the integration test verifies that operator-rich queries do
not raise an HTTP 500.
"""
from __future__ import annotations

import secrets
from typing import Any

import httpx
import pytest

from tests.integration.conftest import register_and_login

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_chat(
    client: httpx.AsyncClient,
    cookie: str,
    title: str,
) -> dict[str, Any]:
    resp = await client.post(
        "/api/chats",
        data={"title": title},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 201, f"create_chat failed: {resp.text}"
    return dict(resp.json())


async def _append_message(
    client: httpx.AsyncClient,
    cookie: str,
    chat_id: int,
    content: str,
) -> dict[str, Any]:
    resp = await client.post(
        f"/api/chats/{chat_id}/messages",
        data={"role": "user", "content": content},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 201, f"append_message failed: {resp.text}"
    return dict(resp.json())


async def _pin(
    client: httpx.AsyncClient,
    cookie: str,
    text: str,
) -> dict[str, Any]:
    resp = await client.post(
        "/api/memory/pin",
        data={"text": text},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 201, f"pin failed: {resp.text}"
    return dict(resp.json())


# ---------------------------------------------------------------------------
# GET /api/search — authentication
# ---------------------------------------------------------------------------


async def test_search_unauth_401(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/search", params={"q": "hello"})
    assert resp.status_code == 401


async def test_search_missing_q_422(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    resp = await client.get(
        "/api/search",
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 422


async def test_search_empty_q_422(client: httpx.AsyncClient) -> None:
    """q has min_length=1; empty string → 422."""
    _, cookie = await register_and_login(client)
    resp = await client.get(
        "/api/search",
        params={"q": ""},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# scope=messages (default)
# ---------------------------------------------------------------------------


async def test_search_messages_happy(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    tag = secrets.token_hex(8)
    chat = await _create_chat(client, cookie, "search test chat")
    await _append_message(client, cookie, chat["id"], f"unique phrase {tag}")

    resp = await client.get(
        "/api/search",
        params={"q": tag, "scope": "messages"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    # At least one message should contain the tag.
    contents = [m.get("content", "") for m in body]
    assert any(tag in c for c in contents)


async def test_search_messages_no_results(client: httpx.AsyncClient) -> None:
    """Query that matches nothing returns an empty list (not an error)."""
    _, cookie = await register_and_login(client)
    resp = await client.get(
        "/api/search",
        params={"q": "zzz_no_match_" + secrets.token_hex(16), "scope": "messages"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200
    assert resp.json() == []


async def test_search_messages_scope_user_isolation(
    client: httpx.AsyncClient,
) -> None:
    """Messages from User A must not appear in User B's search results."""
    _, cookie_a = await register_and_login(client)
    _, cookie_b = await register_and_login(client)

    tag = "isolation_tag_" + secrets.token_hex(8)
    chat = await _create_chat(client, cookie_a, "isolation chat")
    await _append_message(client, cookie_a, chat["id"], f"secret message {tag}")

    resp = await client.get(
        "/api/search",
        params={"q": tag, "scope": "messages"},
        headers={"Cookie": f"lmchat_session={cookie_b}"},
    )
    assert resp.status_code == 200
    contents = [m.get("content", "") for m in resp.json()]
    assert not any(tag in c for c in contents)


async def test_search_messages_chat_id_filter(client: httpx.AsyncClient) -> None:
    """chat_id filter restricts results to the specified chat."""
    _, cookie = await register_and_login(client)
    tag = secrets.token_hex(8)
    chat_a = await _create_chat(client, cookie, "chat a")
    chat_b = await _create_chat(client, cookie, "chat b")
    await _append_message(client, cookie, chat_a["id"], f"msg in chat a {tag}")
    await _append_message(client, cookie, chat_b["id"], f"msg in chat b {tag}")

    resp = await client.get(
        "/api/search",
        params={"q": tag, "scope": "messages", "chat_id": str(chat_a["id"])},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    # All returned messages should belong to chat_a only.
    for msg in body:
        assert msg["chat_id"] == chat_a["id"]


# ---------------------------------------------------------------------------
# FTS5 operator robustness
# ---------------------------------------------------------------------------


async def test_search_messages_fts5_or_operator_no_error(
    client: httpx.AsyncClient,
) -> None:
    """Query containing OR operator must not return 500."""
    _, cookie = await register_and_login(client)
    resp = await client.get(
        "/api/search",
        params={"q": "hello OR world", "scope": "messages"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200


async def test_search_messages_fts5_and_operator_no_error(
    client: httpx.AsyncClient,
) -> None:
    """Query containing AND operator must not return 500."""
    _, cookie = await register_and_login(client)
    resp = await client.get(
        "/api/search",
        params={"q": "hello AND world", "scope": "messages"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200


async def test_search_messages_fts5_wildcard_no_error(
    client: httpx.AsyncClient,
) -> None:
    """Query containing * wildcard must not return 500."""
    _, cookie = await register_and_login(client)
    resp = await client.get(
        "/api/search",
        params={"q": "hel*", "scope": "messages"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# scope=chats
# ---------------------------------------------------------------------------


async def test_search_chats_happy(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    tag = secrets.token_hex(8)
    await _create_chat(client, cookie, f"project chat {tag}")

    resp = await client.get(
        "/api/search",
        params={"q": tag, "scope": "chats"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    titles = [c.get("title", "") for c in body]
    assert any(tag in t for t in titles)


async def test_search_chats_isolation(client: httpx.AsyncClient) -> None:
    """User B cannot find User A's chats via scope=chats."""
    _, cookie_a = await register_and_login(client)
    _, cookie_b = await register_and_login(client)

    tag = "chats_iso_" + secrets.token_hex(8)
    await _create_chat(client, cookie_a, f"secret chat {tag}")

    resp = await client.get(
        "/api/search",
        params={"q": tag, "scope": "chats"},
        headers={"Cookie": f"lmchat_session={cookie_b}"},
    )
    assert resp.status_code == 200
    titles = [c.get("title", "") for c in resp.json()]
    assert not any(tag in t for t in titles)


async def test_search_chats_unauth(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/search", params={"q": "test", "scope": "chats"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# scope=memory
# ---------------------------------------------------------------------------


async def test_search_memory_happy(client: httpx.AsyncClient) -> None:
    _, cookie = await register_and_login(client)
    tag = secrets.token_hex(8)
    await _pin(client, cookie, f"remember this thing {tag}")

    resp = await client.get(
        "/api/search",
        params={"q": tag, "scope": "memory"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    texts = [m.get("text", "") for m in body]
    assert any(tag in t for t in texts)


async def test_search_memory_isolation(client: httpx.AsyncClient) -> None:
    """User B cannot find User A's pinned insights via scope=memory."""
    _, cookie_a = await register_and_login(client)
    _, cookie_b = await register_and_login(client)

    tag = "mem_iso_" + secrets.token_hex(8)
    await _pin(client, cookie_a, f"user a private memory {tag}")

    resp = await client.get(
        "/api/search",
        params={"q": tag, "scope": "memory"},
        headers={"Cookie": f"lmchat_session={cookie_b}"},
    )
    assert resp.status_code == 200
    texts = [m.get("text", "") for m in resp.json()]
    assert not any(tag in t for t in texts)


async def test_search_memory_unauth(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/search", params={"q": "test", "scope": "memory"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# scope=all
# ---------------------------------------------------------------------------


async def test_search_all_happy(client: httpx.AsyncClient) -> None:
    """scope=all returns a dict with messages, chats, memory keys."""
    _, cookie = await register_and_login(client)
    tag = secrets.token_hex(8)

    chat = await _create_chat(client, cookie, f"all scope chat {tag}")
    await _append_message(client, cookie, chat["id"], f"all scope message {tag}")
    await _pin(client, cookie, f"all scope memory {tag}")

    resp = await client.get(
        "/api/search",
        params={"q": tag, "scope": "all"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, dict)
    assert "messages" in body
    assert "chats" in body
    assert "memory" in body
    assert isinstance(body["messages"], list)
    assert isinstance(body["chats"], list)
    assert isinstance(body["memory"], list)

    # Each scope should find the tagged item.
    msg_contents = [m.get("content", "") for m in body["messages"]]
    assert any(tag in c for c in msg_contents)

    chat_titles = [c.get("title", "") for c in body["chats"]]
    assert any(tag in t for t in chat_titles)

    mem_texts = [m.get("text", "") for m in body["memory"]]
    assert any(tag in t for t in mem_texts)


async def test_search_all_isolation(client: httpx.AsyncClient) -> None:
    """scope=all must not return any of User A's data to User B."""
    _, cookie_a = await register_and_login(client)
    _, cookie_b = await register_and_login(client)

    tag = "all_iso_" + secrets.token_hex(8)
    chat = await _create_chat(client, cookie_a, f"secret {tag}")
    await _append_message(client, cookie_a, chat["id"], f"secret msg {tag}")
    await _pin(client, cookie_a, f"secret pin {tag}")

    resp = await client.get(
        "/api/search",
        params={"q": tag, "scope": "all"},
        headers={"Cookie": f"lmchat_session={cookie_b}"},
    )
    assert resp.status_code == 200
    body = resp.json()

    msg_contents = [m.get("content", "") for m in body.get("messages", [])]
    assert not any(tag in c for c in msg_contents)

    chat_titles = [c.get("title", "") for c in body.get("chats", [])]
    assert not any(tag in t for t in chat_titles)

    mem_texts = [m.get("text", "") for m in body.get("memory", [])]
    assert not any(tag in t for t in mem_texts)


async def test_search_all_unauth(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/search", params={"q": "test", "scope": "all"})
    assert resp.status_code == 401


async def test_search_invalid_scope_422(client: httpx.AsyncClient) -> None:
    """Invalid scope value → 422 (enum validation by FastAPI)."""
    _, cookie = await register_and_login(client)
    resp = await client.get(
        "/api/search",
        params={"q": "test", "scope": "invalid_scope"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 422
