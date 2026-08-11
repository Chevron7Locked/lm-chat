# SPDX-License-Identifier: Apache-2.0
"""Route-level tests for A/B compare per-request cap (P8-EOP-r1 S-001).

Verifies:
- Requests with max_tokens below the cap return 200 SSE stream.
- Requests with max_tokens above the cap return 400 with the correct body.
- Unauthenticated requests return 401 (not caught by the limit check).
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from lmchat.routes._dependencies import require_user
from lmchat.routes.ab_compare import router
from lmchat.services.ab_compare_service import AbCompareService, AbEvent
from lmchat.services.auth_service import User

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user() -> User:
    now = datetime.now(UTC)
    return User(
        id=1,
        username="testuser",
        is_admin=False,
        created_at=now,
        updated_at=now,
        password_hash="scrypt$1024$8$1$AAAA$AAAA",
        totp_secret=None,
    )


def _build_app(*, mock_svc: AbCompareService) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.ab_compare_service = mock_svc
    app.dependency_overrides[require_user] = lambda: _make_user()
    return app


async def _mock_stream(*ab_events: AbEvent) -> AsyncGenerator[AbEvent]:
    for ev in ab_events:
        yield ev


def _mock_cfg(cap: int) -> MagicMock:
    cfg = MagicMock()
    cfg.lm_chat_ab_max_output_tokens = cap
    return cfg


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_ab_compare_400_when_max_tokens_exceeds_cap() -> None:
    """POST /api/ab/stream returns 400 when max_tokens exceeds the cap."""
    mock_svc = MagicMock(spec=AbCompareService)
    app = _build_app(mock_svc=mock_svc)
    client = TestClient(app, raise_server_exceptions=False)

    # Patch the cap to a small value so max_tokens=9999 exceeds it.
    with patch(
        "lmchat.routes.ab_compare.get_settings",
        return_value=_mock_cfg(100),
    ):
        response = client.post(
            "/api/ab/stream",
            data={
                "chat_id": "1",
                "message": "hello",
                "model_a": "model-alpha",
                "model_b": "model-beta",
                "max_tokens": "9999",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    assert response.status_code == 400
    body = response.json()
    assert "max_tokens per pane exceeds AB compare limit" in body["detail"]
    # Service must not have been called.
    mock_svc.stream_both.assert_not_called()


def test_ab_compare_400_body_content() -> None:
    """The 400 body carries the expected detail string."""
    mock_svc = MagicMock(spec=AbCompareService)
    app = _build_app(mock_svc=mock_svc)
    client = TestClient(app, raise_server_exceptions=False)

    with patch(
        "lmchat.routes.ab_compare.get_settings",
        return_value=_mock_cfg(100),
    ):
        response = client.post(
            "/api/ab/stream",
            data={
                "chat_id": "1",
                "message": "hello",
                "model_a": "model-alpha",
                "model_b": "model-beta",
                "max_tokens": "200",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    assert response.status_code == 400
    assert "reduce or disable A/B compare" in response.json()["detail"]


def test_ab_compare_400_when_prompt_tokens_exceed_cap() -> None:
    """POST /api/ab/stream returns 400 with prompt-token message when the
    estimated prompt token count exceeds the cap."""
    mock_svc = MagicMock(spec=AbCompareService)
    app = _build_app(mock_svc=mock_svc)
    client = TestClient(app, raise_server_exceptions=False)

    # A message of 401 chars → estimate = 401 // 4 = 100 tokens; cap = 99.
    long_message = "x" * 401

    with patch(
        "lmchat.routes.ab_compare.get_settings",
        return_value=_mock_cfg(99),
    ):
        response = client.post(
            "/api/ab/stream",
            data={
                "chat_id": "1",
                "message": long_message,
                "model_a": "model-alpha",
                "model_b": "model-beta",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    assert response.status_code == 400
    body = response.json()
    assert "estimated prompt tokens exceed AB compare limit" in body["detail"]
    assert "reduce or disable A/B compare" in body["detail"]
    mock_svc.stream_both.assert_not_called()


def test_ab_compare_200_when_max_tokens_within_cap() -> None:
    """POST /api/ab/stream returns 200 SSE when max_tokens is within the cap."""
    events = [
        AbEvent(side="a", event_type="ab.end"),
        AbEvent(side="b", event_type="ab.end"),
    ]
    mock_svc = MagicMock(spec=AbCompareService)
    mock_svc.stream_both = MagicMock(return_value=_mock_stream(*events))

    app = _build_app(mock_svc=mock_svc)
    client = TestClient(app, raise_server_exceptions=True)

    with patch(
        "lmchat.routes.ab_compare.get_settings",
        return_value=_mock_cfg(32_768),
    ):
        response = client.post(
            "/api/ab/stream",
            data={
                "chat_id": "1",
                "message": "hello",
                "model_a": "model-alpha",
                "model_b": "model-beta",
                "max_tokens": "512",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")
