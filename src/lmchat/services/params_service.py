# SPDX-License-Identifier: Apache-2.0
"""Per-model rejected-parameter cache for lm-chat.

This is the load-bearing moat: every request body is stripped of
parameters the target model has previously rejected (via HTTP 400),
preventing redundant rejection loops and enabling the SPA to hide
unsupported controls.

Cache architecture
------------------
- In-memory ``dict[str, dict[str, float]]`` on ``app.state.params_service``
  — per model, a mapping of rejected param name → monotonic timestamp of
  when the rejection was recorded.  No DB persistence in v1 — the cache
  rebuilds reactively from upstream 400 responses; startup cost is one 400
  per model per param per replica.
- Entries expire after
  ``_REJECTED_PARAM_TTL_SEC`` (purged lazily on read), so one transient 400
  no longer disables a param until process restart — the param re-probes
  naturally on the next request after expiry.  ``ModelsService`` also calls
  ``clear_for_model()`` when a probe shows a model newly (re)loaded.
- Per-model-id ``asyncio.Lock`` (lazily created via ``dict.setdefault``)
  prevents lost updates when the streaming retry loop fires concurrent
  ``record_rejection`` calls for the same model.
- Global lock (``self._global_lock``) guards only the lock-dict itself
  during lazy creation, NOT the entire cache — per-model granularity is
  deliberate to avoid the global lock becoming a bottleneck under high
  concurrent stream load.

Provider dimension
------------------
The cache key is ``(provider, model_id)``.  v1.0 pins the
provider to ``"lmstudio"``; the API takes only ``model_id`` for simplicity.
The ``_PROVIDER_TAG`` constant is wired into log entries for forward-compat
observability without leaking multi-provider surface into the public API.

Metrics
-------
``lm_chat_params_rejected_total{model_id}`` is incremented on every reactive
rejection learned (not on strip calls, not on proactive seeds).
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Final

from lmchat.logging import get_logger
from lmchat.metrics import PARAMS_REJECTED

log = get_logger(__name__)

_PROVIDER_TAG: Final[str] = "lmstudio"

# How long a rejected param stays
# in the cache before it is purged and re-probes on the next request.
# Module-level so tests can monkeypatch it.
_REJECTED_PARAM_TTL_SEC: float = 1800.0

# Monotonic-clock indirection — monkeypatchable in tests without mutating the
# shared ``time`` module.  Monotonic (not wall-clock) so the TTL is immune to
# clock skew / NTP jumps.
_monotonic = time.monotonic


class ParamsService:
    """In-memory per-model rejected-parameter cache.

    All async methods are safe for concurrent calls across coroutines.
    ``strip_rejected`` is sync and does no I/O — call it on the hot path.
    """

    def __init__(self) -> None:
        """Initialise an empty cache with no locks pre-allocated."""
        # model_id → {rejected param name → monotonic timestamp recorded}.
        # Entries older than _REJECTED_PARAM_TTL_SEC are purged lazily on
        # read so a rejection is bounded, not permanent.
        self._cache: dict[str, dict[str, float]] = {}
        # Per-model asyncio.Lock — lazily created on first use.
        self._locks: dict[str, asyncio.Lock] = {}
        # Guards _locks dict mutation only (not the cache reads/writes).
        self._global_lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _model_lock(self, model_id: str) -> asyncio.Lock:
        """Return (creating if absent) the per-model asyncio.Lock.

        Args:
            model_id: Model identifier string.

        Returns:
            The ``asyncio.Lock`` dedicated to *model_id*.
        """
        async with self._global_lock:
            return self._locks.setdefault(model_id, asyncio.Lock())

    def _purge_expired(self, model_id: str) -> None:
        """Drop TTL-expired rejected params for *model_id*.

        Pure sync, no await points — safe to call from both the async
        read path and the sync hot path without racing the per-model lock
        (the event loop never interleaves another coroutine mid-call).

        Args:
            model_id: LM Studio model key whose entries to purge.
        """
        entries = self._cache.get(model_id)
        if not entries:
            return
        now = _monotonic()
        expired = [
            param
            for param, recorded_at in entries.items()
            if now - recorded_at >= _REJECTED_PARAM_TTL_SEC
        ]
        if not expired:
            return
        for param in expired:
            del entries[param]
        if not entries:
            del self._cache[model_id]
        log.info(
            "params_cache.ttl_expired",
            model_id=model_id,
            expired_params=sorted(expired),
            ttl_sec=_REJECTED_PARAM_TTL_SEC,
            provider=_PROVIDER_TAG,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_rejected(self, *, model_id: str) -> frozenset[str]:
        """Return the current rejected-parameter set for *model_id*.

        Never raises.  Returns an empty frozenset for models not yet
        in the cache.

        Args:
            model_id: LM Studio model key (e.g. ``"qwen3-8b"``).

        Returns:
            Immutable set of param names known to be rejected by the model.
        """
        self._purge_expired(model_id)
        return frozenset(self._cache.get(model_id, {}))

    async def record_rejection(self, *, model_id: str, param: str) -> None:
        """Add *param* to the rejected set for *model_id*.

        Idempotent for logs/metrics — recording the same param twice
        emits nothing new, but refreshes the entry's TTL window (
        a fresh 400 proves the rejection is still live).
        Acquires the per-model lock so concurrent retry-loop calls for
        the same model serialise correctly and never lose an update.

        Args:
            model_id: LM Studio model key.
            param:    Parameter name that triggered HTTP 400 (e.g.
                      ``"reasoning"``).
        """
        lock = await self._model_lock(model_id)
        async with lock:
            entry = self._cache.setdefault(model_id, {})
            if param in entry:
                # Already recorded — idempotent for logs/metrics, but refresh
                # the TTL window: a fresh 400 proves the rejection is still
                # live.
                entry[param] = _monotonic()
                return
            entry[param] = _monotonic()
            log.info(
                "params_cache.rejection_recorded",
                model_id=model_id,
                param=param,
                provider=_PROVIDER_TAG,
            )
            PARAMS_REJECTED.labels(model_id=model_id).inc()

    async def seed_from_capabilities(
        self, *, model_id: str, unsupported: list[str]
    ) -> None:
        """Bulk-seed the cache from a capabilities ``unsupported_params`` list.

        Forward-compatibility hook.  NOT called in v1.0 because
        LM Studio's ``/api/v1/models`` does not expose ``unsupported_params``
        on the capabilities object (verified live; see
        ``docs/LM_STUDIO_HARNESS.md``).  Implemented so the streaming service
        can call it transparently if a future LM Studio release adds the field.

        Each param in *unsupported* is recorded exactly as if
        ``record_rejection`` had been called — per-model lock is acquired once
        for the bulk write rather than N times.

        Args:
            model_id:    LM Studio model key.
            unsupported: List of param names surfaced by the capabilities
                         probe (currently always empty in v1.0).
        """
        if not unsupported:
            return
        lock = await self._model_lock(model_id)
        async with lock:
            entry = self._cache.setdefault(model_id, {})
            new_params = frozenset(unsupported) - entry.keys()
            if not new_params:
                return
            now = _monotonic()
            for param in new_params:
                entry[param] = now
            log.info(
                "params_cache.seeded_from_capabilities",
                model_id=model_id,
                seeded=sorted(new_params),
                provider=_PROVIDER_TAG,
            )
            for _p in new_params:
                PARAMS_REJECTED.labels(model_id=model_id).inc()

    def strip_rejected(self, body: dict[str, Any], *, model_id: str) -> dict[str, Any]:
        """Return a shallow copy of *body* with rejected params removed.

        Pure sync — no I/O.  Safe to call on the hot path before every
        upstream request.  The caller's dict is NEVER mutated.

        Args:
            body:     Encoded request body dict.
            model_id: LM Studio model key used to look up the rejected set.

        Returns:
            A new ``dict`` containing all keys from *body* except those in the
            rejected set for *model_id*.
        """
        self._purge_expired(model_id)
        rejected = self._cache.get(model_id)
        if not rejected:
            # Fast path: no rejections recorded — return a shallow copy
            # (contract: always return a new dict, never the same object).
            return dict(body)
        stripped = {k: v for k, v in body.items() if k not in rejected}
        removed = set(body) & rejected.keys()
        if removed:
            # WARNING (was info) — stripping is silent capability
            # loss (an admin-intended setting never reaches the model),
            # so it must be observable in logs by default.
            log.warning(
                "params_cache.strip_applied",
                model_id=model_id,
                stripped_params=sorted(removed),
                provider=_PROVIDER_TAG,
            )
        return stripped

    def clear_for_model(self, model_key: str) -> None:
        """Synchronously drop all rejected params for *model_key*.

        Called by ``ModelsService`` when a probe shows the model newly
        (re)loaded — a fresh load may use a different runtime/template, so
        stale rejections must not survive the reload.  No-op for models
        with no recorded rejections.

        Args:
            model_key: LM Studio model key whose rejected set to clear.
        """
        if self._cache.pop(model_key, None) is not None:
            log.info(
                "params_cache.cleared_for_reloaded_model",
                model_id=model_key,
                provider=_PROVIDER_TAG,
            )

    async def invalidate(self, *, model_id: str | None = None) -> None:
        """Clear cached rejections for one model (or all models).

        Admin operation.  After invalidation the cache rebuilds reactively
        on the next request that encounters a 400.

        Args:
            model_id: When set, clear only this model's rejected set.
                      When ``None``, clear the entire cache.
        """
        if model_id is None:
            self._cache.clear()
            log.info("params_cache.invalidated_all", provider=_PROVIDER_TAG)
        else:
            self._cache.pop(model_id, None)
            log.info(
                "params_cache.invalidated_model",
                model_id=model_id,
                provider=_PROVIDER_TAG,
            )
