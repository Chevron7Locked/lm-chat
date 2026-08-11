# SPDX-License-Identifier: Apache-2.0
"""Per-model rejected-param cache routes for lm-chat.

Routes
------
GET  /api/params/{model_id}          — debug; returns rejected params for a model.
POST /api/admin/params/invalidate    — admin-gated; clear cache for one model or all.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict

from lmchat.logging import get_logger
from lmchat.routes._dependencies import get_params_service_dep, require_user
from lmchat.security.admin import require_admin
from lmchat.services.auth_service import User
from lmchat.services.params_service import ParamsService

log = get_logger(__name__)

router: APIRouter = APIRouter(prefix="/api", tags=["params"])


class RejectedParamsResponse(BaseModel):
    """Typed response for GET /api/params/{model_id}.

    Matches the ``RejectedParamsResponse`` schema in ``docs/api/openapi.yaml``.
    """

    model_config = ConfigDict(extra="forbid")

    model_id: str
    rejected: list[str]


class InvalidateResponse(BaseModel):
    """Typed response for POST /api/admin/params/invalidate.

    Matches the ``InvalidateResponse`` schema in ``docs/api/openapi.yaml``.
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool
    model_id: str | None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/params/{model_id}", response_model=RejectedParamsResponse)
async def get_rejected_params(
    model_id: str,
    _user: User = Depends(require_user),
    svc: ParamsService = Depends(get_params_service_dep),
) -> RejectedParamsResponse:
    """Return the rejected-param set for *model_id*.

    Useful for debugging the reactive-learn-and-retry cache.
    The ``rejected`` list is sorted alphabetically for stable output.

    Args:
        model_id: The LM Studio model key (e.g. ``"qwen3-8b"``).

    Returns:
        :class:`RejectedParamsResponse` with rejected params sorted
        alphabetically. ``rejected`` is empty for models not yet in the cache.
    """
    rejected_set = await svc.get_rejected(model_id=model_id)
    return RejectedParamsResponse(model_id=model_id, rejected=sorted(rejected_set))


@router.post("/admin/params/invalidate", response_model=InvalidateResponse)
async def invalidate_params(
    model_id: str | None = Query(
        default=None,
        description=(
            "Model key to invalidate.  Omit to clear the entire cache "
            "(all models)."
        ),
    ),
    _user: User = Depends(require_admin),
    svc: ParamsService = Depends(get_params_service_dep),
) -> InvalidateResponse:
    """Clear the rejected-param cache for one model or all models.

    Admin-only.  After invalidation the cache rebuilds reactively on the
    next request that encounters a 400.

    Args:
        model_id: When provided, only this model's cache is cleared.
                  When absent, the entire cache is cleared.

    Returns:
        :class:`InvalidateResponse` confirming the operation; ``model_id``
        is the input value (``None`` means "all models cleared").
    """
    await svc.invalidate(model_id=model_id)
    log.info(
        "params_route.invalidated",
        model_id=model_id if model_id is not None else "<all>",
    )
    return InvalidateResponse(ok=True, model_id=model_id)
