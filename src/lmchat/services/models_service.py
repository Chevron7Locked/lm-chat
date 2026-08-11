# SPDX-License-Identifier: Apache-2.0
"""LM Studio model-list + capability cache service for lm-chat.

Owns the ``GET /api/v1/models`` probe and the in-memory capability cache.
Feeds streaming_service's ``reasoning`` decision, the settings UI's
sampling-control visibility, and (when injected) params_service's
rejected-param cache invalidation on reload.

Three lifecycle methods proxy the corresponding LM Studio endpoints:
- ``load_model(model_key)`` → POST /api/v1/models/load
- ``unload_instance(instance_id)`` → POST /api/v1/models/unload
- ``unload_all_instances(model_key)`` → unloads all loaded instances
- ``download_model(model_key, *, source)`` → POST /api/v1/models/download

Wire-shape notes (live-probed)
-------------------------------
- ``GET /api/v1/models`` returns ``{"models": [...]}``.
- Each element has ``key``, ``loaded_instances`` (array of ``{id, config}``),
  and ``capabilities``. ``loaded_instances[*].id`` is the instance_id used
  for unload calls.
- ``POST /api/v1/models/load`` → ``{type, instance_id, load_time_seconds, status}``.
- ``POST /api/v1/models/unload`` → ``{instance_id}``.
- Upstream 4xx errors use envelope ``{"error": {"type", "message", ...}}``.

Concurrency
-----------
``refresh()`` acquires ``_cache_lock`` while building the new list and
replaces ``_cache`` in a single assignment — ``list_loaded`` never returns
a half-built list. A process-wide ``_load_lock`` serialises concurrent load
operations so two admin loads can't OOM the machine loading two large
models at once; unload/download aren't locked.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select

from lmchat.db.schema import server_lm_studio_default
from lmchat.logging import get_logger
from lmchat.metrics import MODELS_PROBE_DROPPED

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from lmchat.services.params_service import ParamsService

# Backoff after a 401 from LM Studio — refresh() returns immediately
# without re-probing during this window so a stale API key doesn't
# produce a 401 storm.
_AUTH_FAILED_BACKOFF_SEC: float = 60.0

# Minimum interval between forced reprobes (storm guard) — prevents a
# flood of resolution misses from triggering N upstream probes.
_FORCED_REPROBE_MIN_INTERVAL: float = 5.0

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# HTTP client tuning
# ---------------------------------------------------------------------------

# /api/v1/models is a fast local call; 10s read timeout is generous
# headroom without blocking startup for long.
_PROBE_TIMEOUT: httpx.Timeout = httpx.Timeout(
    connect=5.0,
    read=10.0,
    write=5.0,
    pool=5.0,
)

# Standard timeout for fast lifecycle operations (unload).
_UNLOAD_TIMEOUT: httpx.Timeout = httpx.Timeout(
    connect=5.0,
    read=30.0,
    write=5.0,
    pool=5.0,
)

# Process-wide soft lock for load operations — only one load at a time,
# so concurrent admin loads can't OOM the machine loading two large
# models simultaneously. Module-level (not per-user): a single process
# serves all users.
_load_lock: asyncio.Lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Lifecycle result types
# ---------------------------------------------------------------------------


class ModelLoadResult(BaseModel):
    """Result of POST /api/v1/models/load.

    Wire-shape (live-probed):
        {type, instance_id, load_time_seconds, status}
    """

    model_config = {"extra": "ignore"}

    type: str = Field(default="llm")
    instance_id: str
    load_time_seconds: float = Field(default=0.0)
    status: str = Field(default="loaded")


class ModelUnloadResult(BaseModel):
    """Result of POST /api/v1/models/unload.

    Wire-shape (live-probed):
        {instance_id}
    """

    model_config = {"extra": "ignore"}

    instance_id: str


@dataclass
class UnloadAllResult:
    """Result of a best-effort ``unload_all_instances`` call.

    Attributes:
        succeeded: Instance IDs that were successfully unloaded.
        failed:    Pairs of ``(instance_id, error_message)`` for instances
                   that could not be unloaded.
    """

    succeeded: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)


class ModelDownloadResult(BaseModel):
    """Result of POST /api/v1/models/download.

    Success shape unverified (see PROBES_p11b_lifecycle.md §4e).
    Assumes a minimal envelope with status; extra fields are ignored.
    """

    model_config = {"extra": "allow"}

    status: str = Field(default="ok")


# ---------------------------------------------------------------------------
# Upstream error class
# ---------------------------------------------------------------------------


class UpstreamModelError(Exception):
    """Raised when LM Studio returns a 4xx error on a lifecycle endpoint.

    Args:
        status_code: The HTTP status code from upstream (e.g. 404).
        error_type:  The ``error.type`` field from the upstream JSON.
        message:     The ``error.message`` field from the upstream JSON.
        code:        The ``error.code`` field from the upstream JSON (optional).
        param:       The ``error.param`` field from the upstream JSON (optional).
    """

    def __init__(
        self,
        status_code: int,
        error_type: str,
        message: str,
        code: str | None = None,
        param: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.error_type = error_type
        self.message = message
        self.code = code
        self.param = param
        super().__init__(f"LM Studio {status_code}: [{error_type}] {message}")


class UpstreamGatewayError(Exception):
    """Raised when LM Studio returns a 5xx error on a lifecycle endpoint.

    The route handler converts this to HTTP 502 Bad Gateway.
    """

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"LM Studio upstream {status_code}: {detail}")


# ---------------------------------------------------------------------------
# Pydantic shapes
# ---------------------------------------------------------------------------


class ReasoningCapability(BaseModel):
    """Optional reasoning capability block on reasoning-class models.

    Present only when LM Studio surfaces the ``reasoning`` field on the
    model's capabilities object (documented but not observed on MLX models
    in live probes; modelled as optional per LM Studio's schema).
    """

    allowed_options: list[Literal["off", "on", "low", "medium", "high"]]
    default: Literal["off", "on", "low", "medium", "high"]


class Capabilities(BaseModel):
    """Model capability flags from LM Studio's ``/api/v1/models`` response.

    Live-probed shape: ``{"vision": true, "trained_for_tool_use": true}``.
    ``reasoning`` is optional and present only on reasoning-capable models.
    """

    vision: bool
    trained_for_tool_use: bool
    reasoning: ReasoningCapability | None = None
    # Derived in the probe normalizer when ModelInfo.type == "embedding" —
    # LM Studio omits the capabilities block for embedding models, so
    # without this the frontend can't distinguish an embedding model from
    # a chat model via capabilities alone.
    embedding: bool = False


class QuantizationInfo(BaseModel):
    """Quantization details for a model.

    Live-probed: ``{"name": "4bit", "bits_per_weight": 4}``.
    Some models may have a plain string quantization (e.g. "Q4_K_M") or
    None; this class covers the object form.
    """

    model_config = {"extra": "ignore"}

    name: str = Field(default="")
    bits_per_weight: float | None = Field(default=None)


class ModelInfo(BaseModel):
    """One model entry from LM Studio's ``GET /api/v1/models`` response.

    LM Studio's REST surface mixes casings: ``displayName`` / ``sizeBytes``
    / ``paramsString`` / ``maxContextLength`` are camelCase while ``key`` /
    ``loaded_instances`` / ``capabilities`` stay snake_case. Camelcase keys
    are accepted via ``validation_alias`` (input only) — serialization
    carries no aliases, so the lm-chat wire is snake_case everywhere.
    Optional fields default gracefully when absent so a new LM Studio
    release that omits a field doesn't fail the service.

    ``loaded_instance_ids`` carries the ``id`` field from each
    ``loaded_instances`` entry — the identifiers used for
    ``POST /api/v1/models/unload``.
    """

    model_config = {"populate_by_name": True, "extra": "ignore"}

    # Primary key / identifier
    key: str

    # Model taxonomy
    type: str = Field(default="llm", description="e.g. 'llm' or 'embedding'")
    publisher: str = Field(default="", description="Model publisher / org")
    display_name: str = Field(default="", validation_alias="displayName")
    architecture: str = Field(default="", description="e.g. 'qwen3'")
    # quantization may be a dict (object) or None; we map to QuantizationInfo.
    quantization: QuantizationInfo | None = Field(
        default=None,
        description="Quantization details; None for MLX/full-precision",
    )

    # Size + context
    size_bytes: int = Field(default=0, validation_alias="sizeBytes")
    params_string: str | None = Field(
        default=None,
        validation_alias="paramsString",
        description="e.g. '35.4B'; None when not provided by LM Studio",
    )
    loaded_instances: int = Field(
        default=0,
        description="Number of currently loaded instances of this model",
    )
    # Individual instance IDs for targeted unload calls.
    # Populated from loaded_instances[*].id in _probe_upstream.
    loaded_instance_ids: list[str] = Field(
        default_factory=list,
        description="Instance IDs for loaded instances (used by unload calls)",
    )
    max_context_length: int = Field(default=0, validation_alias="maxContextLength")
    # Actual loaded context (per-instance config.context_length), NOT the
    # model's capability maximum. When multiple instances are loaded, this
    # is the MIN across them (safe floor for any request gated by this
    # number). Falls back to max_context_length when nothing is loaded.
    loaded_context_length: int = Field(
        default=0,
        validation_alias="loadedContextLength",
        description="Min context_length across loaded instances; 0 when none loaded",
    )

    # Format
    format: str = Field(default="", description="e.g. 'mlx', 'gguf'")

    # Optional: embedding models (and future model types) may omit the
    # capabilities block entirely. When None, treat as no capabilities
    # (vision=False, trained_for_tool_use=False, reasoning=None).
    capabilities: Capabilities | None = Field(default=None)

    # Human-readable description (may be null in LM Studio responses)
    description: str | None = Field(default=None)

    # "lmstudio" for locally-served models; the provider's registered name
    # (e.g. "openrouter", "groq") for cloud models added by the catalog
    # merge layer. Defaults to "lmstudio" so existing flows are unchanged.
    provider: str = Field(
        default="lmstudio",
        description="Provider slug: 'lmstudio' for local models, provider name for cloud.",
    )


@dataclass(frozen=True)
class ResolvedModel:
    """Outcome of resolving a requested model id to a wire-ready instance.

    Returned by :meth:`ModelsService.resolve_to_loaded_or_fallback`.
    ``wire_id`` is always a live ``loaded_instance_id`` or ``None`` — never
    the bare catalog ``key`` for an in-catalog-but-unloaded model, which LM
    Studio rejects when JIT loading is disabled.

    Attributes:
        wire_id: The loaded_instance_id to send to LM Studio, or ``None`` when
            no LLM is loaded at all (caller surfaces a clear error).
        requested: The model key/instance id the caller asked for.
        substituted: ``True`` when the requested model was not loaded and a
            different loaded LLM was chosen — callers should surface this so the
            user knows which model actually answered.
        fallback_key: The catalog ``key`` of the substituted model (when
            ``substituted``), else ``None``.
        reason: Machine tag — ``""`` (clean resolve), ``requested_not_loaded``
            (substituted), ``no_models_loaded`` / ``only_non_llm_loaded``
            (``wire_id is None``).
    """

    wire_id: str | None
    requested: str
    substituted: bool = False
    fallback_key: str | None = None
    reason: str = ""


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ModelsService:
    """Probe LM Studio's ``/api/v1/models`` and cache the result.

    Inject via ``app.state.models_service``; construct once at lifespan
    start with ``make_models_service()``.
    """

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient,
        base_url: str,
        long_op_timeout_seconds: float = 600.0,
        params_service: ParamsService | None = None,
        loaded_models_ttl: float = _FORCED_REPROBE_MIN_INTERVAL,
    ) -> None:
        """Initialise the service with an injected HTTP client.

        Args:
            http_client:              Shared ``httpx.AsyncClient`` (headers pre-loaded
                                      with the LM Studio API key by the caller).
            base_url:                 LM Studio base URL, e.g. ``"http://localhost:1234"``.
            long_op_timeout_seconds:  httpx timeout for load/download operations (seconds).
                                      Default 600 s (10 min).  Sourced from
                                      ``Settings.lm_chat_lmstudio_long_op_timeout_seconds``.
            params_service:           Rejected-param cache.  When set,
                                      ``refresh()`` clears a model's rejected params
                                      whenever a probe shows it newly (re)loaded.
                                      Optional so existing call sites and
                                      tests that don't care keep working unchanged.
            loaded_models_ttl:        TTL (seconds) for the loaded-models set inside
                                      :meth:`resolve_to_loaded_or_fallback`.  When the
                                      cache age exceeds this value a fresh upstream probe
                                      is triggered before resolving the wire model id.
                                      Sourced from
                                      ``Settings.lm_chat_loaded_models_ttl_seconds``
                                      at startup; default matches
                                      ``_FORCED_REPROBE_MIN_INTERVAL`` (5 s).
        """
        self._http_client = http_client
        self._base_url = base_url.rstrip("/")
        self._params_service = params_service
        self._cache: list[ModelInfo] | None = None
        self._cache_lock: asyncio.Lock = asyncio.Lock()
        # Monotonic timestamp of the last successful cache write. 0.0 means
        # "never written" — any TTL check against it always triggers a
        # re-probe on the first resolve call.
        self._cache_timestamp: float = 0.0
        # TTL for the loaded-models set inside resolve_to_loaded_or_fallback;
        # seeded from Settings.lm_chat_loaded_models_ttl_seconds via
        # make_models_service(), defaulting to _FORCED_REPROBE_MIN_INTERVAL.
        self._loaded_models_ttl: float = loaded_models_ttl
        # Monotonic timestamp of the last 401; reset to 0.0 on a
        # successful probe or once the backoff window passes.
        self._auth_failed_at: float = 0.0
        # True while auth is failing and within the backoff window; exposed
        # via the auth_failed property for app.state.lm_studio_auth_failed.
        self._auth_failed: bool = False
        # Set when a forced reprobe is in-flight; concurrent callers await
        # it rather than firing a second upstream probe.
        self._forced_reprobe_in_flight: asyncio.Event = asyncio.Event()
        # Monotonic timestamp of the last forced reprobe; enforces
        # _FORCED_REPROBE_MIN_INTERVAL between successive forced probes.
        self._last_forced_reprobe_at: float = 0.0
        # Invoked when a forced reprobe clears _auth_failed; the lifespan
        # uses it to clear app.state.lm_studio_auth_failed for the FE banner.
        self._on_forced_reprobe_auth_cleared: Callable[[], None] | None = None
        # Live-reachability state for GET /api/lmstudio/health, distinct
        # from the catalog cache: a successful HTTP response means
        # reachable=True even with 0 models loaded; a network failure means
        # reachable=False. 401 auth-failure is a separate dimension — LM
        # Studio IS reachable but rejected the key. None ("unknown") until
        # the first probe completes.
        self._last_probe_reachable: bool | None = None
        # Epoch timestamp of the last probe that updated
        # _last_probe_reachable; None until the first probe.
        self._last_probe_at: float | None = None
        self._long_op_timeout = httpx.Timeout(
            connect=10.0,
            read=long_op_timeout_seconds,
            write=10.0,
            pool=10.0,
        )
        # TTL-path probe-storm dedup guard. Separate from _cache_lock to
        # avoid deadlock (refresh() takes _cache_lock internally). At most
        # one upstream probe fires per TTL window even under N concurrent
        # callers.
        self._stale_reprobe_lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Public property — auth-failed state
    # ------------------------------------------------------------------

    @property
    def auth_failed(self) -> bool:
        """True when the most recent refresh returned 401 and the backoff window
        has not yet expired.  The lifespan copies this to
        ``app.state.lm_studio_auth_failed`` so the FE can banner it."""
        if not self._auth_failed:
            return False
        # Auto-clear once the backoff window expires so a successful probe
        # after key re-entry resets the flag without extra wiring.
        elapsed = time.monotonic() - self._auth_failed_at
        if elapsed >= _AUTH_FAILED_BACKOFF_SEC:
            self._auth_failed = False
            self._auth_failed_at = 0.0
        return self._auth_failed

    async def live_health(self) -> dict[str, object]:
        """Return a live reachability snapshot for GET /api/lmstudio/health.

        Calls :meth:`_refresh_if_loaded_cache_stale` to trigger an upstream
        probe if the cache is stale (honouring the TTL and 401 backoff),
        then returns the most recently observed probe state::

            {
                "reachable":    bool,        # True = LM Studio answered the probe
                "loaded_count": int,         # LLM-type models with ≥1 loaded instance
                "auth_failed":  bool,        # True = 401 backoff in effect
                "last_probe_at": float|None, # epoch seconds of last probe (or None)
            }

        ``reachable=False`` means LM Studio didn't respond at all (refused
        / timeout / DNS failure), even if the catalog cache still holds
        stale data. ``auth_failed=True`` is a separate dimension — LM
        Studio IS reachable but rejected the API key.
        """
        # Re-probe if stale (respects TTL and 401 backoff).
        await self._refresh_if_loaded_cache_stale(self._loaded_models_ttl)
        models = list(self._cache or [])
        loaded_count = sum(
            1 for m in models if m.type == "llm" and m.loaded_instance_ids
        )
        # None (never probed) → treat as False/unknown.
        reachable: bool = self._last_probe_reachable is True
        return {
            "reachable": reachable,
            "loaded_count": loaded_count,
            "auth_failed": self.auth_failed,
            "last_probe_at": self._last_probe_at,
        }

    def set_auth_cleared_callback(self, callback: Callable[[], None]) -> None:
        """Register a callback invoked when a forced reprobe clears the 401
        auth-failed flag.

        The caller (app lifespan) uses it to mirror the cleared state onto
        ``app.state.lm_studio_auth_failed`` so the FE auth banner clears on
        recovery without a restart.
        """
        self._on_forced_reprobe_auth_cleared = callback

    async def force_refresh(self) -> bool:
        """Force an immediate upstream probe, bypassing the 401 backoff.

        Storm guard: concurrent callers trigger at most ONE upstream probe
        via ``_forced_reprobe_in_flight``; a caller that arrives mid-probe
        waits on the event and returns its outcome. Rate-limited by
        ``_FORCED_REPROBE_MIN_INTERVAL`` — returns ``False`` without
        probing if the last forced reprobe was too recent.

        On success, the caller is expected to mirror the cleared
        ``_auth_failed`` state to ``app.state.lm_studio_auth_failed``.

        Returns:
            ``True`` if the probe succeeded and the cache was updated.
            ``False`` if skipped (rate limit or in-flight) or failed.
        """
        now = time.monotonic()
        if now - self._last_forced_reprobe_at < _FORCED_REPROBE_MIN_INTERVAL:
            log.debug(
                "models_service.force_refresh_rate_limited",
                last_forced_reprobe_sec_ago=round(now - self._last_forced_reprobe_at, 1),
                min_interval_sec=_FORCED_REPROBE_MIN_INTERVAL,
            )
            return False

        # Storm guard: if another forced reprobe is already in-flight,
        # wait for it and return its outcome.
        if self._forced_reprobe_in_flight.is_set():
            log.debug(
                "models_service.force_refresh_awaiting_in_flight",
                hint="Another forced reprobe is in progress; awaiting result.",
            )
            await self._forced_reprobe_in_flight.wait()
            # Cache may have been updated by the in-flight probe; True if
            # auth_failed cleared.
            return not self._auth_failed

        # Acquire the storm-guard event so subsequent callers queue up.
        self._forced_reprobe_in_flight.set()
        self._last_forced_reprobe_at = now

        try:
            log.info("models_service.forced_reprobe_start")
            async with self._cache_lock:
                try:
                    new_cache = await self._probe_upstream()
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 401:
                        log.warning(
                            "models_service.forced_reprobe_401",
                            hint="LM Studio returned 401 on forced reprobe.",
                        )
                    else:
                        log.error(
                            "models_service.forced_reprobe_failed",
                            error=str(exc),
                            error_type=type(exc).__name__,
                        )
                    return False
                except (httpx.RequestError, ValueError) as exc:
                    log.error(
                        "models_service.forced_reprobe_failed",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                    if isinstance(exc, httpx.RequestError):
                        self._last_probe_reachable = False
                        self._last_probe_at = time.time()
                    return False

                # Probe succeeded — update cache and clear auth flags.
                self._auth_failed = False
                self._auth_failed_at = 0.0
                # Notify the lifespan so app.state.lm_studio_auth_failed is
                # cleared and the FE banner disappears.
                if self._on_forced_reprobe_auth_cleared is not None:
                    self._on_forced_reprobe_auth_cleared()
                self._clear_rejected_for_reloaded(prior=self._cache, new=new_cache)
                self._cache = new_cache
                self._cache_timestamp = time.monotonic()

            log.info(
                "models_service.forced_reprobe_complete",
                model_count=len(new_cache),
            )
            return True
        finally:
            # Clear the in-flight event so the next forced reprobe can fire.
            self._forced_reprobe_in_flight.clear()

    def _models_url(self) -> str:
        """Return the canonical ``/api/v1/models`` URL."""
        return f"{self._base_url}/api/v1/models"

    def _extract_upstream_error(
        self, status_code: int, body: dict[str, Any]
    ) -> UpstreamModelError:
        """Parse a 4xx upstream error body into an ``UpstreamModelError``.

        LM Studio 4xx errors use the envelope::

            {"error": {"type": "<string>", "message": "<string>",
                       "code": "<string>", "param": "<string>"}}

        Args:
            status_code: HTTP status code from the upstream response.
            body:        Parsed JSON body dict.

        Returns:
            An :class:`UpstreamModelError` with the extracted fields.
        """
        err_block = body.get("error", body)
        if isinstance(err_block, dict):
            error_type = str(err_block.get("type", "upstream_error"))
            message = str(err_block.get("message", "LM Studio error"))
            code_val = err_block.get("code")
            code: str | None = str(code_val) if code_val is not None else None
            param_val = err_block.get("param")
            param: str | None = str(param_val) if param_val is not None else None
        else:
            error_type = "upstream_error"
            message = str(body)
            code = None
            param = None
        return UpstreamModelError(status_code, error_type, message, code=code, param=param)

    async def _probe_upstream(self) -> list[ModelInfo]:
        """Fetch and parse ``/api/v1/models`` from LM Studio.

        Returns:
            Parsed list of :class:`ModelInfo`.

        Raises:
            httpx.HTTPStatusError: On non-2xx response from LM Studio.
            httpx.RequestError:    On connection failure.
            ValueError:            On JSON shape mismatch.
        """
        url = self._models_url()
        log.info("models_service.probe_start", url=url)

        response = await self._http_client.get(url, timeout=_PROBE_TIMEOUT)
        response.raise_for_status()

        data = response.json()
        # LM Studio's native /api/v1/models returns {"models": [...]} but
        # the OpenAI-compat /v1/models surface returns {"data": [...]};
        # accept either. Log a warning with the keys seen if neither
        # is present, so an admin can debug the upstream shape.
        raw_models = data.get("models")
        if raw_models is None:
            raw_models = data.get("data", [])
            if not data.get("data"):
                log.warning(
                    "models_service.upstream_shape_unexpected",
                    keys=list(data.keys()),
                )
        if not isinstance(raw_models, list):
            raise ValueError(
                f"Expected list under 'models' or 'data' key, got {type(raw_models).__name__!r}"
            )

        models: list[ModelInfo] = []
        for raw in raw_models:
            if not isinstance(raw, dict):
                log.warning(
                    "models_service.skipping_non_dict_entry",
                    entry_type=type(raw).__name__,
                )
                continue
            # loaded_instances is an array of {id, config} objects; extract
            # instance IDs before collapsing to a count. Also extract each
            # instance's config.context_length (the ACTUAL loaded context)
            # — max_context_length is the architectural max, not what's
            # allocated. Take the MIN across instances so any budget gate
            # using this number is safe for every instance.
            instance_ids: list[str] = []
            instance_ctx_lengths: list[int] = []
            raw_instances = raw.get("loaded_instances", [])
            if isinstance(raw_instances, list):
                for inst in raw_instances:
                    if isinstance(inst, dict) and "id" in inst:
                        instance_ids.append(str(inst["id"]))
                        cfg = inst.get("config")
                        if isinstance(cfg, dict):
                            ctx = cfg.get("context_length")
                            if isinstance(ctx, int) and ctx > 0:
                                instance_ctx_lengths.append(ctx)
                # Min across instances (safer floor); 0 if nothing is
                # loaded, in which case the consumer falls back to
                # max_context_length for the UI.
                loaded_ctx = (
                    min(instance_ctx_lengths) if instance_ctx_lengths else 0
                )
                raw = dict(
                    raw,
                    loaded_instances=len(raw_instances),
                    loaded_instance_ids=instance_ids,
                    loaded_context_length=loaded_ctx,
                )
            try:
                parsed = ModelInfo.model_validate(raw)
                # LM Studio omits the capabilities block on embedding
                # models; surface capabilities.embedding=True so the
                # frontend doesn't need to special-case `type`.
                if parsed.type == "embedding":
                    if parsed.capabilities is None:
                        parsed.capabilities = Capabilities(
                            vision=False,
                            trained_for_tool_use=False,
                            embedding=True,
                        )
                    elif not parsed.capabilities.embedding:
                        parsed.capabilities = parsed.capabilities.model_copy(
                            update={"embedding": True}
                        )
                models.append(parsed)
            except Exception as exc:
                # Classify the drop reason for Prometheus.
                from pydantic import ValidationError as _ValidationError  # noqa: PLC0415

                reason = "validation_error" if isinstance(exc, _ValidationError) else "unknown"
                MODELS_PROBE_DROPPED.labels(reason=reason).inc()
                log.warning(
                    "models_service.model_parse_error",
                    key=raw.get("key", "<unknown>"),
                    error=str(exc),
                    reason=reason,
                )
                continue
        # LM Studio's /api/v1/models can return the same key more than
        # once (e.g. an embedding model with one unloaded and one loaded
        # entry). Merge all occurrences: union loaded_instance_ids, take
        # max(loaded_instances), and take the MIN of non-zero
        # loaded_context_length values — never lose a live instance id
        # regardless of ordering.
        key_to_entry: dict[str, ModelInfo] = {}
        merged_dupes = 0
        for m in models:
            existing = key_to_entry.get(m.key)
            if existing is None:
                key_to_entry[m.key] = m
            else:
                merged_dupes += 1
                # Union loaded_instance_ids — maintain insertion order
                # via dict.fromkeys.
                merged_ids = list(
                    dict.fromkeys(existing.loaded_instance_ids + m.loaded_instance_ids)
                )
                # Take max(loaded_instances) — the true loaded count.
                merged_count = max(existing.loaded_instances, m.loaded_instances)
                # MIN of non-zero loaded_context_length values (same
                # safe-floor invariant as above) — a zero means "no
                # instances reported a value" and must never win over a
                # real number.
                non_zero_ctxs = [
                    c
                    for c in (existing.loaded_context_length, m.loaded_context_length)
                    if c != 0
                ]
                merged_ctx = min(non_zero_ctxs) if non_zero_ctxs else 0
                # Keep the first occurrence's static fields (type, key,
                # capabilities, etc.) — model_copy only overrides the
                # three mergeable fields.
                key_to_entry[m.key] = existing.model_copy(
                    update={
                        "loaded_instance_ids": merged_ids,
                        "loaded_instances": merged_count,
                        "loaded_context_length": merged_ctx,
                    }
                )
        if merged_dupes:
            log.warning(
                "models_service.upstream_duplicate_keys_merged",
                merged=merged_dupes,
                kept=len(key_to_entry),
                hint=(
                    "LM Studio's /api/v1/models returned ≥1 model key "
                    "more than once. Merged all occurrences; this "
                    "is the upstream's anomaly, not ours."
                ),
            )

        deduped = list(key_to_entry.values())
        log.info("models_service.probe_complete", model_count=len(deduped))
        # Mark reachable — probe succeeded (even if 0 models returned).
        self._last_probe_reachable = True
        self._last_probe_at = time.time()
        return deduped

    # ------------------------------------------------------------------
    # Public API — lifecycle
    # ------------------------------------------------------------------

    async def load_model(self, model_key: str) -> ModelLoadResult:
        """Load a model via POST /api/v1/models/load.

        Acquires the process-wide ``_load_lock`` so concurrent admin
        loads serialize — loading two large models simultaneously can OOM
        the admin's machine.

        Args:
            model_key: The LM Studio model key, e.g. ``"qwen3-8b"``.

        Returns:
            :class:`ModelLoadResult` mirroring the upstream
            ``{type, instance_id, load_time_seconds, status}`` shape.

        Raises:
            UpstreamModelError:   Upstream returned 4xx.
            UpstreamGatewayError: Upstream returned 5xx.
            httpx.RequestError:   Connection failure.
        """
        url = f"{self._base_url}/api/v1/models/load"
        log.info("models_service.load_start", model=model_key, url=url)

        async with _load_lock:
            try:
                resp = await self._http_client.post(
                    url,
                    json={"model": model_key},
                    timeout=self._long_op_timeout,
                )
            except httpx.RequestError:
                raise

            if resp.is_success:
                data = resp.json()
                log.info(
                    "models_service.load_complete",
                    model=model_key,
                    instance_id=data.get("instance_id"),
                    load_time=data.get("load_time_seconds"),
                )
                return ModelLoadResult.model_validate(data)

            body = resp.json() if resp.content else {}
            if 400 <= resp.status_code < 500:
                raise self._extract_upstream_error(resp.status_code, body)
            raise UpstreamGatewayError(
                resp.status_code,
                str(body.get("error", {}).get("message", resp.text)),
            )

    async def unload_instance(self, instance_id: str) -> ModelUnloadResult:
        """Unload a single model instance via POST /api/v1/models/unload.

        Uses a short timeout (30 s) — unload is a fast operation.

        Args:
            instance_id: The instance identifier, e.g. ``"qwen3-8b"``
                         or ``"qwen3-8b:2"`` for duplicates.

        Returns:
            :class:`ModelUnloadResult` with ``instance_id``.

        Raises:
            UpstreamModelError:   Upstream returned 4xx (e.g. 404 not loaded).
            UpstreamGatewayError: Upstream returned 5xx.
            httpx.RequestError:   Connection failure.
        """
        url = f"{self._base_url}/api/v1/models/unload"
        log.info("models_service.unload_start", instance_id=instance_id, url=url)

        try:
            resp = await self._http_client.post(
                url,
                json={"instance_id": instance_id},
                timeout=_UNLOAD_TIMEOUT,
            )
        except httpx.RequestError:
            raise

        if resp.is_success:
            data = resp.json()
            log.info("models_service.unload_complete", instance_id=instance_id)
            return ModelUnloadResult.model_validate(data)

        body = resp.json() if resp.content else {}
        if 400 <= resp.status_code < 500:
            raise self._extract_upstream_error(resp.status_code, body)
        raise UpstreamGatewayError(
            resp.status_code,
            str(body.get("error", {}).get("message", resp.text)),
        )

    async def unload_all_instances(self, model_key: str) -> UnloadAllResult:
        """Unload all loaded instances of *model_key* with best-effort semantics.

        Reads the current cache (or re-probes if cold) to find instance IDs for
        the given model key, then attempts to unload each sequentially.  A failure
        on one instance does NOT abort the remaining unloads — all instances are
        attempted regardless.

        Args:
            model_key: The LM Studio model key.

        Returns:
            :class:`UnloadAllResult` with ``succeeded`` (instance IDs that were
            unloaded) and ``failed`` (pairs of ``(instance_id, error_message)``
            for instances that could not be unloaded).
        """
        models = await self.list_loaded()
        target: ModelInfo | None = next((m for m in models if m.key == model_key), None)
        if target is None or not target.loaded_instance_ids:
            log.info(
                "models_service.unload_all_no_instances",
                model=model_key,
            )
            return UnloadAllResult()

        result = UnloadAllResult()
        for instance_id in list(target.loaded_instance_ids):
            try:
                await self.unload_instance(instance_id)
                result.succeeded.append(instance_id)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "models_service.unload_instance_failed",
                    instance_id=instance_id,
                    error=str(exc),
                )
                result.failed.append((instance_id, str(exc)))
        return result

    async def download_model(
        self, model_key: str, *, source: str | None = None
    ) -> ModelDownloadResult:
        """Initiate a model download via POST /api/v1/models/download.

        NOTE: The success response shape from LM Studio was not confirmed at
        the time of implementation.  This method is implemented and the route
        is wired, but the frontend Download button is not rendered until
        the response shape is confirmed.

        Args:
            model_key: Hub-format model key, e.g.
                       ``"bartowski/Phi-3.5-mini-instruct-GGUF/..."``
            source:    Optional download source hint (passed to LM Studio
                       if provided; omitted if None).

        Returns:
            :class:`ModelDownloadResult` with parsed response fields.

        Raises:
            UpstreamModelError:   Upstream returned 4xx.
            UpstreamGatewayError: Upstream returned 5xx.
            httpx.RequestError:   Connection failure.
        """
        url = f"{self._base_url}/api/v1/models/download"
        payload: dict[str, str] = {"model": model_key}
        if source is not None:
            payload["source"] = source

        log.info("models_service.download_start", model=model_key, url=url)

        try:
            resp = await self._http_client.post(
                url,
                json=payload,
                timeout=self._long_op_timeout,
            )
        except httpx.RequestError:
            raise

        if resp.is_success:
            data = resp.json() if resp.content else {}
            log.info("models_service.download_complete", model=model_key)
            return ModelDownloadResult.model_validate(data)

        body = resp.json() if resp.content else {}
        if 400 <= resp.status_code < 500:
            raise self._extract_upstream_error(resp.status_code, body)
        raise UpstreamGatewayError(
            resp.status_code,
            str(body.get("error", {}).get("message", resp.text)),
        )

    # ------------------------------------------------------------------
    # Public API — cache
    # ------------------------------------------------------------------

    async def list_loaded(self) -> list[ModelInfo]:
        """Return the cached model list, probing upstream if cold.

        On first call (cache is None), runs ``refresh()`` to populate.
        Subsequent calls return the cache without I/O.

        Returns:
            List of :class:`ModelInfo` for all models known to LM Studio.
        """
        if self._cache is None:
            await self.refresh()
        # After refresh(), _cache is always non-None (refresh assigns []).
        return list(self._cache or [])

    async def get_capabilities(self, model_id: str) -> Capabilities:
        """Return the :class:`Capabilities` for *model_id*.

        When the model's capabilities field is ``None`` (e.g. embedding models
        that LM Studio does not surface a capabilities block for), a default
        ``Capabilities`` object is returned with ``vision=False``,
        ``trained_for_tool_use=False``, and ``reasoning=None``.

        Args:
            model_id: The model ``key`` string from LM Studio.

        Returns:
            The :class:`Capabilities` object for the model.

        Raises:
            KeyError: If *model_id* is not in the current cache.
        """
        models = await self.list_loaded()
        for m in models:
            if m.key == model_id:
                return m.capabilities or Capabilities(
                    vision=False, trained_for_tool_use=False
                )
        raise KeyError(f"model {model_id!r} not in models cache")

    async def get_max_context_length(self, model_id: str) -> int:
        """Return the model's CURRENTLY-LOADED context length.

        Used by the streaming service's pre-flight budget gate to decide
        whether to trim MCP integrations before opening the upstream stream.

        Prefers :attr:`ModelInfo.loaded_context_length` (the per-instance
        actual context) over :attr:`max_context_length` (the architectural
        max) — using the capability max here would let a model loaded at a
        smaller context silently overflow (the original silent-stream-death
        shape; do not regress it). Falls back to ``max_context_length`` only
        when nothing is loaded (UI courtesy for unloaded rows).

        Returns 0 when the model is unknown to the cache or LM Studio
        didn't report a value — the gate treats 0 as "unknown; skip".

        Args:
            model_id: The model ``key`` string from LM Studio.

        Returns:
            The actually-loaded context length in tokens (per-instance MIN
            when multiple instances), or the architectural max as fallback
            when nothing is loaded, or 0 when unknown.
        """
        models = await self.list_loaded()
        for m in models:
            if m.key == model_id:
                loaded = int(m.loaded_context_length or 0)
                if loaded > 0:
                    return loaded
                # Nothing loaded — fall back to the architectural max so
                # unloaded rows still display something; the budget gate
                # handles 0 separately upstream.
                return int(m.max_context_length or 0)
        return 0

    async def _refresh_if_loaded_cache_stale(self, ttl: float) -> None:
        """Re-probe LM Studio if the loaded-models cache is older than *ttl* seconds.

        Called by :meth:`resolve_to_loaded_or_fallback` before reading
        loaded state, so an externally-unloaded model is noticed within
        *ttl* seconds rather than up to the 30-minute periodic refresh.

        Guards:
        * Fast path (lock-free) when the cache is still fresh.
        * Skips the probe entirely inside the 401 backoff window (mirrors
          :meth:`refresh`).
        * Storm dedup via ``_stale_reprobe_lock`` (separate from
          ``_cache_lock`` to avoid deadlock, since ``refresh()`` acquires
          ``_cache_lock`` internally): a concurrent caller waits for the
          lock then double-checks freshness, so exactly ONE probe fires
          per TTL window regardless of N concurrent callers.

        Args:
            ttl: Maximum acceptable cache age in seconds. The caller
                 sources this from
                 ``Settings.lm_chat_loaded_models_ttl_seconds``.
        """
        # Fast path (lock-free): cache is fresh — do nothing.
        now = time.monotonic()
        if now - self._cache_timestamp < ttl:
            return

        # Within 401 backoff — do NOT re-probe; let the caller see the stale
        # (likely empty) cache and surface the existing error path.
        if self._auth_failed:
            elapsed = now - self._auth_failed_at
            if elapsed < _AUTH_FAILED_BACKOFF_SEC:
                log.debug(
                    "models_service.loaded_ttl_skip_auth_backoff",
                    elapsed_sec=round(elapsed, 1),
                    backoff_sec=_AUTH_FAILED_BACKOFF_SEC,
                )
                return

        # Storm dedup: serialise concurrent stale-cache callers. A peer may
        # have completed a probe while we waited for the lock.
        async with self._stale_reprobe_lock:
            # Double-check freshness under the lock.
            if time.monotonic() - self._cache_timestamp < ttl:
                return

            log.debug(
                "models_service.loaded_ttl_expired_reprobe",
                cache_age_sec=round(time.monotonic() - self._cache_timestamp, 1),
                ttl_sec=ttl,
            )
            await self.refresh()

    async def resolve_to_loaded_or_fallback(
        self, model_key_or_id: str, *, prefer_key: str | None = None
    ) -> ResolvedModel:
        """Resolve a model key/instance id to a LOADED instance, falling back
        to another loaded LLM when the requested model is not currently loaded.

        Naively resolving to the bare catalog ``key`` for an unloaded model
        returns a value LM Studio rejects with "Invalid model identifier"
        when JIT loading is disabled — a mechanism for silent stream death
        on a normal chat turn. This method never returns a non-loaded
        identifier: when the requested model has no loaded instance it
        picks another loaded LLM so the call still completes.

        Fallback order: requested model if loaded → ``prefer_key`` if loaded
        → first loaded LLM in catalog order. ``wire_id`` is ``None`` only
        when no LLM is loaded at all; ``reason`` then distinguishes
        ``no_models_loaded`` from ``only_non_llm_loaded``. Loaded-ness is
        judged by ``loaded_instance_ids`` (the live list), never
        ``loaded_instances`` (a possibly-stale int count).

        Args:
            model_key_or_id: requested model ``key`` or ``loaded_instance_id``.
            prefer_key: model key to prefer for the fallback (e.g. the
                admin/user default), used only when the requested model is
                unloaded.

        Returns:
            A :class:`ResolvedModel`.
        """
        # TTL-guarded re-probe so an externally-unloaded model is noticed
        # within ~5s instead of up to the 30-minute periodic refresh.
        await self._refresh_if_loaded_cache_stale(self._loaded_models_ttl)
        models = await self.list_loaded()

        # (1) Passthrough — caller already holds a live loaded_instance_id.
        for m in models:
            if model_key_or_id in m.loaded_instance_ids:
                return ResolvedModel(wire_id=model_key_or_id, requested=model_key_or_id)

        # (2) Requested key is itself loaded — use its first live instance.
        for m in models:
            if m.key == model_key_or_id and m.loaded_instance_ids:
                return ResolvedModel(
                    wire_id=m.loaded_instance_ids[0], requested=model_key_or_id
                )

        # (3) Requested model is unloaded (or absent) — fall back to a loaded
        #     LLM. Exclude embedding models; an empty loaded_instance_ids means
        #     not actually loaded regardless of the int count.
        loaded_llms = [
            m for m in models if m.type == "llm" and m.loaded_instance_ids
        ]
        if not loaded_llms:
            any_loaded = any(m.loaded_instance_ids for m in models)
            return ResolvedModel(
                wire_id=None,
                requested=model_key_or_id,
                reason="only_non_llm_loaded" if any_loaded else "no_models_loaded",
            )

        # Prefer the caller's preferred key when it is itself a loaded LLM
        # (mirrors the FE's user-choice > persisted > default precedence).
        chosen = None
        if prefer_key is not None:
            chosen = next(
                (
                    m
                    for m in loaded_llms
                    if m.key == prefer_key or prefer_key in m.loaded_instance_ids
                ),
                None,
            )
        if chosen is None:
            chosen = loaded_llms[0]

        return ResolvedModel(
            wire_id=chosen.loaded_instance_ids[0],
            requested=model_key_or_id,
            substituted=True,
            fallback_key=chosen.key,
            reason="requested_not_loaded",
        )

    async def resolve_embedding_wire_id(self, model_id: str) -> str | None:
        """Resolve an embedding ``model_id`` to its LOADED INSTANCE wire id.

        LM Studio routes embedding requests by loaded *instance* name (e.g.
        ``text-embedding-nomic-embed-text-v1.5@q8_0``) when JIT loading is
        disabled — the bare catalog key is rejected with a 400 "Invalid
        model identifier". Maps a configured key OR a stored
        ``embedding_model_id`` (possibly the bare key from an older row) to
        the wire id LM Studio accepts.

        EXACT / PREFIX / PASSTHROUGH / bare-fallback only — **no
        cross-model fallback**: a ``nomic`` request must never resolve to
        ``bge-m3``, or per-chunk re-embedding would compare the query
        against stored vectors in the wrong vector space. Cross-model
        *selection* (which model to write NEW chunks with) is a separate
        concern handled by ``_resolve_active_embedding_model_id`` /
        ``resolve_embedding_model_status``.

        1. **Passthrough** — ``model_id`` is already a loaded embedding
           instance id → return it unchanged.
        2. **Exact key** — ``model_id`` matches a loaded embedding model's
           catalog ``key`` → return its ``loaded_instance_ids[0]``.
        3. **Prefix** — a loaded instance id equals ``model_id + "@" +
           <quant>`` → return it. The ``"@"`` guard prevents cross-family
           prefix collisions.
        4. **Bare fallback** — ``model_id`` carries a stale ``@<quant>``
           that no longer matches (steps 1-3 missed), but the SAME
           embedding model's bare key IS loaded (possibly under a
           different quant). Same-bare-family quants share output
           dimensions, so this is dimension-safe; closes the
           stale-quant-suffix recall/retrieval gap.
        5. Otherwise → ``None``.

        Args:
            model_id: configured key or stored ``embedding_model_id``.

        Returns:
            The loaded instance wire id, or ``None`` when no loaded
            embedding instance matches exactly, by prefix, or by
            bare-key fallback.
        """
        await self._refresh_if_loaded_cache_stale(self._loaded_models_ttl)
        models = await self.list_loaded()

        # (1) Passthrough — model_id is already a loaded embedding instance id.
        for m in models:
            if m.type == "embedding" and model_id in m.loaded_instance_ids:
                return model_id

        # (2) Exact catalog-key match → first loaded instance.
        for m in models:
            if (
                m.type == "embedding"
                and m.key == model_id
                and m.loaded_instance_ids
            ):
                return m.loaded_instance_ids[0]

        # (3) Prefix match: a loaded instance id == model_id + "@" + <quant>.
        prefix = model_id + "@"
        matches: list[str] = []
        for m in models:
            if m.type != "embedding":
                continue
            for iid in m.loaded_instance_ids:
                if iid.startswith(prefix):
                    matches.append(iid)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            # Ambiguous (two quant variants of the same key loaded). Pick the
            # first deterministically and warn — extremely rare in practice.
            log.warning(
                "models_service.embed_wire_prefix_ambiguous",
                model_id=model_id,
                matches=matches,
            )
            return matches[0]

        # (4) Bare fallback: same-base-family only — bare is derived from
        # model_id itself, so this can never cross to a different model.
        if "@" in model_id:
            bare = model_id.split("@", 1)[0]

            for m in models:
                if (
                    m.type == "embedding"
                    and m.key == bare
                    and m.loaded_instance_ids
                ):
                    return m.loaded_instance_ids[0]

            bare_prefix = bare + "@"
            bare_matches: list[str] = []
            for m in models:
                if m.type != "embedding":
                    continue
                for iid in m.loaded_instance_ids:
                    if iid.startswith(bare_prefix):
                        bare_matches.append(iid)
            if len(bare_matches) == 1:
                return bare_matches[0]
            if len(bare_matches) > 1:
                log.warning(
                    "models_service.embed_wire_bare_fallback_ambiguous",
                    model_id=model_id,
                    bare=bare,
                    matches=bare_matches,
                )
                return bare_matches[0]

        # (5) No exact/prefix/bare-fallback match — no cross-model fallback here.
        return None

    async def refresh(self) -> None:
        """Re-probe LM Studio and replace the cache atomically.

        Called at lifespan start and on the admin refresh route. Acquires
        ``_cache_lock`` while building the new list; the assignment is
        atomic — ``list_loaded`` never returns a half-built list.

        Side-effect: when a ``ParamsService`` was injected, a successful
        probe clears the rejected-param cache for any model the new list
        shows newly (re)loaded — see ``_clear_rejected_for_reloaded``.
        """
        # Skip re-probe if within the 401 backoff window.
        if self._auth_failed:
            elapsed = time.monotonic() - self._auth_failed_at
            if elapsed < _AUTH_FAILED_BACKOFF_SEC:
                log.warning(
                    "models_service.refresh_skipped_auth_failed_backoff",
                    elapsed_sec=round(elapsed, 1),
                    backoff_sec=_AUTH_FAILED_BACKOFF_SEC,
                )
                return
            # Backoff window expired — try again; flag cleared on success.
            self._auth_failed = False
            self._auth_failed_at = 0.0

        async with self._cache_lock:
            try:
                new_cache = await self._probe_upstream()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 401:
                    # Transport succeeded (server IS reachable) — mark
                    # reachable=True/auth_failed=True. Without this, a
                    # cold-start 401 leaves _last_probe_reachable=None,
                    # incorrectly reporting LM Studio as unreachable.
                    self._last_probe_reachable = True
                    self._last_probe_at = time.time()
                    self._auth_failed = True
                    self._auth_failed_at = time.monotonic()
                    # Rate-limit future probes during the backoff window so
                    # a stale API key doesn't produce a probe storm.
                    self._cache_timestamp = time.monotonic()
                    log.warning(
                        "models_service.auth_failed_401",
                        hint="Re-save the LM Studio API key in Settings → LM Studio.",
                        backoff_sec=_AUTH_FAILED_BACKOFF_SEC,
                    )
                else:
                    log.error(
                        "models_service.refresh_failed",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                # Keep stale cache on failure rather than replacing with [].
                return
            except (httpx.RequestError, ValueError) as exc:
                log.error(
                    "models_service.refresh_failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                # Distinct from auth failure (401, server up but rejected
                # the key) — for RequestError the server is DOWN.
                if isinstance(exc, httpx.RequestError):
                    self._last_probe_reachable = False
                    self._last_probe_at = time.time()
                    # Stamp _cache_timestamp on failure too, so the TTL
                    # floor applies to the down-state — otherwise the cache
                    # stays "stale forever" during an outage and every
                    # live_health() call re-probes.
                    self._cache_timestamp = time.monotonic()
                # Keep stale cache on failure. On cold start leave _cache
                # as None so the next caller retries upstream rather than
                # reading a poisoned empty list — pinning it to [] here
                # would make embedding_status report "no models loaded"
                # forever until a write path invalidates the cache.
                return
            # Probe succeeded — clear the auth-failed flag and replace cache.
            self._auth_failed = False
            self._auth_failed_at = 0.0
            # Clear rejected params for any model this probe shows newly
            # (re)loaded, so a runtime/template change re-probes params
            # instead of inheriting stale rejections. Runs before the swap
            # so the comparison uses the genuinely-prior cache.
            self._clear_rejected_for_reloaded(prior=self._cache, new=new_cache)
            # Stamp timestamp BEFORE releasing the lock so readers that
            # observe the new cache also see a fresh timestamp.
            self._cache = new_cache
            self._cache_timestamp = time.monotonic()

    def _clear_rejected_for_reloaded(
        self, *, prior: list[ModelInfo] | None, new: list[ModelInfo]
    ) -> None:
        """Clear rejected-param cache entries for newly (re)loaded models.

        A model counts as newly (re)loaded when either:
        - its ``key`` was absent from the prior cached list (first sighting), or
        - it has a loaded instance id that the prior probe did not show
          (covers load-after-unload and reload-with-new-instance).

        Defensive no-ops: when no ``ParamsService`` was injected, or on the
        first probe of the process lifetime (*prior* is ``None``).

        Args:
            prior: The previously cached model list (``None`` on first probe).
            new:   The freshly probed model list.
        """
        if self._params_service is None or prior is None:
            return
        prior_keys = {m.key for m in prior}
        prior_instances: dict[str, set[str]] = {
            m.key: set(m.loaded_instance_ids) for m in prior
        }
        for m in new:
            newly_listed = m.key not in prior_keys
            new_instance_ids = (
                set(m.loaded_instance_ids) - prior_instances.get(m.key, set())
            )
            if newly_listed or new_instance_ids:
                self._params_service.clear_for_model(m.key)


def make_models_service(
    http_client: httpx.AsyncClient,
    base_url: str,
    long_op_timeout_seconds: float = 600.0,
    params_service: ParamsService | None = None,
    loaded_models_ttl: float = _FORCED_REPROBE_MIN_INTERVAL,
) -> ModelsService:
    """Factory function for ``ModelsService``.

    Args:
        http_client:              Shared ``httpx.AsyncClient`` with auth headers set.
        base_url:                 LM Studio base URL.
        long_op_timeout_seconds:  Timeout for load/download operations in seconds.
                                  Sourced from
                                  ``Settings.lm_chat_lmstudio_long_op_timeout_seconds``.
        params_service:           Optional rejected-param cache; see
                                  :meth:`ModelsService.__init__`.
        loaded_models_ttl:        TTL (seconds) for the loaded-models set inside
                                  :meth:`ModelsService.resolve_to_loaded_or_fallback`.
                                  Sourced from
                                  ``Settings.lm_chat_loaded_models_ttl_seconds``
                                  at startup.

    Returns:
        A new :class:`ModelsService` instance.
    """
    return ModelsService(
        http_client=http_client,
        base_url=base_url,
        long_op_timeout_seconds=long_op_timeout_seconds,
        params_service=params_service,
        loaded_models_ttl=loaded_models_ttl,
    )


# Admin singleton row id (mirrors _ADMIN_RECORD_ID in lm_studio_overrides_service
# and _EMBED_PREF_ADMIN_ID in memory_service). Only one row ever exists.
_BACKGROUND_PREF_ADMIN_ID: int = 1


async def resolve_background_model_id(
    *,
    engine: AsyncEngine,
    models_service: ModelsService | None,
    chat_model_id: str,
) -> str:
    """Return the model id the OUT-OF-BAND background tasks should use.

    Background tasks (auto-memory distillation, chat-title generation,
    follow-up chips) are best-effort auxiliary LLM calls fired after the
    user's turn streams. On a single local model they'd otherwise queue
    behind the user's NEXT turn; pinning a small/always-resident background
    model keeps the chat model free.

    FAIL-SOFT contract (unlike :func:`resolve_active_embedding_model_key`,
    which fails LOUD): this NEVER raises — any "not configured" /
    "configured but not loaded" / lookup-error path returns
    ``chat_model_id``.

    Resolution:

    1. Read ``preferred_background_model_id`` from
       ``server_lm_studio_default`` (id=1).
    2. If set AND currently loaded as an LLM (exact key, instance id, or
       ``preferred + "@"`` prefix — same match logic as the embedding
       resolver) → return the preferred id.
    2b. Reverse bare-``@``-strip fallback: if ``preferred`` pins a stale
        ``@<quant>`` that no longer matches, but the SAME bare-key family
        IS loaded (possibly under a different quant) → return the BARE
        key. Needed because the downstream wire-id resolver
        (``resolve_to_loaded_or_fallback``) has no family-aware fallback
        of its own and would otherwise substitute an unrelated loaded LLM.
    3. Otherwise (unset, not loaded, or any error) → return
       ``chat_model_id``.

    NOTE: NOT used by quality modes (self-consistency / CoVe) — those must
    stay on the chat model.

    Args:
        engine:         Async SQLAlchemy engine for reading the admin row.
        models_service: For querying currently-loaded models.
        chat_model_id:  The chat's model id — the always-safe fallback.

    Returns:
        Either the configured (and loaded) background model id, or
        ``chat_model_id``. Never raises.
    """
    if models_service is None:
        # No models service (legacy/test paths) → can't verify a model is
        # loaded; reuse the chat model (always safe).
        return chat_model_id
    try:
        preferred: str | None = None
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    select(
                        server_lm_studio_default.c.preferred_background_model_id
                    ).where(server_lm_studio_default.c.id == _BACKGROUND_PREF_ADMIN_ID)
                )
            ).first()
        if row is not None and row.preferred_background_model_id:
            preferred = str(row.preferred_background_model_id)

        if not preferred:
            # Unset → prefer the chat model if it's a non-coder,
            # non-embedding LLM; otherwise find any loaded non-coder LLM so
            # auto-memory distillation doesn't land on a coding specialist.

            def _is_coder_or_embed(key: str) -> bool:
                k = key.lower()
                return "coder" in k or "embed" in k

            loaded_for_fallback = await models_service.list_loaded()
            # Build the candidate set: LLMs with at least one loaded instance.
            eligible = [
                m
                for m in loaded_for_fallback
                if m.type == "llm"
                and m.loaded_instance_ids
                and not _is_coder_or_embed(m.key)
            ]
            if not _is_coder_or_embed(chat_model_id):
                # Chat model is fine — use it directly (original behaviour).
                return chat_model_id
            # Chat model IS a coder/embed variant — swap to first eligible.
            if eligible:
                swapped = eligible[0].key
                log.info(
                    "background_model.coder_chat_swapped",
                    original_chat_model_id=chat_model_id,
                    background_model_id=swapped,
                )
                return swapped
            # No eligible alternative — fall back to the chat model (fail-soft).
            return chat_model_id

        loaded = await models_service.list_loaded()
        # A model is only ACTUALLY usable when it has loaded_instance_ids; an
        # entry with an empty instance list is a downloaded-but-unloaded model.
        llm_models = [
            m for m in loaded if m.type == "llm" and m.loaded_instance_ids
        ]
        loaded_keys: set[str] = {m.key for m in llm_models}
        loaded_instance_ids: set[str] = {
            iid for m in llm_models for iid in m.loaded_instance_ids
        }

        def _resolve_loaded(key: str) -> str | None:
            """Return the id to use for *key* if it resolves to a loaded LLM
            instance, else ``None``.

            Steps 1-3 mirror the embedding resolver's matcher (exact key,
            exact instance id, or ``key + "@"`` quant prefix) and return
            *key* unchanged — the downstream ``resolve_to_loaded_or_fallback``
            accepts either a catalog key or an instance id.

            Step 4 (reverse bare-``@``-strip) is adapted, not mirrored:
            unlike the embedding resolver, ``resolve_to_loaded_or_fallback``
            has no family-aware fallback and would substitute ANY other
            loaded LLM when handed a stale ``key+"@"+quant``. So when
            *key*'s bare family IS loaded (under a different quant), this
            returns the BARE key instead — which hits the downstream
            resolver's exact-key match and lands on the right family.
            Same-bare-family only: *bare* is derived from *key* itself.
            """
            if key in loaded_keys:
                return key
            if key in loaded_instance_ids:
                return key
            prefix = key + "@"
            if any(iid.startswith(prefix) for iid in loaded_instance_ids):
                return key
            if "@" in key:
                bare = key.split("@", 1)[0]
                if bare in loaded_keys:
                    return bare
                bare_prefix = bare + "@"
                if any(iid.startswith(bare_prefix) for iid in loaded_instance_ids):
                    return bare
            return None

        resolved_preferred = _resolve_loaded(preferred)
        if resolved_preferred is not None:
            log.info(
                "background_model.resolved",
                background_model_id=resolved_preferred,
                chat_model_id=chat_model_id,
            )
            return resolved_preferred

        # Configured but not currently loaded → fall back to the chat model.
        log.info(
            "background_model.skipped_not_loaded",
            background_model_id=preferred,
            chat_model_id=chat_model_id,
        )
        return chat_model_id
    except Exception as exc:  # noqa: BLE001
        # Background work is best-effort: never let resolution failure break it.
        log.debug(
            "background_model.resolve_failed",
            chat_model_id=chat_model_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return chat_model_id
