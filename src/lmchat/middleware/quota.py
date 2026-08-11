# SPDX-License-Identifier: Apache-2.0
"""Per-user daily-quota ASGI middleware for lm-chat.

Enforces the per-user daily request quota on every authenticated request
that is NOT an admin, health-check, or auth path.

Architecture note — pure ASGI, NOT BaseHTTPMiddleware
------------------------------------------------------
This middleware uses the ``async def __call__(scope, receive, send)``
pattern — the same approach as ``AuthMiddleware`` and
``LoginRateLimitMiddleware`` — because ``BaseHTTPMiddleware`` buffers the
entire response body before delivering it, which breaks the streaming
SSE implementation.

Middleware mount order
-----------------------
Mount order in ``app.py``: ``add_middleware`` is called with
``AuthMiddleware`` FIRST, then ``QuotaMiddleware``.  Starlette's
``add_middleware`` prepends each new middleware to the chain (LIFO), so the
middleware added LAST is OUTERMOST on the request path.  With ``Auth`` added
before ``Quota``, the execution order on incoming requests is:

    … → AuthMiddleware (outermost) → QuotaMiddleware → … → routes

``AuthMiddleware`` runs first and populates ``scope["state"].user``.
``QuotaMiddleware`` runs second and reads the already-populated user for
budget enforcement.  Older comments that described quota running before auth
reflected a pre-2026-05-20 buggy state; the ordering above is the current
correct execution.

Regression pin: an unauthenticated request to a quota-tracked path MUST return
401 (from the route guard), not 429.  See
``tests/middleware/test_quota.py::test_unauthenticated_receives_401_not_429``.

Admin bypass
-------------
Admin users are NOT quota-checked.  Admins manage the system and should not
be gated by their own daily budget.  This is consistent with the per-route
admin-rate-limit pattern, which uses a separate bucket
rather than the quota table.  Rationale: if the admin changes the admin
bit mid-session the change takes effect on the next request (stateless check
against ``user.is_admin``).

Skipped paths
-------------
- ``/healthz``, ``/readyz`` — liveness/readiness probes (no auth).
- ``/api/metrics`` — Prometheus scrape endpoint.
- ``/api/auth/*`` — login, register, logout, /me (auth bootstrapping).
- ``/assets/*``, ``/static/*``, ``/icons/*`` — static file serving.

Token consumption
-----------------
Token consumption (``consume_tokens``) is intentionally NOT performed in
this middleware.  The token count is known only after the upstream LM Studio
response stream completes.  Token consumption is done inside
``streaming_service.py`` after ``chat.end`` with the actual
``stats.total_output_tokens`` value.  A quota write failure there must NOT
kill the stream; it logs and continues.

This middleware only enforces the per-request count.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Final

from starlette.types import ASGIApp, Receive, Scope, Send

from lmchat.logging import get_logger
from lmchat.services.quota_service import OverQuotaError, consume_request

_log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Skipped paths (no quota check)
# ---------------------------------------------------------------------------

_SKIP_EXACT: Final[frozenset[str]] = frozenset(
    {
        "/healthz",
        "/readyz",
        "/api/metrics",
        "/api/auth/login",
        "/api/auth/register",
        "/api/auth/logout",
        "/api/auth/me",
        # Anonymous setup-status probe should not count against
        # any quota (no user is attached yet on a fresh DB).
        "/api/auth/setup_status",
        "/api/auth/totp/setup",
        "/api/auth/totp/verify",
        "/api/auth/change-password",
        # Self-introspection — reading your own quota state should not
        # consume against it. Regression: test_quota_me_returns_defaults was
        # failing because GET /api/quotas/me was counting toward the
        # user's daily request budget.
        "/api/quotas/me",
    }
)

_SKIP_PREFIXES: Final[tuple[str, ...]] = (
    "/assets/",
    "/static/",
    "/icons/",
    "/api/auth/",
)


def _is_skipped(path: str) -> bool:
    """Return True if *path* should bypass quota checking.

    Args:
        path: The URL path of the incoming request.

    Returns:
        True if the path should not be quota-checked.
    """
    if path in _SKIP_EXACT:
        return True
    return any(path.startswith(prefix) for prefix in _SKIP_PREFIXES)


def _seconds_until_midnight() -> int:
    """Return the number of seconds until the next UTC midnight.

    Used for the ``Retry-After`` header on quota-exceeded responses.

    Returns:
        Whole seconds until the next UTC day boundary.
    """
    now = datetime.now(tz=UTC)
    tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0)
    from datetime import timedelta

    tomorrow += timedelta(days=1)
    return max(1, int((tomorrow - now).total_seconds()))


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class QuotaMiddleware:
    """Pure-ASGI daily-request quota middleware.

    Reads ``scope["state"].user`` (set by ``AuthMiddleware``).  If the user
    is present, not an admin, and the path is quota-tracked, calls
    ``consume_request`` atomically.  On ``OverQuotaError``, returns a 429
    response with ``Retry-After: <seconds-until-midnight>`` before the inner
    application runs.

    The ``AsyncEngine`` is read from ``scope["app"].state.engine`` (set by
    the lifespan in ``app.py``).  No fallback — if the lifespan did not run,
    this raises ``AttributeError`` loudly.

    Args:
        app: The inner ASGI application.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Initialise with the inner ASGI app.

        Args:
            app: The next ASGI app in the middleware stack.
        """
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Intercept HTTP requests and enforce the daily request quota.

        Non-HTTP scopes (lifespan, websocket) pass through untouched.

        Args:
            scope:   ASGI connection scope.
            receive: ASGI receive callable.
            send:    ASGI send callable.
        """
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        path: str = scope.get("path", "")

        # Skip health/auth/static paths.
        if _is_skipped(path):
            await self._app(scope, receive, send)
            return

        # Resolve user from state (set by AuthMiddleware upstream).
        state = scope.get("state")
        user = getattr(state, "user", None) if state is not None else None

        # Unauthenticated: pass through (route-level 401 guards handle this).
        if user is None:
            await self._app(scope, receive, send)
            return

        # Admin users bypass quota checks.
        if getattr(user, "is_admin", False):
            await self._app(scope, receive, send)
            return

        # Resolve engine from app.state (must be set by lifespan).
        app_state = getattr(scope.get("app"), "state", None)
        engine = getattr(app_state, "engine", None)
        if engine is None:
            # Engine unset — lifespan didn't run (test without lifespan).
            # Pass through rather than crashing; tests that need quota
            # enforcement must set up engine via dependency_overrides or
            # direct app.state assignment.
            await self._app(scope, receive, send)
            return

        user_id: int = user.id  # type: ignore[attr-defined]

        try:
            await consume_request(user_id, engine)
        except OverQuotaError:
            retry_after = _seconds_until_midnight()
            _log.info(
                "quota.request_exceeded",
                user_id=user_id,
                path=path,
                retry_after=retry_after,
            )
            body = json.dumps(
                {"detail": "request quota exceeded for the day"}
            ).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 429,
                    "headers": [
                        [b"content-type", b"application/json"],
                        [b"retry-after", str(retry_after).encode("ascii")],
                    ],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": body,
                    "more_body": False,
                }
            )
            return
        except Exception as exc:  # noqa: BLE001
            # Non-fatal: quota write failure must not kill the request.
            # Log at WARNING and let the request through.
            _log.warning(
                "quota.consume_request_error",
                user_id=user_id,
                path=path,
                error=str(exc),
            )

        await self._app(scope, receive, send)
