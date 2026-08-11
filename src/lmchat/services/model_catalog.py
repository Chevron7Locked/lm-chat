# SPDX-License-Identifier: Apache-2.0
"""Model catalog merge service — W1 of the multi-provider FE catalog plan.

Merges LM Studio's locally-loaded models with each enabled cloud provider's
model list to produce a unified ``list[ModelInfo]`` for ``GET /api/models``.

Design goals
------------
- **Additive / regression-safe:** when no cloud providers are configured or
  enabled, the output is identical to calling ``models_svc.list_loaded()``
  directly (byte-for-byte equivalent list, same ``provider="lmstudio"``
  defaults).
- **Per-provider cache (TTL ≥ 60 s):** ``GET /api/models`` is polled every
  25 s by the FE; without a cache that is an OpenRouter ``/v1/models`` call
  on every poll.  Each cloud provider's model list is cached independently so
  a slow provider does not delay the whole response.
- **Timeout + degrade:** a slow/unreachable cloud provider must NOT block
  ``GET /api/models``.  Its models are omitted from the merged list; the
  companion ``provider_status()`` method reports the per-provider reachability
  so the FE can surface "OpenRouter unreachable".
- **Auth-failure invalidation:** a 401/403 from ``list_models_detailed()``
  immediately evicts that provider's cache entry so a corrected key takes
  effect on the next poll rather than waiting for the TTL.
- **Registry invalidation:** ``invalidate(provider)`` / ``invalidate_all()``
  are called by the registry refresh path (admin PUT/DELETE already calls
  ``registry.refresh()``), ensuring stale entries do not linger after a
  provider is removed or reconfigured.

Cache storage
-------------
In-process ``dict`` keyed by provider slug.  Each entry is a
``_CacheEntry`` dataclass holding the cached model list, a UTC timestamp,
and the last reachability status.  No external storage; TTL ≥ 60 s.

Companion endpoint: ``GET /api/providers/status``
--------------------------------------------------
Returns ``list[ProviderStatus]`` — one entry per registered cloud provider
(never for "lmstudio") with ``{provider, reachable, error}``.  The route
handler calls :meth:`ModelCatalogService.provider_status`.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel

from lmchat.logging import get_logger
from lmchat.services.models_service import Capabilities, ModelInfo, ReasoningCapability

if TYPE_CHECKING:
    from lmchat.providers.openai_compat import OpenAICompatProvider
    from lmchat.services.models_service import ModelsService
    from lmchat.services.provider_config_service import ProviderConfigService
    from lmchat.services.provider_registry import ProviderRegistry

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CACHE_TTL_SEC: float = 60.0
"""Minimum time between cloud provider model-list fetches (seconds)."""

_FETCH_TIMEOUT_SEC: float = 8.0
"""Per-provider fetch timeout.  Slow providers degrade gracefully."""

_AUTH_FAIL_CODES: frozenset[int] = frozenset({401, 403})
"""HTTP status codes that trigger immediate cache invalidation (auth failure)."""


# ---------------------------------------------------------------------------
# Internal cache entry
# ---------------------------------------------------------------------------


@dataclass
class _CacheEntry:
    """Per-provider cache record."""

    models: list[ModelInfo]
    fetched_at: float  # time.monotonic()
    reachable: bool
    error: str | None = None

    def is_fresh(self, now: float, ttl: float = _CACHE_TTL_SEC) -> bool:
        """Return True if the entry is younger than *ttl* seconds."""
        return (now - self.fetched_at) < ttl


# ---------------------------------------------------------------------------
# Public response model
# ---------------------------------------------------------------------------


class ProviderStatus(BaseModel):
    """Reachability status for a single cloud provider."""

    provider: str
    reachable: bool
    error: str | None = None


# ---------------------------------------------------------------------------
# Capability mapping helpers
# ---------------------------------------------------------------------------


def _map_capabilities(item: Mapping[str, object]) -> Capabilities:
    """Map an OpenRouter-style model dict to :class:`Capabilities`.

    Fields checked (OpenRouter ``/v1/models`` shape):

    - ``context_length`` → not a capability; handled separately.
    - ``architecture.input_modalities`` list contains ``"image"``  → vision.
    - ``architecture.modality`` string contains ``"image"`` → vision fallback.
    - ``supported_parameters`` list contains ``"tools"`` or ``"tool_choice"``
      → trained_for_tool_use. If ``supported_parameters`` is ABSENT (or not a
      list) — e.g. Groq native, or a generic OpenAI-compat endpoint that
      doesn't emit OpenRouter's shape — trained_for_tool_use defaults to
      True rather than False. An unadvertised field is not evidence the
      model can't call tools, and defaulting False here used to hide the
      FE's entire integrations/tools picker for perfectly tool-capable
      cloud models that simply don't publish ``supported_parameters``.
      Only an EXPLICIT list that omits both ``"tools"`` and ``"tool_choice"``
      forces False.
    - ``supported_parameters`` contains ``"reasoning"`` or
      ``"include_reasoning"`` → reasoning capable.

    Missing / unknown fields default to ``False`` (never block a model),
    EXCEPT trained_for_tool_use, whose unknown-state default is True — see
    above. (Local LM Studio models resolve tool-use capability through a
    separate path in ``models_service.py``; this function only covers
    cloud / OpenAI-compat catalog inference.)
    """
    vision = False
    tool_use = False
    reasoning: ReasoningCapability | None = None

    # Vision: check architecture block first, then fallback to top-level modality.
    arch = item.get("architecture")
    if isinstance(arch, dict):
        # input_modalities is a list of strings like ["text", "image"]
        input_mods = arch.get("input_modalities")
        if isinstance(input_mods, list) and "image" in input_mods:
            vision = True
        # modality is sometimes a comma-string like "text+image->text"
        if not vision:
            modality = arch.get("modality")
            if isinstance(modality, str) and "image" in modality:
                vision = True

    # Fallback: top-level modality field (some providers use this)
    if not vision:
        top_modality = item.get("modality")
        if isinstance(top_modality, str) and "image" in top_modality:
            vision = True

    # Tool use and reasoning from supported_parameters.
    supported = item.get("supported_parameters")
    if isinstance(supported, list):
        # Field IS present as a list — trust it explicitly, including the
        # case where it's present-but-empty (a provider declaring "nothing
        # extra is supported" is a real signal, not the same as "absent").
        tool_use = "tools" in supported or "tool_choice" in supported
        if "reasoning" in supported or "include_reasoning" in supported:
            # Mark reasoning capability; use "on" as conservative default
            # (matches LM Studio's observed shape for reasoning models).
            reasoning = ReasoningCapability(
                allowed_options=["off", "on"],
                default="on",
            )
    else:
        # Field entirely absent (or malformed / not a list): unknown, not
        # "no tools". Default to tool-capable so providers that don't emit
        # OpenRouter's supported_parameters shape aren't penalized.
        tool_use = True

    return Capabilities(
        vision=vision,
        trained_for_tool_use=tool_use,
        reasoning=reasoning,
        embedding=False,
    )


def _map_model_info(item: Mapping[str, object], *, provider_slug: str) -> ModelInfo:
    """Convert a raw provider model dict to a :class:`ModelInfo` instance.

    Args:
        item:          Raw dict from the provider's ``/v1/models`` ``data`` array.
        provider_slug: Provider name (e.g. ``"openrouter"``).

    Returns:
        A :class:`ModelInfo` with ``provider=provider_slug`` and
        capabilities mapped from the raw item.
    """
    model_id = str(item.get("id", ""))
    # OpenRouter uses the model id as display name; some providers supply a
    # human-readable "name" field.
    display_name = str(item.get("name") or model_id)

    context_length_raw = item.get("context_length")
    try:
        if isinstance(context_length_raw, (int, float, str)):
            max_ctx = int(context_length_raw)  # type: ignore[arg-type]
        else:
            max_ctx = 0
    except (TypeError, ValueError):
        max_ctx = 0

    capabilities = _map_capabilities(item)

    return ModelInfo(
        key=model_id,
        display_name=display_name,
        type="llm",
        provider=provider_slug,
        # Cloud models are always "available" — no loaded/unloaded split.
        loaded_instances=1,
        loaded_instance_ids=[],
        max_context_length=max_ctx,
        loaded_context_length=max_ctx,
        capabilities=capabilities,
    )


# ---------------------------------------------------------------------------
# ModelCatalogService
# ---------------------------------------------------------------------------


class ModelCatalogService:
    """Merges LM Studio models with enabled cloud provider models.

    Intended lifecycle: constructed once during the FastAPI lifespan and
    stored at ``app.state.model_catalog``.  The route handler calls
    :meth:`list_merged` for ``GET /api/models`` and
    :meth:`provider_status` for ``GET /api/providers/status``.

    Args:
        models_svc: The LM Studio :class:`~lmchat.services.models_service.ModelsService`.
        registry:   The live provider registry.
        ttl:        Cache TTL in seconds (default ``_CACHE_TTL_SEC = 60``).
    """

    def __init__(
        self,
        *,
        models_svc: ModelsService,
        registry: ProviderRegistry,
        config_svc: ProviderConfigService | None = None,
        ttl: float = _CACHE_TTL_SEC,
        fetch_timeout: float = _FETCH_TIMEOUT_SEC,
    ) -> None:
        self._models_svc = models_svc
        self._registry = registry
        self._config_svc = config_svc
        self._ttl = ttl
        self._fetch_timeout = fetch_timeout
        # provider slug → _CacheEntry
        self._cache: dict[str, _CacheEntry] = {}
        # Per-provider asyncio.Lock for fetch de-duplication.  Created lazily
        # so concurrent callers for the same provider share one fetch instead
        # of racing to issue duplicate network calls.
        self._fetch_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # Cache management
    # ------------------------------------------------------------------

    def invalidate(self, provider: str) -> None:
        """Evict *provider*'s cache entry immediately.

        Called on registry refresh (admin PUT/DELETE) so a reconfigured
        provider's stale list is never served after the key is updated.

        Args:
            provider: Provider slug to evict.
        """
        self._cache.pop(provider, None)
        log.debug(
            "model_catalog.invalidate",
            provider=provider,
        )

    def invalidate_all(self) -> None:
        """Evict all cloud provider cache entries.

        Called after a full registry refresh so stale entries from removed
        providers are not served.
        """
        self._cache.clear()
        log.debug("model_catalog.invalidate_all")

    # ------------------------------------------------------------------
    # Internal fetch helpers
    # ------------------------------------------------------------------

    async def _fetch_provider(
        self,
        provider_slug: str,
        provider: OpenAICompatProvider,
    ) -> _CacheEntry:
        """Fetch and cache detailed models for one cloud provider.

        Respects the per-entry TTL; returns a cached entry when fresh.
        Invalidates on 401/403 auth failures immediately.

        Args:
            provider_slug: The registry name for this provider.
            provider:      The :class:`OpenAICompatProvider` instance.

        Returns:
            A :class:`_CacheEntry` (cached or freshly fetched).
        """
        # --- fetch de-duplication ---
        # Lazily create a per-provider lock.  A concurrent second caller for the
        # same provider waits here and, once the first caller exits the lock body,
        # finds a fresh cache entry and returns it without issuing a second fetch.
        if provider_slug not in self._fetch_locks:
            self._fetch_locks[provider_slug] = asyncio.Lock()
        lock = self._fetch_locks[provider_slug]

        async with lock:
            # Re-check freshness inside the lock — a concurrent caller may have
            # just completed a fetch while we were waiting.
            now = time.monotonic()
            existing = self._cache.get(provider_slug)
            if existing is not None and existing.is_fresh(now, self._ttl):
                return existing

            return await self._do_fetch(provider_slug, provider, now)

    async def _do_fetch(
        self,
        provider_slug: str,
        provider: OpenAICompatProvider,
        now: float,
    ) -> _CacheEntry:
        """Execute a real network fetch (called under the per-provider lock)."""
        # Fetch with timeout so a slow provider never blocks the whole response.
        try:
            items, http_status, error = await asyncio.wait_for(
                provider.list_models_detailed(),
                timeout=self._fetch_timeout,
            )
        except TimeoutError:
            log.warning(
                "model_catalog.fetch_timeout",
                provider=provider_slug,
                timeout_sec=self._fetch_timeout,
            )
            error = f"Timed out after {self._fetch_timeout}s"
            items = []
            http_status = None

        # Auth failure: store the failure entry so provider_status() surfaces the
        # real error instead of "not yet fetched".  Any key correction goes through
        # PUT /api/admin/providers → registry.refresh() → catalog.invalidate(), which
        # evicts this entry, so caching here does NOT prevent a corrected key from
        # taking effect on the next poll.
        if http_status is not None and http_status in _AUTH_FAIL_CODES:
            log.warning(
                "model_catalog.auth_failure",
                provider=provider_slug,
                status=http_status,
            )
            entry = _CacheEntry(
                models=[],
                fetched_at=now,
                reachable=False,
                error=f"Auth failure (HTTP {http_status})",
            )
            self._cache[provider_slug] = entry
            return entry

        if error is not None:
            # Network/parse error — cache the failure briefly to avoid hammering.
            entry = _CacheEntry(
                models=[],
                fetched_at=now,
                reachable=False,
                error=error,
            )
            self._cache[provider_slug] = entry
            log.warning(
                "model_catalog.fetch_error",
                provider=provider_slug,
                error=error,
            )
            return entry

        models = [_map_model_info(item, provider_slug=provider_slug) for item in items]
        entry = _CacheEntry(
            models=models,
            fetched_at=now,
            reachable=True,
            error=None,
        )
        self._cache[provider_slug] = entry
        log.info(
            "model_catalog.fetched",
            provider=provider_slug,
            count=len(models),
        )
        return entry

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def list_merged(self) -> list[ModelInfo]:
        """Return LM Studio models + all enabled cloud providers' models.

        When no cloud providers are registered (i.e. ``ProviderRegistry.names()``
        is ``["lmstudio"]`` only), the result is identical to calling
        ``models_svc.list_loaded()`` directly — same objects, same order.

        Cloud providers are fetched concurrently (with a per-provider timeout
        and TTL cache).  An unreachable provider contributes an empty slice;
        its status is surfaced via :meth:`provider_status`.

        Returns:
            Merged ``list[ModelInfo]``, LM Studio first, then cloud providers
            in sorted name order (matching ``ProviderRegistry.names()``).
        """
        # LM Studio models first — primary, always present.
        lm_models = await self._models_svc.list_loaded()

        # Identify cloud (non-lmstudio) providers.
        cloud_names = [n for n in self._registry.names() if n != "lmstudio"]
        if not cloud_names:
            # Regression-safe: identical to the legacy path.
            return lm_models

        # Fetch all cloud providers concurrently.
        cloud_providers: list[tuple[str, OpenAICompatProvider]] = []
        for name in cloud_names:
            prov = self._registry.get(name)
            if prov is not None:
                cloud_providers.append((name, prov))  # type: ignore[arg-type]

        if not cloud_providers:
            return lm_models

        tasks = [
            self._fetch_provider(slug, prov) for slug, prov in cloud_providers
        ]
        entries = await asyncio.gather(*tasks)

        # Build slug → allowed_models map from the config service (cheap
        # in-memory list_all(); called once per list_merged invocation).
        # NULL/empty list → no filter (all models shown).
        # Non-empty list → exact case-sensitive id match.
        # LM Studio is never filtered (no config row for it).
        allowlists: dict[str, list[str]] = {}
        if self._config_svc is not None:
            for cfg in await self._config_svc.list_all():
                if cfg.allowed_models:
                    allowlists[cfg.provider] = cfg.allowed_models

        merged = list(lm_models)
        for (slug, _), entry in zip(cloud_providers, entries, strict=True):
            allowed = allowlists.get(slug)
            if allowed:
                # Allowlist governs the PICKER only; dispatch is not blocked
                # (the model id remains valid upstream).  Filter here so the
                # FE picker and /api/models only show the approved subset.
                filtered = [m for m in entry.models if m.key in allowed]
            else:
                filtered = entry.models
            merged.extend(filtered)

        return merged

    async def provider_status(self) -> list[ProviderStatus]:
        """Return per-provider reachability status for all cloud providers.

        Actively fetches each cloud provider (respecting the TTL cache) so
        that ``GET /api/providers/status`` returns real reachability immediately
        after a provider is added — without needing a prior ``GET /api/models``
        poll to warm the cache.

        Providers are fetched concurrently (same path as :meth:`list_merged`).
        A configured, reachable provider reports ``reachable=True`` right away;
        an unreachable or auth-failed one reports the real error (e.g.
        ``"Timed out after 8.0s"`` or ``"Auth failure (HTTP 401)"``), never
        ``"not yet fetched"``.

        The only edge case where ``reachable=False`` with a descriptive note is
        returned without an actual fetch is when the provider slug appears in
        the registry name list but ``registry.get(slug)`` returns ``None``
        (i.e. no live provider instance — should not happen under normal
        operation).

        Returns:
            One :class:`ProviderStatus` per cloud provider (never "lmstudio").
        """
        cloud_names = [n for n in self._registry.names() if n != "lmstudio"]
        if not cloud_names:
            return []

        # Collect (name, provider) pairs.  Slugs with no live instance get a
        # sentinel result (no fetch needed — something is wrong with the registry).
        fetch_pairs: list[tuple[str, OpenAICompatProvider]] = []
        no_instance: list[str] = []
        for name in cloud_names:
            prov = self._registry.get(name)
            if prov is not None:
                fetch_pairs.append((name, prov))  # type: ignore[arg-type]
            else:
                no_instance.append(name)

        # Fetch all live providers concurrently (TTL cache respected inside
        # _fetch_provider; lock prevents duplicate in-flight calls).
        entries: list[_CacheEntry] = []
        if fetch_pairs:
            tasks = [self._fetch_provider(slug, prov) for slug, prov in fetch_pairs]
            entries = list(await asyncio.gather(*tasks))

        result: list[ProviderStatus] = []
        for (name, _), entry in zip(fetch_pairs, entries, strict=True):
            result.append(
                ProviderStatus(
                    provider=name,
                    reachable=entry.reachable,
                    error=entry.error,
                )
            )
        for name in no_instance:
            result.append(
                ProviderStatus(
                    provider=name,
                    reachable=False,
                    error="no provider instance in registry",
                )
            )
        return result
