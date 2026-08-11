# SPDX-License-Identifier: Apache-2.0
"""CORS ASGI middleware for lm-chat.

**Implementation choice: thin wrapper over Starlette's CORSMiddleware.**

Starlette's :class:`starlette.middleware.cors.CORSMiddleware` is the
canonical FastAPI CORS solution and is battle-tested across thousands of
production deployments.  Re-implementing CORS (pre-flight handling,
``Vary`` headers, credential semantics, origin matching) would be both
error-prone and unnecessary.  The wrapper pattern used here:

1. Reads ``settings.cors_allow_origins`` — a comma-separated string
   (``CORS_ALLOW_ORIGINS="https://a.com,https://b.com"``), parsed to
   ``list[str]`` inside :meth:`_build_inner`.
   An **empty string** means no CORS exposure — the middleware is skipped
   entirely and the application behaves as same-origin only.
2. Delegates all pre-flight and simple-request CORS header logic to
   Starlette's ``CORSMiddleware`` when origins are configured.

This approach is a deliberate choice ("pick one and document").

**Allowed methods and headers**
- ``allow_methods=["*"]`` — any standard method is permitted.
  Consumers can restrict further via API gateway rules.
- ``allow_headers=["*"]`` — any request header is permitted.
  Required to support ``Authorization``, ``Content-Type``, and custom
  ``X-Request-ID`` headers from API clients.
- ``allow_credentials=True`` — required for cookie-based auth on
  cross-origin UIs (e.g. a separately-deployed SPA hitting the
  backend on a different port during development).

**Security note on ``allow_origins``**
When ``allow_origins`` is non-empty, only the listed origins are
granted cross-origin access.  Starlette's ``CORSMiddleware`` performs
exact-match comparison on the ``Origin`` request header — no glob or
regex expansion occurs unless the list contains ``"*"``.  The admin
must list each origin explicitly; ``settings.cors_allow_origins``
accepts a CSV string for env-var convenience.
"""
from __future__ import annotations

from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from lmchat.config import get_settings
from lmchat.logging import get_logger

_log = get_logger(__name__)


class CorsMiddleware:
    """Thin wrapper over Starlette's ``CORSMiddleware``.

    When ``settings.cors_allow_origins`` is empty the CORS layer is
    bypassed entirely — the request passes straight to the inner app,
    and no CORS headers are added.  This is the correct behaviour for
    single-origin production deployments.

    Args:
        app: The next ASGI application in the stack.

    The ``allow_origins`` list is resolved lazily on the first request
    so the middleware can be constructed before the lifespan hook runs.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap *app* with optional CORS header handling."""
        self.app = app
        # Lazily resolved inner ASGI app — either the raw *app* (no CORS)
        # or a CORSMiddleware wrapping *app*.
        self._inner: ASGIApp | None = None

    def _build_inner(self) -> ASGIApp:
        """Construct the inner ASGI callable from settings.

        Called once on the first request and cached on the instance.
        Parses ``settings.cors_allow_origins`` (CSV string) into a list.
        """
        raw: str = get_settings().cors_allow_origins.strip()
        origins: list[str] = (
            [o.strip() for o in raw.split(",") if o.strip()] if raw else []
        )

        if not origins:
            _log.info("cors.disabled", reason="cors_allow_origins is empty")
            return self.app

        _log.info("cors.enabled", origins=origins)
        return CORSMiddleware(
            app=self.app,
            allow_origins=origins,
            allow_methods=["*"],
            allow_headers=["*"],
            allow_credentials=True,
        )

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        """Dispatch to the inner CORS-aware (or pass-through) application.

        For non-HTTP scopes (lifespan, websocket), the middleware is bypassed
        entirely so that ``get_settings()`` is never called before the lifespan
        has had a chance to validate the environment and call ``os._exit(78)``
        on misconfiguration.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if self._inner is None:
            self._inner = self._build_inner()
        await self._inner(scope, receive, send)
