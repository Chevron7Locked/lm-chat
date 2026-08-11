# SPDX-License-Identifier: Apache-2.0
"""Admin routes for cloud-provider configuration (Workstream A4).

Endpoints (all admin-only, mirror lm_studio_settings auth pattern)
------------------------------------------------------------------
- ``GET  /api/admin/providers``                 — list all provider configs
  (safe views: no API key cleartext).
- ``PUT  /api/admin/providers/{provider}``      — upsert a provider config;
  triggers registry refresh.
- ``DELETE /api/admin/providers/{provider}``    — delete a provider config;
  triggers registry refresh.
- ``POST /api/admin/providers/{provider}/test`` — one-shot probe: builds a
  transient provider from the posted-or-stored config, calls
  list_models_detailed (the same method the live model_catalog fetch
  uses), returns ``{ok, model_count}`` or ``{ok: false, error}``.

Auth: all routes require ``require_admin`` (which implies ``require_user`` +
HTTP 401 / 403 for unauthenticated / non-admin users).  The ``PUT`` and
``POST /test`` routes are additionally subject to the ``admin_rate_limit``
dependency.

SSRF: ``base_url`` is validated to accept only ``http://`` and ``https://``
schemes (same validator used in lm_studio_settings).  Private/loopback/LAN
hosts are explicitly allowed — cloud providers on the LAN are a valid
configuration in this local-first app.

Additive only — does NOT touch StreamingService, models_service,
lmstudio_adapter, or any LM Studio behavior.
"""
from __future__ import annotations

from typing import Annotated, Any, Final
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from lmchat.routes._dependencies import (
    admin_rate_limit,
    require_admin,
)
from lmchat.services.auth_service import User
from lmchat.services.provider_config_service import (
    ProviderConfigSafeView,
    ProviderConfigService,
)
from lmchat.services.provider_registry import ProviderRegistry

router = APIRouter()

_HTTP_400: Final[int] = 400
_HTTP_404: Final[int] = 404

# ---------------------------------------------------------------------------
# URL validator (mirrors lm_studio_settings._validate_http_url)
# ---------------------------------------------------------------------------


def _validate_provider_url(value: str) -> str:
    """Reject non-HTTP(S) URLs and bare filesystem paths.

    Only ``http://`` and ``https://`` are accepted.  This closes the SSRF
    non-HTTP-scheme surface on provider base URLs (same pattern as the LM
    Studio settings validator).

    Raises:
        ValueError: If the URL scheme is not http or https, or the host is
            missing.

    Returns:
        The stripped, validated URL.
    """
    stripped = value.strip()
    if not stripped:
        raise ValueError("base_url cannot be empty")
    lower = stripped.lower()
    if lower.startswith(("file://", "/", "~")):
        raise ValueError("base_url must be an http(s) URL, not a filesystem path")
    if not (lower.startswith("http://") or lower.startswith("https://")):
        raise ValueError("base_url must start with http:// or https://")
    parsed = urlparse(stripped)
    if not (parsed.hostname or ""):
        raise ValueError("base_url must contain a hostname or IP address")
    return stripped


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class ProviderConfigResponse(BaseModel):
    """Safe wire view of a single provider config (API key never returned)."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    base_url: str
    default_model: str | None
    extra_headers: dict[str, Any] | None
    enabled: bool
    api_key_set: bool
    allowed_models: list[str] | None = None


class UpsertProviderRequest(BaseModel):
    """Body for ``PUT /api/admin/providers/{provider}``."""

    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(..., min_length=1)
    api_key: str | None = None
    default_model: str | None = None
    extra_headers: dict[str, Any] | None = None
    enabled: bool = True
    allowed_models: list[str] | None = None

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, v: str) -> str:
        return _validate_provider_url(v)


class TestProviderRequest(BaseModel):
    """Body for ``POST /api/admin/providers/{provider}/test``.

    All fields are optional — when omitted the handler falls back to the
    stored provider config.  Sending ``base_url`` + ``api_key`` overrides
    the stored values for this probe only (useful for testing new creds
    before saving).
    """

    model_config = ConfigDict(extra="forbid")

    base_url: str | None = None
    api_key: str | None = None
    extra_headers: dict[str, Any] | None = None

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _validate_provider_url(v)


class TestProviderResponse(BaseModel):
    """Result of a one-shot provider probe."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    model_count: int | None = None
    model_ids: list[str] | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def _get_provider_config_service(request: Request) -> ProviderConfigService:
    svc = getattr(request.app.state, "provider_config_service", None)
    if svc is None:
        raise RuntimeError(
            "app.state.provider_config_service is unset — lifespan did not run."
        )
    return svc  # type: ignore[return-value]


def _get_provider_registry(request: Request) -> ProviderRegistry:
    reg = getattr(request.app.state, "provider_registry", None)
    if reg is None:
        raise RuntimeError(
            "app.state.provider_registry is unset — lifespan did not run."
        )
    return reg  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Route helpers
# ---------------------------------------------------------------------------


def _safe_view_to_response(view: ProviderConfigSafeView) -> ProviderConfigResponse:
    return ProviderConfigResponse(
        provider=view.provider,
        base_url=view.base_url,
        default_model=view.default_model,
        extra_headers=view.extra_headers,
        enabled=view.enabled,
        api_key_set=view.api_key_set,
        allowed_models=view.allowed_models,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(
    "/api/admin/providers",
    response_model=list[ProviderConfigResponse],
)
async def list_providers(
    request: Request,
    _user: Annotated[User, Depends(require_admin)],
) -> list[ProviderConfigResponse]:
    """List all cloud-provider configs (safe views; no API key cleartext).

    Returns all rows from ``provider_configs`` ordered by provider slug.
    Disabled providers are included so the admin can re-enable them.
    """
    svc = _get_provider_config_service(request)
    views = await svc.list_all()
    return [_safe_view_to_response(v) for v in views]


@router.put(
    "/api/admin/providers/{provider}",
    response_model=ProviderConfigResponse,
    dependencies=[Depends(admin_rate_limit)],
    responses={
        400: {"description": "Invalid base_url or other validation failure"},
    },
)
async def upsert_provider(
    provider: str,
    body: UpsertProviderRequest,
    request: Request,
    _user: Annotated[User, Depends(require_admin)],
) -> ProviderConfigResponse:
    """Add or update a cloud-provider config row; triggers registry refresh.

    The provider slug is taken from the URL path (``{provider}``), not the
    body, so the route is idempotent for the same slug.

    After a successful DB write the live registry
    (:class:`~lmchat.services.provider_registry.ProviderRegistry`)
    is refreshed so the new/updated provider is immediately available for
    dispatch — no restart required.

    Raises:
        HTTPException 400 on URL validation failure.
    """
    svc = _get_provider_config_service(request)
    try:
        await svc.add_or_update(
            provider=provider,
            base_url=body.base_url,
            api_key=body.api_key,
            default_model=body.default_model,
            extra_headers=body.extra_headers,
            enabled=body.enabled,
            allowed_models=body.allowed_models,
        )
    except ValueError as exc:
        raise HTTPException(status_code=_HTTP_400, detail=str(exc)) from exc

    # Refresh the live registry so this provider is immediately active.
    reg = _get_provider_registry(request)
    await reg.refresh()

    # Invalidate the catalog cache for this provider so the next
    # GET /api/models fetches fresh model data (including potentially new
    # API key — stale auth would keep serving an empty/error slice until TTL).
    from lmchat.services.model_catalog import ModelCatalogService  # noqa: PLC0415

    catalog: ModelCatalogService | None = getattr(
        request.app.state, "model_catalog", None
    )
    if catalog is not None:
        catalog.invalidate(provider)

    # Return the safe view of the just-written row.
    view = await svc.list_all()
    for v in view:
        if v.provider == provider:
            return _safe_view_to_response(v)

    # Should never happen (we just wrote it), but be safe.
    raise HTTPException(
        status_code=_HTTP_404,
        detail=f"Provider {provider!r} not found after write — DB error?",
    )


@router.delete(
    "/api/admin/providers/{provider}",
    status_code=204,
    dependencies=[Depends(admin_rate_limit)],
)
async def delete_provider(
    provider: str,
    request: Request,
    _user: Annotated[User, Depends(require_admin)],
) -> None:
    """Delete a cloud-provider config row; triggers registry refresh.

    No-op (204) when the provider slug does not exist.  After deletion the
    live registry is refreshed so the removed provider is no longer
    dispatchable.
    """
    svc = _get_provider_config_service(request)
    await svc.delete(provider)

    reg = _get_provider_registry(request)
    await reg.refresh()

    # Invalidate the deleted provider's catalog cache so stale model data
    # is not served after removal.
    from lmchat.services.model_catalog import ModelCatalogService  # noqa: PLC0415

    catalog: ModelCatalogService | None = getattr(
        request.app.state, "model_catalog", None
    )
    if catalog is not None:
        catalog.invalidate(provider)


@router.post(
    "/api/admin/providers/{provider}/test",
    response_model=TestProviderResponse,
    dependencies=[Depends(admin_rate_limit)],
    responses={
        400: {"description": "Invalid base_url or missing stored config"},
    },
)
async def test_provider(
    provider: str,
    body: TestProviderRequest,
    request: Request,
    _user: Annotated[User, Depends(require_admin)],
) -> TestProviderResponse:
    """One-shot probe: call ``GET {base_url}/v1/models`` and report results.

    Uses the posted ``base_url`` / ``api_key`` / ``extra_headers`` when
    provided; falls back to the stored config for any omitted field.  This
    lets the admin test new credentials before saving them.

    A **transient** ``httpx.AsyncClient`` is used for the probe — the
    lifespan-shared client is never touched so a failed probe cannot
    poison the running app's upstream credentials.

    Probes via
    :meth:`~lmchat.providers.openai_compat.OpenAICompatProvider.list_models_detailed`
    — the SAME method :meth:`~lmchat.services.model_catalog.ModelCatalogService._do_fetch`
    calls for the live model picker — rather than the plain ``list_models``
    (which collapses a non-200 / malformed response into an empty list,
    indistinguishable from a provider that genuinely has zero enabled
    models).  A green ``ok:true`` here now means the live picker would ALSO
    see this provider as reachable; a non-200 (e.g. a doubled
    ``/v1/v1/...`` path from an un-normalized ``base_url``) reports
    ``ok:false`` instead of a misleadingly-successful ``model_count: 0``.
    ``OpenAICompatProvider.__init__`` also normalizes ``base_url`` the same
    way for both this probe and the live registry-built provider, so a
    green result here reflects the SAME effective URL the live fetch uses.

    Returns:
        ``{ok: true, model_count: N}`` on success (``N`` may legitimately be
        ``0`` when the provider reports an empty-but-well-formed model
        list); ``{ok: false, error: "..."}`` on any failure (network error,
        non-200, or a malformed response body).

    Raises:
        HTTPException 400 when ``base_url`` is not supplied in the body AND
        no stored config exists for *provider* (nothing to probe against).
    """
    svc = _get_provider_config_service(request)

    # Resolve effective probe parameters: posted body wins; stored config fills gaps.
    effective_base_url: str | None = body.base_url
    effective_api_key: str | None = body.api_key
    effective_extra_headers: dict[str, str] = {}

    if effective_base_url is None or effective_api_key is None:
        stored = await svc.get(provider)
        if stored is None and effective_base_url is None:
            raise HTTPException(
                status_code=_HTTP_400,
                detail=(
                    f"No stored config found for provider {provider!r} and "
                    "no base_url supplied in the request body — nothing to probe."
                ),
            )
        if stored is not None:
            if effective_base_url is None:
                effective_base_url = stored.base_url
            if effective_api_key is None:
                effective_api_key = stored.api_key
            if body.extra_headers is None and stored.extra_headers:
                effective_extra_headers = {
                    str(k): str(v) for k, v in stored.extra_headers.items()
                }

    if body.extra_headers is not None:
        effective_extra_headers = {str(k): str(v) for k, v in body.extra_headers.items()}

    # Build a transient provider on a private client so the probe is isolated.
    assert effective_base_url is not None  # guarded above
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
        ) as probe_client:
            from lmchat.providers.openai_compat import OpenAICompatProvider

            transient = OpenAICompatProvider(
                name=provider,
                base_url=effective_base_url,
                api_key=effective_api_key,
                http_client=probe_client,
                extra_headers=effective_extra_headers or {},
            )
            items, _http_status, error = await transient.list_models_detailed()
    except Exception as exc:  # noqa: BLE001
        return TestProviderResponse(
            ok=False,
            model_count=None,
            error=f"Probe failed: {exc}",
        )

    if error is not None:
        # Non-200 (e.g. a doubled /v1/v1 path), network failure, or a
        # malformed response body — the SAME failure signal the live
        # model_catalog fetch would see.  Do NOT report ok:true here.
        return TestProviderResponse(
            ok=False, model_count=None, model_ids=None, error=error
        )

    model_ids = [str(item["id"]) for item in items if isinstance(item.get("id"), str)]
    return TestProviderResponse(ok=True, model_count=len(model_ids), model_ids=model_ids)
