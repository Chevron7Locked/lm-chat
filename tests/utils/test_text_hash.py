# SPDX-License-Identifier: Apache-2.0
"""Tests for lmchat.utils.text_hash.

This is the shared dedup-key implementation extracted out of
memory_service and documents_service — both persist ``text_hash`` as a
comparison key, so any drift in the algorithm, digest size, normalization,
or encoding silently breaks dedup. The known-vector test below pins the
exact hash the pre-extraction, per-service implementations produced, so a
future change to this module can't silently change the persisted key.

Covers:
- normalize_for_hash: whitespace collapse + casefold.
- text_hash: known-vector pin (blake2b, digest_size=32, 64 hex chars).
- Differently-whitespaced/cased input normalizes to the same hash.
"""
from __future__ import annotations

from lmchat.utils.text_hash import normalize_for_hash, text_hash

# Pinned against the pre-extraction implementations in memory_service and
# documents_service: blake2b(digest_size=32) over the whitespace-collapsed,
# case-folded text. Do not "fix" this value to match a changed algorithm —
# it exists to catch exactly that kind of drift.
_KNOWN_HASH = "b3549db28dc2114a3e0bcf11fdffb7d1e10a0b89dbb850aa4d4e85dc5cd2b2af"


# ---------------------------------------------------------------------------
# normalize_for_hash
# ---------------------------------------------------------------------------


def test_normalize_collapses_internal_whitespace() -> None:
    """Runs of internal whitespace collapse to a single space."""
    assert normalize_for_hash("Name  is   Kevin") == "name is kevin"


def test_normalize_strips_leading_and_trailing_whitespace() -> None:
    """Leading/trailing whitespace (including newlines) is stripped."""
    assert normalize_for_hash("  Name is Kevin\n") == "name is kevin"


def test_normalize_casefolds() -> None:
    """Text is case-folded, not just lowercased."""
    assert normalize_for_hash("NAME IS KEVIN") == "name is kevin"


def test_normalize_is_idempotent() -> None:
    """Normalizing already-normalized text is a no-op."""
    normalized = normalize_for_hash("Name  is   Kevin\n")
    assert normalize_for_hash(normalized) == normalized


# ---------------------------------------------------------------------------
# text_hash — known-vector pin
# ---------------------------------------------------------------------------


def test_text_hash_known_vector() -> None:
    """text_hash of normalized 'Name is Kevin' matches the pinned digest."""
    assert text_hash(normalize_for_hash("Name is Kevin")) == _KNOWN_HASH


def test_text_hash_is_64_hex_chars() -> None:
    """digest_size=32 → 64 hex characters, matching the String(64) column."""
    digest = text_hash(normalize_for_hash("some content"))
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_text_hash_differs_for_different_content() -> None:
    """Distinct normalized content hashes to distinct digests."""
    a = text_hash(normalize_for_hash("Name is Kevin"))
    b = text_hash(normalize_for_hash("Name is Kevan"))
    assert a != b


# ---------------------------------------------------------------------------
# End-to-end dedup behavior: whitespace/case variants collide
# ---------------------------------------------------------------------------


def test_whitespace_and_case_variants_hash_identically() -> None:
    """Content differing only in whitespace/case produces the same dedup key."""
    variants = ["  Name  is   Kevin\n", "Name is Kevin", "NAME IS KEVIN"]
    hashes = {text_hash(normalize_for_hash(v)) for v in variants}
    assert hashes == {_KNOWN_HASH}
