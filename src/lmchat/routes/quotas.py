# SPDX-License-Identifier: Apache-2.0
"""Quota routes for lm-chat.

Routes
------
GET  /api/quotas/me                — current user quota + today's usage.
GET  /api/admin/quotas             — list[QuotaSummary] (admin-only).
PATCH /api/admin/quotas/{user_id}  — set a user's quota (admin-only).
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from fastapi import APIRouter, Depends, Form, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncEngine

from lmchat.logging import get_logger
from lmchat.routes._dependencies import (
    get_engine_dep,
    require_admin,
    require_user,
)
from lmchat.services.auth_service import User
from lmchat.services.quota_service import (
    get_all_quotas,
    get_quota,
    get_usage,
    set_quota,
)
from lmchat.utils.clock import utc_today

log = get_logger(__name__)

router = APIRouter()

_HTTP_200: Final[int] = 200
_HTTP_404: Final[int] = 404

# ---------------------------------------------------------------------------
# Response shapes
# ---------------------------------------------------------------------------


class QuotaResponse(BaseModel):
    """Quota + today's usage for the current user."""

    model_config = ConfigDict(from_attributes=False)

    tokens_per_day: int
    requests_per_day: int
    tokens_consumed_today: int
    requests_consumed_today: int
    resets_at: str  # ISO-8601 midnight UTC


class QuotaSummary(BaseModel):
    """Admin view of a single user's quota limits."""

    model_config = ConfigDict(from_attributes=False)

    user_id: int
    tokens_per_day: int
    requests_per_day: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _next_midnight_iso() -> str:
    """Return the next UTC midnight as an ISO-8601 string, e.g.
    ``"2026-05-21T00:00:00+00:00"``.
    """
    from datetime import timedelta

    now = datetime.now(tz=UTC)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
        days=1
    )
    return midnight.isoformat()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/api/quotas/me", response_model=QuotaResponse)
async def get_my_quota(
    user: User = Depends(require_user),
    engine: AsyncEngine = Depends(get_engine_dep),
) -> QuotaResponse:
    """Return the current user's quota limits and today's usage.

    Admins aren't quota-enforced at request time (see ``QuotaMiddleware``)
    but their usage row is still tracked for observability.
    """
    user_id: int = user.id  # type: ignore[attr-defined]
    quota = await get_quota(user_id, engine)
    # quota_usage is keyed by UTC calendar day — use utc_today(), not the
    # host-local date, so "today" matches what the write side stamped.
    usage = await get_usage(user_id, utc_today(), engine)
    return QuotaResponse(
        tokens_per_day=quota.tokens_per_day,
        requests_per_day=quota.requests_per_day,
        tokens_consumed_today=usage.tokens_consumed,
        requests_consumed_today=usage.requests_consumed,
        resets_at=_next_midnight_iso(),
    )


@router.get("/api/admin/quotas", response_model=list[QuotaSummary])
async def list_quotas(
    admin: User = Depends(require_admin),
    engine: AsyncEngine = Depends(get_engine_dep),
) -> list[QuotaSummary]:
    """List quota rows for all users that have an explicit quota row.

    Users without one use the system defaults (100 000 tokens/day, 1 000
    requests/day) and won't appear here.
    """
    rows = await get_all_quotas(engine)
    return [
        QuotaSummary(
            user_id=r.user_id,
            tokens_per_day=r.tokens_per_day,
            requests_per_day=r.requests_per_day,
        )
        for r in rows
    ]


@router.patch("/api/admin/quotas/{user_id}", response_model=QuotaSummary)
async def update_quota(
    user_id: int,
    tokens_per_day: int = Form(..., ge=0),
    requests_per_day: int = Form(..., ge=0),
    admin: User = Depends(require_admin),
    engine: AsyncEngine = Depends(get_engine_dep),
    request: Request = None,  # type: ignore[assignment]
) -> QuotaSummary:
    """Admin upsert for a user's quota limits.

    ``request`` is unused but required by the dependency-injection
    signature.
    """
    updated = await set_quota(
        user_id=user_id,
        tokens_per_day=tokens_per_day,
        requests_per_day=requests_per_day,
        engine=engine,
    )
    log.info(
        "quota.admin_update",
        target_user_id=user_id,
        admin_id=admin.id,  # type: ignore[attr-defined]
        tokens_per_day=tokens_per_day,
        requests_per_day=requests_per_day,
    )
    return QuotaSummary(
        user_id=updated.user_id,
        tokens_per_day=updated.tokens_per_day,
        requests_per_day=updated.requests_per_day,
    )
