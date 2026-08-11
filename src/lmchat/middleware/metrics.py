# SPDX-License-Identifier: Apache-2.0
"""Prometheus metrics ASGI middleware.

Records HTTP request count and latency, labeled by HTTP method,
the FastAPI **route template** (e.g. ``/api/chats/{chat_id}``),
and response status. The ``/api/metrics`` endpoint is excluded
from the request counter to prevent self-amplification on every
Prometheus scrape.

- **Cardinality discipline.** The ``path`` label is the route
  template, not the rendered URL. Path-parameter explosion is
  prevented by reading ``scope["route"].path`` after the inner
  app has matched the route. Unmatched routes (404 with no
  route mount) fall back to the literal string ``"unknown"``.
- **Self-amplification.** Requests whose path equals
  ``/api/metrics`` bypass the middleware bookkeeping
  altogether.
"""
from __future__ import annotations

from time import perf_counter
from typing import Final

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from lmchat.metrics import REQUEST_COUNT, REQUEST_LATENCY

_METRICS_PATH: Final[str] = "/api/metrics"
_UNKNOWN_ROUTE: Final[str] = "unknown"
_INTERNAL_ERROR_STATUS: Final[int] = 500


class PrometheusMiddleware:
    """ASGI middleware that records request count + latency.

    For every HTTP request (excluding ``/api/metrics``) the
    middleware captures:

    - the HTTP method,
    - the matched route template (or ``"unknown"`` if the
      request never matched a route, e.g., a 404),
    - the response status code as a string,
    - the wall-clock elapsed time between request entry and
      response completion (or exception).

    If the inner application raises before sending
    ``http.response.start``, the status code defaults to ``500``
    so failure modes are still recorded.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app`` so every HTTP scope is observed."""
        self.app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        """Observe one ASGI scope."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Self-amplification guard: scraping /api/metrics would
        # inflate the counter on every scrape interval.
        if scope.get("path") == _METRICS_PATH:
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        status_code = _INTERNAL_ERROR_STATUS

        async def capturing_send(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        start = perf_counter()
        try:
            await self.app(scope, receive, capturing_send)
        finally:
            elapsed = perf_counter() - start
            route_path = self._route_template(scope)
            REQUEST_COUNT.labels(
                method=method,
                path=route_path,
                status=str(status_code),
            ).inc()
            REQUEST_LATENCY.labels(
                method=method,
                path=route_path,
            ).observe(elapsed)

    @staticmethod
    def _route_template(scope: Scope) -> str:
        """Resolve the FastAPI route template post-match.

        Starlette/FastAPI populates ``scope["route"]`` after the
        router matches the request. If the request hit no route
        (404 outside any mounted prefix) the scope lacks the
        attribute; fall back to ``_UNKNOWN_ROUTE``.
        """
        route = scope.get("route")
        path = getattr(route, "path", None)
        if isinstance(path, str):
            return path
        return _UNKNOWN_ROUTE
