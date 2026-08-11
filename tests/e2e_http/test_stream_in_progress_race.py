# SPDX-License-Identifier: Apache-2.0
"""§2I — stream_in_progress 409 race test.

Tests the single-stream-per-chat invariant under concurrent access.

Scenarios
---------
1. **Same-chat race**: N=10 concurrent POST /api/chat/stream to the same chat_id.
   - Exactly 1 returns HTTP 200 (the winner).
   - N-1 return HTTP 409 with ``code: stream_in_progress``.
   - No 5xx responses.
   - Exactly 1 ``messages`` row in ``state='draft'`` (no race-double-persist).

2. **Different-chat race**: N=10 concurrent POSTs to *different* chat_ids.
   - All 10 return HTTP 200.

3. **Cancel-during-race**: Client aborts one mid-flight; assert the next
   request to the same chat starts fresh (no orphan draft).
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from tests.integration.conftest import register_and_login

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_STUB_MODEL: str = "stub-model-q4"
_CONCURRENT_N: int = 10


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stream_body(chat_id: int) -> dict[str, Any]:
    """Return a minimal valid ChatStreamRequest dict."""
    return {
        "chat_id": chat_id,
        "payload": {
            "model": _STUB_MODEL,
            "input": [{"type": "text", "content": "hello"}],
        },
    }


async def _create_chat(
    client: httpx.AsyncClient,
    cookie: str,
    title: str = "race chat",
) -> dict[str, Any]:
    """Create a chat and return the parsed JSON body."""
    resp = await client.post(
        "/api/chats",
        data={"title": title},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert resp.status_code == 201, f"create chat failed: {resp.text}"
    return dict(resp.json())


async def _fire_stream(
    client: httpx.AsyncClient,
    chat_id: int,
    cookie: str,
    req_timeout: float = 30.0,
) -> httpx.Response:
    """POST /api/chat/stream and return the full response.

    Uses a *new* ``AsyncClient`` session so concurrent requests don't
    share connection pools in ways that might serialise them at the
    transport layer.
    """
    async with httpx.AsyncClient(
        base_url=client.base_url,
        timeout=req_timeout,
    ) as fresh:
        return await fresh.post(
            "/api/chat/stream",
            json=_stream_body(chat_id),
            headers={"Cookie": f"lmchat_session={cookie}"},
        )


def _parse_sse_frames(raw: bytes) -> list[dict[str, Any]]:
    """Parse lm-chat SSE output into a list of data dicts."""
    frames: list[dict[str, Any]] = []
    current_data: str | None = None

    for raw_line in raw.decode("utf-8", errors="replace").splitlines():
        line = raw_line.rstrip("\r")
        if line.startswith("data:"):
            current_data = line[len("data:"):].strip()
        elif line == "":
            if current_data is not None:
                try:
                    frames.append(json.loads(current_data))
                except json.JSONDecodeError:
                    pass
            current_data = None

    if current_data is not None:
        try:
            frames.append(json.loads(current_data))
        except json.JSONDecodeError:
            pass

    return frames


# ---------------------------------------------------------------------------
# Scenario 1: same-chat race — exactly 1 winner, N-1 × 409
# ---------------------------------------------------------------------------


async def test_stream_race_same_chat_returns_409(
    client: httpx.AsyncClient,
    live_server: dict[str, Any],
) -> None:
    """N concurrent streams on the same chat → 1×200, N-1×409, no 5xx.

    Verifies that the per-chat lock + single-stream invariant in
    StreamingService.stream_chat serializes concurrent access correctly.
    """
    _, cookie = await register_and_login(client)
    chat = await _create_chat(client, cookie)
    chat_id = chat["id"]

    # Fire N concurrent stream requests to the SAME chat_id.
    tasks = [_fire_stream(client, chat_id, cookie) for _ in range(_CONCURRENT_N)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Classify responses.
    status_counts: dict[int, int] = {}
    for result in results:
        assert not isinstance(result, BaseException), (
            f"Unexpected exception: {result}"
        )
        r: httpx.Response = result
        status_counts[r.status_code] = status_counts.get(r.status_code, 0) + 1

    # Assert exactly one 200 and N-1 409.
    assert status_counts.get(200, 0) == 1, (
        f"Expected exactly 1 × 200, got {status_counts.get(200, 0)}: "
        f"status distribution: {status_counts}"
    )
    assert status_counts.get(409, 0) == _CONCURRENT_N - 1, (
        f"Expected {_CONCURRENT_N - 1} × 409, got {status_counts.get(409, 0)}: "
        f"status distribution: {status_counts}"
    )

    # No 5xx.
    for code in status_counts:
        assert code < 500, f"Unexpected 5xx status {code}"

    # Verify the 409 bodies carry the expected error shape.
    for result in results:
        assert isinstance(result, httpx.Response)
        if result.status_code == 409:
            body = result.json()
            assert body["detail"]["code"] == "stream_in_progress"
            assert body["detail"]["chat_id"] == chat_id

    # DB assertion: exactly 1 assistant messages row created (no double-persist).
    # By the time we check, the winning stream may have already completed and
    # transitioned from "draft" to a terminal state, so we count ALL rows
    # for this chat (any state), not just those still in draft.
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(
        f"sqlite+aiosqlite:///{live_server['db_path']}"
    )
    try:
        async with eng.begin() as conn:
            result = await conn.execute(
                text(
                    "SELECT COUNT(*) FROM messages "
                    "WHERE chat_id = :cid AND role = 'assistant'"
                ),
                {"cid": chat_id},
            )
            msg_count = result.scalar()
        # The winning stream may finalize before our DB query, so the row
        # could be in any state. The invariant is: only ONE assistant row
        # was ever created for this chat by the race (no double-persist).
        assert msg_count == 1, (
            f"Expected exactly 1 assistant message row, got {msg_count} — "
            "race may have double-persisted"
        )
    finally:
        await eng.dispose()


# ---------------------------------------------------------------------------
# Scenario 2: different-chat race — all 10 succeed
# ---------------------------------------------------------------------------


async def test_stream_race_different_chats_all_200(
    client: httpx.AsyncClient,
) -> None:
    """N concurrent streams on N *different* chats → all 200.

    When each stream targets a different chat, the per-chat lock doesn't
    serialise across chats, so all N should complete normally.
    """
    _, cookie = await register_and_login(client)

    # Create N chats (one per concurrent stream).
    chats: list[dict[str, Any]] = []
    for i in range(_CONCURRENT_N):
        chat = await _create_chat(client, cookie, title=f"race chat {i}")
        chats.append(chat)

    # Fire one stream per chat concurrently.
    tasks = [_fire_stream(client, chat["id"], cookie) for chat in chats]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for result in results:
        assert not isinstance(result, BaseException), (
            f"Unexpected exception: {result}"
        )
        r: httpx.Response = result
        assert r.status_code == 200, (
            f"Expected 200, got {r.status_code}: {r.text[:200]}"
        )
        # Verify each response is a valid SSE stream.
        assert "text/event-stream" in r.headers.get("content-type", ""), (
            f"Missing event-stream content-type: {r.headers}"
        )


# ---------------------------------------------------------------------------
# Scenario 3: cancel-during-race — abort one, next starts fresh
# ---------------------------------------------------------------------------


async def test_stream_race_cancel_then_retry(
    client: httpx.AsyncClient,
    live_server: dict[str, Any],
) -> None:
    """Complete a stream normally, then verify a second stream starts fresh.

    The disconnect-watcher fires on a 500ms poll, but the happy-text mock
    stream completes in < 0.1s (< 7 events × 0.01s delay).  To prove the
    "no orphan draft" invariant under fast stream completion, this test:

    1. Starts a stream and reads the full SSE response body (completes
       normally, which runs the finalize path).
    2. Queries the DB to confirm the assistant message is in a terminal
       state (not stuck in draft).
    3. Sends a new stream request to the same chat.
    4. Asserts 200 — the previous draft was properly finalized, so the
       lock+invariant guard lets the second stream through.

    For mid-flight disconnect cleanup (abort before stream end), the
    disconnect watcher needs a mock script with > 500ms of delay between
    events — deferred to a follow-up test (stalled-stream variant).
    """
    _, cookie = await register_and_login(client)
    chat = await _create_chat(client, cookie)
    chat_id = chat["id"]

    # Step 1: Start a stream and read the FULL response (normal completion).
    async with httpx.AsyncClient(
        base_url=client.base_url,
        timeout=30.0,
    ) as fresh:
        resp = await fresh.post(
            "/api/chat/stream",
            json=_stream_body(chat_id),
            headers={"Cookie": f"lmchat_session={cookie}"},
        )
    assert resp.status_code == 200, (
        f"First stream expected 200, got {resp.status_code}: {resp.text[:200]}"
    )

    # Verify the response contains valid SSE frames (proves stream ran).
    _frames = _parse_sse_frames(resp.content)
    assert len(_frames) >= 1, "Expected at least one SSE frame in response"
    # Verify the stream completed. ``chat.end`` signals the answer is done;
    # an optional out-of-band ``followups`` frame MAY follow it (decoupled
    # 2026-06-23 so the chips don't block the answer — emitted even when the
    # OOB call yields no questions). Any of the three is a valid terminal frame.
    last_type = _frames[-1]["type"]
    assert last_type in ("chat.end", "stats", "followups"), (
        f"Expected terminal frame type 'chat.end', 'stats', or 'followups', "
        f"got '{last_type}'"
    )

    # Step 2: DB assertion — the message should be finalized, not in draft.
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(
        f"sqlite+aiosqlite:///{live_server['db_path']}"
    )
    try:
        async with eng.begin() as conn:
            result = await conn.execute(
                text(
                    "SELECT COUNT(*) FROM messages "
                    "WHERE chat_id = :cid AND state = 'draft'"
                ),
                {"cid": chat_id},
            )
            draft_count = result.scalar()
        assert draft_count == 0, (
            f"Expected 0 draft rows (stream finalized), got {draft_count}"
        )
    finally:
        await eng.dispose()

    # Step 3: Fire a second stream to the same chat — should start fresh.
    async with httpx.AsyncClient(
        base_url=client.base_url,
        timeout=30.0,
    ) as fresh2:
        resp2 = await fresh2.post(
            "/api/chat/stream",
            json=_stream_body(chat_id),
            headers={"Cookie": f"lmchat_session={cookie}"},
        )
    assert resp2.status_code == 200, (
        f"Retry expected 200, got {resp2.status_code}: {resp2.text[:300]}"
    )

    # Final DB assertion: the retry created a fresh assistant message.
    eng2 = create_async_engine(
        f"sqlite+aiosqlite:///{live_server['db_path']}"
    )
    try:
        async with eng2.begin() as conn:
            result = await conn.execute(
                text(
                    "SELECT COUNT(*) FROM messages "
                    "WHERE chat_id = :cid AND role = 'assistant'"
                ),
                {"cid": chat_id},
            )
            msg_count = result.scalar()
        # The retry stream may have finalized before the query, so we check the
        # total assistant message count across all states.  The first stream
        # created 1 (now finalized) and the retry created 1 (possibly still
        # draft or also finalized) = 2 total assistant rows.
        assert msg_count == 2, (
            f"Expected exactly 2 assistant messages (1 per stream), got {msg_count}"
        )
    finally:
        await eng2.dispose()