# SPDX-License-Identifier: Apache-2.0
"""Regression test: CSP header contains default-src and all required directives.

ZAP rule 10055 — Content-Security-Policy Missing default-src.
This file is the authoritative regression guard: if ``default-src`` is ever
removed from ``_CSP_TEMPLATE`` this test will catch it before ZAP does.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from httpx import ASGITransport

from lmchat.middleware.security import SecurityMiddleware


def _make_app() -> FastAPI:
    """Minimal FastAPI app with SecurityMiddleware for CSP header tests."""
    app = FastAPI()
    app.add_middleware(SecurityMiddleware)

    @app.get("/healthz")
    async def healthz() -> PlainTextResponse:
        return PlainTextResponse("ok")

    return app


@pytest.mark.asyncio
async def test_csp_contains_default_src() -> None:
    """CSP header must contain ``default-src 'self'`` as required by ZAP rule 10055."""
    app = _make_app()
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/healthz")

    assert resp.status_code == 200
    csp = resp.headers["content-security-policy"]
    assert "default-src 'self'" in csp, (
        f"CSP is missing 'default-src' directive (ZAP rule 10055). Got: {csp!r}"
    )


@pytest.mark.asyncio
async def test_csp_default_src_is_first_directive() -> None:
    """``default-src`` must be the first directive so it acts as the fallback."""
    app = _make_app()
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/healthz")

    csp = resp.headers["content-security-policy"]
    first_directive = csp.split(";")[0].strip()
    assert first_directive == "default-src 'self'", (
        f"Expected 'default-src 'self'' as first CSP directive, got: {first_directive!r}"
    )


@pytest.mark.asyncio
async def test_csp_all_required_directives_present() -> None:
    """All directives that were present before the default-src fix remain intact."""
    app = _make_app()
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/healthz")

    csp = resp.headers["content-security-policy"]

    expected_directives = [
        "default-src 'self'",
        "script-src 'self'",
        # Task #182: 'unsafe-inline' removed from style-src — @keyframes migrated
        # to globals.css, eliminating all component-level <style> blocks.
        "style-src 'self'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "form-action 'self'",
        "base-uri 'self'",
        "frame-ancestors 'none'",
    ]
    for directive in expected_directives:
        assert directive in csp, (
            f"CSP is missing required directive {directive!r}. Got: {csp!r}"
        )


@pytest.mark.asyncio
async def test_csp_style_src_no_unsafe_inline() -> None:
    """style-src must NOT contain 'unsafe-inline' (ZAP rule 10055 — task #182).

    All @keyframes were migrated from component <style> blocks to globals.css.
    """
    app = _make_app()
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/healthz")

    csp = resp.headers["content-security-policy"]
    assert "style-src 'self'" in csp, (
        f"CSP must contain 'style-src 'self''. Got: {csp!r}"
    )
    # Extract the style-src directive value to check it specifically.
    directives = {d.strip().split()[0]: d.strip() for d in csp.split(";")}
    style_directive = directives.get("style-src", "")
    assert "'unsafe-inline'" not in style_directive, (
        f"style-src must not contain 'unsafe-inline' (ZAP 10055). Got: {style_directive!r}"
    )


@pytest.mark.asyncio
async def test_csp_nonce_included_in_script_src() -> None:
    """script-src must carry the per-request nonce (prevents inline-script fallback)."""
    app = _make_app()
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/healthz")

    csp = resp.headers["content-security-policy"]
    assert "nonce-" in csp, (
        f"CSP script-src is missing per-request nonce. Got: {csp!r}"
    )
