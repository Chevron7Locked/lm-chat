# SPDX-License-Identifier: Apache-2.0
"""Model-list, capability, and lifecycle routes for lm-chat.

Routes
------
GET  /api/models                — list all models known to LM Studio.
POST /api/admin/models/refresh  — admin-gated; re-probe LM Studio.
POST /api/models/load           — admin-only; load a model instance.
POST /api/models/unload         — admin-only; unload one or all instances.
POST /api/models/download       — admin-only; download a model from hub.
"""
from __future__ import annotations

from typing import Annotated, Final

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncEngine

from lmchat.logging import get_logger
from lmchat.routes._dependencies import (
    admin_rate_limit,
    get_engine_dep,
    get_models_service_dep,
    require_user,
)
from lmchat.security.admin import require_admin
from lmchat.services.auth_service import User
from lmchat.services.models_service import (
    ModelInfo,
    ModelsService,
    ModelUnloadResult,
    UnloadAllResult,
    UpstreamGatewayError,
    UpstreamModelError,
)

log = get_logger(__name__)

router: APIRouter = APIRouter(prefix="/api", tags=["models"])

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_HTTP_400: Final[int] = 400
_HTTP_429: Final[int] = 429
_HTTP_502: Final[int] = 502

_DEFAULT_MODEL_ADMIN_OPS_PER_DAY: Final[int] = 1000


# ---------------------------------------------------------------------------
# Response shapes
# ---------------------------------------------------------------------------


class RefreshModelsResponse(BaseModel):
    """Typed response for POST /api/admin/models/refresh.

    Matches the ``RefreshModelsResponse`` schema in ``docs/api/openapi.yaml``.
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool
    model_count: int


class ModelLoadResponse(BaseModel):
    """Response for POST /api/models/load."""

    model_config = ConfigDict(extra="forbid")

    instance_id: str
    model: str
    load_time_seconds: float
    status: str


class FailedUnloadEntry(BaseModel):
    """One failed-unload entry in a best-effort unload-all response."""

    model_config = ConfigDict(extra="forbid")

    instance_id: str
    error: str


class ModelUnloadResponse(BaseModel):
    """Response for POST /api/models/unload.

    For single-instance unloads, ``failed`` is always an empty list.
    For ``all=true`` unloads, ``failed`` may contain entries for instances
    that could not be unloaded (best-effort semantics).
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool
    unloaded: list[str]
    failed: list[FailedUnloadEntry] = []


class ModelDownloadResponse(BaseModel):
    """Response for POST /api/models/download.

    Shape unverified; proxies whatever upstream returns.
    """

    model_config = ConfigDict(extra="allow")

    status: str = "ok"


# ---------------------------------------------------------------------------
# Quota helpers (model_admin_ops_per_day)
# ---------------------------------------------------------------------------


async def _ensure_ops_usage_row(user_id: int, engine: AsyncEngine) -> None:
    """Idempotently create today's quota_usage row if absent."""
    async with engine.begin() as conn:
        dialect = conn.dialect.name  # type: ignore[union-attr]
        if dialect == "sqlite":
            await conn.execute(
                sa.text(
                    "INSERT OR IGNORE INTO quota_usage "
                    "(user_id, date, tokens_consumed, requests_consumed, "
                    "model_admin_ops_consumed) "
                    "VALUES (:uid, DATE('now'), 0, 0, 0)"
                ),
                {"uid": user_id},
            )
        else:
            await conn.execute(
                sa.text(
                    "INSERT INTO quota_usage "
                    "(user_id, date, tokens_consumed, requests_consumed, "
                    "model_admin_ops_consumed) "
                    "VALUES (:uid, CURRENT_DATE, 0, 0, 0) "
                    "ON CONFLICT (user_id, date) DO NOTHING"
                ),
                {"uid": user_id},
            )


async def _consume_model_admin_op(user_id: int, engine: AsyncEngine) -> None:
    """Atomically consume one model-admin op from today's budget.

    Raises HTTPException 429 if quota exhausted.
    """
    await _ensure_ops_usage_row(user_id, engine)

    async with engine.begin() as conn:
        dialect = conn.dialect.name  # type: ignore[union-attr]
        if dialect == "sqlite":
            result = await conn.execute(
                sa.text(
                    "UPDATE quota_usage "
                    "SET model_admin_ops_consumed = model_admin_ops_consumed + 1 "
                    "WHERE user_id = :uid "
                    "  AND date = DATE('now') "
                    "  AND model_admin_ops_consumed + 1 <= COALESCE("
                    "    (SELECT model_admin_ops_per_day FROM quotas WHERE user_id = :uid), "
                    "    :default_ops"
                    "  )"
                ),
                {"uid": user_id, "default_ops": _DEFAULT_MODEL_ADMIN_OPS_PER_DAY},
            )
        else:
            result = await conn.execute(
                sa.text(
                    "UPDATE quota_usage "
                    "SET model_admin_ops_consumed = model_admin_ops_consumed + 1 "
                    "WHERE user_id = :uid "
                    "  AND date = CURRENT_DATE "
                    "  AND model_admin_ops_consumed + 1 <= COALESCE("
                    "    (SELECT model_admin_ops_per_day FROM quotas WHERE user_id = :uid), "
                    "    :default_ops"
                    "  )"
                ),
                {"uid": user_id, "default_ops": _DEFAULT_MODEL_ADMIN_OPS_PER_DAY},
            )

    if result.rowcount == 0:  # type: ignore[union-attr]
        raise HTTPException(
            status_code=_HTTP_429,
            detail="model_admin_ops daily quota exceeded",
            headers={"Retry-After": "86400"},
        )


# ---------------------------------------------------------------------------
# Error mapping helpers
# ---------------------------------------------------------------------------


def _handle_upstream_error(exc: UpstreamModelError) -> None:
    """Re-raise upstream 4xx as our 4xx with full error fidelity.

    The ``detail`` dict preserves all fields from the upstream error envelope
    so callers can inspect ``type``, ``code``, and ``param`` in addition to
    ``message``.

    Raises:
        HTTPException: Always raised with status_code from upstream.
    """
    raise HTTPException(
        status_code=exc.status_code,
        detail={
            "message": exc.message,
            "type": exc.error_type,
            "code": exc.code,
            "param": exc.param,
        },
    )


def _handle_gateway_error(exc: UpstreamGatewayError) -> None:
    """Re-raise upstream 5xx as our 502 Bad Gateway.

    Raises:
        HTTPException: Always raised as 502.
    """
    raise HTTPException(
        status_code=_HTTP_502,
        detail=f"LM Studio upstream error: {exc.detail}",
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/models", response_model=list[ModelInfo])
async def list_models(
    request: Request,
    _user: User = Depends(require_user),
    svc: ModelsService = Depends(get_models_service_dep),
) -> list[ModelInfo]:
    """Return all models known to LM Studio and enabled cloud providers.

    When no cloud providers are configured or enabled, the result is
    identical to the legacy LM-Studio-only path (regression-safe).

    Reads from the per-provider in-memory cache (TTL ≥ 60 s); cloud
    providers are fetched in the background if the cache is cold or
    stale.  An unreachable cloud provider degrades gracefully — its
    models are omitted and its status is reported by
    ``GET /api/providers/status``.

    Use ``POST /api/admin/models/refresh`` to force a re-probe of LM
    Studio.  Invalidation of cloud caches happens on admin provider
    PUT/DELETE (registry refresh).

    Returns:
        Merged list of :class:`~lmchat.services.models_service.ModelInfo`
        objects — LM Studio first, then cloud providers in sorted name
        order.
    """
    from lmchat.services.model_catalog import ModelCatalogService  # noqa: PLC0415

    catalog: ModelCatalogService | None = getattr(
        request.app.state, "model_catalog", None
    )
    if catalog is None:
        # Fallback for test environments / legacy lifespan without catalog.
        return await svc.list_loaded()
    return await catalog.list_merged()


@router.get("/providers/status")
async def provider_status(
    request: Request,
    _user: User = Depends(require_user),
) -> list[dict[str, object]]:
    """Return per-provider reachability status for all cloud providers.

    Reports the last-known reachability of each cloud provider registered
    in the ``ProviderRegistry``.  Does NOT trigger new fetches — reads
    from the in-process catalog cache populated by ``GET /api/models``.

    Returns:
        List of ``{provider, reachable, error}`` dicts.  Empty when no
        cloud providers are configured.  "lmstudio" is never included
        (it is always locally reachable from the app's perspective).
    """
    from lmchat.services.model_catalog import ModelCatalogService  # noqa: PLC0415

    catalog: ModelCatalogService | None = getattr(
        request.app.state, "model_catalog", None
    )
    if catalog is None:
        return []
    statuses = await catalog.provider_status()
    return [s.model_dump() for s in statuses]


@router.post("/admin/models/refresh", response_model=RefreshModelsResponse)
async def refresh_models(
    _user: User = Depends(require_admin),
    _rate: None = Depends(admin_rate_limit),
    svc: ModelsService = Depends(get_models_service_dep),
) -> RefreshModelsResponse:
    """Re-probe LM Studio and replace the model cache.

    Admin-only.  Useful after loading or unloading a model without restarting
    lm-chat.

    Returns:
        :class:`RefreshModelsResponse` carrying ``ok=True`` and ``model_count``
        — the size of the refreshed cache.
    """
    await svc.refresh()
    models = await svc.list_loaded()
    model_count = len(models)
    log.info("models_route.refresh_complete", model_count=model_count)
    return RefreshModelsResponse(ok=True, model_count=model_count)


@router.post("/models/load", response_model=ModelLoadResponse)
async def load_model(
    request: Request,
    model: Annotated[str, Form()],
    _user: User = Depends(require_admin),
    _rate: None = Depends(admin_rate_limit),
    svc: ModelsService = Depends(get_models_service_dep),
    engine: AsyncEngine = Depends(get_engine_dep),
) -> ModelLoadResponse:
    """Load a model instance via LM Studio.

    Admin-only.  Accepts ``application/x-www-form-urlencoded`` body.
    Counts against ``model_admin_ops_per_day`` quota (default 1000/day).

    Note: ``config`` override is NOT forwarded to LM Studio because LM Studio
    rejects the ``config`` field (probed 2026-05-21, returns 400
    ``unrecognized_keys``).  See PROBES_p11b_lifecycle.md §1c.

    Args:
        model: LM Studio model key (e.g. ``"qwen3-8b"``).

    Returns:
        :class:`ModelLoadResponse` with ``{instance_id, model, load_time_seconds, status}``.
    """
    user_id: int = _user.id  # type: ignore[attr-defined]
    await _consume_model_admin_op(user_id, engine)

    try:
        result = await svc.load_model(model)
    except UpstreamModelError as exc:
        _handle_upstream_error(exc)
    except UpstreamGatewayError as exc:
        _handle_gateway_error(exc)

    log.info(
        "models_route.load_complete",
        model=model,
        instance_id=result.instance_id,  # type: ignore[possibly-undefined]
    )
    return ModelLoadResponse(
        instance_id=result.instance_id,  # type: ignore[possibly-undefined]
        model=model,
        load_time_seconds=result.load_time_seconds,  # type: ignore[possibly-undefined]
        status=result.status,  # type: ignore[possibly-undefined]
    )


@router.post("/models/unload", response_model=ModelUnloadResponse)
async def unload_model(
    request: Request,
    instance_id: Annotated[str | None, Form()] = None,
    model: Annotated[str | None, Form()] = None,
    all: Annotated[bool, Form()] = False,
    _user: User = Depends(require_admin),
    _rate: None = Depends(admin_rate_limit),
    svc: ModelsService = Depends(get_models_service_dep),
    engine: AsyncEngine = Depends(get_engine_dep),
) -> ModelUnloadResponse:
    """Unload one or all instances of a model.

    Admin-only.  Accepts ``application/x-www-form-urlencoded`` body.

    Logic:
      - ``all=true, model=<key>``: unload all loaded instances of the model.
      - ``instance_id=<id>``: unload the specific instance.
      - Both missing: returns 400.

    Counts one ``model_admin_ops_per_day`` quota unit per call (not per
    instance), since the UI bottleneck is the number of admin decisions,
    not the number of instances being cleaned up.

    Returns:
        :class:`ModelUnloadResponse` with ``{ok: true, unloaded: [instance_id, ...]}``.
    """
    # Validate body.
    if all and not model:
        raise HTTPException(
            status_code=_HTTP_400,
            detail="'model' is required when 'all' is true",
        )
    if not all and not instance_id:
        raise HTTPException(
            status_code=_HTTP_400,
            detail="provide 'instance_id' or set 'all=true' with 'model'",
        )

    user_id: int = _user.id  # type: ignore[attr-defined]
    await _consume_model_admin_op(user_id, engine)

    unloaded_ids: list[str] = []
    failed_entries: list[FailedUnloadEntry] = []

    try:
        if all and model:
            all_result: UnloadAllResult = await svc.unload_all_instances(model)
            unloaded_ids = all_result.succeeded
            failed_entries = [
                FailedUnloadEntry(instance_id=iid, error=err)
                for iid, err in all_result.failed
            ]
        else:
            result_single: ModelUnloadResult = await svc.unload_instance(
                instance_id  # type: ignore[arg-type]
            )
            unloaded_ids = [result_single.instance_id]
    except UpstreamModelError as exc:
        _handle_upstream_error(exc)
    except UpstreamGatewayError as exc:
        _handle_gateway_error(exc)

    log.info("models_route.unload_complete", unloaded=unloaded_ids)
    return ModelUnloadResponse(ok=True, unloaded=unloaded_ids, failed=failed_entries)


@router.post("/models/download", response_model=ModelDownloadResponse)
async def download_model(
    request: Request,
    model: Annotated[str, Form()],
    source: Annotated[str | None, Form()] = None,
    _user: User = Depends(require_admin),
    _rate: None = Depends(admin_rate_limit),
    svc: ModelsService = Depends(get_models_service_dep),
    engine: AsyncEngine = Depends(get_engine_dep),
) -> ModelDownloadResponse:
    """Initiate a model download via LM Studio.

    Admin-only.  Accepts ``application/x-www-form-urlencoded`` body.

    NOTE: The success response shape from LM Studio could not be verified
    during probing (see PROBES_p11b_lifecycle.md §4e).  The frontend Download
    button is NOT rendered until that shape is confirmed.  This route is
    functional but the UI is gated.

    Args:
        model:  Hub-format model key (e.g. ``"bartowski/Phi-3.5-mini-instruct-GGUF/..."``).
        source: Optional download source hint.

    Returns:
        :class:`ModelDownloadResponse` with proxied upstream response.
    """
    user_id: int = _user.id  # type: ignore[attr-defined]
    await _consume_model_admin_op(user_id, engine)

    try:
        result = await svc.download_model(model, source=source)
    except UpstreamModelError as exc:
        _handle_upstream_error(exc)
    except UpstreamGatewayError as exc:
        _handle_gateway_error(exc)

    log.info("models_route.download_complete", model=model)
    return ModelDownloadResponse.model_validate(  # type: ignore[possibly-undefined]
        result.model_dump()  # type: ignore[possibly-undefined]
    )
