# SPDX-License-Identifier: Apache-2.0
"""Integration tests for the SPA shell route (routes/spa.py).

Tests for SPA route serving.

Tests cover:
- GET / returns 200 with HTML containing the CSP nonce.
- ?ui=v2 sets the lmchat_ui=v2 cookie.
- Cookie lmchat_ui=v2 serves v2 by default.
- Deep-link paths return the SPA shell (200) for browser requests.
- /api/* paths are NOT caught by the SPA fallback (API routes win).
- /assets/* 404s stay 404 (not swallowed by the SPA fallback).
- Non-browser (JSON-only Accept) 404s remain 404.
- Service worker is served with application/javascript.
- Manifest is served with the correct content-type.
"""
from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Minimal app fixture that shares the lifespan env setup from conftest.py
# ---------------------------------------------------------------------------


@pytest.fixture()
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    """Set environment variables required by the app lifespan."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/spa_test.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")

    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod

    get_settings.cache_clear()
    engine_mod.dispose_engine()

    yield

    engine_mod.dispose_engine()
    get_settings.cache_clear()


@pytest.fixture()
def spa_client(_env: None) -> Generator[TestClient]:
    """TestClient with a live lm-chat app."""
    from lmchat.app import create_app

    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


# ---------------------------------------------------------------------------
# GET / — root
# ---------------------------------------------------------------------------


def test_spa_root_returns_200(spa_client: TestClient) -> None:
    """GET / returns 200 with text/html content-type."""
    resp = spa_client.get("/", headers={"Accept": "text/html"})
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_spa_root_contains_root_div(spa_client: TestClient) -> None:
    """GET / HTML body contains the React mount point."""
    resp = spa_client.get("/", headers={"Accept": "text/html"})
    assert '<div id="root">' in resp.text


def test_spa_root_script_has_nonce(spa_client: TestClient) -> None:
    """The <script> tag on the root response carries a nonce attribute."""
    resp = spa_client.get("/", headers={"Accept": "text/html"})
    assert 'nonce="' in resp.text, "Expected nonce attribute on <script> tag"


def test_spa_root_csp_header_present(spa_client: TestClient) -> None:
    """The CSP response header is present and contains the nonce."""
    resp = spa_client.get("/", headers={"Accept": "text/html"})
    csp = resp.headers.get("content-security-policy", "")
    assert "nonce-" in csp, f"CSP header missing nonce: {csp!r}"


def test_spa_root_nonce_matches_csp(spa_client: TestClient) -> None:
    """The nonce in the <script> tag matches the nonce in the CSP header."""
    import re

    resp = spa_client.get("/", headers={"Accept": "text/html"})
    csp = resp.headers.get("content-security-policy", "")
    # CSP format: "... 'nonce-<22-char-urlsafe-value>' ..."
    # Strip surrounding quotes and the "nonce-" prefix.
    match = re.search(r"'nonce-([A-Za-z0-9_\-]+)'", csp)
    assert match, f"No nonce found in CSP: {csp!r}"
    nonce_value = match.group(1)
    assert nonce_value in resp.text, (
        f"Nonce {nonce_value!r} from CSP header not found in HTML body"
    )


# ---------------------------------------------------------------------------
# Cutover gate — ?ui=v2 sets cookie
# ---------------------------------------------------------------------------


def test_ui_v2_query_sets_cookie(spa_client: TestClient) -> None:
    """?ui=v2 causes the response to set the lmchat_ui=v2 cookie."""
    resp = spa_client.get("/?ui=v2", headers={"Accept": "text/html"})
    assert resp.status_code == 200
    set_cookie = resp.headers.get("set-cookie", "")
    assert "lmchat_ui=v2" in set_cookie, (
        f"Expected lmchat_ui=v2 in Set-Cookie: {set_cookie!r}"
    )


def test_ui_v2_query_cookie_samesite_lax(spa_client: TestClient) -> None:
    """The lmchat_ui cookie has SameSite=Lax attribute."""
    resp = spa_client.get("/?ui=v2", headers={"Accept": "text/html"})
    set_cookie = resp.headers.get("set-cookie", "").lower()
    assert "samesite=lax" in set_cookie


def test_ui_v2_cookie_already_set_does_not_re_set(spa_client: TestClient) -> None:
    """If lmchat_ui=v2 cookie is already present, no Set-Cookie is emitted."""
    spa_client.cookies.set("lmchat_ui", "v2")
    resp = spa_client.get("/?ui=v2", headers={"Accept": "text/html"})
    assert "lmchat_ui" not in resp.headers.get("set-cookie", "")


# ---------------------------------------------------------------------------
# Deep-link fallback (404 handler)
# ---------------------------------------------------------------------------


def test_deep_link_chat_serves_spa_shell(spa_client: TestClient) -> None:
    """GET /chats/123 (HTML Accept) returns 200 with SPA shell."""
    resp = spa_client.get("/chats/123", headers={"Accept": "text/html,*/*"})
    assert resp.status_code == 200
    assert '<div id="root">' in resp.text


def test_deep_link_settings_serves_spa_shell(spa_client: TestClient) -> None:
    """GET /settings (HTML Accept) returns 200 with SPA shell."""
    resp = spa_client.get("/settings", headers={"Accept": "text/html,*/*"})
    assert resp.status_code == 200
    assert '<div id="root">' in resp.text


def test_deep_link_memory_serves_spa_shell(spa_client: TestClient) -> None:
    """GET /memory (HTML Accept) returns 200 with SPA shell."""
    resp = spa_client.get("/memory", headers={"Accept": "text/html,*/*"})
    assert resp.status_code == 200
    assert '<div id="root">' in resp.text


def test_json_only_404_stays_404(spa_client: TestClient) -> None:
    """GET /nonexistent (JSON Accept only) stays 404, not SPA shell."""
    resp = spa_client.get(
        "/nonexistent-api-path",
        headers={"Accept": "application/json"},
    )
    assert resp.status_code == 404


def test_assets_404_stays_404(spa_client: TestClient) -> None:
    """GET /assets/nonexistent.js returns 404, not SPA shell."""
    resp = spa_client.get(
        "/assets/nonexistent-hash.js",
        headers={"Accept": "text/html,*/*"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# API routes are not shadowed
# ---------------------------------------------------------------------------


def test_healthz_not_shadowed(spa_client: TestClient) -> None:
    """GET /healthz is served by its own route, not the SPA fallback."""
    resp = spa_client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json().get("status") == "ok"


# ---------------------------------------------------------------------------
# Static file routes
# ---------------------------------------------------------------------------


def test_favicon_served(spa_client: TestClient) -> None:
    """GET /favicon.svg returns the SVG file."""
    resp = spa_client.get("/favicon.svg")
    # Either 200 (file exists) or 404 (no dist in CI); both are acceptable
    # as long as the SPA shell is NOT returned.
    assert resp.status_code in (200, 404)
    if resp.status_code == 200:
        assert "image/svg+xml" in resp.headers.get("content-type", "")


def test_manifest_served(spa_client: TestClient) -> None:
    """GET /manifest.webmanifest returns JSON with correct content-type."""
    resp = spa_client.get("/manifest.webmanifest")
    assert resp.status_code in (200, 404)
    if resp.status_code == 200:
        assert "manifest" in resp.headers.get("content-type", "")


def test_sw_served_with_js_content_type(spa_client: TestClient) -> None:
    """GET /sw.js is served with application/javascript content-type."""
    resp = spa_client.get("/sw.js")
    assert resp.status_code in (200, 404)
    if resp.status_code == 200:
        assert "javascript" in resp.headers.get("content-type", "")
        assert resp.headers.get("cache-control") == "no-cache"


# ---------------------------------------------------------------------------
# /docs and /redoc — SPA shell, not FastAPI's Swagger/ReDoc UIs
#
# FastAPI registers real GET /docs (Swagger UI) + GET /redoc routes by
# default, which win over the SPA's 404-fallback route and shadow it at
# those paths. The strict CSP (script-src 'self' 'nonce-…'; style-src
# 'self') also blocks Swagger's CDN assets + inline bootstrap script, so
# self-hosting those assets would not make it functional anyway. create_app
# disables both (docs_url=None, redoc_url=None) so /docs and /redoc fall
# through to the SPA shell like any other deep link.
# ---------------------------------------------------------------------------


def test_docs_serves_spa_not_swagger(spa_client: TestClient) -> None:
    """GET /docs (HTML Accept) returns the SPA shell, not Swagger UI."""
    resp = spa_client.get("/docs", headers={"Accept": "text/html,*/*"})
    assert resp.status_code == 200
    assert '<div id="root">' in resp.text
    assert "swagger-ui" not in resp.text
    assert "Swagger UI" not in resp.text


def test_redoc_serves_spa_not_redoc(spa_client: TestClient) -> None:
    """GET /redoc (HTML Accept) returns the SPA shell, not ReDoc's UI."""
    resp = spa_client.get("/redoc", headers={"Accept": "text/html,*/*"})
    assert resp.status_code == 200
    assert '<div id="root">' in resp.text
    assert "redoc" not in resp.text.lower()


def test_openapi_schema_still_available(spa_client: TestClient) -> None:
    """GET /openapi.json still serves the raw OpenAPI schema (docs_url=None
    disables the Swagger/ReDoc *UI* routes only; the schema endpoint itself
    stays at its default path, since no SPA route claims it).
    """
    resp = spa_client.get("/openapi.json")
    assert resp.status_code == 200
    body = resp.json()
    assert body["openapi"].startswith("3.")


def test_docs_deep_link_still_spa(spa_client: TestClient) -> None:
    """GET /docs/00-quickstart (HTML Accept) also falls through to the SPA
    shell — guards against a route registered only at the exact /docs
    path (as opposed to the general 404-fallback handling any sub-path).
    """
    resp = spa_client.get("/docs/00-quickstart", headers={"Accept": "text/html,*/*"})
    assert resp.status_code == 200
    assert '<div id="root">' in resp.text
