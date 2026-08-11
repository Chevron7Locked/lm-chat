# SPDX-License-Identifier: Apache-2.0
"""Regression test pinning 401-before-429 middleware behavior (P8-EOP-r1 S-006).

Due to FastAPI/Starlette's LIFO ``add_middleware`` semantics, AuthMiddleware
is OUTERMOST on the request path (runs first), and QuotaMiddleware is inner
(runs after, with ``scope["state"].user`` already populated by AuthMiddleware).
When a request arrives unauthenticated, AuthMiddleware sets ``state.user =
None``; QuotaMiddleware treats ``user is None`` as a pass-through (not 429),
leaving 401 handling to route-level ``Depends(require_user)`` guards.

This test pins that contract: an unauthenticated request to a quota-tracked
path MUST receive 401 (not 429).

See ``src/lmchat/middleware/quota.py`` docstring for the full mount-order
explanation.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from starlette.datastructures import State
from starlette.types import ASGIApp, Receive, Scope, Send

from lmchat.middleware.quota import QuotaMiddleware
from lmchat.services.quota_service import OverQuotaError


def _build_stack_unauthenticated() -> ASGIApp:
    """Build a minimal ASGI stack that simulates the real middleware order.

    Stack (outer → inner on the request path):
        _InjectNoUser      — simulates AuthMiddleware running first;
                             sets scope["state"].user = None (unauthenticated)
        QuotaMiddleware    — reads state.user (already set); passes through
                             when None
        _RouteLayer401     — returns 401 (simulates route-level auth guard)

    This mirrors the real production order where AuthMiddleware is OUTERMOST
    (runs first) and QuotaMiddleware is inner (sees the populated user state).
    """

    class _RouteLayer401:
        """Innermost ASGI app: always returns 401."""

        def __init__(self) -> None:
            pass

        async def __call__(
            self, scope: Scope, receive: Receive, send: Send
        ) -> None:
            if scope["type"] == "http":
                body = b'{"detail":"Not authenticated"}'
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [[b"content-type", b"application/json"]],
                    }
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": body,
                        "more_body": False,
                    }
                )

    # Fake Starlette app with an engine on state (QuotaMiddleware reads it).
    _fake_app = MagicMock()
    _fake_app.state = MagicMock()
    _fake_app.state.engine = MagicMock()

    route_layer = _RouteLayer401()
    quota_wrapped = QuotaMiddleware(route_layer)

    class _InjectNoUser:
        """Simulates AuthMiddleware: sets scope state with user=None (unauthenticated)."""

        def __init__(self, inner: ASGIApp) -> None:
            self._inner = inner

        async def __call__(
            self, scope: Scope, receive: Receive, send: Send
        ) -> None:
            if scope["type"] == "http":
                state = State()
                state.user = None  # type: ignore[attr-defined]
                scope["state"] = state
                scope["app"] = _fake_app
            await self._inner(scope, receive, send)

    return _InjectNoUser(quota_wrapped)


def test_unauthenticated_receives_401_not_429() -> None:
    """An unauthenticated request to a quota-tracked path returns 401, not 429.

    Regression pin for P8-EOP-r1 S-006: AuthMiddleware runs first (outermost)
    and sets state.user; QuotaMiddleware runs after and passes through when
    user is None, leaving 401 to the inner auth guard.
    """
    app = _build_stack_unauthenticated()

    # Even if OverQuotaError would be raised for a known user, unauthenticated
    # requests must never see 429.
    with patch(
        "lmchat.middleware.quota.consume_request",
        new_callable=AsyncMock,
        side_effect=OverQuotaError("requests", 999),
    ):
        client = TestClient(app, raise_server_exceptions=True)
        response = client.get("/api/chats")

    # Must be 401 from the route guard — NOT 429 from quota middleware.
    assert response.status_code == 401, (
        f"Expected 401 for unauthenticated request, got {response.status_code}. "
        "QuotaMiddleware must not return 429 for unauthenticated requests."
    )
    # Quota consume_request must NOT have been called for unauthenticated users.
    # (The patch's side_effect would have raised if it was called.)


def test_unauthenticated_quota_consume_not_called() -> None:
    """consume_request is never called for unauthenticated requests."""
    app = _build_stack_unauthenticated()

    with patch(
        "lmchat.middleware.quota.consume_request",
        new_callable=AsyncMock,
    ) as mock_consume:
        client = TestClient(app, raise_server_exceptions=True)
        client.get("/api/chats")

    mock_consume.assert_not_called()
