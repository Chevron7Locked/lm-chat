# SPDX-License-Identifier: Apache-2.0
"""Integration tests for the streaming route (POST /api/chat/stream).

Tests for the streaming route.

Tests:
- test_post_chat_stream_requires_auth_401
- test_post_chat_stream_rate_limit_429_after_threshold
- test_post_chat_stream_returns_sse_content_type
- test_post_chat_stream_409_when_stream_in_progress
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from lmchat.services.streaming_errors import StreamInProgressError


def _make_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: ANN401
    """Build the app with isolated tmp DB.

    Args:
        tmp_path:   pytest tmp_path fixture for DB isolation.
        monkeypatch: pytest monkeypatch fixture.

    Returns:
        The FastAPI application instance.
    """
    from lmchat.app import create_app
    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod

    db_url = f"sqlite+aiosqlite:///{tmp_path}/streaming_route_test.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")

    get_settings.cache_clear()
    engine_mod.dispose_engine()

    return create_app()


def _make_stream_payload(chat_id: int = 1) -> dict:  # type: ignore[type-arg]
    """Build a valid ChatStreamRequest JSON body for POST /api/chat/stream.

    Args:
        chat_id: The chat ID to use.

    Returns:
        A dict suitable for ``json=`` in a TestClient call.
    """
    return {
        "chat_id": chat_id,
        "payload": {
            "model": "test-model",
            "input": [{"type": "text", "content": "hello"}],
        },
    }


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------


def test_post_chat_stream_requires_auth_401(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unauthenticated POST /api/chat/stream returns 401.

    ``Depends(require_user)`` must fire before any draft row
    is created.
    """
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        resp = client.post("/api/chat/stream", json=_make_stream_payload())
    assert resp.status_code == 401

    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod
    get_settings.cache_clear()
    engine_mod.dispose_engine()


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------


def test_post_chat_stream_rate_limit_429_after_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-user stream rate limit returns 429 after bucket is exhausted.

    We override LM_CHAT_STREAM_RATE_LIMIT_PER_MIN to 2 so the bucket
    exhausts quickly.  The test registers a user, logs in, then fires
    requests until 429 appears.
    """
    monkeypatch.setenv("LM_CHAT_STREAM_RATE_LIMIT_PER_MIN", "2")

    app = _make_app(tmp_path, monkeypatch)

    with TestClient(app, raise_server_exceptions=False) as client:
        # Register + login.
        client.post(
            "/api/auth/register",
            data={"username": "ratelimit_user", "password": "Test1234!"},
        )
        login = client.post(
            "/api/auth/login",
            data={"username": "ratelimit_user", "password": "Test1234!"},
        )
        assert login.status_code == 200

        # Override the streaming service to avoid actual streaming.
        from lmchat.routes._dependencies import get_streaming_service_dep

        async def _empty_stream(*args: object, **kwargs: object) -> AsyncIterator[bytes]:
            return
            yield  # make it an async generator

        mock_svc = MagicMock()
        mock_svc.stream_chat = _empty_stream
        app.dependency_overrides[get_streaming_service_dep] = lambda: mock_svc  # type: ignore[assignment]

        got_429 = False
        for _ in range(5):
            resp = client.post("/api/chat/stream", json=_make_stream_payload())
            if resp.status_code == 429:
                got_429 = True
                assert "retry-after" in {k.lower() for k in resp.headers}
                break

        app.dependency_overrides.clear()

    assert got_429, "Expected 429 after rate-limit threshold was exceeded"

    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod
    get_settings.cache_clear()
    engine_mod.dispose_engine()


# ---------------------------------------------------------------------------
# Content-Type
# ---------------------------------------------------------------------------


def test_post_chat_stream_returns_sse_content_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /api/chat/stream with valid auth returns text/event-stream.

    Verifies the StreamingResponse is wired with media_type="text/event-stream".
    """
    app = _make_app(tmp_path, monkeypatch)

    with TestClient(app, raise_server_exceptions=False) as client:
        # Register + login.
        client.post(
            "/api/auth/register",
            data={"username": "sse_user", "password": "Test1234!"},
        )
        login = client.post(
            "/api/auth/login",
            data={"username": "sse_user", "password": "Test1234!"},
        )
        assert login.status_code == 200

        # Override streaming service to yield a minimal SSE frame.
        from lmchat.routes._dependencies import get_streaming_service_dep

        async def _minimal_stream(
            *args: object, **kwargs: object
        ) -> AsyncIterator[bytes]:
            frame = b"event: chat.start\ndata: {\"type\": \"chat.start\", \"msg_id\": 1}\n\n"
            yield frame

        mock_svc = MagicMock()
        mock_svc.stream_chat = _minimal_stream
        app.dependency_overrides[get_streaming_service_dep] = lambda: mock_svc  # type: ignore[assignment]

        resp = client.post(
            "/api/chat/stream",
            json=_make_stream_payload(),
            # Don't follow SSE — just check headers.
        )

        app.dependency_overrides.clear()

    # Content-Type must be text/event-stream.
    content_type = resp.headers.get("content-type", "")
    assert "text/event-stream" in content_type, (
        f"Expected text/event-stream, got: {content_type}"
    )

    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod
    get_settings.cache_clear()
    engine_mod.dispose_engine()


# ---------------------------------------------------------------------------
# 409 when stream in progress
# ---------------------------------------------------------------------------


def test_post_chat_stream_409_when_stream_in_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /api/chat/stream returns 409 when stream is already in progress.

    Single-stream-per-chat enforcement.
    """
    app = _make_app(tmp_path, monkeypatch)

    with TestClient(app, raise_server_exceptions=False) as client:
        # Register + login.
        client.post(
            "/api/auth/register",
            data={"username": "conflict_user", "password": "Test1234!"},
        )
        login = client.post(
            "/api/auth/login",
            data={"username": "conflict_user", "password": "Test1234!"},
        )
        assert login.status_code == 200

        # Override streaming service to raise StreamInProgressError.
        from lmchat.routes._dependencies import get_streaming_service_dep

        async def _conflict_stream(
            *, chat_id: int, **kwargs: object
        ) -> AsyncIterator[bytes]:
            raise StreamInProgressError(chat_id)
            yield  # make it an async generator

        mock_svc = MagicMock()
        mock_svc.stream_chat = _conflict_stream
        app.dependency_overrides[get_streaming_service_dep] = lambda: mock_svc  # type: ignore[assignment]

        resp = client.post("/api/chat/stream", json=_make_stream_payload(chat_id=5))

        app.dependency_overrides.clear()

    assert resp.status_code == 409
    body = resp.json()
    assert body["detail"]["code"] == "stream_in_progress"
    assert body["detail"]["chat_id"] == 5

    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod
    get_settings.cache_clear()
    engine_mod.dispose_engine()
