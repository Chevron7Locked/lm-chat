# SPDX-License-Identifier: Apache-2.0
"""Shared embedding vector helpers — pack/unpack/cosine.

Single source of truth for the pack/unpack/cosine-similarity trio that
was previously copy-pasted across ``memory_service``, ``retrieval_service``,
and ``documents_service``. The copies diverged on dimension-mismatch
policy: ``memory_service._cosine_similarity`` used
``zip(va, vb, strict=False)`` with no length guard (silently truncating
to the shorter vector on a dim mismatch — a silently-wrong similarity),
while ``retrieval_service._cosine_similarity`` raised ``ValueError``.
This module keeps ONE policy: fail loud.

Storage format
--------------
Each vector is packed as little-endian IEEE-754 single-precision floats
using ``struct.pack(f"<{n}f", *vector)`` where *n* is the embedding
dimension (e.g. 768, 1024, 3072 — depends on the loaded model). To
unpack: ``struct.unpack(f"<{n}f", blob)`` where ``n = len(blob) // 4``.
The format is dialect-neutral (SQLite LargeBinary, Postgres BYTEA) and
requires no third-party codec. This is byte-identical to the format
previously implemented separately in each service — do not change it,
existing persisted blobs depend on it.

Cosine similarity
-----------------
Pure-Python O(n) dot-product and Euclidean norm. Raises ``ValueError``
on a length mismatch between the two vectors rather than silently
truncating to the shorter one via ``zip(..., strict=False)`` — a
dimension mismatch means the vectors come from different embedding
models/spaces and are not comparable. Callers that may encounter
cross-model rows (e.g. ``MemoryService.recall``,
``retrieval_service.retrieve``) already skip/``continue`` past
mismatched rows before calling this function; the raise is a
defense-in-depth guard, not a code path any current caller can hit in
normal operation.
"""
from __future__ import annotations

import struct
from math import sqrt

__all__ = ["pack_embedding", "unpack_embedding", "cosine_similarity"]


def pack_embedding(vector: list[float]) -> bytes:
    """Pack *vector* as little-endian single-precision floats.

    Format: ``struct.pack(f"<{n}f", *vector)``

    Args:
        vector: Embedding vector.

    Returns:
        Byte string (4 bytes per element, little-endian IEEE-754 float32).
    """
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack_embedding(blob: bytes) -> list[float]:
    """Unpack bytes produced by :func:`pack_embedding` back to a float list.

    Args:
        blob: Bytes from an ``embedding`` column
            (e.g. ``message_embeddings.embedding``,
            ``document_chunks.embedding``).

    Returns:
        List of floats in original order.
    """
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


def cosine_similarity(va: list[float], vb: list[float]) -> float:
    """Return the cosine similarity between *va* and *vb*.

    Pure-Python O(n) implementation — no NumPy dependency.

    Raises:
        ValueError: If *va* and *vb* have different lengths
            (cross-model mismatch guard). This is the ONE policy
            for this codebase: fail loud rather than silently truncate
            to the shorter vector.

    Returns:
        Cosine similarity in [-1, 1] (0.0 when either vector is zero).
    """
    if len(va) != len(vb):
        raise ValueError(
            f"Vector dimension mismatch: query={len(va)}, stored={len(vb)}. "
            "Vectors must be compared within the same embedding model/dim."
        )
    dot = sum(a * b for a, b in zip(va, vb, strict=False))
    norm = sqrt(sum(a * a for a in va)) * sqrt(sum(b * b for b in vb))
    return dot / norm if norm > 0.0 else 0.0
