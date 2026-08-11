# SPDX-License-Identifier: Apache-2.0
"""Unit tests for QuotaMiddleware.

Tests:
- 429 response shape when OverQuotaError raised
- Retry-After header present and positive integer
- Skip paths bypass quota (/healthz, /api/auth/*)
- Unauthenticated request passes through (no user on state)
- Admin user bypasses quota check
- Non-fatal quota error does not block request
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from starlette.applications import Starlette
from starlette.datastructures import State
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from starlette.types import ASGIApp, Receive, Scope, Send

from lmchat.middleware.quota import QuotaMiddleware
from lmchat.services.quota_service import OverQuotaError

# ---------------------------------------------------------------------------
# Helper — inject user into scope["state"]
# ---------------------------------------------------------------------------


def _build_app(
    route_path: str = "/api/chats",
    user=None,
) -> ASGIApp:
    """Build a minimal ASGI stack: _InjectState → QuotaMiddleware → Starlette.

    Stack from outer to inner:
        _InjectState (sets scope["state"].user + scope["app"])
        → QuotaMiddleware (reads state.user, calls consume_request)
        → Starlette (route handler)

    _InjectState MUST be the outermost layer so scope["state"] is populated
    before QuotaMiddleware executes.

    Args:
        route_path: Path to register as a GET endpoint.
        user:       User object to inject (None = unauthenticated).

    Returns:
        Raw ASGI callable.
    """

    async def endpoint(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    starlette = Starlette(routes=[Route(route_path, endpoint)])
    starlette.state.engine = MagicMock()  # type: ignore[attr-defined]

    _user = user
    _starlette = starlette

    # QuotaMiddleware wraps Starlette (inner).
    quota_wrapped = QuotaMiddleware(starlette)

    # _InjectState wraps QuotaMiddleware (outer) — runs first.
    class _InjectState:
        def __init__(self, inner: ASGIApp) -> None:
            self._inner = inner

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] == "http":
                # Set scope["state"] to a Starlette State object with .user
                # so QuotaMiddleware can read it.
                state = State()
                state.user = _user  # type: ignore[attr-defined]
                scope["state"] = state
                scope["app"] = _starlette
            await self._inner(scope, receive, send)

    return _InjectState(quota_wrapped)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_skip_healthz():
    """GET /healthz bypasses quota entirely."""
    app = _build_app(route_path="/healthz", user=None)

    with patch(
        "lmchat.middleware.quota.consume_request",
        new_callable=AsyncMock,
    ) as mock_cr:
        client = TestClient(app, raise_server_exceptions=True)
        response = client.get("/healthz")
        assert response.status_code == 200
        mock_cr.assert_not_called()


def test_skip_auth_login():
    """POST /api/auth/login bypasses quota check."""
    from starlette.routing import Route as R

    async def login_ep(req: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    starlette = Starlette(routes=[R("/api/auth/login", login_ep, methods=["POST"])])
    starlette.state.engine = MagicMock()

    user = MagicMock()
    user.is_admin = False
    user.id = 1

    quota_mid = QuotaMiddleware(starlette)

    class _Inj:
        def __init__(self, inner):
            self._inner = inner

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http":
                state = State()
                state.user = user
                scope["state"] = state
                scope["app"] = starlette
            await self._inner(scope, receive, send)

    wrapped = _Inj(quota_mid)

    with patch(
        "lmchat.middleware.quota.consume_request",
        new_callable=AsyncMock,
    ) as mock_cr:
        client = TestClient(wrapped, raise_server_exceptions=True)
        response = client.post("/api/auth/login")
        assert response.status_code in (200, 404, 405)
        mock_cr.assert_not_called()


def test_admin_bypasses_check():
    """Admin users bypass the quota check."""
    admin = MagicMock()
    admin.is_admin = True
    admin.id = 99

    app = _build_app(user=admin)

    with patch(
        "lmchat.middleware.quota.consume_request",
        new_callable=AsyncMock,
    ) as mock_cr:
        client = TestClient(app, raise_server_exceptions=True)
        client.get("/api/chats")
        mock_cr.assert_not_called()


def test_unauthenticated_passes_through():
    """Requests with no user bypass quota (route-level 401 handles auth)."""
    app = _build_app(user=None)

    with patch(
        "lmchat.middleware.quota.consume_request",
        new_callable=AsyncMock,
    ) as mock_cr:
        client = TestClient(app, raise_server_exceptions=True)
        client.get("/api/chats")
        mock_cr.assert_not_called()


def test_429_response_body():
    """When consume_request raises OverQuotaError, returns 429 with correct body."""
    user = MagicMock()
    user.is_admin = False
    user.id = 7

    app = _build_app(user=user)

    with patch(
        "lmchat.middleware.quota.consume_request",
        new_callable=AsyncMock,
        side_effect=OverQuotaError("requests", 7),
    ):
        client = TestClient(app, raise_server_exceptions=True)
        response = client.get("/api/chats")
        assert response.status_code == 429
        body = response.json()
        assert body["detail"] == "request quota exceeded for the day"


def test_429_retry_after_header():
    """The 429 response includes a positive-integer Retry-After header."""
    user = MagicMock()
    user.is_admin = False
    user.id = 7

    app = _build_app(user=user)

    with patch(
        "lmchat.middleware.quota.consume_request",
        new_callable=AsyncMock,
        side_effect=OverQuotaError("requests", 7),
    ):
        client = TestClient(app, raise_server_exceptions=True)
        response = client.get("/api/chats")
        assert response.status_code == 429
        retry_after = response.headers.get("retry-after")
        assert retry_after is not None
        assert int(retry_after) > 0


def test_non_fatal_error_passes_through():
    """A generic quota error logs and lets the request pass through."""
    user = MagicMock()
    user.is_admin = False
    user.id = 7

    app = _build_app(user=user)

    with patch(
        "lmchat.middleware.quota.consume_request",
        new_callable=AsyncMock,
        side_effect=RuntimeError("db unavailable"),
    ):
        client = TestClient(app, raise_server_exceptions=True)
        response = client.get("/api/chats")

    assert response.status_code == 200
