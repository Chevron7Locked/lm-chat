# SPDX-License-Identifier: Apache-2.0
"""Shared content-hash dedup helpers.

Both the memory service (message/insight recall dedup) and the documents
service (RAG chunk dedup) key their dedup rows off a normalized-text
blake2b digest. This module is the single implementation both call into —
the hash is a persisted/compared dedup key, so the algorithm, digest size,
normalization, and encoding must never drift between callers.

Text hash: ``blake2b(digest_size=32)`` over the normalized text (whitespace-
collapsed, case-folded) → 64 hex chars, matching each table's
``String(64)`` ``text_hash`` column.
"""
from __future__ import annotations

import re
from hashlib import blake2b


def normalize_for_hash(text: str) -> str:
    """Collapse whitespace and case-fold *text* for content-hash dedup."""
    return re.sub(r"\s+", " ", text).strip().casefold()


def text_hash(text: str) -> str:
    """Return the blake2b hex digest (32 bytes → 64 hex chars) of *text*.

    Callers pass already-:func:`normalize_for_hash`-normalized text so that
    equivalent content (differing only in whitespace or case) hashes to the
    same dedup key.
    """
    return blake2b(text.encode(), digest_size=32).hexdigest()
