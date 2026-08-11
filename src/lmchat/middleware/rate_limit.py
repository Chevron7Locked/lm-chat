# SPDX-License-Identifier: Apache-2.0
"""Login rate-limit ASGI middleware.

This middleware enforces a per-key
token-bucket rate limit on ``POST /api/auth/login`` only.  All other
scopes (lifespan, websocket, non-matching HTTP paths/methods) pass
through untouched.

**Why pure-ASGI, not BaseHTTPMiddleware?**
Starlette's ``BaseHTTPMiddleware`` buffers the entire response body
before calling the inner application's ``send()``, which breaks the
streaming SSE implementation (the streaming response hangs waiting for
the body to complete before the first chunk is delivered).  Pure-ASGI
middleware keeps the request/response lifecycle transparent and is
compatible with all response types.  See
``docs/IMPLEMENTATION_METHODOLOGY.md`` and ``docs/HANDOFF.md`` for the
full rationale.

**Body-replay pattern**
To derive the rate-limit key, the middleware needs the ``username``
field from the ``application/x-www-form-urlencoded`` request body.
Reading the body in ASGI requires consuming the ``receive`` channel
(which returns ``http.request`` messages).  Once consumed, the body
would be gone from the perspective of the inner application.

The replay pattern works as follows:
1.  Call ``receive()`` in a loop to collect all ``http.request`` chunks
    until ``more_body`` is ``False``.  This buffers the full body.
2.  Parse the form fields from the buffered bytes.
3.  Build a *replacement ``receive`` coroutine* (``_make_replay_receive``)
    that, when called by the inner application, first replays the
    ``http.request`` message carrying the buffered body, then delegates
    any subsequent calls to the real ``receive``.  This means the inner
    application sees the body exactly once, just as if the middleware had
    not touched it.

The body is bounded: ``application/x-www-form-urlencoded`` login
payloads are tiny (username + password ≈ a few hundred bytes).  There
is no risk of unbounded memory growth from buffering here.

**Client IP resolution**
``scope["client"]`` is a ``(host, port)`` tuple or ``None``.  The
middleware uses ``host`` directly.

``X-Forwarded-For`` is honoured **only** when
``settings.lm_chat_trusted_proxy`` is non-empty (the admin has
explicitly declared a trusted reverse proxy).  Trusting
``X-Forwarded-For`` without a controlled ingress allows any client to
spoof its IP and bypass the rate limit.

**Structured audit log**
When a request is rate-limited the middleware emits a structured
warning via structlog (``event="login.rate_limited"``).  Writing a row
to ``audit_log`` is ``auth_service``'s responsibility; the middleware
does not touch the database.
"""
from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from typing import Final
from urllib.parse import parse_qs

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from lmchat.config import get_settings
from lmchat.logging import get_logger
from lmchat.middleware._bucket_store import BucketStore, InMemoryBucketStore

_LOGIN_PATH: Final[str] = "/api/auth/login"
_LOGIN_METHOD: Final[str] = "POST"
_SECONDS_PER_MINUTE: Final[float] = 60.0

_log = get_logger(__name__)


def _extract_client_ip(scope: Scope, *, trusted_proxy: str) -> str:
    """Resolve the client IP from the ASGI scope.

    Checks ``scope["client"]`` first (always trustworthy from the ASGI
    server's perspective).  If ``trusted_proxy`` is non-empty, also
    checks ``X-Forwarded-For`` and uses the *leftmost* entry (the
    originating client IP in a standard proxy chain).

    The trusted-proxy setting is passed in by the caller (resolved once
    per middleware instance, cached) rather than re-read from settings
    on every request — important under load.

    When neither client tuple nor XFF is available, returns ``"unknown"``.
    """
    if trusted_proxy:
        for name, value in scope.get("headers", []):
            if name == b"x-forwarded-for":
                try:
                    xff = value.decode("ascii")
                    first_ip = xff.split(",")[0].strip()
                    if first_ip:
                        return first_ip
                except (UnicodeDecodeError, IndexError):
                    pass

    client = scope.get("client")
    if client is not None:
        return client[0]

    return "unknown"


async def _read_body(receive: Receive) -> bytes:
    """Consume all ``http.request`` messages from ``receive`` and return the body.

    Drains the ASGI receive channel until ``more_body`` is ``False``.
    """
    body_chunks: list[bytes] = []
    while True:
        message: Message = await receive()
        chunk = message.get("body", b"")
        if chunk:
            body_chunks.append(chunk)
        if not message.get("more_body", False):
            break
    return b"".join(body_chunks)


def _make_replay_receive(
    body: bytes,
    real_receive: Receive,
) -> Callable[[], Awaitable[Message]]:
    """Return a ``receive`` coroutine that replays ``body`` on the first call.

    The inner application calls ``receive()`` to read the request body.
    After the middleware has already consumed the body for key derivation,
    this wrapper ensures the inner app still sees the complete body on its
    first ``receive()`` call, with ``more_body=False`` to signal that
    there are no further chunks.  Subsequent calls are forwarded to the
    real ``receive`` (for disconnect / upgrade messages).
    """
    replayed = False

    async def replay_receive() -> Message:
        nonlocal replayed
        if not replayed:
            replayed = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await real_receive()

    return replay_receive


def _extract_username(body: bytes) -> str | None:
    """Parse ``username`` from a URL-encoded form body.

    Returns the first ``username`` value decoded as UTF-8, or ``None``
    if the field is absent or the body is not valid form data.
    """
    try:
        parsed = parse_qs(body.decode("utf-8"), keep_blank_values=False)
        values = parsed.get("username")
        if values:
            return values[0]
        return None
    except (UnicodeDecodeError, ValueError):
        return None


def _build_key(ip: str, username: str | None) -> str:
    """Construct the rate-limit bucket key.

    When ``username`` is available: ``"login:<ip>:<username>"``.
    When absent (malformed body): ``"login:<ip>"`` — still rate-limited
    by IP so a flood of malformed requests does not escape accounting.
    """
    if username:
        return f"login:{ip}:{username}"
    return f"login:{ip}"


class LoginRateLimitMiddleware:
    """Pure-ASGI middleware that rate-limits ``POST /api/auth/login``.

    Only the specific ``(method="POST", path="/api/auth/login")``
    combination is affected.  Everything else passes through with zero
    overhead.

    Args:
        app:             The next ASGI application in the stack.
        store:           A :class:`BucketStore` instance.  Defaults to a
                         new :class:`InMemoryBucketStore` when ``None``.
        rate_per_minute: Token refill rate expressed as attempts per
                         minute.  Converted to tokens/second internally.
                         Defaults to ``settings.lm_chat_login_rate_limit_per_min``
                         when ``None``.
        burst:           Maximum burst capacity (token ceiling).
                         Defaults to ``rate_per_minute`` when ``None``,
                         meaning a fully-charged bucket allows exactly
                         ``rate_per_minute`` consecutive immediate attempts.
        ip_rate_per_minute: Token refill rate for the per-IP bucket (attempts
                         per minute across ALL usernames from that IP).
                         Defaults to
                         ``settings.lm_chat_login_rate_limit_per_ip_per_min``
                         when ``None``.
        ip_burst:        Maximum burst capacity for the per-IP bucket.
                         Defaults to ``ip_rate_per_minute`` when ``None``.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        store: BucketStore | None = None,
        rate_per_minute: int | None = None,
        burst: int | None = None,
        ip_rate_per_minute: int | None = None,
        ip_burst: int | None = None,
    ) -> None:
        """Initialise the middleware with an optional custom store and limits.

        ``rate_per_minute`` and ``burst`` default to ``None``, meaning the
        values are read from ``get_settings()`` lazily on the **first request**
        rather than at construction time.  This allows the application to be
        constructed before the settings are validated (e.g. during tests or
        before the lifespan hook runs).

        ``ip_rate_per_minute`` and ``ip_burst`` govern the per-IP bucket
        (``login:<ip>``, consumed on every login request across all
        usernames) that closes the username-rotation evasion of the
        per-account bucket above.  Same lazy-resolution semantics as
        ``rate_per_minute``/``burst``; default from
        ``settings.lm_chat_login_rate_limit_per_ip_per_min``.
        """
        self.app = app
        self._store: BucketStore = store if store is not None else InMemoryBucketStore()

        # Store the explicit overrides; None means "read from settings at first request".
        self._rate_per_minute_override: int | None = rate_per_minute
        self._burst_override: int | None = burst
        # Lazily resolved on first request; avoids calling get_settings() at
        # construction time so create_app() works before the lifespan validates
        # the required env vars.
        self._rate_per_minute: int | None = rate_per_minute
        self._burst: int | None = burst
        # Per-IP bucket rate/burst — same lazy-resolution pattern as above.
        self._ip_rate_per_minute: int | None = ip_rate_per_minute
        self._ip_burst: int | None = ip_burst
        # Trusted-proxy resolution is also lazy; resolved once on first
        # matching request and cached for the lifetime of the middleware
        # instance. Avoids calling get_settings() on every request.
        self._trusted_proxy: str | None = None

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        """Handle a single ASGI scope.

        Non-HTTP scopes and non-matching HTTP requests pass through
        immediately.  Matching requests are rate-checked against BOTH a
        per-account bucket (``login:<ip>:<username>``, when a username is
        present) and a per-IP bucket (``login:<ip>``, consumed on EVERY
        login request regardless of username).  Either bucket being
        exhausted results in a ``429 Too Many Requests`` response directly
        from this middleware without touching the inner app.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method: str = scope.get("method", "")
        path: str = scope.get("path", "")

        if method != _LOGIN_METHOD or path != _LOGIN_PATH:
            await self.app(scope, receive, send)
            return

        # --- Rate-limit path ---

        # Resolve rate/burst/trusted-proxy lazily on first login request so
        # that create_app() does not call get_settings() at construction time.
        # After first resolution these are cached on the instance and not
        # re-read on subsequent requests — important under load.
        rate_per_minute = self._rate_per_minute
        if rate_per_minute is None:
            rate_per_minute = get_settings().lm_chat_login_rate_limit_per_min
            self._rate_per_minute = rate_per_minute
        burst = self._burst
        if burst is None:
            burst = rate_per_minute
            self._burst = burst
        ip_rate_per_minute = self._ip_rate_per_minute
        if ip_rate_per_minute is None:
            ip_rate_per_minute = get_settings().lm_chat_login_rate_limit_per_ip_per_min
            self._ip_rate_per_minute = ip_rate_per_minute
        ip_burst = self._ip_burst
        if ip_burst is None:
            ip_burst = ip_rate_per_minute
            self._ip_burst = ip_burst
        trusted_proxy = self._trusted_proxy
        if trusted_proxy is None:
            trusted_proxy = get_settings().lm_chat_trusted_proxy.strip()
            self._trusted_proxy = trusted_proxy

        # Buffer the request body so we can extract the username, then
        # build a replay receive so the inner app still sees the body.
        body = await _read_body(receive)
        replay_receive = _make_replay_receive(body, receive)

        ip = _extract_client_ip(scope, trusted_proxy=trusted_proxy)
        username = _extract_username(body)

        # Per-IP bucket — consumed on EVERY login request regardless of
        # username. This closes the evasion where an attacker rotates
        # `username` from a single IP to get a fresh per-account bucket on
        # each attempt. The key is distinct from the per-account key below
        # (that key always carries a trailing `:<username>` segment), so
        # there is no collision between the two bucket namespaces.
        ip_key = f"login:{ip}"
        ip_rate_per_sec = ip_rate_per_minute / _SECONDS_PER_MINUTE
        ip_allowed, ip_retry_after = await self._store.consume(
            ip_key,
            rate=ip_rate_per_sec,
            burst=ip_burst,
        )

        # Per-account bucket — only consumed when a username was extracted.
        # When absent (malformed body), the per-IP bucket above is the sole
        # limiter, preserving the historical "malformed body still
        # IP-limited" behavior (its key, `login:<ip>`, is exactly the old
        # fallback key `_build_key(ip, None)` used to produce).
        if username:
            account_key = _build_key(ip, username)
            rate_per_sec = rate_per_minute / _SECONDS_PER_MINUTE
            account_allowed, account_retry_after = await self._store.consume(
                account_key,
                rate=rate_per_sec,
                burst=burst,
            )
        else:
            account_key = None
            account_allowed, account_retry_after = True, 0.0

        allowed = ip_allowed and account_allowed
        retry_after = max(ip_retry_after, account_retry_after)

        if allowed:
            await self.app(scope, replay_receive, send)
            return

        # Bucket(s) empty — respond with 429 directly.
        retry_after_secs = math.ceil(retry_after)
        await _log.awarning(
            "login.rate_limited",
            key=account_key or ip_key,
            ip_key=ip_key,
            retry_after=retry_after_secs,
            ip=ip,
            username=username,
        )
        await _send_429(send, retry_after_secs)

    @property
    def store(self) -> BucketStore:
        """Expose the underlying store for testing and lifecycle management."""
        return self._store


async def _send_429(send: Send, retry_after_secs: int) -> None:
    """Emit a ``429 Too Many Requests`` ASGI response.

    Sends ``http.response.start`` followed by ``http.response.body``
    with a plain-text body and a ``Retry-After`` header indicating
    when the client may retry.
    """
    body = (
        f"Too many login attempts. Retry after {retry_after_secs} second(s)."
    ).encode()
    headers = [
        (b"content-type", b"text/plain; charset=utf-8"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"retry-after", str(retry_after_secs).encode("ascii")),
    ]
    await send(
        {
            "type": "http.response.start",
            "status": 429,
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
