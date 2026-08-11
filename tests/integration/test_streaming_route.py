# SPDX-License-Identifier: Apache-2.0
"""P10a.2 — live-backend integration tests for POST /api/chat/stream.

Endpoint under test
-------------------
POST /api/chat/stream

Invariants tested
-----------------
- Happy path: text/event-stream response; frames carry
  event: <type> + data: {"type":..., "msg_id":...}; sequence includes
  chat.start → message.delta (×N) → message.end → chat.end.
- 409 StreamInProgressError: second concurrent stream on same chat.
- 401 unauthenticated.
- 404 cross-user (stream into another user's chat).

Stub requirements
-----------------
The conftest stub LM Studio (``_stub_lm_app``) serves POST /api/v1/chat.
The main app's native adapter calls it and expects proper LM Studio SSE:

    event: chat.start
    data: {"type":"chat.start","response_id":"r-stub"}

    event: message.delta
    data: {"type":"message.delta","content":"word"}

    event: message.end
    data: {"type":"message.end"}

    event: chat.end
    data: {"type":"chat.end","response_id":"r-stub"}

The original stub used bare ``data:`` lines only (no ``event:`` field).
That format is not parsed by the native SSE decoder.  The conftest was
updated to emit proper LM Studio native SSE in the same commit.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tests.integration.conftest import register_and_login

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Minimal valid ChatStreamRequest payload
# ---------------------------------------------------------------------------

_STUB_MODEL = "stub-model-q4"


def _stream_body(chat_id: int) -> dict[str, Any]:
    """Return a minimal valid ChatStreamRequest as a JSON-serialisable dict."""
    return {
        "chat_id": chat_id,
        "payload": {
            "model": _STUB_MODEL,
            "input": [{"type": "text", "content": "hello"}],
        },
    }


# ---------------------------------------------------------------------------
# SSE parsing helpers
# ---------------------------------------------------------------------------


def _parse_sse_frames(raw: bytes) -> list[dict[str, Any]]:
    """Parse lm-chat SSE output into a list of data dicts.

    The streaming route emits:
        event: <type>
        data: <json>

    Returns list of parsed ``data`` dicts, preserving order.
    """
    frames: list[dict[str, Any]] = []
    current_data: str | None = None

    for raw_line in raw.decode("utf-8", errors="replace").splitlines():
        line = raw_line.rstrip("\r")
        if line.startswith("event:"):
            pass  # event name is also in data.type; not needed for assertions
        elif line.startswith("data:"):
            current_data = line[len("data:"):].strip()
        elif line == "":
            if current_data is not None:
                try:
                    frames.append(json.loads(current_data))
                except json.JSONDecodeError:
                    pass
            current_data = None

    # Handle stream without trailing blank line
    if current_data is not None:
        try:
            frames.append(json.loads(current_data))
        except json.JSONDecodeError:
            pass

    return frames


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_chat(
    client: httpx.AsyncClient,
    cookie: str,
    title: str = "stream chat",
) -> dict[str, Any]:
    resp = await client.post(
        "/api/chats",
        data={"title": title},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 201
    return dict(resp.json())


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_stream_happy_path(client: httpx.AsyncClient) -> None:
    """Full SSE happy path: content-type + frame sequence + msg_id present."""
    _, cookie = await register_and_login(client)
    chat = await _create_chat(client, cookie)

    resp = await client.post(
        "/api/chat/stream",
        json=_stream_body(chat["id"]),
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers.get("content-type", "")

    frames = _parse_sse_frames(resp.content)
    assert len(frames) > 0, "Expected at least one SSE frame"

    event_types = [f["type"] for f in frames]
    # Every frame must carry msg_id
    for frame in frames:
        assert "msg_id" in frame, f"Frame missing msg_id: {frame}"
        assert isinstance(frame["msg_id"], int)

    # chat.start must be first
    assert event_types[0] == "chat.start", f"First event was {event_types[0]!r}"

    # chat.end must be present
    assert "chat.end" in event_types, f"chat.end missing from {event_types}"

    # At least one message.delta
    assert "message.delta" in event_types, (
        f"message.delta missing from {event_types}"
    )


async def test_stream_response_headers(client: httpx.AsyncClient) -> None:
    """Verify anti-buffering headers are present."""
    _, cookie = await register_and_login(client)
    chat = await _create_chat(client, cookie)

    resp = await client.post(
        "/api/chat/stream",
        json=_stream_body(chat["id"]),
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 200
    assert resp.headers.get("x-accel-buffering") == "no"
    assert resp.headers.get("cache-control") == "no-cache"


# ---------------------------------------------------------------------------
# 401 unauthenticated
# ---------------------------------------------------------------------------


async def test_stream_unauth_401(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/chat/stream",
        json={"chat_id": 1, "payload": {"model": _STUB_MODEL}},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 404 cross-user
# ---------------------------------------------------------------------------


async def test_stream_cross_user_404(client: httpx.AsyncClient) -> None:
    """Streaming into another user's chat returns 404 (not 403).

    Bug surfaced by P10a.2: the streaming service lacked an ownership check
    before inserting the draft row.  Fixed in the same commit by adding a
    chat ownership query inside the per-chat lock in StreamingService.stream_chat.
    """
    _, cookie_a = await register_and_login(client)
    _, cookie_b = await register_and_login(client)
    chat_a = await _create_chat(client, cookie_a)

    resp = await client.post(
        "/api/chat/stream",
        json=_stream_body(chat_a["id"]),
        headers={"Cookie": f"lmchat_session={cookie_b}"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 409 StreamInProgressError
# ---------------------------------------------------------------------------


async def test_stream_409_concurrent_stream(
    client: httpx.AsyncClient,
    live_servers: dict[str, Any],
) -> None:
    """A second stream on the same chat while a draft row exists → 409.

    We simulate this by manually inserting a draft row into the messages table,
    then attempting a stream.  The streaming service's single-stream invariant
    check will detect the in-progress row and raise StreamInProgressError.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    _, cookie = await register_and_login(client)
    chat = await _create_chat(client, cookie)

    # Create a DB engine pointing at the test DB
    db_url = f"sqlite+aiosqlite:///{live_servers['db_path']}"
    eng = create_async_engine(db_url)

    try:
        # Insert a fake draft row for this chat so the invariant check fires
        async with eng.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO messages "
                    "(chat_id, role, content, state, reasoning_content, model_id) "
                    "VALUES (:chat_id, 'assistant', '', 'draft', NULL, 'stub')"
                ),
                {"chat_id": chat["id"]},
            )

        resp = await client.post(
            "/api/chat/stream",
            json=_stream_body(chat["id"]),
            headers={"Cookie": f"lmchat_session={cookie}"},
        )
        assert resp.status_code == 409
        body = resp.json()
        # The route returns {"code": "stream_in_progress", "chat_id": ...}
        assert body["detail"]["code"] == "stream_in_progress"
        assert body["detail"]["chat_id"] == chat["id"]
    finally:
        # Clean up the draft row so the session-scoped server stays usable
        async with eng.begin() as conn:
            await conn.execute(
                text(
                    "DELETE FROM messages WHERE chat_id = :cid AND state = 'draft'"
                ),
                {"cid": chat["id"]},
            )
        await eng.dispose()


# ---------------------------------------------------------------------------
# Malformed payload
# ---------------------------------------------------------------------------


async def test_stream_missing_chat_id_422(client: httpx.AsyncClient) -> None:
    """Missing chat_id in JSON body → 422 (Pydantic validation)."""
    _, cookie = await register_and_login(client)
    resp = await client.post(
        "/api/chat/stream",
        json={"payload": {"model": _STUB_MODEL}},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 422


async def test_stream_missing_model_422(client: httpx.AsyncClient) -> None:
    """Missing model in payload → 422."""
    _, cookie = await register_and_login(client)
    chat = await _create_chat(client, cookie)
    resp = await client.post(
        "/api/chat/stream",
        json={"chat_id": chat["id"], "payload": {}},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 422
