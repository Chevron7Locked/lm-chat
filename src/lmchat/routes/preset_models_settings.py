# SPDX-License-Identifier: Apache-2.0
"""Per-preset model/provider default settings routes (W5).

Endpoints
---------
- ``GET  /api/settings/preset-models`` — return the calling user's
  preset→{provider, model_id} mapping.  Empty dict when not configured.
- ``PUT  /api/settings/preset-models`` — upsert the mapping.  Entries
  with unknown providers are silently dropped.  Body: the full mapping
  dict.  Empty body or all-dropped entries clears the column.

Both routes require an authenticated user (single-admin model: the same
user who configures LM Studio also owns the preset defaults).  The
``PresetModelsService`` enforces provider validation.
"""
from __future__ import annotations

from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, field_validator

from lmchat.routes._dependencies import require_user
from lmchat.services.auth_service import User
from lmchat.services.preset_models_service import PresetModelsService

router = APIRouter()

_HTTP_400: Final[int] = 400
_HTTP_422: Final[int] = 422

# ---------------------------------------------------------------------------
# Dependency
# ---------------------------------------------------------------------------


def _get_preset_models_service(request: Request) -> PresetModelsService:
    """Return the ``PresetModelsService`` attached at lifespan time."""
    svc = getattr(request.app.state, "preset_models_service", None)
    if svc is None:
        raise RuntimeError(
            "app.state.preset_models_service is unset — the FastAPI "
            "lifespan did not run, and no dependency_overrides entry "
            "exists for _get_preset_models_service."
        )
    return svc  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class PresetEntry(BaseModel):
    """A single preset's model/provider default."""

    model_config = ConfigDict(extra="forbid")

    provider: str = "lmstudio"
    model_id: str

    @field_validator("model_id")
    @classmethod
    def _check_model_id(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("model_id may not be empty")
        return v.strip()

    @field_validator("provider")
    @classmethod
    def _check_provider(cls, v: str) -> str:
        if not v or not v.strip():
            return "lmstudio"
        return v.strip()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/api/settings/preset-models")
async def get_preset_models(
    request: Request,
    user: Annotated[User, Depends(require_user)],
) -> dict[str, Any]:
    """Return the preset model defaults for the calling user.

    Returns an empty dict when no per-preset defaults have been saved.
    Model ids are not secret — no masking required.
    """
    svc = _get_preset_models_service(request)
    return await svc.get_preset_models(user.id)


@router.put("/api/settings/preset-models")
async def set_preset_models(
    body: dict[str, Any],
    request: Request,
    user: Annotated[User, Depends(require_user)],
) -> dict[str, Any]:
    """Upsert the preset model defaults for the calling user.

    Body: ``{"<presetId>": {"provider": "<slug>", "model_id": "<id>"}, ...}``.
    An empty body clears all preset defaults.
    Entries with unknown/unconfigured providers are silently dropped.

    Returns the sanitised mapping that was persisted.

    Raises:
        HTTPException 422 on malformed body structure.
    """
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=_HTTP_422,
            detail="Request body must be a JSON object mapping preset ids to entries.",
        )
    svc = _get_preset_models_service(request)
    try:
        return await svc.set_preset_models(user.id, body)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=_HTTP_400,
            detail=f"Failed to save preset models: {exc}",
        ) from exc
