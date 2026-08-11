# SPDX-License-Identifier: Apache-2.0
"""Tests for lmchat.embedding.vector_math — pack/unpack/cosine_similarity.

The pack/unpack/cosine helpers used to be copy-pasted
across memory_service, retrieval_service, and documents_service, and
the cosine copies diverged on dimension-mismatch policy —
memory_service._cosine_similarity truncated silently via
zip(strict=False), retrieval_service._cosine_similarity raised. This
module is the single extracted implementation and the ONE policy is
fail-loud: raise ValueError on a length mismatch rather than silently
compare a truncated prefix. The test below is the one that pins that
policy down — it must FAIL if the raise is ever reverted back to
silent truncation.
"""
from __future__ import annotations

import struct

import pytest

from lmchat.embedding.vector_math import (
    cosine_similarity,
    pack_embedding,
    unpack_embedding,
)


def test_pack_embedding_little_endian_float32() -> None:
    """pack_embedding matches the documented on-wire/on-disk format:
    little-endian float32, 4 bytes per element."""
    vector = [0.1, -0.2, 3.5]
    blob = pack_embedding(vector)
    assert blob == struct.pack("<3f", *vector)
    assert len(blob) == 4 * len(vector)


def test_unpack_embedding_round_trips_pack_embedding() -> None:
    vector = [1.0, -2.5, 0.0, 42.125]
    blob = pack_embedding(vector)
    restored = unpack_embedding(blob)
    assert restored == pytest.approx(vector)


def test_unpack_embedding_derives_length_from_blob_size() -> None:
    """n = len(blob) // 4 — no separate length prefix is stored."""
    vector = [0.5] * 768
    blob = pack_embedding(vector)
    assert len(unpack_embedding(blob)) == 768


def test_cosine_similarity_identical_vectors_is_one() -> None:
    v = [1.0, 2.0, 3.0]
    assert cosine_similarity(v, v) == pytest.approx(1.0)


def test_cosine_similarity_orthogonal_vectors_is_zero() -> None:
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_similarity_zero_vector_returns_zero_not_nan() -> None:
    """A zero-norm vector must short-circuit to 0.0, not raise/NaN."""
    assert cosine_similarity([0.0, 0.0], [1.0, 2.0]) == 0.0


def test_cosine_similarity_raises_on_length_mismatch() -> None:
    """THE policy-pinning test.

    cosine_similarity must RAISE ValueError when the two vectors have
    different lengths — it must NOT silently truncate to the shorter
    vector's length via zip(..., strict=False) and return a
    similarity computed over a truncated prefix. A dimension mismatch
    means the vectors come from different embedding models/spaces and
    are not comparable; silently comparing a truncated prefix produces
    a silently-wrong similarity score.

    Red-on-revert: reverting cosine_similarity to the old
    `zip(va, vb, strict=False)`-with-no-guard behavior makes this test
    FAIL (it would return a float instead of raising).
    """
    va = [1.0, 2.0, 3.0]
    vb = [1.0, 2.0]
    with pytest.raises(ValueError, match="dimension mismatch"):
        cosine_similarity(va, vb)


def test_cosine_similarity_raises_regardless_of_which_side_is_longer() -> None:
    with pytest.raises(ValueError, match="dimension mismatch"):
        cosine_similarity([1.0, 2.0], [1.0, 2.0, 3.0])
