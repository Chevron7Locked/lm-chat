# SPDX-License-Identifier: Apache-2.0
"""LM Studio config-override routes.

Endpoints
---------
- ``GET  /api/settings/lmstudio`` — return the resolved per-user config
  view (base URL + default model + ``api_key_set: bool`` + ``source``).
  The raw API key is never returned over HTTP.

- ``PUT  /api/settings/lmstudio`` — patch the per-user override row.
  Body: ``{ base_url?, api_key?, default_model?, clear? }``.  Missing
  fields are left unchanged.

- ``POST /api/settings/lmstudio/test`` — one-shot probe against
  ``{base_url}/api/v1/models`` using caller-supplied creds.  Returns
  ``{ ok, model_count?, error? }``.

- ``PATCH /api/admin/lmstudio/default`` — admin-only; same body shape
  as the user PUT.  Writes the singleton row at id=1 in
  ``server_lm_studio_default``.

The user-facing routes (GET, PUT) require ``require_user``; the probe
route (POST /test) requires ``require_admin`` plus the admin rate limit;
the admin PATCH route adds ``require_admin`` plus the admin rate limit.
All routes return JSON; errors normalise via FastAPI's standard
``HTTPException`` path.
"""
from __future__ import annotations

from typing import Annotated, Final, Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from lmchat.routes._dependencies import (
    admin_rate_limit,
    require_admin,
    require_user,
)
from lmchat.services.auth_service import User
from lmchat.services.lm_studio_overrides_service import (
    LmStudioOverridesService,
    ResolvedLmStudioConfig,
)

router = APIRouter()


def _validate_ssrf_target(url: str) -> str:
    """Reject *url* if its scheme is not ``http`` or ``https``.

    For a local admin-only app, private/loopback/LAN hosts are the
    LEGITIMATE targets (LM Studio lives there).  The admin-gate on
    the probe route is the real protection — the admin is trusted.
    This validator only rejects non-HTTP schemes (``file://``,
    ``gopher://``, ``dict://``, ``ftp://``, etc.) which are never
    valid LM Studio URLs.

    Raises:
        ValueError: If the URL scheme is not http or https.

    Returns:
        The validated URL (unchanged).
    """
    parsed = urlparse(url)
    scheme = parsed.scheme or ""
    if scheme not in ("http", "https"):
        raise ValueError(
            f"base_url scheme {scheme!r} is not allowed — "
            "only http:// and https:// URLs are valid LM Studio targets"
        )

    host = parsed.hostname or ""
    if not host:
        raise ValueError("base_url must contain a hostname or IP address")

    return url


def _validate_http_url(value: str | None) -> str | None:
    """Reject non-HTTP(S) URLs and filesystem paths.

    At-rest encryption and no-filesystem-access requirements mean
    the base_url is part of an HTTP-first integration. File-scheme URLs,
    bare filesystem paths, and tilde-expansions are explicitly rejected
    so an attacker (or a confused admin) cannot redirect lm-chat to
    read from disk under ~/.lmstudio/ or any other path.
    """
    if value is None:
        return value
    stripped = value.strip()
    if not stripped:
        raise ValueError("base_url cannot be an empty string; use `clear` to reset")
    lower = stripped.lower()
    if lower.startswith(("file://", "/", "~")):
        raise ValueError("base_url must be an http(s) URL, not a filesystem path")
    if not (lower.startswith("http://") or lower.startswith("https://")):
        raise ValueError("base_url must start with http:// or https://")
    return stripped

_HTTP_400: Final[int] = 400


# ──────────────────────────────────────────────────────────────────────────
# Request / response models
# ──────────────────────────────────────────────────────────────────────────


class ResolvedConfigResponse(BaseModel):
    """View of the active (post-fallback) LM Studio config for a user.

    The API key is **never** echoed back — only the ``api_key_set``
    flag travels.
    """

    model_config = ConfigDict(extra="forbid")

    base_url: str
    default_model: str
    api_key_set: bool
    source_base_url: Literal["user", "server_admin", "env", "unset"]
    source_api_key: Literal["user", "server_admin", "env", "unset"]
    source_default_model: Literal["user", "server_admin", "env", "unset"]
    # True when boot detected an undecryptable api_key envelope and
    # cleared it (LM_CHAT_SECRET rotation). FE renders a banner so the
    # admin knows to re-save. Cleared by rewire_singletons on next save.
    key_pruned: bool = False
    # True when refresh() received a 401 from LM Studio.
    # Backoff is in effect; FE renders a banner prompting re-auth.
    # Cleared when a subsequent refresh succeeds after backoff expiry.
    auth_failed: bool = False
    # Currently-persisted embedding model preference.
    # None means auto-pick (lexicographic sort over loaded embedders).
    preferred_embedding_model_id: str | None = None
    # Loaded embedding models available for selection.
    # Each entry: {"key": <catalog key>, "active": <bool>}. Only LOADED
    # embedders appear (an unloaded quant variant can't embed — pinning one
    # silently kills memory). ``active`` flags the one the index/recall path
    # actually resolves to so the FE can mark it unambiguously.
    loaded_embedding_models: list[dict[str, str | bool]] = []
    # Background-tasks model: out-of-band auxiliary LLM calls (auto-memory
    # distillation, chat titles, follow-up chips) use this instead of the chat
    # model so they stop competing with the user's next turn. None means
    # "Same as chat model" (default — today's behaviour).
    preferred_background_model_id: str | None = None
    # Loaded LLMs available for selection as the background-tasks model. Each
    # entry: {"key": <catalog key>}. Only models with a live instance appear.
    loaded_background_models: list[dict[str, str | bool]] = []
    # Endpoint-mode toggle (native vs OpenAI-compat surface). "native"
    # (default) — LM Studio's own MCP host runs tools server-side.
    # "openai_compat" — LM Chat's own MCP Store drives tools client-side.
    lm_studio_endpoint_mode: Literal["native", "openai_compat"] = "native"


class UpdateOverrideRequest(BaseModel):
    """PATCH-style body for the user PUT + admin PATCH endpoints.

    A ``None`` (omitted) field leaves the stored value unchanged.  To
    revert a field to "fall through to the next tier", include its name
    in ``clear``.
    """

    model_config = ConfigDict(extra="forbid")

    base_url: str | None = None
    api_key: str | None = None
    default_model: str | None = None
    clear: list[Literal["base_url", "api_key", "default_model"]] | None = None

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, v: str | None) -> str | None:
        return _validate_http_url(v)


class ProbeRequest(BaseModel):
    """Body for the ``POST /api/settings/lmstudio/test`` endpoint."""

    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(..., min_length=1)
    api_key: str | None = None

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, v: str) -> str:
        result = _validate_http_url(v)
        if result is None:
            raise ValueError("base_url is required")
        _validate_ssrf_target(v)
        return result


class ProbedModelWire(BaseModel):
    """Single normalised model entry, sent back with a probe response."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    loaded: bool
    is_embedding: bool


class ProbeResponse(BaseModel):
    """One-shot probe result."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    model_count: int | None = None
    error: str | None = None
    # When ok=true, the normalised model catalogue from the probed
    # instance. SetupLmStudio populates its dropdown directly from
    # this so the admin doesn't have to save unverified config
    # just to discover what models exist.
    models: list[ProbedModelWire] = []


# ──────────────────────────────────────────────────────────────────────────
# Dependency
# ──────────────────────────────────────────────────────────────────────────


def get_lm_studio_overrides_service_dep(
    request: Request,
) -> LmStudioOverridesService:
    """Return the ``LmStudioOverridesService`` attached at lifespan time."""
    svc = getattr(request.app.state, "lm_studio_overrides_service", None)
    if svc is None:
        raise RuntimeError(
            "app.state.lm_studio_overrides_service is unset — the FastAPI "
            "lifespan did not run, and no dependency_overrides entry exists "
            "for get_lm_studio_overrides_service_dep."
        )
    return svc  # type: ignore[return-value]


# ──────────────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────────────


async def _build_loaded_embedders(request: Request) -> list[dict[str, str | bool]]:
    """Build the loaded-embedder option list with an ``active`` marker.

    Returns ``[{"key": <catalog key>, "active": <bool>}, ...]`` for every
    embedding model that has at least one loaded instance.  Downloaded-but-
    unloaded quant variants are EXCLUDED — a model with no live instance can't
    embed, and surfacing it let the admin pin a not-loaded quant (e.g.
    ``…v1.5@q8_0`` while only ``…v1.5`` was loaded), silently killing memory.

    ``active`` is resolved once via
    :func:`resolve_active_embedding_model_key` (the SAME source of truth the
    index/recall path uses) so the FE never has to re-derive which embedder
    is currently in effect — especially in the auto-pick case where the
    choice is the resolver's lexicographic-first, not something the FE can
    compute from the preference alone.  Read-only (``persist_default=False``).
    """
    models_svc = getattr(request.app.state, "models_service", None)
    if models_svc is None:
        return []
    try:
        all_models = await models_svc.list_loaded()
    except Exception:  # noqa: BLE001
        return []
    loaded_keys = [
        m.key
        for m in all_models
        if m.type == "embedding" and m.loaded_instance_ids
    ]

    active_key: str | None = None
    engine = getattr(request.app.state, "engine", None)
    if engine is not None and loaded_keys:
        try:
            from lmchat.services.memory_service import (  # noqa: PLC0415
                resolve_active_embedding_model_key,
            )

            active_key = await resolve_active_embedding_model_key(
                engine=engine,
                models_service=models_svc,
                persist_default=False,
            )
        except Exception:  # noqa: BLE001
            # NoEmbeddingModelLoadedError or any hiccup: leave active unset.
            active_key = None

    return [{"key": k, "active": k == active_key} for k in loaded_keys]


async def _build_loaded_llms(request: Request) -> list[dict[str, str | bool]]:
    """Build the loaded-LLM option list for the background-tasks selector.

    Returns ``[{"key": <catalog key>}, ...]`` for every LLM that has at least
    one loaded instance.  Downloaded-but-unloaded models are EXCLUDED — only a
    model with a live instance can serve background calls without an
    on-the-fly load that would defeat the purpose (keeping the chat model
    free).  Read-only; never raises.
    """
    models_svc = getattr(request.app.state, "models_service", None)
    if models_svc is None:
        return []
    try:
        all_models = await models_svc.list_loaded()
    except Exception:  # noqa: BLE001
        return []
    return [
        {"key": m.key}
        for m in all_models
        if m.type == "llm" and m.loaded_instance_ids
    ]


async def _reject_embedding_dimension_mismatch(
    request: Request, embedding_model_id: str
) -> None:
    """Raise 400 if *embedding_model_id*'s dimension differs from the corpus.

    The memory corpus has exactly one embedding dimension at any time. Switching
    to a different-dimension embedder without a re-index corrupts recall
    (cross-dimension cosine). This guard probes the candidate model's output
    dimension and compares it against the current corpus dimension:

    * empty corpus (dim is ``None``) → allowed (the model sets the dimension on
      first index);
    * same dimension → allowed;
    * different dimension → 400 with a clear "re-index to change" message.

    Probe/engine hiccups never block the set — if the corpus dimension or the
    candidate dimension can't be determined, the index-time fail-loud guard in
    ``MemoryService.index_message`` remains as the backstop.

    Args:
        request:            FastAPI Request (for ``app.state`` access).
        embedding_model_id: The candidate embedding model key being pinned.

    Raises:
        HTTPException: 400 when the candidate dimension differs from the
            non-empty corpus dimension.
    """
    engine = getattr(request.app.state, "engine", None)
    embedding_client = getattr(request.app.state, "embedding_client", None)
    if engine is None or embedding_client is None:
        return

    from lmchat.embedding.errors import EmbeddingError  # noqa: PLC0415
    from lmchat.services.memory_service import (  # noqa: PLC0415
        corpus_embedding_dimension,
        probe_embedding_dimension,
    )

    corpus_dim = await corpus_embedding_dimension(engine)
    if corpus_dim is None:
        # Empty corpus — any embedder is allowed; it sets the corpus dimension.
        return

    try:
        candidate_dim = await probe_embedding_dimension(
            embedding_client, embedding_model_id
        )
    except EmbeddingError:
        # Could not probe (e.g. transient upstream blip). Don't block the set;
        # the index-time fail-loud guard catches a true mismatch.
        return

    if candidate_dim != corpus_dim:
        raise HTTPException(
            status_code=_HTTP_400,
            detail=(
                f"This model is {candidate_dim}-dim but your memory is "
                f"{corpus_dim}-dim; switching requires a full re-index. "
                "Re-index to change embedding models."
            ),
        )


@router.get("/api/settings/lmstudio", response_model=ResolvedConfigResponse)
async def get_resolved_lmstudio_config(
    request: Request,
    user: Annotated[User, Depends(require_user)],
    svc: Annotated[
        LmStudioOverridesService, Depends(get_lm_studio_overrides_service_dep)
    ],
) -> ResolvedConfigResponse:
    """Return the resolved LM Studio config for the calling user.

    Applies the fallback chain (user override → server admin
    default → env) and returns the active values + per-field source
    discriminators.  The API key cleartext never leaves the server;
    only ``api_key_set`` travels.

    Also surfaces the current ``preferred_embedding_model_id`` value
    from the admin row and the list of currently-loaded embedding models
    so the FE can render the embedding-model selector.
    """
    resolved: ResolvedLmStudioConfig = await svc.resolve(user.id)

    # key_pruned / auth_failed are boot/probe diagnostics — but the banner they
    # drive claims "models won't load". That is MISLEADING when another tier
    # (env / user override) is loading models fine: the admin hit a stuck
    # "key cleared by secret rotation" banner while inference worked (the
    # undecryptable DB admin key was pruned at boot, env fallback kept serving).
    # Only surface these flags when models are ACTUALLY unavailable, so the
    # banner reflects real impact rather than an incidental boot event.
    _key_pruned = bool(getattr(request.app.state, "lm_studio_key_pruned", False))
    _auth_failed = bool(getattr(request.app.state, "lm_studio_auth_failed", False))
    if _key_pruned or _auth_failed:
        try:
            _loaded = await request.app.state.models_service.list_loaded()
            if any(m.loaded_instance_ids for m in _loaded):
                _key_pruned = False
                _auth_failed = False
        except Exception:  # noqa: BLE001
            # Never let the diagnostic suppression break config resolution.
            pass

    # Read preferred_embedding_model_id + loaded embedders.
    # The embedder list is LOADED-only and carries an ``active`` marker (see
    # _build_loaded_embedders) so the FE selector can show which embedder is
    # actually in effect and disable not-loaded picks.
    preferred_embedding_model_id: str | None = None
    loaded_embedding_models: list[dict[str, str | bool]] = []
    try:
        loaded_embedding_models = await _build_loaded_embedders(request)
        preferred_embedding_model_id = await svc.fetch_preferred_embedding_model()
    except Exception:  # noqa: BLE001
        # Never let embedding-pref lookup break the main settings response.
        pass

    # Background-tasks model: persisted preference + loaded LLMs for the
    # selector. Independent best-effort lookup so a hiccup here can't break
    # the main settings response either.
    preferred_background_model_id: str | None = None
    loaded_background_models: list[dict[str, str | bool]] = []
    try:
        loaded_background_models = await _build_loaded_llms(request)
        preferred_background_model_id = await svc.fetch_preferred_background_model()
    except Exception:  # noqa: BLE001
        pass

    # Endpoint-mode toggle — independent best-effort lookup, same pattern
    # as the preferences above.
    lm_studio_endpoint_mode: Literal["native", "openai_compat"] = "native"
    try:
        lm_studio_endpoint_mode = await svc.fetch_endpoint_mode()  # type: ignore[assignment]
    except Exception:  # noqa: BLE001
        pass

    return ResolvedConfigResponse(
        base_url=resolved.base_url,
        default_model=resolved.default_model,
        api_key_set=resolved.api_key_set,
        source_base_url=resolved.source_base_url,  # type: ignore[arg-type]
        source_api_key=resolved.source_api_key,  # type: ignore[arg-type]
        source_default_model=resolved.source_default_model,  # type: ignore[arg-type]
        key_pruned=_key_pruned,
        # Surface auth_failed flag so the FE can banner it.
        auth_failed=_auth_failed,
        preferred_embedding_model_id=preferred_embedding_model_id,
        loaded_embedding_models=loaded_embedding_models,
        preferred_background_model_id=preferred_background_model_id,
        loaded_background_models=loaded_background_models,
        lm_studio_endpoint_mode=lm_studio_endpoint_mode,
    )


@router.put(
    "/api/settings/lmstudio",
    response_model=ResolvedConfigResponse,
    responses={
        400: {"description": "Invalid settings payload — empty URL, filesystem path, or unknown"},
    },
)
async def update_user_lmstudio_override(
    body: UpdateOverrideRequest,
    user: Annotated[User, Depends(require_user)],
    svc: Annotated[
        LmStudioOverridesService, Depends(get_lm_studio_overrides_service_dep)
    ],
) -> ResolvedConfigResponse:
    """Patch the per-user LM Studio override row.

    Body fields:
      - ``base_url``     — new override URL, or omit to leave unchanged.
      - ``api_key``      — new API key (encrypted at rest), or omit.
      - ``default_model``— new default model id, or omit.
      - ``clear``        — list of field names to set NULL.

    Returns the post-patch resolved view.

    Raises:
        HTTPException 400 on invalid clear field or empty-string value.
    """
    try:
        await svc.set_user_override(
            user_id=user.id,
            base_url=body.base_url,
            api_key=body.api_key,
            default_model=body.default_model,
            clear=body.clear,
        )
    except ValueError as exc:
        raise HTTPException(status_code=_HTTP_400, detail=str(exc)) from exc
    resolved = await svc.resolve(user.id)
    return ResolvedConfigResponse(
        base_url=resolved.base_url,
        default_model=resolved.default_model,
        api_key_set=resolved.api_key_set,
        source_base_url=resolved.source_base_url,  # type: ignore[arg-type]
        source_api_key=resolved.source_api_key,  # type: ignore[arg-type]
        source_default_model=resolved.source_default_model,  # type: ignore[arg-type]
    )


@router.get("/api/settings/lmstudio/env_suggestion")
async def get_env_suggestion(
    _user: Annotated[User, Depends(require_user)],
    svc: Annotated[
        LmStudioOverridesService, Depends(get_lm_studio_overrides_service_dep)
    ],
) -> dict[str, str | bool]:
    """Return env-derived LM Studio values for Settings UI pre-fill.

    Any authenticated user — the env-derived defaults are the entry point
    every user needs to point at LM Studio. Admin-gating was preventing
    non-admin users (the common case in single-admin deployments) from
    pre-filling the form, leaving the Save button perpetually disabled.
    The cleartext API key is NEVER returned — only ``api_key_set: bool``.

    Returns ``{"base_url": str, "api_key_set": bool, "default_model": str}``.
    """
    return svc.get_env_suggestion()


@router.post(
    "/api/settings/lmstudio/test",
    response_model=ProbeResponse,
    dependencies=[Depends(admin_rate_limit)],
    responses={
        400: {"description": "Invalid probe target — unreachable, non-HTTP URL, or missing host"},
    },
)
async def test_lmstudio_connection(
    body: ProbeRequest,
    _user: Annotated[User, Depends(require_admin)],
    svc: Annotated[
        LmStudioOverridesService, Depends(get_lm_studio_overrides_service_dep)
    ],
) -> ProbeResponse:
    """Probe ``{body.base_url}/api/v1/models`` with the caller's creds.

    Admin-only: the probe endpoint makes an outbound HTTP request to
    a caller-supplied URL.  Without admin gating, any authenticated
    user could enumerate internal RFC-1918 networks via this endpoint
    (classic SSRF).  Configuring LM Studio URLs is admin work
    anyway — gating to admin matches the threat model AND the actual
    workflow.

The SSRF validator in :func:`_validate_ssrf_target` rejects non-HTTP
      schemes (defense in depth).  Private/loopback/LAN hosts are allowed
      because LM Studio runs on the local machine or LAN.
      """
    result = await svc.probe(base_url=body.base_url, api_key=body.api_key)
    return ProbeResponse(
        ok=result.ok,
        model_count=result.model_count,
        error=result.error,
        models=[
            ProbedModelWire(
                id=m.id,
                name=m.name,
                loaded=m.loaded,
                is_embedding=m.is_embedding,
            )
            for m in result.models
        ],
    )


@router.patch(
    "/api/admin/lmstudio/default",
    response_model=ResolvedConfigResponse,
    dependencies=[Depends(admin_rate_limit)],
    responses={400: {"description": "Invalid request payload or probe of the new URL failed"}},
)
async def update_server_admin_default(
    body: UpdateOverrideRequest,
    request: Request,   # required for app.state access
    user: Annotated[User, Depends(require_admin)],
    svc: Annotated[
        LmStudioOverridesService, Depends(get_lm_studio_overrides_service_dep)
    ],
) -> ResolvedConfigResponse:
    """Admin-only: patch the singleton server-wide LM Studio default row.

    Same patch semantics as :func:`update_user_lmstudio_override` but
    targets ``server_lm_studio_default`` (id=1).  Admin rate limit
    applies.

    When ``base_url`` or ``api_key`` is present in the body, a probe is
    issued against the new URL before the DB write.  Save is rejected with
    HTTP 400 if the probe fails — this prevents replacing a working client
    with a broken one.

    After a successful DB write, the four live singletons are rewired to the
    resolved admin values so the change takes effect immediately without a
    restart.

    Returns the calling admin's *resolved* view (which after the write
    reflects the new admin tier).
    """
    import httpx as _httpx

    # Probe gate — verify the new URL is reachable BEFORE DB write or swap.
    # The probe client lives in its own `async with` scope; it is GCed when
    # the block exits, BEFORE rewire_lock is acquired (isolation prevents
    # deadlock under concurrent admin saves).
    #
    # Gate fires on EITHER
    # base_url or api_key being submitted. The pre-fix gate
    # (``if body.base_url is not None``) skipped the probe entirely
    # for ``{"api_key": "wrong-key"}`` bodies, writing the bad key to
    # the DB and rewiring all five live singletons onto it — defeating
    # the gate's whole purpose for exactly the credential field this
    # whole remediation is about. ``probe_url`` falls back to the
    # resolved admin base_url when api_key is the only field changing.
    if body.base_url is not None or body.api_key is not None:
        # When only api_key is changing, probe against the existing
        # admin base_url. Resolve once and reuse for both URL fallback
        # and the api_key fallback below.
        _resolved_admin = await svc.resolve_admin_tier_only()
        if body.base_url is not None:
            probe_url = body.base_url.rstrip("/")
        else:
            probe_url = (_resolved_admin.base_url or "").rstrip("/")

        # Only probe when the submitted value actually CHANGES vs
        # the currently-stored resolved admin values.  Re-submitting the same
        # base_url (e.g. Settings form pre-fills it) or an omitted / empty
        # api_key that hasn't changed must NOT fire the probe — a model-only
        # PATCH that pre-fills base_url was failing with a 401 from LM Studio
        # (which requires an API key) even though nothing LM-Studio-facing
        # changed, bricking the Settings save.
        #
        # This guard is fully preserved: a genuinely different base_url,
        # or a new non-empty api_key that differs from the stored one, still
        # gets probed and rejected on failure.
        _current_url = (_resolved_admin.base_url or "").rstrip("/")
        _url_actually_changed = body.base_url is not None and probe_url != _current_url
        _key_actually_changed = (
            body.api_key is not None
            and body.api_key != ""
            and body.api_key != (_resolved_admin.api_key or "")
        )
        _needs_probe = _url_actually_changed or _key_actually_changed

        # If the admin is re-saving a URL without re-typing the api_key,
        # fall back to the previously-saved admin_default api_key for the
        # probe. Otherwise a URL-only resave would probe unauthenticated
        # against a server that requires auth and fail with 401 -> 400.
        probe_api_key = body.api_key
        if probe_api_key is None or probe_api_key == "":
            if _resolved_admin.api_key:
                probe_api_key = _resolved_admin.api_key
        # When base_url AND its fallback are both empty, there's nothing
        # to probe against — skip the gate (the rewire below will go
        # through with whatever the admin saved).
        if _needs_probe and probe_url:
            probe_headers = (
                {"Authorization": f"Bearer {probe_api_key}"}
                if probe_api_key
                else {}
            )
            _probe_display_url = body.base_url if body.base_url else probe_url
            try:
                async with _httpx.AsyncClient(
                    base_url=probe_url,
                    headers=probe_headers,
                    timeout=_httpx.Timeout(
                        connect=5.0, read=5.0, write=5.0, pool=5.0
                    ),
                ) as probe_client:
                    probe_resp = await probe_client.get("/api/v1/models")
                if probe_resp.status_code != 200:
                    raise HTTPException(
                        status_code=_HTTP_400,
                        detail=(
                            f"Probe of {_probe_display_url} failed: "
                            f"HTTP {probe_resp.status_code}.  Save aborted "
                            "(would replace working client with broken one)."
                        ),
                    )
            except (_httpx.ConnectError, _httpx.ReadTimeout, _httpx.HTTPError) as exc:
                raise HTTPException(
                    status_code=_HTTP_400,
                    detail=(
                        f"Probe of {_probe_display_url} failed: {exc}.  "
                        "Save aborted."
                    ),
                ) from exc
            # probe_client and its connection pool are released here.

    try:
        await svc.set_admin_default(
            base_url=body.base_url,
            api_key=body.api_key,
            default_model=body.default_model,
            clear=body.clear,
        )
    except ValueError as exc:
        raise HTTPException(status_code=_HTTP_400, detail=str(exc)) from exc

    # Trigger singleton rewire when base_url or api_key changed.
    # default_model changes do NOT require a swap — they flow through
    # resolve() at chat-time, not through the singletons.
    # An explicit ``clear: ["api_key"]`` (or "base_url") also rewires
    # because set_admin_default null'd the column even though the
    # corresponding body field was omitted — the rewire must see the
    # post-clear resolved state.
    _cleared = body.clear or []
    if (
        body.base_url is not None
        or body.api_key is not None
        or "base_url" in _cleared
        or "api_key" in _cleared
    ):
        resolved_admin = await svc.resolve_admin_tier_only()
        await svc.rewire_singletons(
            request.app.state,
            new_base_url=resolved_admin.base_url,
            new_api_key=resolved_admin.api_key,
        )
        # Clear the boot-time prune flag so the FE banner ("re-enter the
        # API key in Settings") disappears. Fires on any admin action
        # that touched the api_key (save a new value OR explicit clear)
        # — once the admin has *acknowledged* the pruned key, the
        # banner has done its job. base_url-only changes leave the
        # prune notice in place.
        if (body.api_key is not None and body.api_key != "") or (
            "api_key" in _cleared
        ):
            request.app.state.lm_studio_key_pruned = False

    resolved = await svc.resolve(user.id)
    return ResolvedConfigResponse(
        base_url=resolved.base_url,
        default_model=resolved.default_model,
        api_key_set=resolved.api_key_set,
        source_base_url=resolved.source_base_url,  # type: ignore[arg-type]
        source_api_key=resolved.source_api_key,  # type: ignore[arg-type]
        source_default_model=resolved.source_default_model,  # type: ignore[arg-type]
    )


class _SetEmbeddingModelRequest(BaseModel):
    """Body for PATCH /api/settings/lmstudio/embedding-model."""

    model_config = ConfigDict(extra="forbid")

    embedding_model_id: str | None


class _EmbeddingModelResponse(BaseModel):
    """Response from PATCH /api/settings/lmstudio/embedding-model."""

    model_config = ConfigDict(extra="forbid")

    preferred_embedding_model_id: str | None
    # Each entry: {"key": <catalog key>, "active": <bool>}. Only LOADED
    # embedders appear (a model with no live instance can't embed). ``active``
    # marks the one the index/recall path resolves to, so the FE can render an
    # unambiguous "· active" badge without re-deriving the resolver's pick.
    loaded_embedding_models: list[dict[str, str | bool]]


@router.patch(
    "/api/settings/lmstudio/embedding-model",
    response_model=_EmbeddingModelResponse,
    dependencies=[Depends(admin_rate_limit)],
    responses={
        400: {"description": "Chosen embedding model is not currently loaded"},
    },
)
async def set_preferred_embedding_model(
    body: _SetEmbeddingModelRequest,
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    svc: Annotated[
        LmStudioOverridesService, Depends(get_lm_studio_overrides_service_dep)
    ],
) -> _EmbeddingModelResponse:
    """Admin-only: set or clear the preferred embedding model.

    Body: ``{"embedding_model_id": "<key>"}`` to pin, or
    ``{"embedding_model_id": null}`` to clear (returns to auto-pick).

    - ``null`` → clears the column; selection returns to deterministic
      auto-pick (lexicographic sort over loaded embedding models).
    - a key that IS a currently-loaded embedding model → persisted. 200.
    - a key that is NOT currently loaded → 400 with a descriptive message.

    This endpoint does NOT trigger ``rewire_singletons`` — the embedding
    preference is independent of the LM Studio connection lifecycle.
    """
    if body.embedding_model_id is not None:
        # Validate against the currently-loaded embedders (exact key match).
        # Build the list BEFORE persisting so an unloaded pick is rejected.
        loaded_before = await _build_loaded_embedders(request)
        loaded_keys = {str(entry["key"]) for entry in loaded_before}
        if body.embedding_model_id not in loaded_keys:
            raise HTTPException(
                status_code=_HTTP_400,
                detail=(
                    f"Embedding model '{body.embedding_model_id}' is not "
                    "currently loaded in LM Studio. Load the model first, "
                    "or set to null to return to auto-pick."
                ),
            )

        # Dimension lock: refuse to silently switch to a
        # different-dimension embedder on a NON-EMPTY corpus. The corpus has
        # exactly one embedding dimension; mixing dimensions corrupts recall
        # (bge-m3 1024-dim vs nomic 768-dim). Switching dimension is only legal
        # via a full re-index (POST /api/memory/reindex), which re-embeds the
        # whole corpus under the new model. An empty corpus accepts any model.
        await _reject_embedding_dimension_mismatch(request, body.embedding_model_id)

    await svc.set_preferred_embedding_model(body.embedding_model_id)

    # Rebuild AFTER persisting so the ``active`` markers reflect the new
    # preference (the resolver reads the just-written row).
    loaded_embedding_models = await _build_loaded_embedders(request)

    return _EmbeddingModelResponse(
        preferred_embedding_model_id=body.embedding_model_id,
        loaded_embedding_models=loaded_embedding_models,
    )


class _SetBackgroundModelRequest(BaseModel):
    """Body for PATCH /api/settings/lmstudio/background-model."""

    model_config = ConfigDict(extra="forbid")

    background_model_id: str | None


class _BackgroundModelResponse(BaseModel):
    """Response from PATCH /api/settings/lmstudio/background-model."""

    model_config = ConfigDict(extra="forbid")

    preferred_background_model_id: str | None
    # Each entry: {"key": <catalog key>}. Only LOADED LLMs appear.
    loaded_background_models: list[dict[str, str | bool]]


@router.patch(
    "/api/settings/lmstudio/background-model",
    response_model=_BackgroundModelResponse,
    dependencies=[Depends(admin_rate_limit)],
    responses={
        400: {"description": "Chosen background model is not currently loaded"},
    },
)
async def set_preferred_background_model(
    body: _SetBackgroundModelRequest,
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    svc: Annotated[
        LmStudioOverridesService, Depends(get_lm_studio_overrides_service_dep)
    ],
) -> _BackgroundModelResponse:
    """Admin-only: set or clear the preferred background-tasks model.

    Out-of-band auxiliary LLM calls (auto-memory distillation, chat-title
    generation, follow-up chips) use this model instead of the chat's model so
    they stop competing with the user's next turn on a single local model.

    Body: ``{"background_model_id": "<key>"}`` to pin, or
    ``{"background_model_id": null}`` to clear (returns to "Same as chat
    model" — the default).

    - ``null`` → clears the column; background tasks reuse the chat model.
    - a key that IS a currently-loaded LLM → persisted. 200.
    - a key that is NOT currently loaded → 400 with a descriptive message.

    Unlike the embedding resolver, the runtime resolver
    (``resolve_background_model_id``) FAILS SOFT — but the SET endpoint still
    validates against loaded LLMs so the admin gets immediate feedback that
    their pick won't take effect until the model is loaded. This endpoint does
    NOT trigger ``rewire_singletons``.
    """
    if body.background_model_id is not None:
        # Validate against the currently-loaded LLMs (exact key match).
        loaded_before = await _build_loaded_llms(request)
        loaded_keys = {str(entry["key"]) for entry in loaded_before}
        if body.background_model_id not in loaded_keys:
            raise HTTPException(
                status_code=_HTTP_400,
                detail=(
                    f"Background model '{body.background_model_id}' is not "
                    "currently loaded in LM Studio. Load the model first, "
                    "or set to null to return to the chat model."
                ),
            )

    await svc.set_preferred_background_model(body.background_model_id)

    loaded_background_models = await _build_loaded_llms(request)

    return _BackgroundModelResponse(
        preferred_background_model_id=body.background_model_id,
        loaded_background_models=loaded_background_models,
    )


class _SetEndpointModeRequest(BaseModel):
    """Body for PATCH /api/settings/lmstudio/endpoint-mode."""

    model_config = ConfigDict(extra="forbid")

    endpoint_mode: Literal["native", "openai_compat"]


class _EndpointModeResponse(BaseModel):
    """Response from PATCH /api/settings/lmstudio/endpoint-mode."""

    model_config = ConfigDict(extra="forbid")

    endpoint_mode: Literal["native", "openai_compat"]


@router.patch(
    "/api/settings/lmstudio/endpoint-mode",
    response_model=_EndpointModeResponse,
    dependencies=[Depends(admin_rate_limit)],
)
async def set_lmstudio_endpoint_mode(
    body: _SetEndpointModeRequest,
    _user: Annotated[User, Depends(require_admin)],
    svc: Annotated[
        LmStudioOverridesService, Depends(get_lm_studio_overrides_service_dep)
    ],
) -> _EndpointModeResponse:
    """Admin-only: switch LM Studio between native and OpenAI-compat mode.

    ``"native"`` (default) — LM Chat talks to LM Studio's ``/api/v1/chat``
    surface; LM Studio executes MCP tools itself from its own
    ``~/.lmstudio/mcp.json`` and keeps the conversation server-side
    (``previous_response_id`` chaining).

    ``"openai_compat"`` — LM Chat talks to LM Studio's
    ``/v1/chat/completions`` surface instead, replaying the full history
    each turn and driving any selected MCP integrations through LM Chat's
    own MCP Store (the agentic tool loop cloud providers already use).

    This endpoint does NOT trigger ``rewire_singletons`` — the endpoint
    mode is independent of the connection lifecycle (base_url / api_key),
    same as the embedding- and background-model preferences above.
    """
    await svc.set_endpoint_mode(body.endpoint_mode)
    return _EndpointModeResponse(endpoint_mode=body.endpoint_mode)
