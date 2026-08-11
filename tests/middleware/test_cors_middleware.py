# SPDX-License-Identifier: Apache-2.0
"""Tests for CorsMiddleware.

CORS middleware tests.

Test architecture
-----------------
We build a minimal FastAPI app with ``CorsMiddleware`` mounted and
drive it through ``httpx.AsyncClient(transport=ASGITransport(...))``.
The ``cors_allow_origins`` setting is injected via monkeypatch so each
test runs with an isolated configuration.

Pre-flight requests use ``OPTIONS`` with ``Origin``,
``Access-Control-Request-Method``, and ``Access-Control-Request-Headers``
headers as required by the CORS spec.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport

from lmchat.middleware.cors import CorsMiddleware

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_app() -> FastAPI:
    """Build a minimal FastAPI app with CorsMiddleware."""
    app = FastAPI()
    app.add_middleware(CorsMiddleware)

    @app.get("/probe")
    async def probe() -> JSONResponse:
        return JSONResponse({"ok": True})

    return app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cors_pre_flight_allowed_for_configured_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-flight request from a configured origin receives CORS allow headers."""
    from lmchat.config import get_settings

    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://allowed.example.com")
    get_settings.cache_clear()

    app = make_app()
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.options(
            "/probe",
            headers={
                "Origin": "https://allowed.example.com",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Content-Type",
            },
        )

    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "https://allowed.example.com"

    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_cors_pre_flight_blocked_for_other_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-flight request from an un-listed origin does NOT receive CORS allow headers."""
    from lmchat.config import get_settings

    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://allowed.example.com")
    get_settings.cache_clear()

    app = make_app()
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.options(
            "/probe",
            headers={
                "Origin": "https://evil.example.com",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Content-Type",
            },
        )

    # Starlette's CORSMiddleware returns 400 for disallowed pre-flight origins.
    assert "access-control-allow-origin" not in resp.headers

    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_cors_empty_allow_list_blocks_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When cors_allow_origins is empty, no CORS headers are added to any response."""
    from lmchat.config import get_settings

    # Ensure the list is empty.
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "")
    get_settings.cache_clear()

    app = make_app()
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.options(
            "/probe",
            headers={
                "Origin": "https://attacker.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )

    # No CORS headers at all — the middleware is bypassed.
    assert "access-control-allow-origin" not in resp.headers
    assert "access-control-allow-methods" not in resp.headers

    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_cors_simple_request_returns_allow_origin_for_configured_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A simple cross-origin GET with a configured Origin gets allow-origin header."""
    from lmchat.config import get_settings

    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://spa.example.com")
    get_settings.cache_clear()

    app = make_app()
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/probe",
            headers={"Origin": "https://spa.example.com"},
        )

    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "https://spa.example.com"

    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_cors_simple_request_no_origin_header_passes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request without an Origin header always passes through and gets a 200."""
    from lmchat.config import get_settings

    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://spa.example.com")
    get_settings.cache_clear()

    app = make_app()
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/probe")

    assert resp.status_code == 200

    get_settings.cache_clear()
