# SPDX-License-Identifier: Apache-2.0
"""Tests for SecurityMiddleware.

Security middleware tests.

Test architecture
-----------------
A minimal FastAPI app with SecurityMiddleware mounted is driven through
``httpx.AsyncClient(transport=ASGITransport(...))``.  The CSP nonce is
captured from the response header and from a probe endpoint that reads
``request.state.csp_nonce``.  We use monkeypatch to toggle
``settings.lm_chat_trust_forwarded_proto`` for the X-Forwarded-Proto
tests.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport

from lmchat.middleware.security import SecurityMiddleware

# ---------------------------------------------------------------------------
# Test app factory
# ---------------------------------------------------------------------------


def make_app() -> FastAPI:
    """Build a minimal FastAPI app with SecurityMiddleware."""
    app = FastAPI()
    app.add_middleware(SecurityMiddleware)

    @app.get("/probe")
    async def probe(request: Request) -> JSONResponse:
        nonce = getattr(request.state, "csp_nonce", None)
        scheme = request.scope.get("scheme", "http")
        return JSONResponse({"csp_nonce": nonce, "scheme": scheme})

    return app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_security_headers_present_on_response() -> None:
    """All required security headers are present on every response."""
    app = make_app()
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/probe")

    assert resp.status_code == 200
    headers = resp.headers

    # HSTS
    assert headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"
    # nosniff
    assert headers["x-content-type-options"] == "nosniff"
    # Frame deny
    assert headers["x-frame-options"] == "DENY"
    # Referrer
    assert headers["referrer-policy"] == "strict-origin-when-cross-origin"
    # CSP present and includes nonce
    csp = headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "nonce-" in csp
    # Phase F regression guard: font-src 'self' must be explicit so future
    # CSP hardening cannot silently break self-hosted font serving.
    assert "font-src 'self'" in csp, (
        "CSP missing 'font-src 'self'' — self-hosted fonts will 404 if "
        "a future hardening pass adds 'font-src 'none''"
    )


@pytest.mark.asyncio
async def test_csp_nonce_generated_per_request() -> None:
    """Two separate requests receive distinct CSP nonces."""
    app = make_app()
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r1 = await client.get("/probe")
        r2 = await client.get("/probe")

    csp1 = r1.headers["content-security-policy"]
    csp2 = r2.headers["content-security-policy"]
    # Both have nonces but they must differ.
    assert "nonce-" in csp1
    assert "nonce-" in csp2
    assert csp1 != csp2


@pytest.mark.asyncio
async def test_csp_nonce_attached_to_state() -> None:
    """The CSP nonce stored on request.state matches the response header nonce."""
    app = make_app()
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/probe")

    body_nonce: str | None = resp.json().get("csp_nonce")
    assert body_nonce is not None, "csp_nonce not attached to request.state"

    csp = resp.headers["content-security-policy"]
    assert f"nonce-{body_nonce}" in csp, (
        f"Header nonce does not match state nonce. "
        f"state={body_nonce!r}, csp={csp!r}"
    )


@pytest.mark.asyncio
async def test_trust_forwarded_proto_when_setting_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When lm_chat_trust_forwarded_proto=True, scope scheme is updated from header."""
    from lmchat.config import get_settings

    monkeypatch.setenv("LM_CHAT_TRUST_FORWARDED_PROTO", "true")
    get_settings.cache_clear()

    app = FastAPI()
    # Use a fresh SecurityMiddleware so _trust_forwarded_proto is not cached.
    app.add_middleware(SecurityMiddleware)

    @app.get("/probe")
    async def probe(request: Request) -> JSONResponse:
        return JSONResponse({"scheme": request.scope.get("scheme", "http")})

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/probe", headers={"X-Forwarded-Proto": "https"}
        )

    assert resp.status_code == 200
    assert resp.json()["scheme"] == "https"

    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_ignore_forwarded_proto_when_setting_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When lm_chat_trust_forwarded_proto=False (default), X-Forwarded-Proto is ignored."""
    from lmchat.config import get_settings

    monkeypatch.setenv("LM_CHAT_TRUST_FORWARDED_PROTO", "false")
    get_settings.cache_clear()

    app = FastAPI()
    app.add_middleware(SecurityMiddleware)

    @app.get("/probe")
    async def probe(request: Request) -> JSONResponse:
        return JSONResponse({"scheme": request.scope.get("scheme", "http")})

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/probe", headers={"X-Forwarded-Proto": "https"}
        )

    assert resp.status_code == 200
    # Scope scheme must remain as the raw transport scheme (http), not spoofed.
    assert resp.json()["scheme"] == "http"

    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_csp_nonce_unique_across_concurrent_requests() -> None:
    """Concurrent requests receive independent nonces (closes TOCTOU race)."""
    import asyncio

    app = make_app()
    nonces: list[str] = []

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        responses = await asyncio.gather(*[client.get("/probe") for _ in range(10)])

    for resp in responses:
        nonce = resp.json().get("csp_nonce")
        assert nonce is not None
        nonces.append(nonce)

    # All nonces must be distinct.
    assert len(set(nonces)) == len(nonces), "Duplicate nonces detected — TOCTOU race"


@pytest.mark.asyncio
async def test_security_headers_on_non_200_response() -> None:
    """Security headers are injected even on error responses."""
    app = FastAPI()
    app.add_middleware(SecurityMiddleware)

    @app.get("/boom")
    async def boom() -> JSONResponse:
        return JSONResponse({"error": "not found"}, status_code=404)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/boom")

    assert resp.status_code == 404
    assert "strict-transport-security" in resp.headers
    assert "content-security-policy" in resp.headers


# ---------------------------------------------------------------------------
# P9b Item C — Permissions-Policy header (S-010)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_permissions_policy_header_present() -> None:
    """Permissions-Policy header is present and contains microphone=(self).

    Closes P8 DEFERRED §17 S-010.
    """
    app = make_app()
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/probe")

    assert resp.status_code == 200
    pp = resp.headers.get("permissions-policy", "")
    assert pp != "", "Permissions-Policy header is missing"
    assert "microphone=(self)" in pp
    assert "camera=()" in pp
    assert "geolocation=()" in pp


# ---------------------------------------------------------------------------
# non-ASCII X-Forwarded-Proto coverage
# ---------------------------------------------------------------------------


def test_apply_forwarded_proto_non_ascii_header_silently_ignored() -> None:
    """A non-ASCII byte sequence in X-Forwarded-Proto must not crash;
    the header is ignored and the scope's scheme stays unchanged."""
    from lmchat.middleware.security import _apply_forwarded_proto

    scope = {
        "type": "http",
        "scheme": "http",
        "headers": [(b"x-forwarded-proto", b"\xff\xfe")],
    }
    # Should not raise; scheme stays "http".
    _apply_forwarded_proto(scope)
    assert scope["scheme"] == "http"
