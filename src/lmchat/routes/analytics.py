# SPDX-License-Identifier: Apache-2.0
"""Analytics routes for lm-chat.

Endpoints
---------
GET /api/analytics/me
    User-scoped usage stats. Auth-gated (any authenticated user).
GET /api/analytics/system
    System-wide aggregate stats. Admin-only.
"""
from __future__ import annotations

from typing import Final

from fastapi import APIRouter, Depends, Request

from lmchat.logging import get_logger
from lmchat.routes._dependencies import require_user
from lmchat.security.admin import require_admin
from lmchat.services.analytics_service import (
    AnalyticsService,
    SystemAnalytics,
    UserAnalytics,
)
from lmchat.services.auth_service import User

log = get_logger(__name__)

_HTTP_503: Final[int] = 503

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


def _get_analytics_service(request: Request) -> AnalyticsService:
    """Return ``app.state.analytics_service``; raise ``RuntimeError`` if unset."""
    svc: AnalyticsService | None = getattr(
        request.app.state, "analytics_service", None
    )
    if svc is None:
        raise RuntimeError(
            "app.state.analytics_service is not set — "
            "AnalyticsService must be initialised in the app lifespan."
        )
    return svc


@router.get("/me", response_model=UserAnalytics)
async def get_my_analytics(
    request: Request,
    user: User = Depends(require_user),
    svc: AnalyticsService = Depends(_get_analytics_service),
) -> UserAnalytics:
    """Return usage statistics for the authenticated user.

    Args:
        request: FastAPI Request.
        user:    Authenticated user.
        svc:     Injected ``AnalyticsService``.

    Returns:
        :class:`UserAnalytics` with message counts, chat count, and top models.
    """
    return await svc.user_stats(user.id)


@router.get("/system", response_model=SystemAnalytics)
async def get_system_analytics(
    request: Request,
    admin: User = Depends(require_admin),
    svc: AnalyticsService = Depends(_get_analytics_service),
) -> SystemAnalytics:
    """Return system-wide aggregate statistics. Admin-only.

    Args:
        request: FastAPI Request.
        admin:   Authenticated admin user.
        svc:     Injected ``AnalyticsService``.

    Returns:
        :class:`SystemAnalytics` with system-level counts and top models.
    """
    return await svc.system_stats()
