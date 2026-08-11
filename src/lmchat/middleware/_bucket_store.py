# SPDX-License-Identifier: Apache-2.0
"""Token-bucket store for login rate-limiting.

The ``BucketStore`` ABC defines the consume interface;
``InMemoryBucketStore`` is the default.  A Redis-backed
implementation is deferred for multi-replica deploys.

Design decisions:
- **Per-key asyncio.Lock** rather than a global lock.  A global lock
  would serialize all rate-limit checks across all users; per-key locks
  confine contention to a single key and keep the hot-path latency low.
  The tradeoff is a slightly higher memory footprint (one Lock object per
  live key), which is negligible at the login traffic volumes this service
  handles.
- **Opportunistic cleanup on consume()**.  Rather than running a separate
  lifespan task, ``InMemoryBucketStore`` checks on every ``consume()``
  call whether the number of buckets has grown past a threshold and runs a
  cleanup pass then.  This keeps the store's memory bounded without
  requiring a background task, and stays correct under the single-event-
  loop (single-machine) deployment model.  A dedicated lifespan sweeper
  could be added later if bucket cardinality grows unexpectedly.
"""
from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from typing import Final

_CLEANUP_THRESHOLD: Final[int] = 1000
_DEFAULT_MAX_AGE_SECONDS: Final[float] = 3600.0


class BucketStore(ABC):
    """Abstract token-bucket store.

    Implementations must be safe for concurrent ``asyncio`` callers.
    """

    @abstractmethod
    async def consume(
        self,
        key: str,
        *,
        rate: float,
        burst: int,
    ) -> tuple[bool, float]:
        """Attempt to consume one token for ``key``.

        Args:
            key:   Opaque string identifying the rate-limit bucket
                   (e.g. ``"login:<ip>:<username>"``).
            rate:  Token refill rate in tokens per second.
            burst: Maximum token capacity (and the initial fill level
                   for a new bucket).

        Returns:
            A ``(allowed, retry_after)`` tuple.  ``allowed=True`` means
            a token was consumed and the caller may proceed.
            ``retry_after`` is ``0.0`` when ``allowed`` is ``True``; it
            is the number of seconds the caller should wait before the
            next token is available when ``allowed`` is ``False``.
        """


class _BucketState:
    """Mutable token-bucket state for a single key.

    Attributes:
        tokens:    Current token count (may be fractional between refills).
        last_seen: Monotonic timestamp of the last ``consume()`` call for
                   this key (used for both refill computation and stale
                   cleanup).
        lock:      Per-key asyncio lock; serializes concurrent updates.
    """

    __slots__ = ("lock", "last_seen", "tokens")

    def __init__(self, *, tokens: float, now: float) -> None:
        """Initialise with ``tokens`` pre-filled and ``last_seen=now``."""
        self.tokens: float = tokens
        self.last_seen: float = now
        self.lock: asyncio.Lock = asyncio.Lock()


class InMemoryBucketStore(BucketStore):
    """In-process token-bucket store backed by a plain ``dict``.

    Each key gets its own :class:`_BucketState` instance, isolated
    behind a per-key ``asyncio.Lock``.  Stale buckets are evicted
    opportunistically when the total bucket count passes
    ``_CLEANUP_THRESHOLD``.

    Thread-safety: **not thread-safe**.  This store is intended for use
    within a single asyncio event loop.  Multi-process or multi-replica
    deployments should use the Redis backend.
    """

    def __init__(self) -> None:
        """Initialise an empty bucket store."""
        self._buckets: dict[str, _BucketState] = {}

    async def consume(
        self,
        key: str,
        *,
        rate: float,
        burst: int,
    ) -> tuple[bool, float]:
        """Attempt to consume one token for ``key``.

        If the key is new, the bucket starts at ``burst`` tokens (fully
        charged), then one token is consumed immediately — so the first
        ``burst`` calls in quick succession all succeed.

        The refill is computed as ``elapsed_seconds * rate``, capped at
        ``burst``.  Fractional tokens accumulate until they cross 1.0.

        Returns:
            ``(True, 0.0)`` when the token was consumed.
            ``(False, retry_after_seconds)`` when the bucket is empty.
            ``retry_after_seconds`` is the ceiling time until one full
            token is available: ``(1 - current_tokens) / rate``.
        """
        now = time.monotonic()

        # Create bucket if needed (without the per-key lock — the check
        # is safe because Python dict writes are atomic at the GIL level,
        # and asyncio is single-threaded; the asyncio.Lock below handles
        # re-entrant concurrent coroutines on the same key).
        if key not in self._buckets:
            self._buckets[key] = _BucketState(tokens=float(burst), now=now)

        # Opportunistic cleanup: if the store has grown large, evict
        # buckets that have been idle for _DEFAULT_MAX_AGE_SECONDS.
        if len(self._buckets) > _CLEANUP_THRESHOLD:
            self.cleanup_stale(now, max_age_seconds=_DEFAULT_MAX_AGE_SECONDS)

        state = self._buckets[key]

        async with state.lock:
            # Refill tokens based on elapsed time.
            elapsed = now - state.last_seen
            state.tokens = min(float(burst), state.tokens + elapsed * rate)
            state.last_seen = now

            if state.tokens >= 1.0:
                state.tokens -= 1.0
                return (True, 0.0)

            # Bucket is empty; compute when the next token will arrive.
            retry_after = (1.0 - state.tokens) / rate
            return (False, retry_after)

    def cleanup_stale(
        self,
        now_ts: float,
        *,
        max_age_seconds: float,
    ) -> int:
        """Remove buckets idle for longer than ``max_age_seconds``.

        Args:
            now_ts:           Current monotonic timestamp (from
                              ``time.monotonic()``).
            max_age_seconds:  Buckets with ``last_seen`` older than this
                              many seconds are removed.

        Returns:
            The number of buckets removed.

        Note:
            This method does **not** acquire per-key locks before
            removing entries.  It is intended to be called from within a
            ``consume()`` call (where the opportunistic threshold has been
            crossed) or from the asyncio event loop before any concurrent
            ``consume()`` is in flight.  In practice, the cleanup removes
            buckets for keys that have been idle for at least an hour, so
            the probability of a concurrent ``consume()`` on a stale key
            is negligible.
        """
        cutoff = now_ts - max_age_seconds
        stale = [k for k, v in self._buckets.items() if v.last_seen < cutoff]
        for k in stale:
            self._buckets.pop(k, None)
        return len(stale)
