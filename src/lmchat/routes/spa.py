# SPDX-License-Identifier: Apache-2.0
"""SPA shell route for lm-chat.

Serves the React SPA shell on all non-API, non-asset paths so that
React Router's HTML5 history mode works for deep links (e.g.
``/chats/abc``, ``/settings``).

The route is intentionally mounted last in ``app.py`` so it acts as a
catch-all that does not shadow any ``/api/*`` or ``/assets/*`` path.

CSP nonce
---------
The per-request nonce is available on ``request.state.csp_nonce``
(set by ``middleware/security.py`` before the route handler runs).
The Jinja2 template embeds it on the ``<script>`` tag so the CSP
``script-src 'self' 'nonce-<value>'`` directive passes without
``'unsafe-inline'``.

Manifest-driven asset injection
--------------------------------
Vite's ``build.manifest: true`` emits ``dist/.vite/manifest.json``.
``spa.py`` reads the manifest once at startup (via the ``lifespan``
or on first request — a simple module-level lazy singleton is used)
to resolve the hashed entry script and CSS paths.  This avoids
hardcoding filenames that change with every build.

Cutover gate (priority order)
------------------------------
1. Cookie ``lmchat_ui=v2`` — primary, persists across navigations.
2. Query param ``?ui=v2`` — sets the cookie then serves v2.
3. Default — always serves v2 (this repo has no legacy SPA mount).

Cookie attributes
-----------------
``SameSite=Lax``, ``Max-Age=2592000`` (30 days).
``HttpOnly=false`` so the SPA can read its own version cookie.
``Secure`` is set when the request arrives over HTTPS.

Static files
------------
``/assets/*``  — served by FastAPI ``StaticFiles`` (wired in app.py).
``/favicon.svg``, ``/manifest.webmanifest``, ``/sw.js``  — likewise.
This route only handles the HTML shell.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jinja2
from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from lmchat.logging import get_logger

_log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Paths — relative to the *installed package* tree.
# The templates dir lives alongside this file in the source tree; the
# dist dir is resolved relative to the repo root (two levels up from here
# at src/lmchat/routes/).
# ---------------------------------------------------------------------------

_HERE = Path(__file__).parent
_TEMPLATES_DIR = _HERE.parent / "templates"
_WEB_DIST = _HERE.parent.parent.parent / "web" / "dist"
_MANIFEST_PATH = _WEB_DIST / ".vite" / "manifest.json"

# Construct an explicit Jinja2 Environment so the autoescape security
# property is locally auditable at the call site.  Starlette's default
# Environment already enables autoescape for .html / .htm / .xml via
# select_autoescape — this construction makes that invariant visible and
# explicit rather than relying on an implicit default.
_jinja2_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=jinja2.select_autoescape(["html", "htm", "xml"]),
)
templates = Jinja2Templates(env=_jinja2_env)

router = APIRouter(
    tags=["spa"],
    include_in_schema=False,
)

# ---------------------------------------------------------------------------
# Manifest singleton — loaded once, cached in process memory.
# ---------------------------------------------------------------------------

_manifest_cache: dict[str, Any] = {}
_manifest_loaded: bool = False


def _load_manifest() -> dict[str, Any]:
    """Read and return the Vite manifest, cached after the first load.

    Returns an empty dict if the manifest file is absent (dev-only
    scenario where ``web/dist`` has not been built yet).  Routes will
    fall through to the dev server in that case.
    """
    global _manifest_cache, _manifest_loaded  # noqa: PLW0603
    if _manifest_loaded:
        return _manifest_cache
    if not _MANIFEST_PATH.exists():
        _log.warning(
            "spa.manifest_missing",
            path=str(_MANIFEST_PATH),
            note="Run `pnpm build` in web/ to generate the Vite manifest.",
        )
        _manifest_loaded = True
        return _manifest_cache
    with _MANIFEST_PATH.open() as fh:
        _manifest_cache = json.load(fh)
    _log.info(
        "spa.manifest_loaded",
        path=str(_MANIFEST_PATH),
        entries=len(_manifest_cache),
    )
    _manifest_loaded = True
    return _manifest_cache


def _resolve_entry(manifest: dict[str, Any]) -> tuple[str, str]:
    """Return ``(entry_script_path, entry_css_path)`` from the manifest.

    The entry chunk has ``"isEntry": true`` and ``"src": "index.html"``.
    Falls back to empty strings when the manifest is missing so the
    template renders without crashing (useful in dev).
    """
    entry = manifest.get("index.html", {})
    script_file = entry.get("file", "")
    css_files = entry.get("css", [])
    css_file = css_files[0] if css_files else ""
    script_path = f"/assets/{Path(script_file).name}" if script_file else ""
    css_path = f"/assets/{Path(css_file).name}" if css_file else ""
    return script_path, css_path


# ---------------------------------------------------------------------------
# Cookie helpers
# ---------------------------------------------------------------------------

_COOKIE_NAME = "lmchat_ui"
_COOKIE_VALUE = "v2"
_COOKIE_MAX_AGE = 2_592_000  # 30 days


def _set_ui_cookie(response: Response, *, secure: bool) -> None:
    """Attach the ``lmchat_ui=v2`` cookie to *response*."""
    response.set_cookie(
        key=_COOKIE_NAME,
        value=_COOKIE_VALUE,
        max_age=_COOKIE_MAX_AGE,
        httponly=False,
        samesite="lax",
        secure=secure,
    )


# ---------------------------------------------------------------------------
# SPA shell endpoint
# ---------------------------------------------------------------------------


def _serve_spa(request: Request) -> HTMLResponse:
    """Render and return the SPA HTML shell.

    Args:
        request: The incoming FastAPI/Starlette request.

    Returns:
        An :class:`~fastapi.responses.HTMLResponse` with the rendered
        Jinja2 template.  The ``Content-Type`` is set to
        ``text/html; charset=utf-8``.
    """
    nonce: str = getattr(request.state, "csp_nonce", "")
    manifest = _load_manifest()
    entry_script_path, entry_css_path = _resolve_entry(manifest)

    context: dict[str, Any] = {
        "csp_nonce": nonce,
        "entry_script_path": entry_script_path,
        "entry_css_path": entry_css_path,
    }
    # Starlette >= 0.36 uses the new-style signature:
    #   TemplateResponse(request, name, context)
    # The ``request`` is no longer injected via the context dict.
    html_response = templates.TemplateResponse(
        request=request,
        name="spa.html",
        context=context,
        status_code=200,
    )
    return html_response  # type: ignore[return-value]


@router.get("/favicon.svg")
async def favicon() -> Response:
    """Serve the SVG favicon directly from the Vite dist directory."""
    path = _WEB_DIST / "favicon.svg"
    if path.exists():
        return FileResponse(str(path), media_type="image/svg+xml")
    return Response(status_code=404)


@router.get("/manifest.webmanifest")
async def webmanifest() -> Response:
    """Serve the PWA web app manifest."""
    path = _WEB_DIST / "manifest.webmanifest"
    if path.exists():
        return FileResponse(str(path), media_type="application/manifest+json")
    return Response(status_code=404)


@router.get("/sw.js")
async def service_worker() -> Response:
    """Serve the service worker script.

    The service worker MUST be served with ``Content-Type:
    application/javascript`` from the same origin it controls.  It is
    intentionally excluded from the CSP nonce requirement — the browser
    validates the SW registration URL, not a nonce attribute.
    """
    path = _WEB_DIST / "sw.js"
    if path.exists():
        return FileResponse(
            str(path),
            media_type="application/javascript",
            headers={"Cache-Control": "no-cache"},
        )
    return Response(status_code=404)


@router.get("/")
async def spa_root(request: Request) -> Response:
    """Serve the React SPA shell on ``GET /``.

    Cutover gate:

    1. Cookie ``lmchat_ui=v2`` → serve v2.
    2. Query param ``?ui=v2`` → set cookie + serve v2.
    3. Default → serve v2 (no legacy SPA in this repo).
    """
    is_https = request.url.scheme == "https"
    ui_param = request.query_params.get("ui")
    ui_cookie = request.cookies.get(_COOKIE_NAME)

    response = _serve_spa(request)

    if ui_param == _COOKIE_VALUE and ui_cookie != _COOKIE_VALUE:
        # Param-triggered cutover: set the sticky cookie.
        _set_ui_cookie(response, secure=is_https)

    return response


def serve_spa_for_request(request: Request) -> Response:
    """Public helper for the 404 handler in app.py.

    Called from ``app.add_exception_handler(404, ...)`` when a browser
    navigation hits a path that has no registered route (e.g.
    ``/chats/123``, ``/settings``, ``/memory``).  Returning 200 with
    the SPA shell is correct here — React Router reads the path
    client-side and renders the right page.

    Only fires for HTML-accepting requests (browser navigations).
    JSON-only callers receive the original 404 response unchanged so
    API clients are not confused.
    """
    accept = request.headers.get("accept", "")
    if "text/html" not in accept:
        # Not a browser navigation; pass through the 404.
        return Response(status_code=404)

    # Let /assets/* 404s propagate — they are real missing-file errors.
    if request.url.path.startswith("/assets/"):
        return Response(status_code=404)

    is_https = request.url.scheme == "https"
    ui_param = request.query_params.get("ui")
    ui_cookie = request.cookies.get(_COOKIE_NAME)

    response = _serve_spa(request)

    if ui_param == _COOKIE_VALUE and ui_cookie != _COOKIE_VALUE:
        _set_ui_cookie(response, secure=is_https)

    return response
