# SPDX-License-Identifier: Apache-2.0
"""Tests for ab_compare route — per P8b.2 brief §Item 10 (Tests).

Covers:
- Two streams interleaved correctly (pane discriminator present).
- Auth-gated: 401 for unauthenticated request.
- 400 / 422 on missing required fields.
"""
from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from lmchat.routes._dependencies import require_user
from lmchat.routes.ab_compare import router
from lmchat.services.ab_compare_service import AbCompareService, AbEvent
from lmchat.services.auth_service import User
from lmchat.services.models_service import ResolvedModel

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
    """Build a minimal FastAPI app with the ab_compare router and a mock service."""
    app = FastAPI()
    app.include_router(router)
    app.state.ab_compare_service = mock_svc
    app.dependency_overrides[require_user] = lambda: _make_user()
    return app


async def _mock_stream(*ab_events: AbEvent) -> AsyncGenerator[AbEvent]:
    """Async generator that yields the provided AbEvent objects."""
    for ev in ab_events:
        yield ev


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_ab_compare_stream_interleaved_panes() -> None:
    """SSE stream carries events from both panes with correct 'pane' field."""
    events = [
        AbEvent(side="a", event_type="chat.start", response_id="r-a"),
        AbEvent(side="b", event_type="chat.start", response_id="r-b"),
        AbEvent(side="a", event_type="message.delta", delta="Hello from A"),
        AbEvent(side="b", event_type="message.delta", delta="Hello from B"),
        AbEvent(side="a", event_type="ab.end"),
        AbEvent(side="b", event_type="ab.end"),
    ]

    mock_svc = MagicMock(spec=AbCompareService)
    mock_svc.stream_both = MagicMock(return_value=_mock_stream(*events))

    app = _build_app(mock_svc=mock_svc)
    client = TestClient(app, raise_server_exceptions=True)

    response = client.post(
        "/api/ab/stream",
        data={
            "chat_id": "1",
            "message": "test message",
            "model_a": "model-alpha",
            "model_b": "model-beta",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")

    # Collect SSE frames.
    raw = response.text
    parsed = []
    for block in raw.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        data_line = next(
            (line[6:] for line in block.split("\n") if line.startswith("data: ")),
            None,
        )
        if data_line:
            parsed.append(json.loads(data_line))

    # Every event should carry a 'pane' field.
    assert all("pane" in ev for ev in parsed)
    # Both panes should be represented.
    panes = {ev["pane"] for ev in parsed}
    assert panes == {"a", "b"}


def test_ab_compare_stream_401_unauthenticated() -> None:
    """Unauthenticated request returns 401."""
    from fastapi import HTTPException

    mock_svc = MagicMock(spec=AbCompareService)

    app = FastAPI()
    app.include_router(router)
    app.state.ab_compare_service = mock_svc
    app.dependency_overrides[require_user] = lambda: (_ for _ in ()).throw(
        HTTPException(status_code=401, detail="Not authenticated")
    )

    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        "/api/ab/stream",
        data={
            "chat_id": "1",
            "message": "test",
            "model_a": "model-a",
            "model_b": "model-b",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 401


def test_ab_compare_stream_400_when_pane_model_unloaded() -> None:
    """A pane whose model is not loaded must 400 with a clear message —
    never silently substitute a fallback that can collapse the compare
    into one model streamed against itself.
    """
    mock_svc = MagicMock(spec=AbCompareService)
    mock_svc.stream_both = MagicMock(return_value=_mock_stream())

    async def _resolve(model_key_or_id: str, **_: object) -> ResolvedModel:
        if model_key_or_id == "model-alpha":
            # model-alpha is not loaded; the resolver substitutes the only
            # other loaded LLM — which happens to be model-beta's own wire
            # id, exactly the silent self-compare scenario.
            return ResolvedModel(
                wire_id="model-beta@q4",
                requested=model_key_or_id,
                substituted=True,
                fallback_key="model-beta",
                reason="requested_not_loaded",
            )
        return ResolvedModel(wire_id="model-beta@q4", requested=model_key_or_id)

    mock_models_svc = MagicMock()
    mock_models_svc.resolve_to_loaded_or_fallback = AsyncMock(side_effect=_resolve)

    app = _build_app(mock_svc=mock_svc)
    app.state.models_service = mock_models_svc
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/api/ab/stream",
        data={
            "chat_id": "1",
            "message": "test message",
            "model_a": "model-alpha",
            "model_b": "model-beta",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 400
    assert "not currently loaded" in response.json()["detail"].lower()
    mock_svc.stream_both.assert_not_called()


def test_ab_compare_stream_422_missing_fields() -> None:
    """Missing required form fields return 422."""
    mock_svc = MagicMock(spec=AbCompareService)
    app = _build_app(mock_svc=mock_svc)
    client = TestClient(app, raise_server_exceptions=False)

    # Missing model_b.
    response = client.post(
        "/api/ab/stream",
        data={
            "chat_id": "1",
            "message": "test",
            "model_a": "model-a",
            # model_b absent
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 422
