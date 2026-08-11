# SPDX-License-Identifier: Apache-2.0
"""Authentication ASGI middleware for lm-chat.

Reads the ``lmchat_session`` cookie from each inbound HTTP request.
When the cookie is present, resolves the session via
:class:`~lmchat.session.base.SessionStore` and attaches the
corresponding :class:`~lmchat.services.auth_service.User` to
``scope["state"].user``.

Absence of the cookie is treated as **anonymous** — the middleware
passes the request through and lets route-level ``require_user``
dependencies raise HTTP 401 when authentication is actually required.

A cookie that is present but invalid (unknown session, expired session,
or an unknown/deleted user) returns **HTTP 401** immediately from the
middleware layer.  This avoids leaking information about which paths
require authentication while giving honest feedback to automated clients
that send a stale cookie.

Non-HTTP scopes (lifespan, websocket) pass through untouched.

**Pure-ASGI pattern**
This middleware uses ``async def __call__(scope, receive, send)`` —
NOT Starlette's ``BaseHTTPMiddleware``.  The pure-ASGI pattern is
required for compatibility with the streaming SSE routes.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.datastructures import State
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from lmchat.config import get_settings
from lmchat.db.engine import get_engine
from lmchat.db.schema import users
from lmchat.logging import get_logger
from lmchat.session.base import SessionStore
from lmchat.session.sqlite_store import SQLiteSessionStore

_log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SESSION_COOKIE: Final[str] = "lmchat_session"

# Authorization header scheme for the cookie-less fallback below — mirrors
# ``auth_service.get_current_user``'s ``_BEARER_PREFIX``. Needed so API/CLI
# clients that authenticate via ``Authorization: Bearer <session>`` (no
# cookie) still get ``scope["state"].user`` populated: QuotaMiddleware keys
# off that state and silently passes through when it is unset, so without
# this fallback a Bearer client's requests never counted against the daily
# quota.
_BEARER_PREFIX: Final[str] = "Bearer "

# Paths that never require an authenticated session. The middleware will
# forward requests for these paths without attempting cookie lookup.
# Leading slash is included; comparisons are exact prefix or exact match.
_SKIP_EXACT: Final[frozenset[str]] = frozenset(
    {
        "/healthz",
        "/readyz",
        "/api/metrics",
        "/api/auth/login",
        "/api/auth/register",
        # Anonymous one-bit needs_setup signal for the Login page.
        # See routes/auth.py:setup_status_endpoint for the recon-leak
        # rationale (binary, not raw count).
        "/api/auth/setup_status",
        # /me/probe's entire contract is "never
        # 401" (mount-time hydration probe that mirrors /me but returns a
        # null user instead of raising) — see
        # routes/auth.py:me_probe_endpoint. It does its own tolerant
        # cookie/session lookup, so a stale/invalid lmchat_session cookie
        # must not be hard-401'd here before the handler runs.
        "/api/auth/me/probe",
    }
)

# Prefix-skipped paths: anything under /static or /assets (SPA).
# "/api/share/": the public share view
# (routes/share.py:get_public_share) is fully anonymous and never reads
# scope["state"].user — a visitor holding a stale/invalid session cookie
# must still be able to open someone else's share link instead of getting
# hard-401'd by cookie validation that the handler doesn't even need.
_SKIP_PREFIXES: Final[tuple[str, ...]] = ("/static/", "/assets/", "/api/share/")


def _is_skipped(path: str) -> bool:
    """Return True if *path* does not require authentication checking.

    Exact-match paths take precedence; prefix-match paths follow.
    """
    if path in _SKIP_EXACT:
        return True
    return any(path.startswith(prefix) for prefix in _SKIP_PREFIXES)


def _parse_cookies(headers: list[tuple[bytes, bytes]]) -> dict[str, str]:
    """Parse ``Cookie`` header bytes into a name→value dict.

    Follows RFC 6265 §5.2: split on ``;``, strip whitespace, split on
    ``=`` at the first occurrence.  Malformed pairs (no ``=``) are
    silently dropped.
    """
    cookies: dict[str, str] = {}
    for name, value in headers:
        if name != b"cookie":
            continue
        try:
            raw = value.decode("latin-1")
        except UnicodeDecodeError:
            continue
        for pair in raw.split(";"):
            pair = pair.strip()
            if "=" in pair:
                k, _, v = pair.partition("=")
                cookies[k.strip()] = v.strip()
    return cookies


def _extract_bearer_token(headers: list[tuple[bytes, bytes]]) -> str | None:
    """Extract the session id from an ``Authorization: Bearer <token>`` header.

    Mirrors ``auth_service.get_current_user``'s header-fallback parsing
    exactly (same prefix, same case-sensitive match) so the two code paths
    agree on what counts as a valid Bearer token.  Returns ``None`` when no
    ``Authorization`` header is present, the scheme isn't ``Bearer``, or the
    token is empty after stripping whitespace.
    """
    for name, value in headers:
        if name != b"authorization":
            continue
        try:
            raw = value.decode("latin-1")
        except UnicodeDecodeError:
            continue
        if raw.startswith(_BEARER_PREFIX):
            token = raw[len(_BEARER_PREFIX) :].strip()
            if token:
                return token
    return None


def _is_api_path(path: str) -> bool:
    """True for backend API paths that should receive a JSON 401 (not the SPA).

    Everything else (``/``, ``/chats/…``, ``/settings``, etc.) is a document
    navigation that must render the SPA shell rather than raw JSON.
    """
    return path.startswith("/api/")


# Set-Cookie that expires the session cookie (matches the attributes the
# session layer sets so the browser actually drops it).
_CLEAR_COOKIE_HEADER: Final[tuple[bytes, bytes]] = (
    b"set-cookie",
    (
        f"{_SESSION_COOKIE}=; Path=/; Max-Age=0; "
        "HttpOnly; SameSite=Lax"
    ).encode("latin-1"),
)


def _wrap_send_clear_cookie(send: Send) -> Send:
    """Wrap an ASGI ``send`` to append a session-cookie-clearing Set-Cookie
    header onto the response start.

    Used when an invalid/expired session cookie accompanies an HTML
    navigation: we pass the request through to the SPA shell but expire the
    stale cookie so the browser stops re-sending it on every subsequent
    request (which otherwise re-triggers the invalid-cookie path + log spam).
    """
    async def _send(message: Message) -> None:
        if message["type"] == "http.response.start":
            headers = list(message.get("headers", []))
            headers.append(_CLEAR_COOKIE_HEADER)
            message = {**message, "headers": headers}
        await send(message)

    return _send


async def _send_401(send: Send) -> None:
    """Emit a minimal HTTP 401 Unauthorized ASGI response."""
    body = b'{"detail":"Unauthorized"}'
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": headers,
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": body,
            "more_body": False,
        }
    )


def _is_secure_scheme(scope: Scope) -> bool:
    """Return True if the request should be treated as HTTPS for cookies.

    Mirrors ``middleware.security._apply_forwarded_proto``'s trusted-proxy
    correction: when ``lm_chat_trust_forwarded_proto`` is enabled, a trusted
    reverse proxy's ``X-Forwarded-Proto: https`` header overrides the raw
    ASGI ``scope["scheme"]`` (which reads "http" for a TLS-terminating
    proxy). AuthMiddleware is mounted OUTER of SecurityMiddleware (see
    app.py's middleware ordering), so SecurityMiddleware's own correction
    has not been applied to ``scope["scheme"]`` yet by the time this runs —
    this duplicates just enough of that logic to agree with what
    routes/auth.py's ``_set_session_cookie`` sees once the request reaches
    the route. Keep in sync with
    ``middleware/security.py::_apply_forwarded_proto`` if either changes.
    """
    if get_settings().lm_chat_trust_forwarded_proto:
        for name, value in scope.get("headers", []):
            if name == b"x-forwarded-proto":
                try:
                    proto = value.decode("ascii").strip().lower()
                except UnicodeDecodeError:
                    break
                if proto in ("http", "https"):
                    return proto == "https"
                break
    return scope.get("scheme") == "https"


def _build_renewal_cookie_header(
    token: str, *, max_age: int, secure: bool
) -> tuple[bytes, bytes]:
    """Build the sliding-TTL renewal ``Set-Cookie`` header.

    Mirrors routes/auth.py's ``_set_session_cookie`` attribute-for-attribute
    (HttpOnly, SameSite=Lax, Path=/, Secure when the request is HTTPS) so a
    renewed session's cookie is indistinguishable from a freshly-issued one.
    This middleware runs at the raw-ASGI layer (no ``Response`` object), so
    it cannot call that helper directly and reconstructs the header instead
    — keep the two in sync if either changes.
    """
    parts = [
        f"{_SESSION_COOKIE}={token}",
        "Path=/",
        f"Max-Age={max_age}",
        "HttpOnly",
        "SameSite=Lax",
    ]
    if secure:
        parts.append("Secure")
    return (b"set-cookie", "; ".join(parts).encode("latin-1"))


def _wrap_send_renew_cookie(send: Send, header: tuple[bytes, bytes]) -> Send:
    """Wrap an ASGI ``send`` to append a sliding-TTL renewal Set-Cookie header.

    Used when an active session's sliding-TTL window renewed the store-side
    expiry (see ``AuthMiddleware._resolve_user``) — the browser's Max-Age
    must be re-issued to match, or the cookie would expire client-side
    before the server-side session does.
    """

    async def _send(message: Message) -> None:
        if message["type"] == "http.response.start":
            headers = list(message.get("headers", []))
            headers.append(header)
            message = {**message, "headers": headers}
        await send(message)

    return _send


class AuthMiddleware:
    """Pure-ASGI middleware that resolves the session cookie and attaches the user.

    Args:
        app:           The next ASGI application in the stack.
        session_store: :class:`~lmchat.session.base.SessionStore` instance.
                       When ``None``, a :class:`~lmchat.session.sqlite_store.SQLiteSessionStore`
                       is constructed lazily on the first request using the
                       application-global engine.
        engine:        SQLAlchemy async engine for user lookups.
                       Defaults to the application-global engine when ``None``.
        fastapi_app:   The root FastAPI application instance.  When provided,
                       ``app.state.session_store`` is checked first on each
                       request so test fixtures that set ``app.state.session_store``
                       are respected without requiring a global-engine redirect.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        session_store: SessionStore | None = None,
        engine: AsyncEngine | None = None,
        fastapi_app: ASGIApp | None = None,
    ) -> None:
        """Wrap *app* with session-cookie authentication."""
        self.app = app
        self._session_store = session_store
        self._engine = engine
        # Reference to the root FastAPI instance so we can read app.state.session_store
        # inside _resolve_user even though scope["app"] is not yet set during
        # pure-ASGI middleware execution (it is populated by the Router later).
        self._fastapi_app = fastapi_app

    def _get_store(self) -> SessionStore:
        """Lazily resolve the session store (construction-time safe)."""
        if self._session_store is not None:
            return self._session_store
        engine = self._engine if self._engine is not None else get_engine()
        return SQLiteSessionStore(engine=engine)

    def _get_engine(self) -> AsyncEngine:
        """Lazily resolve the engine (construction-time safe)."""
        return self._engine if self._engine is not None else get_engine()

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        """Handle one ASGI scope.

        Non-HTTP scopes and skip-listed paths pass through immediately.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")

        if _is_skipped(path):
            await self.app(scope, receive, send)
            return

        cookies = _parse_cookies(scope.get("headers", []))
        cookie_session_id: str | None = cookies.get(_SESSION_COOKIE)
        session_id: str | None = cookie_session_id

        if session_id is None:
            # No cookie — fall back to Authorization: Bearer for API/CLI
            # clients that can't use cookies (mirrors auth_service's own
            # cookie→Bearer fallback). Without this, a Bearer-authenticated
            # request would reach the route just fine but scope["state"].user
            # would never be set, so QuotaMiddleware would never enforce the
            # daily quota against it.
            session_id = _extract_bearer_token(scope.get("headers", []))

        if session_id is None:
            # Anonymous request — pass through; let route dependency 401 if needed.
            await self.app(scope, receive, send)
            return

        # Cookie is present — validate it.
        user, renewed = await self._resolve_user(session_id, scope=scope)
        if user is None:
            await _log.awarning(
                "auth.invalid_cookie",
                path=path,
                session_prefix=session_id[:8] + "...",
            )
            # An invalid/expired cookie must NOT dump raw 401 JSON onto an
            # HTML navigation — that's the "{"detail":"Invalid or expired
            # session"}" page users were seeing on the SPA shell. Only API
            # callers (XHR/fetch under /api/) expect a JSON 401; for any
            # document navigation, expire the stale cookie and pass through
            # so the SPA shell renders and its own /api/auth/me check
            # redirects to /login cleanly.
            if _is_api_path(path):
                await _send_401(send)
                return
            await self.app(scope, receive, _wrap_send_clear_cookie(send))
            return

        # Attach user to scope state so downstream middleware and routes can read it.
        # Starlette TestClient initialises scope["state"] as a plain dict copied
        # from app_state.  Normalise to State() so attribute-access is consistent.
        if "state" not in scope:
            scope["state"] = State()
        elif isinstance(scope["state"], dict):
            scope["state"] = State()
        scope["state"].user = user

        # Sliding-TTL renewal happened inside _resolve_user (only when this
        # session was inside its trailing renewal window) — re-issue the
        # Set-Cookie so the browser's Max-Age tracks the new server-side
        # expiry. Bearer clients (no cookie) still get the store-side
        # extend from _resolve_user; they just never had a cookie to
        # re-issue here.
        if renewed and cookie_session_id is not None:
            header = _build_renewal_cookie_header(
                cookie_session_id,
                max_age=get_settings().lm_chat_session_ttl_seconds,
                secure=_is_secure_scheme(scope),
            )
            send = _wrap_send_renew_cookie(send, header)

        await self.app(scope, receive, send)

    async def _resolve_user(
        self, session_id: str, *, scope: Scope | None = None
    ) -> tuple[object | None, bool]:
        """Resolve *session_id* to a User, or return ``(None, False)`` if invalid.

        Queries the session store, then the users table.  Returns
        ``(None, False)`` if the session is not found, is expired, or the
        user has been deleted.

        As a side effect, when the session is valid and inside its
        sliding-TTL renewal window (less than half of
        ``lm_chat_session_ttl_seconds`` remaining), extends the session in
        the store and reports that back via the ``renewed`` flag so the
        caller can re-issue the Set-Cookie with a matching Max-Age. This
        keeps every request cheap: the store write only happens for the
        (rare) request that lands inside the trailing half of the window,
        not on every request.

        Session store resolution priority:
        1. ``self._fastapi_app.state.session_store`` — set by the lifespan or
           by test fixtures that call ``client.app.state.session_store = store``.
           This makes the middleware share the session store with route-level
           dependencies even in tests that use ``dependency_overrides`` rather
           than redirecting the global engine.
        2. ``self._session_store`` — explicit constructor argument (tests that
           construct ``AuthMiddleware`` directly).
        3. Lazily constructed ``SQLiteSessionStore(engine=get_engine())`` —
           production fallback; always shares the lifespan-set engine via the
           ``get_engine()`` singleton.

        Returns:
            A ``(user, renewed)`` tuple. ``renewed`` is ``True`` only when
            this call triggered a store-side ``extend()``.
        """
        from sqlalchemy import select

        store = self._get_store()
        engine = self._get_engine()
        # Check fastapi_app.state.session_store first (set by lifespan/tests).
        # When the state store is found, also use its engine for user lookups so
        # that session writes (to the per-test DB) and user reads use the SAME DB.
        if self._fastapi_app is not None:
            state_store = getattr(
                getattr(self._fastapi_app, "state", None), "session_store", None
            )
            if state_store is not None:
                store = state_store
                # Use the session store's own engine so user lookups hit the
                # same DB as session lookups (critical for per-test DB isolation).
                store_engine = getattr(store, "_engine", None)
                if store_engine is not None:
                    engine = store_engine

        session = await store.get(session_id)
        if session is None:
            return None, False

        # --- sliding-TTL renewal ---
        # Half the configured TTL is the renewal window: a session with
        # more than half its life left is untouched (no DB write, no
        # re-cookie); one inside the trailing half gets pushed back out to
        # a full TTL from now. Derived inline rather than as its own config
        # knob — this codebase has exactly one TTL setting
        # (lm_chat_session_ttl_seconds) and no separate absolute-lifetime
        # cap, so sliding the expiry back out to now + ttl_seconds on each
        # renewal IS the intended policy (see the config.py comment on
        # lm_chat_session_ttl_seconds: a hard expiry with no sliding
        # renewal was the exact problem being fixed).
        renewed = False
        ttl_seconds = get_settings().lm_chat_session_ttl_seconds
        renewal_window = timedelta(seconds=ttl_seconds // 2)
        if session.expires_at - datetime.now(UTC) < renewal_window:
            try:
                await store.extend(session_id, ttl_seconds=ttl_seconds)
                renewed = True
            except KeyError:
                # Expired/revoked between get() and extend() (raced with
                # cleanup() or a concurrent logout) — this request still
                # resolves against the get() snapshot above; the NEXT
                # request will correctly see the session as gone.
                pass

        async with engine.connect() as conn:
            result = await conn.execute(
                select(users).where(users.c.id == session.user_id)
            )
            row = result.fetchone()

        if row is None:
            return None, False

        from lmchat.services.auth_service import User

        return User.model_validate(row, from_attributes=True), renewed


# Expose the skip list as a module-level constant for tests.
AUTH_SKIP_PATHS: Final[frozenset[str]] = _SKIP_EXACT
AUTH_SKIP_PREFIXES: Final[tuple[str, ...]] = _SKIP_PREFIXES
