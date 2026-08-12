# SPDX-License-Identifier: Apache-2.0
"""App-level admin settings routes.

Endpoints
---------
- ``GET /api/settings/app`` — return the 5 resolved values with
  ``is_override`` flags.
- ``PATCH /api/settings/app`` — admin-only; set/clear overrides.

Validation
----------
- ``web_search_provider`` must be ``"searxng"``, ``"ddg"``, ``"brave"``, or
  ``"brave_llm"``.
- ``searxng_url`` must be a valid http(s) URL (validated via
  ``validate_searxng_url`` from ``web_search_service``).
- ``repeat_warning_cut_k`` must be an integer 0..100 (enforced via the
  Pydantic ``Field(ge=0, le=100)`` bound on the request model).

Auth
----
The PATCH route requires ``require_admin`` plus ``admin_rate_limit``,
mirroring the sibling ``/api/settings/lmstudio/*`` admin routes.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from lmchat.logging import get_logger
from lmchat.routes._dependencies import (
    admin_rate_limit,
    get_engine_dep,
    require_admin,
    require_user,
)
from lmchat.services.app_settings_service import (
    resolve_memory_distillation_enabled,
    resolve_repeat_warning_cut_k,
    resolve_searxng_url,
    resolve_subsession_memory_distillation_enabled,
    resolve_web_search_provider,
    set_memory_distillation_enabled,
    set_repeat_warning_cut_k,
    set_searxng_url,
    set_subsession_memory_distillation_enabled,
    set_web_search_provider,
)
from lmchat.services.auth_service import User
from lmchat.services.web_search_service import validate_searxng_url

log = get_logger(__name__)

router = APIRouter()

_HTTP_400: Final[int] = 400


# ─── Request / Response models ────────────────────────────────────────────────


class _AppSettingItem(BaseModel):
    """One app setting value with an ``is_override`` flag."""

    model_config = ConfigDict(extra="forbid")

    value: bool | str | int | None
    is_override: bool


class _AppSettingsResponse(BaseModel):
    """Body for GET /api/settings/app."""

    model_config = ConfigDict(extra="forbid")

    memory_distillation_enabled: _AppSettingItem
    subsession_memory_distillation_enabled: _AppSettingItem
    web_search_provider: _AppSettingItem
    searxng_url: _AppSettingItem
    repeat_warning_cut_k: _AppSettingItem


class _PatchAppSettingsRequest(BaseModel):
    """Body for PATCH /api/settings/app.

    Any field may be omitted (no-op) or explicitly set to ``null`` to
    clear the override (return to config default).
    """

    model_config = ConfigDict(extra="forbid")

    memory_distillation_enabled: bool | None = None
    subsession_memory_distillation_enabled: bool | None = None
    web_search_provider: str | None = None
    searxng_url: str | None = Field(default=None, max_length=512)
    repeat_warning_cut_k: int | None = Field(default=None, ge=0, le=100)


# ─── GET /api/settings/app ────────────────────────────────────────────────────


@router.get("/api/settings/app", response_model=_AppSettingsResponse)
async def get_app_settings(
    _user: Annotated[User, Depends(require_user)],
    request: Request,
    engine: AsyncEngine = Depends(get_engine_dep),
) -> _AppSettingsResponse:
    """Return the 5 app settings with ``is_override`` flags.

    Authenticated-user read (any role).  The values are resolved
    (DB override → config default) so the FE always gets the effective value.
    """
    # Resolve each value; also check the raw DB column to determine
    # whether it is an explicit override.
    md_enabled = await resolve_memory_distillation_enabled(engine)
    ss_md_enabled = await resolve_subsession_memory_distillation_enabled(engine)
    ws_provider = await resolve_web_search_provider(engine)
    ws_url = await resolve_searxng_url(engine)
    repeat_warning_cut_k = await resolve_repeat_warning_cut_k(engine)

    # Determine is_override by re-reading the raw columns.
    try:
        from sqlalchemy import select as _select  # noqa: PLC0415

        from lmchat.db.schema import (  # noqa: PLC0415
            server_lm_studio_default as _tbl,
        )

        async with engine.connect() as _conn:
            _raw = await _conn.execute(
                _select(
                    _tbl.c.memory_distillation_enabled,
                    _tbl.c.subsession_memory_distillation_enabled,
                    _tbl.c.web_search_provider,
                    _tbl.c.searxng_url,
                    _tbl.c.repeat_warning_cut_k,
                ).where(_tbl.c.id == 1)
            )
            _row = _raw.fetchone()
            _raw_md = _row[0] if _row else None
            _raw_ss_md = _row[1] if _row else None
            _raw_ws_provider = _row[2] if _row else None
            _raw_ws_url = _row[3] if _row else None
            _raw_repeat_k = _row[4] if _row else None
    except Exception:  # noqa: BLE001
        # If we can't read overrides, everything is "config default".
        _raw_md = _raw_ss_md = _raw_ws_provider = _raw_ws_url = _raw_repeat_k = None

    return _AppSettingsResponse(
        memory_distillation_enabled=_AppSettingItem(
            value=md_enabled, is_override=_raw_md is not None
        ),
        subsession_memory_distillation_enabled=_AppSettingItem(
            value=ss_md_enabled, is_override=_raw_ss_md is not None
        ),
        web_search_provider=_AppSettingItem(
            value=ws_provider, is_override=_raw_ws_provider is not None
        ),
        repeat_warning_cut_k=_AppSettingItem(
            value=repeat_warning_cut_k, is_override=_raw_repeat_k is not None
        ),
        searxng_url=_AppSettingItem(value=ws_url, is_override=_raw_ws_url is not None),
    )


# ─── PATCH /api/settings/app ──────────────────────────────────────────────────


@router.patch(
    "/api/settings/app",
    response_model=_AppSettingsResponse,
    dependencies=[Depends(admin_rate_limit)],
    responses={
        400: {"description": "Invalid provider or searxng_url"},
    },
)
async def patch_app_settings(
    body: _PatchAppSettingsRequest,
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    engine: AsyncEngine = Depends(get_engine_dep),
) -> _AppSettingsResponse:
    """Admin-only: set or clear app-level settings overrides.

    Any field may be omitted (no-op).  Set to ``null`` to clear
    (return to config default).  Set to a concrete value to override.

    Validation:
    - ``web_search_provider`` must be ``"searxng"``, ``"ddg"``, ``"brave"``,
      or ``"brave_llm"``.
    - ``searxng_url`` must be a valid http(s) URL (SSRF guard via
      ``validate_searxng_url``).
    - ``repeat_warning_cut_k`` must be an integer 0..100 (Pydantic
      ``Field(ge=0, le=100)`` on the request model raises 422 otherwise).
    """
    # Validate web_search_provider if provided.
    if body.web_search_provider is not None:
        if body.web_search_provider not in ("searxng", "ddg", "brave", "brave_llm"):
            raise HTTPException(
                status_code=_HTTP_400,
                detail=(
                    f"web_search_provider must be 'searxng', 'ddg', 'brave', "
                    f"or 'brave_llm', got {body.web_search_provider!r}"
                ),
            )

    # Validate searxng_url if provided (not None).
    if body.searxng_url is not None:
        try:
            validate_searxng_url(body.searxng_url)
        except ValueError as exc:
            raise HTTPException(status_code=_HTTP_400, detail=str(exc)) from exc

    # Apply changes for any field explicitly set (including null to clear).
    # ``model_fields_set`` lets us distinguish "field omitted" from
    # "field explicitly set to null".
    if "memory_distillation_enabled" in body.model_fields_set:
        await set_memory_distillation_enabled(engine, body.memory_distillation_enabled)
    if "subsession_memory_distillation_enabled" in body.model_fields_set:
        await set_subsession_memory_distillation_enabled(
            engine, body.subsession_memory_distillation_enabled
        )
    if "web_search_provider" in body.model_fields_set:
        await set_web_search_provider(engine, body.web_search_provider)
    if "searxng_url" in body.model_fields_set:
        await set_searxng_url(engine, body.searxng_url)
    if "repeat_warning_cut_k" in body.model_fields_set:
        await set_repeat_warning_cut_k(engine, body.repeat_warning_cut_k)

    # Live-rebind the web_search_service singleton if web-search settings
    # were touched (Finding A: overrides must take effect at runtime).
    if "web_search_provider" in body.model_fields_set or "searxng_url" in body.model_fields_set:
        _wss = getattr(request.app.state, "web_search_service", None)
        if _wss is not None:
            try:
                _new_provider = await resolve_web_search_provider(engine)
                _new_searxng_url = await resolve_searxng_url(engine)
                _wss.reconfigure(
                    provider=_new_provider,
                    searxng_url=_new_searxng_url,
                )
            except Exception:  # noqa: BLE001
                log.warning(
                    "app_settings.web_search_rebind_failed",
                    exc_info=True,
                )

    # Return the updated resolved view.
    return await get_app_settings(_user=user, request=request, engine=engine)
