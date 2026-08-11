# SPDX-License-Identifier: Apache-2.0
"""Request-logging ASGI middleware for lm-chat.

Emits exactly **one** structured log line per HTTP request at
``INFO`` level with ``event="http.request"`` and the following fields:

- ``method``     — HTTP method (GET, POST, …).
- ``path``       — Route template (e.g. ``/api/chats/{chat_id}``), not
                   the rendered URL. Populated after the inner app has
                   matched the route; unmatched requests use ``"unknown"``.
- ``status``     — Integer HTTP status code captured from
                   ``http.response.start``.
- ``latency_ms`` — Wall-clock elapsed time in milliseconds, measured
                   from scope entry to ``http.response.start`` delivery.
                   Uses :func:`time.perf_counter_ns` for sub-millisecond
                   precision.
- ``request_id`` — Taken from structlog's contextvars (set by
                   :class:`~lmchat.middleware.request_context.RequestContextMiddleware`).
                   Falls back to ``None`` if no ID has been bound.
- ``user_id``    — ``scope["state"].user.id`` when the auth middleware
                   has attached a user; ``None`` for anonymous requests.

The log line is emitted in the ``finally`` block so it fires even when
the inner application raises.  The status defaults to 500 when no
``http.response.start`` message was sent (i.e., the app panicked before
starting the response).

**Note on Prometheus duplication**
The :class:`~lmchat.middleware.metrics.PrometheusMiddleware` already
records ``lm_chat_requests_total`` and ``lm_chat_request_latency_seconds``.
This middleware does NOT duplicate those Prometheus metrics; it only
emits the structured log line.

**Pure-ASGI pattern**
This middleware does NOT extend ``BaseHTTPMiddleware``; see
``docs/IMPLEMENTATION_METHODOLOGY.md``.
"""
from __future__ import annotations

from time import perf_counter_ns
from typing import Final

from starlette.types import ASGIApp, Message, Receive, Scope, Send
from structlog.contextvars import get_contextvars

from lmchat.logging import get_logger

_log = get_logger(__name__)

_INTERNAL_ERROR_STATUS: Final[int] = 500
_UNKNOWN_ROUTE: Final[str] = "unknown"
_NS_PER_MS: Final[float] = 1_000_000.0


def _route_template(scope: Scope) -> str:
    """Resolve the FastAPI route template for *scope*.

    FastAPI/Starlette populates ``scope["route"]`` after the router has
    matched the request.  If no route matched (404 outside any mounted
    prefix), the scope lacks the key; return ``_UNKNOWN_ROUTE``.
    """
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else _UNKNOWN_ROUTE


def _user_id(scope: Scope) -> int | None:
    """Extract the user ID from scope state, or ``None`` if anonymous.

    The :class:`~lmchat.middleware.auth.AuthMiddleware` attaches
    ``scope["state"].user`` when a valid session cookie is present.
    Starlette stores scope state as either a
    :class:`~starlette.datastructures.State` object (when set by our
    middleware) or a plain ``dict`` (when set directly via
    ``request.state`` inside a route handler).  Both cases are handled.
    """
    raw_state = scope.get("state")
    if raw_state is None:
        return None
    # State object path (set by AuthMiddleware via State()).
    user = getattr(raw_state, "user", None)
    # Dict path (set by Starlette's Request.state accessor when no State
    # object was pre-initialised in scope).
    if user is None and isinstance(raw_state, dict):
        user = raw_state.get("user")
    if user is None:
        return None
    return getattr(user, "id", None)


class RequestLoggingMiddleware:
    """Pure-ASGI middleware that emits one structured log line per HTTP request.

    Args:
        app: The next ASGI application in the stack.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap *app* so every HTTP scope is logged."""
        self.app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        """Handle one ASGI scope.

        Non-HTTP scopes pass through immediately.  For HTTP scopes the
        middleware captures method, status, route template, latency,
        request_id (from contextvars), and user_id (from scope state),
        then emits a single structured log line.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method: str = scope.get("method", "")
        status_code: int = _INTERNAL_ERROR_STATUS
        start_ns: int = perf_counter_ns()

        async def capturing_send(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message.get("status", _INTERNAL_ERROR_STATUS))
            await send(message)

        try:
            await self.app(scope, receive, capturing_send)
        finally:
            elapsed_ms = (perf_counter_ns() - start_ns) / _NS_PER_MS
            # request_id is bound to structlog contextvars by
            # RequestContextMiddleware; read it without modifying binding.
            ctx = get_contextvars()
            request_id: str | None = ctx.get("request_id")
            uid = _user_id(scope)
            route = _route_template(scope)

            _log.info(
                "http.request",
                method=method,
                path=route,
                status=status_code,
                latency_ms=round(elapsed_ms, 3),
                request_id=request_id,
                user_id=uid,
            )
