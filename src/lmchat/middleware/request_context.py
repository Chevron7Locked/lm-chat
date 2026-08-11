# SPDX-License-Identifier: Apache-2.0
"""Request-context ASGI middleware.

This middleware does three things, in order, for every HTTP request:

1. Resolve the request ID. If the inbound request carries an
   ``X-Request-ID`` header that decodes cleanly as ASCII, use
   it. Otherwise generate one with
   :func:`secrets.token_urlsafe`.
2. Bind ``request_id`` to structlog's contextvars storage so
   every log line emitted during request handling carries it
   automatically (no manual passing of the ID through call
   stacks).
3. On the outbound response, set the ``Request-ID`` header so
   downstream admins (reverse proxies, log aggregators) can
   correlate client and server traces.

The middleware is **pure ASGI**, not Starlette's
``BaseHTTPMiddleware``: the latter has known interaction issues
with streaming responses, and lm-chat's streaming service
must operate cleanly through this middleware. Pure ASGI keeps
the request/response lifecycle transparent.

Non-HTTP scopes (``lifespan``, ``websocket``) pass through
untouched.
"""
from __future__ import annotations

import secrets
from typing import Final

from starlette.types import ASGIApp, Message, Receive, Scope, Send
from structlog.contextvars import bind_contextvars, clear_contextvars, reset_contextvars

# Inbound header (case-insensitive lookup in the ASGI scope).
_INBOUND_HEADER: Final[bytes] = b"x-request-id"
# Outbound response header.
_OUTBOUND_HEADER: Final[bytes] = b"request-id"
# secrets.token_urlsafe(N) returns ceil(N * 4 / 3) characters;
# 12 bytes of entropy → 16 URL-safe characters — plenty for
# uniqueness and short enough for terminal logs.
_GENERATED_ID_NBYTES: Final[int] = 12


class RequestContextMiddleware:
    """ASGI middleware that propagates a per-request ID.

    Read ``X-Request-ID`` if present; otherwise generate one.
    Bind it to structlog's contextvars so the request_id surfaces
    on every log line emitted in this request's scope. Set the
    outbound ``Request-ID`` header on the response.

    The contextvars binding is unwound in a ``finally`` block so
    no per-request state leaks between requests sharing an
    asyncio task.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app`` and forward ASGI scopes through this middleware."""
        self.app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        """Handle a single ASGI scope.

        For HTTP scopes, do the request-id dance. For lifespan
        and websocket scopes, forward unmodified.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = self._resolve_request_id(scope)

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((_OUTBOUND_HEADER, request_id.encode("ascii")))
                message["headers"] = headers
            await send(message)

        # Defense-in-depth: clear any residual contextvars before
        # binding, so request_id never inherits state from a prior
        # request that crashed before its finally block ran.
        clear_contextvars()
        tokens = bind_contextvars(request_id=request_id)
        try:
            await self.app(scope, receive, send_with_id)
        finally:
            reset_contextvars(**tokens)

    @staticmethod
    def _resolve_request_id(scope: Scope) -> str:
        """Read X-Request-ID from the scope, or generate via secrets.

        The ASGI ``scope["headers"]`` is a list of ``(bytes, bytes)``
        tuples. Names are already lowercased by the ASGI server.
        Malformed (non-ASCII) values fall back to a generated ID.
        """
        for name, value in scope.get("headers", []):
            if name == _INBOUND_HEADER:
                try:
                    return value.decode("ascii")
                except UnicodeDecodeError:
                    break
        return secrets.token_urlsafe(_GENERATED_ID_NBYTES)
