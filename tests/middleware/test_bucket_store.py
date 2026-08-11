# SPDX-License-Identifier: Apache-2.0
"""Unit tests for InMemoryBucketStore.

Verifies token-bucket semantics:
consume under capacity, consume to exhaustion, token refill after
elapsed time, and multi-key isolation.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from lmchat.middleware._bucket_store import InMemoryBucketStore, _BucketState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def drain_bucket(store: InMemoryBucketStore, key: str, *, rate: float, burst: int) -> int:
    """Consume tokens until the bucket is exhausted.

    Returns the number of successful consume() calls.
    """
    consumed = 0
    for _ in range(burst + 2):
        allowed, _ = await store.consume(key, rate=rate, burst=burst)
        if not allowed:
            break
        consumed += 1
    return consumed


# ---------------------------------------------------------------------------
# Under-capacity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_consume_returns_true() -> None:
    """First consume on a fresh bucket returns (True, 0.0)."""
    store = InMemoryBucketStore()
    allowed, retry_after = await store.consume("k", rate=1.0, burst=5)
    assert allowed is True
    assert retry_after == 0.0


@pytest.mark.asyncio
async def test_burst_minus_one_all_succeed() -> None:
    """burst-1 rapid consumes all succeed."""
    store = InMemoryBucketStore()
    burst = 5
    results = [
        await store.consume("k", rate=1.0 / 60.0, burst=burst) for _ in range(burst - 1)
    ]
    assert all(ok for ok, _ in results)


# ---------------------------------------------------------------------------
# At capacity then over
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_burst_th_call_still_succeeds() -> None:
    """The nth call (= burst) succeeds; the (n+1)th fails."""
    store = InMemoryBucketStore()
    burst = 3
    rate = 1.0 / 60.0  # 1 per minute
    for _ in range(burst):
        allowed, _ = await store.consume("k", rate=rate, burst=burst)
        assert allowed is True
    # Bucket now empty
    allowed, retry_after = await store.consume("k", rate=rate, burst=burst)
    assert allowed is False
    assert retry_after > 0.0


@pytest.mark.asyncio
async def test_over_threshold_returns_429_signal() -> None:
    """After burst tokens consumed, allowed=False and retry_after > 0."""
    store = InMemoryBucketStore()
    burst = 2
    rate = 1.0 / 60.0
    consumed = await drain_bucket(store, "k", rate=rate, burst=burst)
    assert consumed == burst

    allowed, retry_after = await store.consume("k", rate=rate, burst=burst)
    assert allowed is False
    assert retry_after > 0.0


@pytest.mark.asyncio
async def test_retry_after_is_bounded() -> None:
    """retry_after must be ≤ (1 / rate) + small epsilon."""
    store = InMemoryBucketStore()
    burst = 1
    rate = 1.0 / 60.0  # 1 per minute → max wait ≈ 60s
    # Consume the single token
    await store.consume("k", rate=rate, burst=burst)
    allowed, retry_after = await store.consume("k", rate=rate, burst=burst)
    assert allowed is False
    # retry_after should be ≤ 61 s (a few ms of jitter budget)
    assert retry_after <= 61.0


# ---------------------------------------------------------------------------
# Token refill after elapsed time
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refill_after_one_token_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    """After 1/rate seconds, a new token is available.

    We monkeypatch time.monotonic to advance the clock without sleeping.
    """
    fake_now: list[float] = [1000.0]

    def fake_monotonic() -> float:
        return fake_now[0]

    monkeypatch.setattr(time, "monotonic", fake_monotonic)

    store = InMemoryBucketStore()
    rate = 2.0  # 2 tokens/sec → 1 token per 0.5s
    burst = 2

    # Drain the bucket
    await store.consume("k", rate=rate, burst=burst)
    await store.consume("k", rate=rate, burst=burst)
    allowed, _ = await store.consume("k", rate=rate, burst=burst)
    assert allowed is False

    # Advance clock by 0.6 seconds (> 1/rate = 0.5 s)
    fake_now[0] += 0.6

    allowed2, retry2 = await store.consume("k", rate=rate, burst=burst)
    assert allowed2 is True
    assert retry2 == 0.0


@pytest.mark.asyncio
async def test_partial_refill_insufficient(monkeypatch: pytest.MonkeyPatch) -> None:
    """After less than 1/rate seconds, the bucket is still empty."""
    fake_now: list[float] = [1000.0]

    def fake_monotonic() -> float:
        return fake_now[0]

    monkeypatch.setattr(time, "monotonic", fake_monotonic)

    store = InMemoryBucketStore()
    rate = 2.0  # 1 token per 0.5s
    burst = 1

    await store.consume("k", rate=rate, burst=burst)
    allowed, _ = await store.consume("k", rate=rate, burst=burst)
    assert allowed is False

    # Advance by only 0.2 s — not enough for a full token
    fake_now[0] += 0.2
    allowed2, _ = await store.consume("k", rate=rate, burst=burst)
    assert allowed2 is False


# ---------------------------------------------------------------------------
# Multi-key isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_different_keys_are_independent() -> None:
    """Draining key A does not affect key B."""
    store = InMemoryBucketStore()
    burst = 3
    rate = 1.0 / 60.0
    # Drain key A
    for _ in range(burst):
        await store.consume("a", rate=rate, burst=burst)
    allowed_a, _ = await store.consume("a", rate=rate, burst=burst)
    assert allowed_a is False

    # Key B should be untouched
    allowed_b, retry_b = await store.consume("b", rate=rate, burst=burst)
    assert allowed_b is True
    assert retry_b == 0.0


@pytest.mark.asyncio
async def test_different_usernames_same_ip_independent() -> None:
    """Separate keys for the same IP but different usernames are independent."""
    store = InMemoryBucketStore()
    burst = 2
    rate = 1.0 / 60.0
    key_alice = "login:10.0.0.1:alice"
    key_bob = "login:10.0.0.1:bob"

    for _ in range(burst):
        await store.consume(key_alice, rate=rate, burst=burst)
    allowed_alice, _ = await store.consume(key_alice, rate=rate, burst=burst)
    assert allowed_alice is False

    allowed_bob, _ = await store.consume(key_bob, rate=rate, burst=burst)
    assert allowed_bob is True


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def test_cleanup_stale_removes_old_entries() -> None:
    """cleanup_stale() removes buckets beyond max_age_seconds."""
    store = InMemoryBucketStore()
    now = time.monotonic()
    store._buckets["old_key"] = _BucketState(tokens=5.0, now=now - 7200)
    store._buckets["new_key"] = _BucketState(tokens=5.0, now=now - 10)

    removed = store.cleanup_stale(now, max_age_seconds=3600.0)
    assert removed == 1
    assert "old_key" not in store._buckets
    assert "new_key" in store._buckets


def test_cleanup_stale_returns_zero_when_nothing_stale() -> None:
    """cleanup_stale() returns 0 when no buckets are stale."""
    store = InMemoryBucketStore()
    now = time.monotonic()
    store._buckets["fresh"] = _BucketState(tokens=5.0, now=now - 100)
    removed = store.cleanup_stale(now, max_age_seconds=3600.0)
    assert removed == 0


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_consumes_do_not_exceed_burst() -> None:
    """Concurrent coroutines consuming the same key never exceed burst."""
    store = InMemoryBucketStore()
    burst = 5
    rate = 1.0 / 60.0

    results = await asyncio.gather(
        *[store.consume("concurrent", rate=rate, burst=burst) for _ in range(burst + 3)]
    )
    success_count = sum(1 for ok, _ in results if ok)
    # Exactly burst tokens are available; no more succeed
    assert success_count == burst
