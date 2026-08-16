# SPDX-License-Identifier: Apache-2.0
"""LM Studio live-reachability health endpoint.

Route
-----
GET /api/lmstudio/health — require_user; returns a live reachability
  snapshot sourced from the 5-second-TTL probe that ModelsService already
  runs for ``resolve_to_loaded_or_fallback``.  The FE topbar status badge
  polls this endpoint every 10 s to detect LM Studio going down without
  waiting for the 30-minute catalog-refresh cycle.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from lmchat.routes._dependencies import (
    get_models_service_dep,
    require_user,
)
from lmchat.services.auth_service import User
from lmchat.services.models_service import ModelsService

router: APIRouter = APIRouter(prefix="/api", tags=["lmstudio-health"])


@router.get(
    "/lmstudio/health",
    summary="Live LM Studio reachability probe",
    description=(
        "Returns a real-time reachability snapshot from the ModelsService "
        "30-second-TTL probe.  ``reachable`` is ``false`` when LM Studio "
        "is not responding (connection refused / timeout); ``auth_failed`` "
        "is ``true`` when the upstream returns 401.  Rate-limited by a "
        "30-second TTL dedicated to this endpoint (independent of the "
        "5-second TTL chat turns use to resolve a loaded model) — the FE "
        "badge polls every 10 s, so at most 1 in 3 polls reaches LM "
        "Studio; this endpoint does NOT hammer LM Studio."
    ),
)
async def get_lmstudio_health(
    _user: Annotated[User, Depends(require_user)],
    models_service: Annotated[ModelsService, Depends(get_models_service_dep)],
) -> dict[str, Any]:
    """Return the live reachability state of the configured LM Studio instance.

    Callers (the FE badge) poll every 10 s; this endpoint's own 30-second
    TTL means only about 1 in 3 polls triggers a fresh upstream probe —
    the rest are served from the cached probe result.

    Returns:
        A dict with keys ``reachable`` (bool), ``loaded_count`` (int),
        ``auth_failed`` (bool), and ``last_probe_at`` (float | null).
    """
    return await models_service.live_health()
