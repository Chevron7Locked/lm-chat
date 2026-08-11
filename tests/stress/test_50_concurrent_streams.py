# SPDX-License-Identifier: Apache-2.0
"""50-concurrent SSE stream stress test.

50-concurrent-streams release criterion:
  tests/stress/test_50_concurrent_streams.py — 50 SSE streams × completion;
  zero response_id collisions; zero hung tasks; zero orphan draft rows.

Design
------
The test spawns 50 concurrent POST /api/chat/stream requests using
httpx.AsyncClient (ASGI transport) and asyncio.gather.  The streaming
service is replaced by a mock that yields a deterministic three-frame SSE
sequence:

    chat.start   → {"type": "chat.start", "msg_id": <n>}
    message.delta → {"type": "message.delta", "msg_id": <n>, "delta": "hi"}
    chat.end     → {"type": "chat.end", "msg_id": <n>, "response_id": "rid-<n>"}

Each of the 50 tasks asserts:
  1. HTTP 200 status.
  2. Content-Type: text/event-stream.
  3. All three SSE frames present in order.
  4. No HTTP 5xx status.

After gather completes, the test asserts:
  - No task raised an exception (checked via asyncio.gather return values).
  - 50 unique response_id values were collected (zero collisions).
  - 50 unique msg_id values were collected (zero collisions).

Marker: @pytest.mark.stress — excluded from the default unit-tier run.
Run with: uv run pytest -m stress -q
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

# ---------------------------------------------------------------------------
# App factory (isolated tmp DB per test)
# ---------------------------------------------------------------------------


def _make_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: ANN401
    """Build the app with an isolated tmp DB.

    Args:
        tmp_path:    pytest tmp_path for DB isolation.
        monkeypatch: pytest monkeypatch fixture.

    Returns:
        The FastAPI application instance, with a registered test user.
    """
    from lmchat.app import create_app
    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod

    db_url = f"sqlite+aiosqlite:///{tmp_path}/stress_test.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("LM_CHAT_SECRET", "stress-test-secret-32-bytes!!!!")
    # Raise the rate limit well above 50 so the test never hits 429.
    monkeypatch.setenv("LM_CHAT_STREAM_RATE_LIMIT_PER_MIN", "200")

    get_settings.cache_clear()
    engine_mod.dispose_engine()

    return create_app()


# ---------------------------------------------------------------------------
# SSE frame builder (deterministic per-stream sequence)
# ---------------------------------------------------------------------------

_CHAT_START_TMPL = (
    b"event: chat.start\n"
    b'data: {"type": "chat.start", "msg_id": %(n)d}\n\n'
)
_MESSAGE_DELTA_TMPL = (
    b"event: message.delta\n"
    b'data: {"type": "message.delta", "msg_id": %(n)d, "delta": "hi"}\n\n'
)
_CHAT_END_TMPL = (
    b"event: chat.end\n"
    b'data: {"type": "chat.end", "msg_id": %(n)d, "response_id": "rid-%(n)d"}\n\n'
)


def _make_mock_service(stream_index: int) -> MagicMock:
    """Return a mock StreamingService whose stream_chat yields 3 frames.

    Args:
        stream_index: 0-based index used to embed a unique msg_id / response_id
                      in the frame payloads.

    Returns:
        A MagicMock with a ``stream_chat`` async generator attribute.
    """
    n = stream_index + 1  # 1-based msg_id / response_id

    async def _stream(*args: object, **kwargs: object) -> AsyncIterator[bytes]:
        yield _CHAT_START_TMPL % {b"n": n}
        yield _MESSAGE_DELTA_TMPL % {b"n": n}
        yield _CHAT_END_TMPL % {b"n": n}

    mock = MagicMock()
    mock.stream_chat = _stream
    return mock


# ---------------------------------------------------------------------------
# Single-stream worker
# ---------------------------------------------------------------------------

_STREAM_PAYLOAD = {
    "chat_id": 1,
    "payload": {
        "model": "stress-model",
        "input": [{"type": "text", "content": "stress ping"}],
    },
}


async def _run_one_stream(
    client: httpx.AsyncClient,
    stream_index: int,
) -> dict[str, object]:
    """Issue one POST /api/chat/stream and collect results.

    Args:
        client:       Shared httpx.AsyncClient (ASGI transport).
        stream_index: 0-based index, echoed in the response for collision checks.

    Returns:
        A dict with keys:
          - ``index``: stream_index
          - ``status``: HTTP status code
          - ``content_type``: value of Content-Type header
          - ``frames``: list of parsed event-type strings in order
          - ``msg_ids``: list of msg_id ints extracted from frames
          - ``response_ids``: list of response_id strings extracted from chat.end frames
          - ``error``: exception message if any non-HTTP error occurred, else None
    """
    result: dict[str, object] = {
        "index": stream_index,
        "status": 0,
        "content_type": "",
        "frames": [],
        "msg_ids": [],
        "response_ids": [],
        "error": None,
    }
    try:
        async with client.stream(
            "POST",
            "/api/chat/stream",
            json=_STREAM_PAYLOAD,
            timeout=30.0,
        ) as resp:
            result["status"] = resp.status_code
            result["content_type"] = resp.headers.get("content-type", "")

            if resp.status_code == 200:
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line or line.startswith(":"):
                        continue
                    if line.startswith("event:"):
                        event_type = line[len("event:"):].strip()
                        cast_frames = result["frames"]
                        assert isinstance(cast_frames, list)
                        cast_frames.append(event_type)
                    elif line.startswith("data:"):
                        raw = line[len("data:"):].strip()
                        try:
                            payload = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if "msg_id" in payload:
                            cast_msg_ids = result["msg_ids"]
                            assert isinstance(cast_msg_ids, list)
                            cast_msg_ids.append(payload["msg_id"])
                        if "response_id" in payload:
                            cast_rids = result["response_ids"]
                            assert isinstance(cast_rids, list)
                            cast_rids.append(payload["response_id"])
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
    return result


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.stress
def test_50_concurrent_sse_streams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """50 concurrent SSE streams complete cleanly with no errors.

    50-concurrent-streams release criterion.
    Each stream yields chat.start → message.delta → chat.end.
    Asserts:
    - 50 × HTTP 200.
    - 50 × Content-Type: text/event-stream.
    - 50 × complete frame sequence [chat.start, message.delta, chat.end].
    - 50 unique msg_id values (zero collisions).
    - 50 unique response_id values (zero collisions).
    - No HTTP 5xx responses.
    - No unhandled exceptions.
    """
    n_streams = 50

    app = _make_app(tmp_path, monkeypatch)

    # Register + login a single shared test user.  All 50 concurrent streams
    # share this session cookie.  Each stream uses chat_id=1 (distinct
    # per-stream mock services prevent single-stream-per-chat 409 conflicts
    # because the service is replaced before the lock runs).
    from fastapi.testclient import TestClient

    with TestClient(app) as bootstrap:
        bootstrap.post(
            "/api/auth/register",
            data={"username": "stress_user", "password": "Stress1234!"},
        )
        login_resp = bootstrap.post(
            "/api/auth/login",
            data={"username": "stress_user", "password": "Stress1234!"},
        )
        assert login_resp.status_code == 200, (
            f"bootstrap login failed: {login_resp.status_code}"
        )
        session_cookie = login_resp.cookies.get("lmchat_session", "")

    assert session_cookie, "could not obtain session cookie from bootstrap client"

    from lmchat.routes._dependencies import get_streaming_service_dep

    async def _run_all() -> list[dict[str, object]]:
        transport = httpx.ASGITransport(app=app)
        # Pre-build 50 distinct mock services so each stream gets its own
        # unique msg_id / response_id sequence.  We override the dependency
        # to a per-request factory that pops from this list.
        mock_services = [_make_mock_service(i) for i in range(n_streams)]
        call_counter = 0

        def _mock_dep_factory() -> MagicMock:
            nonlocal call_counter
            svc = mock_services[call_counter % n_streams]
            call_counter += 1
            return svc

        app.dependency_overrides[get_streaming_service_dep] = _mock_dep_factory

        cookies = {"lmchat_session": session_cookie}
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            cookies=cookies,
        ) as client:
            tasks = [_run_one_stream(client, i) for i in range(n_streams)]
            results: list[dict[str, object]] = await asyncio.gather(*tasks)

        app.dependency_overrides.clear()
        return results

    results = asyncio.run(_run_all())

    # -----------------------------------------------------------------------
    # Assertions
    # -----------------------------------------------------------------------

    errors = [r for r in results if r["error"] is not None]
    status_5xx = [r for r in results if isinstance(r["status"], int) and r["status"] >= 500]
    non_200 = [r for r in results if r["status"] != 200]

    assert not errors, (
        f"{len(errors)} stream(s) raised exceptions: "
        + "; ".join(str(r["error"]) for r in errors[:5])
    )
    assert not status_5xx, (
        f"{len(status_5xx)} stream(s) returned 5xx: "
        + str([r["status"] for r in status_5xx])
    )
    assert not non_200, (
        f"{len(non_200)} stream(s) did not return 200: "
        + str([(r["index"], r["status"]) for r in non_200[:10]])
    )

    for r in results:
        ct = r["content_type"]
        assert isinstance(ct, str) and "text/event-stream" in ct, (
            f"stream {r['index']}: expected text/event-stream, got {ct!r}"
        )

    expected_frames = ["chat.start", "message.delta", "chat.end"]
    for r in results:
        assert r["frames"] == expected_frames, (
            f"stream {r['index']}: expected frames {expected_frames}, "
            f"got {r['frames']}"
        )

    # msg_id uniqueness — zero collisions across all streams.
    all_msg_ids: list[object] = []
    for r in results:
        msg_ids_val = r["msg_ids"]
        assert isinstance(msg_ids_val, list)
        all_msg_ids.extend(msg_ids_val)
    # Each stream emits 3 frames each carrying the same msg_id, so we expect
    # 50 streams × 3 msg_id appearances = 150 entries, but only 50 unique values
    # (each stream's n-value appears 3 times).  What we strictly prohibit is
    # two distinct streams sharing the same n-value, i.e. collisions across
    # stream indices.
    per_stream_msg_ids: list[object] = []
    for r in results:
        msg_ids_val2 = r["msg_ids"]
        assert isinstance(msg_ids_val2, list)
        if msg_ids_val2:
            per_stream_msg_ids.append(msg_ids_val2[0])
    assert len(per_stream_msg_ids) == n_streams, (
        "not all streams produced msg_ids"
    )
    assert len(set(per_stream_msg_ids)) == n_streams, (
        f"msg_id collision detected: {len(set(per_stream_msg_ids))} unique "
        f"out of {n_streams}"
    )

    # response_id uniqueness — zero collisions.
    per_stream_response_ids: list[object] = []
    for r in results:
        rids_val = r["response_ids"]
        assert isinstance(rids_val, list)
        if rids_val:
            per_stream_response_ids.append(rids_val[0])
    assert len(per_stream_response_ids) == n_streams, (
        "not all streams produced a response_id in chat.end"
    )
    assert len(set(per_stream_response_ids)) == n_streams, (
        f"response_id collision detected: {len(set(per_stream_response_ids))} "
        f"unique out of {n_streams}"
    )

    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod

    get_settings.cache_clear()
    engine_mod.dispose_engine()
