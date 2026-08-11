# SPDX-License-Identifier: Apache-2.0
"""Unit tests for lmchat.utils.lru_counter.LruCappedCounter.

Extracted from two hand-duplicated LRU counters (StreamingService's
cross-turn tool-round tracker and the sub-session tool-round registry in
routes/chats.py) — these tests pin the shared eviction contract: increment
bumps recency, plain reads (``.get``) do NOT, and the cap evicts the
least-recently-INCREMENTED key.
"""
from __future__ import annotations

from lmchat.utils.lru_counter import LruCappedCounter


def test_increment_starts_at_one_and_accumulates() -> None:
    counter = LruCappedCounter(cap=10)
    assert counter.increment(1) == 1
    assert counter.increment(1) == 2
    assert counter.increment(1) == 3
    assert counter[1] == 3


def test_get_defaults_like_dict_get() -> None:
    counter = LruCappedCounter(cap=10)
    assert counter.get(1) is None
    assert counter.get(1, 0) == 0
    counter.increment(1)
    assert counter.get(1) == 1


def test_reset_removes_key_and_is_idempotent() -> None:
    counter = LruCappedCounter(cap=10)
    counter.increment(1)
    counter.reset(1)
    assert 1 not in counter
    counter.reset(1)  # must not raise on an absent key
    assert 1 not in counter


def test_dict_like_access_surface() -> None:
    """Subscription, `in`, and len() must behave like a plain dict — callers
    (both StreamingService and its tests) read the counter directly."""
    counter = LruCappedCounter(cap=10)
    counter.increment(5)
    assert counter[5] == 1
    assert 5 in counter
    assert len(counter) == 1


def test_lru_eviction_targets_least_recently_incremented() -> None:
    """Exceeding the cap evicts the LEAST-RECENTLY-INCREMENTED key, not the
    oldest by insertion alone, and not by any other order."""
    cap = 4
    counter = LruCappedCounter(cap=cap)

    for key in range(1, cap + 1):
        counter.increment(key)
    assert len(counter) == cap

    # One more key pushes past the cap; key=1 (never touched again) evicts.
    counter.increment(cap + 1)
    assert len(counter) == cap
    assert 1 not in counter
    assert (cap + 1) in counter

    # key=2 is now the LRU victim.
    counter.increment(cap + 2)
    assert len(counter) == cap
    assert 2 not in counter


def test_get_does_not_bump_recency() -> None:
    """A plain `.get()` read must NOT count as a recency touch — only
    `increment` does. This is the exact bug this test would catch on
    revert: if `.get()` bumped recency, the "wrong" key would survive
    eviction below."""
    cap = 3
    counter = LruCappedCounter(cap=cap)
    for key in range(1, cap + 1):
        counter.increment(key)

    _ = counter.get(1)  # must NOT bump key=1's recency

    counter.increment(cap + 1)
    assert 1 not in counter, "reading via .get() must not protect a key from eviction"
    assert 2 in counter


def test_increment_on_existing_key_bumps_recency() -> None:
    """Re-incrementing an existing key moves it to the back of the eviction
    queue, protecting it from the next eviction."""
    cap = 3
    counter = LruCappedCounter(cap=cap)
    for key in range(1, cap + 1):
        counter.increment(key)

    counter.increment(1)  # touch key=1 again — it should no longer be the LRU victim

    counter.increment(cap + 1)
    assert 1 in counter, "re-incrementing must protect a key from eviction"
    assert 2 not in counter


def test_copy_deepcopy_pickle_preserve_cap_and_counts() -> None:
    """The custom __init__(cap) must not break reconstruction: copy/deepcopy/
    pickle carry the cap so a rebuilt counter still evicts correctly."""
    import copy as _copy
    import pickle

    counter = LruCappedCounter(cap=2)
    counter.increment(1)
    counter.increment(2)

    for clone in (counter.copy(), _copy.copy(counter), _copy.deepcopy(counter),
                  pickle.loads(pickle.dumps(counter))):
        assert isinstance(clone, LruCappedCounter)
        assert dict(clone) == {1: 1, 2: 1}
        # cap survived: incrementing a 3rd key still evicts down to 2 entries.
        clone.increment(3)
        assert len(clone) == 2
