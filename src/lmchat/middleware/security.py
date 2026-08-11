# SPDX-License-Identifier: Apache-2.0
"""Security-header ASGI middleware for lm-chat.

Adds the following headers to every HTTP response:

- ``Strict-Transport-Security: max-age=31536000; includeSubDomains``
- ``X-Content-Type-Options: nosniff``
- ``X-Frame-Options: DENY``
- ``Referrer-Policy: strict-origin-when-cross-origin``
- ``Content-Security-Policy`` — per-request nonce via
  :func:`secrets.token_urlsafe`.
- ``Permissions-Policy: microphone=(self), camera=(), geolocation=()``
  — allows the SPA's own origin to use the microphone (STT); denies
  camera and geolocation.

**CSP nonce**
A fresh nonce is generated for every request with
``secrets.token_urlsafe(16)`` (16 bytes → 22 URL-safe chars). It is
stored on ``scope["state"].csp_nonce`` so Jinja2 templates can embed
it in ``<script nonce="...">`` tags.  The same value is emitted in the
``Content-Security-Policy`` response header.

The full CSP directive::

    default-src 'self';
    script-src 'self' 'nonce-<value>';
    style-src 'self';
    img-src 'self' data:;
    connect-src 'self';
    frame-ancestors 'none'

``style-src 'self'`` — ``'unsafe-inline'`` was removed in task #182.
All ``@keyframes`` that previously lived in component-level ``<style>``
elements (ThinkingIndicator, ThinkingBlock, Toast, ChatMessage,
LmStudioStatusBadge) were migrated into ``web/src/globals.css``, which
Vite/Tailwind bundles into the served stylesheet covered by ``'self'``.
React inline ``style={{}}`` props render as HTML ``style`` attributes
and are not subject to ``style-src`` CSP enforcement in current
browsers — they are allowed regardless.  ZAP rule 10055 was cleared by
removing the header-level ``'unsafe-inline'`` directive.

**X-Forwarded-Proto**
Only honoured when ``settings.lm_chat_trust_forwarded_proto`` is
``True`` (admin-declared trusted proxy).  By default (``False``) the
header is silently ignored — trusting it without a controlled ingress
allows any client to upgrade themselves to HTTPS in header-only checks.

**Pure-ASGI pattern**
This middleware does NOT extend ``BaseHTTPMiddleware``; see
``docs/IMPLEMENTATION_METHODOLOGY.md``.
"""
from __future__ import annotations

import secrets
from typing import Final

from starlette.datastructures import State
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from lmchat.config import get_settings
from lmchat.logging import get_logger

_log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Static security header values
# ---------------------------------------------------------------------------

_HSTS_VALUE: Final[bytes] = b"max-age=31536000; includeSubDomains"
_X_CONTENT_TYPE_VALUE: Final[bytes] = b"nosniff"
_X_FRAME_VALUE: Final[bytes] = b"DENY"
_REFERRER_VALUE: Final[bytes] = b"strict-origin-when-cross-origin"
# microphone=(self)  — allow the SPA's own origin to access the mic (STT).
# camera=()          — deny camera entirely.
# geolocation=()     — deny geolocation entirely.
_PERMISSIONS_POLICY_VALUE: Final[bytes] = (
    b"microphone=(self), camera=(), geolocation=()"
)

# CSP template — {nonce} is replaced per request.
_CSP_TEMPLATE: Final[str] = (
    "default-src 'self'; "
    "script-src 'self' 'nonce-{nonce}'; "
    "style-src 'self'; "
    "font-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "form-action 'self'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'"
)

# Nonce size in bytes — 16 bytes → 22 URL-safe base64 chars.
_NONCE_NBYTES: Final[int] = 16


def _generate_nonce() -> str:
    """Generate a cryptographically random URL-safe nonce string.

    Uses :func:`secrets.token_urlsafe` (16 bytes → 22 chars).
    Each call returns a unique value; no global state is mutated.
    """
    return secrets.token_urlsafe(_NONCE_NBYTES)


def _build_csp(nonce: str) -> bytes:
    """Return the per-request CSP header value as bytes.

    Args:
        nonce: The URL-safe nonce string generated for this request.
    """
    return _CSP_TEMPLATE.format(nonce=nonce).encode("ascii")


class SecurityMiddleware:
    """Pure-ASGI middleware that injects security response headers.

    Args:
        app: The next ASGI application in the stack.

    All configuration is read lazily from :func:`~lmchat.config.get_settings`
    on the first request so the middleware can be constructed before the
    lifespan hook validates the environment.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap *app* with security-header injection."""
        self.app = app
        # Lazily resolved on first request; cached for the instance lifetime.
        self._trust_forwarded_proto: bool | None = None

    def _get_trust_forwarded_proto(self) -> bool:
        """Return the ``lm_chat_trust_forwarded_proto`` setting, cached."""
        if self._trust_forwarded_proto is None:
            self._trust_forwarded_proto = get_settings().lm_chat_trust_forwarded_proto
        return self._trust_forwarded_proto

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        """Handle one ASGI scope.

        Non-HTTP scopes pass through immediately. For HTTP scopes:
        - Generate a per-request CSP nonce.
        - Attach the nonce to ``scope["state"].csp_nonce``.
        - Inject all security headers into ``http.response.start``.
        - Optionally note the X-Forwarded-Proto if trusted.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        nonce = _generate_nonce()

        # Attach nonce to scope state before inner app runs so templates can
        # read it during response rendering.
        # Starlette TestClient (and production ASGI servers) may initialise
        # scope["state"] as a plain dict (copied from app_state) rather than
        # a State object.  We normalise to State() so attribute-access works
        # consistently downstream.
        if "state" not in scope:
            scope["state"] = State()
        elif isinstance(scope["state"], dict):
            scope["state"] = State()
        scope["state"].csp_nonce = nonce

        # Optionally handle X-Forwarded-Proto.
        if self._get_trust_forwarded_proto():
            _apply_forwarded_proto(scope)

        csp_value = _build_csp(nonce)

        async def send_with_security(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers: list[tuple[bytes, bytes]] = list(message.get("headers", []))
                headers.extend(
                    [
                        (b"strict-transport-security", _HSTS_VALUE),
                        (b"x-content-type-options", _X_CONTENT_TYPE_VALUE),
                        (b"x-frame-options", _X_FRAME_VALUE),
                        (b"referrer-policy", _REFERRER_VALUE),
                        (b"content-security-policy", csp_value),
                        (b"permissions-policy", _PERMISSIONS_POLICY_VALUE),
                    ]
                )
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_security)


def _apply_forwarded_proto(scope: Scope) -> None:
    """Upgrade ``scope["scheme"]`` to match ``X-Forwarded-Proto`` when trusted.

    Only executed when ``settings.lm_chat_trust_forwarded_proto`` is ``True``.
    Silently does nothing if the header is absent or non-ASCII.
    """
    for name, value in scope.get("headers", []):
        if name == b"x-forwarded-proto":
            try:
                proto = value.decode("ascii").strip().lower()
                if proto in ("http", "https"):
                    scope["scheme"] = proto
            except UnicodeDecodeError:
                pass
            break
