# SPDX-License-Identifier: Apache-2.0
"""Tests for web_search route — per P8b.2 brief §Item 9 (Tests).

Covers:
- Form-encoded POST + 200 with result array.
- 401 unauthenticated request (via dependency injection).
- Every backend unreachable → 502.
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from lmchat.routes._dependencies import require_user
from lmchat.routes.web_search import router
from lmchat.services.auth_service import User
from lmchat.services.web_search_service import (
    SearchResult,
    WebSearchService,
    WebSearchUnavailable,
)

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


def _build_app(*, mock_svc: WebSearchService, require_auth: bool = True) -> FastAPI:
    """Build a minimal FastAPI app with the web_search router and a mock service."""
    app = FastAPI()
    app.include_router(router)
    app.state.web_search_service = mock_svc

    if require_auth:
        app.dependency_overrides[require_user] = lambda: _make_user()

    return app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_web_search_post_returns_result_array() -> None:
    """Form-encoded POST returns a plain SearchResult[] array (Invariant 3)."""
    mock_svc = MagicMock(spec=WebSearchService)
    mock_svc.search = AsyncMock(
        return_value=[
            SearchResult(title="Result A", url="https://a.example.com", snippet="Snippet A"),
            SearchResult(title="Result B", url="https://b.example.com", snippet="Snippet B"),
        ]
    )

    app = _build_app(mock_svc=mock_svc)
    client = TestClient(app, raise_server_exceptions=True)

    response = client.post(
        "/api/search/web",
        data={"q": "test query"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 200
    body = response.json()
    # Invariant 3: plain array, not a wrapper object.
    assert isinstance(body, list)
    assert len(body) == 2
    assert body[0]["title"] == "Result A"
    assert body[0]["url"] == "https://a.example.com"


def test_web_search_401_unauthenticated() -> None:
    """Unauthenticated request returns 401."""
    from fastapi import HTTPException

    mock_svc = MagicMock(spec=WebSearchService)

    app = FastAPI()
    app.include_router(router)
    app.state.web_search_service = mock_svc
    # Override require_user to raise 401 (no auth).
    app.dependency_overrides[require_user] = lambda: (_ for _ in ()).throw(
        HTTPException(status_code=401, detail="Not authenticated")
    )

    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        "/api/search/web",
        data={"q": "test query"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 401


def test_web_search_502_when_backends_unavailable() -> None:
    """svc.search raising WebSearchUnavailable maps to 502, distinct from a
    genuine empty-result 200 (mcp-tools-7: failure must not look like empty).

    FU-G4 #6: this is now the ONLY way the route surfaces a backend
    failure — the old ``if not svc.available: 503`` branch was removed as
    unreachable dead code (a failed SearXNG probe no longer flips any
    availability flag; see ``WebSearchService.probe``), so a search
    failure always maps to 502, never 503.
    """
    mock_svc = MagicMock(spec=WebSearchService)
    mock_svc.search = AsyncMock(
        side_effect=WebSearchUnavailable("both backends unreachable")
    )

    app = _build_app(mock_svc=mock_svc)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/api/search/web",
        data={"q": "test query"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 502


def test_web_search_422_empty_query() -> None:
    """Empty query returns 422 (FastAPI form validation)."""
    mock_svc = MagicMock(spec=WebSearchService)

    app = _build_app(mock_svc=mock_svc)
    client = TestClient(app, raise_server_exceptions=False)

    # q="" fails the min_length=1 constraint.
    response = client.post(
        "/api/search/web",
        data={"q": ""},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 422
