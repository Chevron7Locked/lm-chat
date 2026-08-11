# SPDX-License-Identifier: Apache-2.0
"""LRU-capped integer counter shared by the cross-turn tool-round trackers.

Both ``StreamingService._tool_round_counts`` and the sub-session tool-round
registry in ``routes/chats.py`` need the same primitive: an int-keyed
counter that increments per key and evicts the least-recently-INCREMENTED
key once a cap is exceeded (``.get(...)`` reads do NOT bump recency — only
``increment`` does). This module extracts that primitive once so both call
sites share one eviction implementation instead of two hand-maintained
copies.
"""
from __future__ import annotations

from collections import OrderedDict


class LruCappedCounter(OrderedDict[int, int]):
    """An int-keyed counter with an LRU eviction cap.

    Subclasses ``OrderedDict`` so it stays a drop-in dict-like counter
    (subscription, ``in``, ``len()``, ``.get()``, ``.pop()``, ...) for
    callers that read it directly, while adding the increment-and-evict
    behaviour shared by both consumers.

    The custom ``__init__(cap)`` would otherwise break reconstruction paths
    that call ``self.__class__(self)`` (``OrderedDict.copy``, ``copy.copy`` /
    ``copy.deepcopy``, pickling), so ``copy`` and ``__reduce__`` below carry
    the cap through explicitly.
    """

    def __init__(self, cap: int) -> None:
        super().__init__()
        self._cap = cap

    def copy(self) -> LruCappedCounter:
        """Return a shallow copy that preserves the cap and current counts."""
        new = LruCappedCounter(self._cap)
        new.update(self)
        return new

    def __reduce__(
        self,
    ) -> tuple[type[LruCappedCounter], tuple[int], None, None, object]:
        # Reconstruct via LruCappedCounter(cap) then replay items (5-tuple
        # dictitems slot), so copy/deepcopy/pickle don't call __init__ without
        # a cap. Item replay uses plain __setitem__ — no eviction re-triggered.
        return (self.__class__, (self._cap,), None, None, iter(self.items()))

    def increment(self, key: int) -> int:
        """Increment the counter for *key*, apply the LRU cap, return the new count.

        Evicts the least-recently-INCREMENTED key by design — plain
        ``.get(...)`` reads elsewhere do not bump recency.
        """
        self[key] = self.get(key, 0) + 1
        self.move_to_end(key)
        if len(self) > self._cap:
            self.popitem(last=False)
        return self[key]

    def reset(self, key: int) -> None:
        """Remove *key* from the counter. Idempotent."""
        self.pop(key, None)
