# SPDX-License-Identifier: Apache-2.0
"""Operations-facing meta routes for lm-chat.

``/healthz`` is defined here (rather than inline in ``app.py``) and
``/readyz`` adds a readiness check.

Routes:
    GET /healthz    — liveness; always 200.
    GET /readyz     — readiness; 200 when DB + sessions pass, 503 otherwise.
                      LM Studio reachability is probed and reported in the
                      response body for observability, but it does NOT gate
                      the 200/503 status: LM Studio is an optional local
                      upstream for this local-first app (the admin closing
                      the LM Studio desktop app is normal, expected
                      operation, not an infra failure), so it must not pull
                      the pod from Service routing.
    GET /api/metrics — Prometheus text exposition.

The ``/readyz`` probe implements a best-effort 3-strike rule over the DB +
sessions checks: three *consecutive* failures of those checks are required
before the endpoint returns 503.  The strike counter is held in module-level
state so it persists across requests without requiring app.state wiring.
Resets to 0 on any cycle where DB + sessions both pass.
"""
from __future__ import annotations

import asyncio
from typing import Final

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import select, text

from lmchat import __version__
from lmchat.config import get_settings
from lmchat.db.engine import get_engine
from lmchat.db.schema import sessions as sessions_table
from lmchat.logging import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROBE_TIMEOUT: Final[float] = 3.0  # seconds per upstream probe
_STRIKE_THRESHOLD: Final[int] = 3   # consecutive failures before 503

# ---------------------------------------------------------------------------
# 3-strike state (module-level; reset on any success)
# ---------------------------------------------------------------------------

_strike_count: int = 0


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router: APIRouter = APIRouter()


# ---------------------------------------------------------------------------
# GET /healthz  — liveness
# ---------------------------------------------------------------------------


@router.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    """Liveness probe — always returns 200 with the package version.

    Container orchestrators use this to decide whether to restart the
    process.  It MUST NOT block on any external dependency.

    Returns:
        ``{"status": "ok", "version": <version>}``
    """
    return {"status": "ok", "version": __version__}


# ---------------------------------------------------------------------------
# GET /readyz  — readiness
# ---------------------------------------------------------------------------


async def _probe_db() -> bool:
    """Return True if the database responds to ``SELECT 1``.

    Uses a 3-second timeout on the query.  Connection errors and
    unexpected exceptions both return False.
    """
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            await asyncio.wait_for(
                conn.execute(text("SELECT 1")),
                timeout=_PROBE_TIMEOUT,
            )
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("readyz.db_probe_failed", error=str(exc))
        return False


async def _probe_sessions() -> bool:
    """Return True if the sessions table is queryable.

    Executes ``SELECT COUNT(*) FROM sessions`` as a session-store
    readiness proxy.  In v1.0, sessions live in the same DB as all
    other tables, so this is effectively a second DB
    health signal; it is kept separate for observability.
    """
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            await asyncio.wait_for(
                conn.execute(select(sessions_table).limit(0)),
                timeout=_PROBE_TIMEOUT,
            )
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("readyz.sessions_probe_failed", error=str(exc))
        return False


async def _probe_lm_studio() -> bool:
    """Return True if LM Studio's ``/api/v1/models`` responds with HTTP 200.

    Uses a separate short-lived ``httpx.AsyncClient`` so this probe does
    NOT share the application's persistent connection pool.  This avoids
    tying up a pooled connection for a health-check request that only fires
    a few times per minute (Kubernetes scrape interval).
    """
    settings = get_settings()
    base = settings.lm_studio_base_url.rstrip("/")
    url = f"{base}/api/v1/models"
    headers: dict[str, str] = {}
    if settings.lm_studio_api_key:
        headers["Authorization"] = f"Bearer {settings.lm_studio_api_key}"
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as client:
            resp = await client.get(url, headers=headers)
        return resp.status_code == 200
    except Exception as exc:  # noqa: BLE001
        log.warning("readyz.lm_studio_probe_failed", error=str(exc))
        return False


@router.get("/readyz", include_in_schema=False)
async def readyz() -> Response:
    """Readiness probe — returns 200 when this instance can serve HTTP.

    Probes:
    1. **DB**: ``SELECT 1`` must succeed within 3 s.
    2. **Sessions**: ``SELECT ... FROM sessions LIMIT 0`` must succeed
       within 3 s.
    3. **LM Studio**: ``GET /api/v1/models`` must return HTTP 200 within 3 s.
       Probed and reported in ``checks.lm_studio`` for observability, but it
       does NOT affect the 200/503 status — LM Studio is an optional local
       upstream, not a signal that this instance can't serve HTTP.  The
       admin closing the LM Studio desktop app is normal, expected
       operation for this local-first app.

    **3-strike rule** (DB + sessions only): three consecutive cycles where
    DB or sessions fails are required before the endpoint returns 503.  A
    single transient hiccup does not immediately flip the endpoint.  On any
    cycle where DB + sessions both pass, the strike counter resets to zero
    (regardless of the LM Studio result).

    Returns:
        ``200 {"status": "ok", "version": ..., "checks": {...}}`` when DB +
        sessions are healthy.  If LM Studio is unreachable, the response
        also includes ``"degraded": ["lm_studio"]`` while still returning
        200.
        ``503 {"status": "not_ready", "checks": {...}}`` when DB or
        sessions has failed 3 consecutive cycles.
    """
    global _strike_count  # noqa: PLW0603

    db_ok, sessions_ok, lm_ok = await asyncio.gather(
        _probe_db(),
        _probe_sessions(),
        _probe_lm_studio(),
    )

    checks = {"db": db_ok, "sessions": sessions_ok, "lm_studio": lm_ok}
    infra_ok = db_ok and sessions_ok

    if infra_ok:
        _strike_count = 0
        content: dict[str, object] = {
            "status": "ok",
            "version": __version__,
            "checks": checks,
        }
        if not lm_ok:
            # LM Studio down is expected/normal for this local-first app
            # (e.g. the admin closed the desktop app) — report it as
            # degraded, but never fail readiness over it.
            content["degraded"] = ["lm_studio"]
        return JSONResponse(status_code=200, content=content)

    _strike_count += 1
    log.warning(
        "readyz.probe_failed",
        checks=checks,
        strike_count=_strike_count,
        threshold=_STRIKE_THRESHOLD,
    )

    if _strike_count < _STRIKE_THRESHOLD:
        # Fewer than 3 consecutive failures — still report ready to absorb
        # transient blips without triggering a pod restart.
        return JSONResponse(
            status_code=200,
            content={
                "status": "ok",
                "version": __version__,
                "checks": checks,
                "warning": f"probe failed (strike {_strike_count}/{_STRIKE_THRESHOLD})",
            },
        )

    return JSONResponse(
        status_code=503,
        content={
            "status": "not_ready",
            "checks": checks,
            "strike_count": _strike_count,
        },
    )


# ---------------------------------------------------------------------------
# GET /api/metrics  — Prometheus
# ---------------------------------------------------------------------------


@router.get("/api/metrics", include_in_schema=False)
async def metrics_endpoint() -> Response:
    """Render the current Prometheus default registry.

    Returns the wire-format exposition with the canonical
    ``CONTENT_TYPE_LATEST`` media type.  No authentication is applied at
    this layer — admins are expected to gate ``/api/metrics`` at the
    reverse proxy per the deploy configs.
    """
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )
