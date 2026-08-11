# SPDX-License-Identifier: Apache-2.0
"""Memory service for lm-chat — embedding index, recall, pinned insights, reindex.

Embedding storage: each vector is packed as little-endian float32 via
``struct.pack(f"<{n}f", *vector)``; unpack with
``struct.unpack(f"<{n}f", blob)`` where ``n = len(blob) // 4``. Dialect-neutral
(SQLite LargeBinary, Postgres BYTEA), no third-party codec.

Text hash: ``blake2b(digest_size=32)`` over the normalized text (whitespace-
collapsed, case-folded) → 64 hex chars, matching the ``String(64)`` column.

Cosine similarity: pure-Python O(n) dot-product + norm, adequate up to ~10k
messages; sqlite-vec/pgvector acceleration would be a transparent swap since
the ``recall()`` signature wouldn't change.
"""
from __future__ import annotations

import math
import re
import time
from datetime import datetime
from typing import Any, Final

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Row, delete, extract, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.sql import ColumnElement

from lmchat.config import get_settings
from lmchat.db.retry import with_write_retry
from lmchat.db.schema import (
    chats,
    memory_insights,
    memory_insights_history,
    message_embeddings,
    messages,
    server_lm_studio_default,
)
from lmchat.embedding.client import EmbeddingClient
from lmchat.embedding.errors import EmbeddingError
from lmchat.embedding.vector_math import (
    cosine_similarity as _cosine_similarity,
)
from lmchat.embedding.vector_math import (
    pack_embedding as _pack_embedding,
)
from lmchat.embedding.vector_math import (
    unpack_embedding as _unpack_embedding,
)
from lmchat.logging import get_logger
from lmchat.metrics import MEMORY_DISTILLATIONS, MEMORY_REINDEXED
from lmchat.services.models_service import ModelsService
from lmchat.utils.clock import ensure_utc
from lmchat.utils.text_hash import normalize_for_hash, text_hash

log = get_logger(__name__)

_REINDEX_BATCH_SIZE: Final[int] = 50


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class MemoryServiceError(Exception):
    """Base class for all MemoryService errors."""


class PinnedInsightsCapError(MemoryServiceError):
    """Raised when the user has reached the pinned-insights cap."""


# Stable ``NoEmbeddingModelLoadedError.reason`` tags set by
# ``resolve_active_embedding_model_key`` — see that exception's docstring.
# Callers that need to branch on WHY resolution failed should match on
# ``exc.reason`` (an attribute), never on ``str(exc)`` (the human-readable
# message, which is free to be reworded without notice).
EMBEDDING_ERROR_REASON_PREFERRED_NOT_LOADED = "preferred_not_loaded"
EMBEDDING_ERROR_REASON_NONE_LOADED = "no_embedding_model_loaded"
EMBEDDING_ERROR_REASON_DEFAULT_NOT_LOADED = "default_not_loaded"


class NoEmbeddingModelLoadedError(MemoryServiceError):
    """Raised when no embedding model is available in the ModelsService cache.

    ``reason`` is an optional stable tag (one of ``EMBEDDING_ERROR_REASON_*``,
    set by :func:`resolve_active_embedding_model_key`) for callers that need
    to branch on the failure kind without parsing ``message``. Other raise
    sites may leave it ``None`` — treat that as "not that reason", not an error.
    """

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


class InsightNotFoundError(MemoryServiceError):
    """Raised when an insight row is missing or owned by another user."""


class RefineUpstreamError(MemoryServiceError):
    """Raised when the refine LM Studio call fails or returns empty output."""


class HistoryNotFoundError(MemoryServiceError):
    """Raised when a memory_insights_history row is missing or not owned."""


# ---------------------------------------------------------------------------
# Public Pydantic models
# ---------------------------------------------------------------------------


class RecalledMessage(BaseModel):
    """One result from ``MemoryService.recall()``: a scored message row."""

    model_config = ConfigDict(from_attributes=True)

    message_id: int
    chat_id: int
    role: str
    content: str
    similarity: float  # cosine similarity in [0, 1]
    created_at: datetime


class MemoryInsight(BaseModel):
    """One row from the ``memory_insights`` table."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    text: str
    pinned: bool
    created_at: datetime


class ScoredInsight(BaseModel):
    """One result from ``MemoryService.recall_insights()``.

    Distinct from :class:`MemoryInsight` (the static row view) because
    recall returns a transient *score* — the compound decay/Bayesian value
    at recall time. Pinned rows carry ``score=float('inf')`` so the caller
    can short-circuit ordering.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    text: str
    category: str  # tier label, drives τ + CATEGORY_WEIGHTS
    score: float
    pinned: bool


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


# Re-exported under the module-private names so existing call sites (and
# tests importing them directly from this module) are unaffected. See
# lmchat.utils.text_hash for the shared implementation — both this module
# and documents_service key their dedup rows off the same hash, so the
# algorithm lives in one place.
_normalize = normalize_for_hash
_text_hash = text_hash


# ---------------------------------------------------------------------------
# Auto-memory (distillation) constants + helpers
# ---------------------------------------------------------------------------

# Category stamped on every AUTO (distilled) insight. Matches "preference" in
# CATEGORY_HALF_LIVES/CATEGORY_WEIGHTS below so an auto-distilled durable fact
# doesn't fade on the volatile "context" half-life an unknown-category
# fallback would otherwise apply.
AUTO_INSIGHT_CATEGORY: Final[str] = "profile"

# Content-token overlap-coefficient (|A∩B| / min(|A|,|B|)) above which two
# facts are near-duplicates. Overlap coefficient, not Jaccard, because one
# fact is often a subset-paraphrase of another ("likes astrophysics" ⊂ "likes
# astrophysics a lot") and Jaccard over-penalizes the extra words. Computed
# over stopword-filtered content tokens; exact-hash dedup handles verbatim
# repeats, this only catches paraphrase drift.
_NEAR_DUP_OVERLAP_THRESHOLD: Final[float] = 0.5

# Floor on shared content tokens (capped at the smaller side's length) so a
# single-content-word fact can still full-containment-match. Without it, two
# distinct 2-token facts sharing one word (e.g. "python backend" / "python
# frontend") hit the overlap coefficient exactly at threshold and would be
# wrongly merged — proportional-only isn't length-robust for short facts.
_NEAR_DUP_MIN_SHARED_TOKENS: Final[int] = 2

# Common English fillers stripped before the overlap check so the signal is
# carried by content words (nouns/proper nouns), not connective tissue.
_NEAR_DUP_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
        "am", "to", "of", "in", "on", "into", "onto", "at", "by", "for",
        "with", "and", "or", "but", "as", "that", "this", "these", "those",
        "it", "its", "their", "his", "her", "they", "he", "she", "very",
        "really", "quite", "lot", "lots", "much", "more", "some", "any",
        "user", "users", "likes", "like", "liking", "prefers", "prefer",
        "interested", "enjoys", "enjoy", "loves", "love",
    }
)


def _token_set(text: str) -> set[str]:
    """Return *text*'s alphanumeric word tokens (already-normalized text expected)."""
    return {tok for tok in re.findall(r"[a-z0-9]+", text) if tok}


def _content_tokens(text: str) -> set[str]:
    """Return *text*'s content tokens (word set minus filler stopwords).

    A durable fact's meaning lives in its content words ("astrophysics"),
    not connectives ("is", "into") — so "likes astrophysics" and "is into
    astrophysics" both reduce to {"astrophysics"}.
    """
    return _token_set(text) - _NEAR_DUP_STOPWORDS


def _is_near_duplicate(candidate: str, existing: list[str]) -> bool:
    """Return True when *candidate* paraphrases any string in *existing*.

    Stopword-filtered overlap coefficient on the normalized forms; catches
    paraphrases ("into astrophysics" vs "likes astrophysics") that exact-hash
    dedup misses. A pair is a near-duplicate when their content tokens share
    at least ``min(_NEAR_DUP_MIN_SHARED_TOKENS, min(|A|,|B|))`` tokens AND
    the overlap coefficient clears the threshold — capping the floor at the
    smaller side's length lets a single-content-word fact match by full
    containment while still requiring a real absolute overlap for longer facts.
    """
    cand_tokens = _content_tokens(_normalize(candidate))
    if not cand_tokens:
        return False
    for prior in existing:
        prior_tokens = _content_tokens(_normalize(prior))
        if not prior_tokens:
            continue
        shared = cand_tokens & prior_tokens
        if not shared:
            continue
        min_len = min(len(cand_tokens), len(prior_tokens))
        required_shared = min(_NEAR_DUP_MIN_SHARED_TOKENS, min_len)
        if len(shared) < required_shared:
            continue
        overlap = len(shared) / min_len
        if overlap >= _NEAR_DUP_OVERLAP_THRESHOLD:
            return True
    return False


# _pack_embedding / _unpack_embedding / _cosine_similarity live in
# lmchat.embedding.vector_math (imported above under their original private
# names) — see that module's docstring for the storage format and the
# dimension-mismatch policy (fail loud, no silent truncation).


# ---------------------------------------------------------------------------
# Moat-truth scoring. Per-category half-lives in DAYS; unknown categories
# fall back to CATEGORY_HALF_LIVES["context"] via _score_insight's .get()
# (defensive — a typo in admin-supplied data shouldn't crash recall).
# ---------------------------------------------------------------------------

CATEGORY_HALF_LIVES: Final[dict[str, float]] = {
    "identity": 180.0,
    "preference": 90.0,
    # Matches "preference": a machine-distilled durable fact is no less
    # durable than an admin-entered one; without this the .get() fallback
    # would fade every auto-saved fact on the 7-day "context" half-life.
    "profile": 90.0,
    "opinion": 60.0,
    "skill": 45.0,
    "project": 30.0,
    "context": 7.0,
}

# "profile" (AUTO_INSIGHT_CATEGORY) added to match "preference" — same
# rationale as CATEGORY_HALF_LIVES above.
CATEGORY_WEIGHTS: Final[dict[str, float]] = {
    "identity": 2.0,
    "preference": 2.0,
    "profile": 2.0,
    "opinion": 1.5,
    "skill": 1.0,
    "project": 1.0,
    "context": 1.0,
}

# Bayesian Laplace parameters (ADR §6.3; α=1.0, β=2.0 carried unchanged).
LAPLACE_ALPHA: Final[float] = 1.0
LAPLACE_BETA: Final[float] = 2.0

# Compound-score floor — clamp so no insight is permanently silenced (§6.5).
SCORE_FLOOR: Final[float] = 0.1

# Fade pass: probability of running the sweep on a given recall() call.  Keeps
# the cost amortised without needing a separate scheduler.  1-in-50 = ~2 %.
_FADE_PASS_PROBABILITY: Final[float] = 0.02


def _recency_order_expr(
    dialect_name: str,
) -> tuple[ColumnElement, ColumnElement]:
    """Return ``(recency_expr, tiebreak_expr)`` for ordering active insights.

    Used by the auto-cap eviction (:meth:`MemoryService._evict_auto_over_cap`)
    and the ``recall_insights`` candidate-pool scan. A never-recalled row
    (``last_active_epoch`` and ``last_used`` both NULL) ranks by its OWN
    creation time rather than always sorting first or last.

    ``recency_expr`` is ``COALESCE(last_active_epoch, last_used,
    epoch(created_at))``. ``last_active_epoch`` (migration 0043) mirrors
    ``last_used`` on recall and is set to ``created_at``'s epoch on insert by
    :meth:`MemoryService.save_auto_insight` / :meth:`_touch_insights`; it's
    NULL only for rows those two call sites didn't touch (a pre-migration row
    the backfill missed, or a raw-SQL insert bypassing ``MemoryService``). The
    ``last_used`` middle rung covers exactly that case — falling straight to
    ``created_at`` would ignore a real ``last_used`` timestamp on such rows.

    Every value in the chain is now a Float epoch, which matters: before
    migration 0043, ``last_used`` (Float) and ``created_at`` (DateTime) were
    different storage types, and SQLite compares mixed storage classes by
    class first (NULL < INTEGER/REAL < TEXT < BLOB) — so a REAL ``last_used``
    ALWAYS sorted before a TEXT ``created_at`` fallback regardless of which
    timestamp was actually more recent (verified empirically), and Postgres's
    ``COALESCE`` over incompatible types didn't execute at all. With
    everything Float, ``COALESCE`` is safe on both dialects and the candidate
    set can be ordered + ``LIMIT``-ed in SQL instead of sorted in Python.

    ``created_at`` is UTC but SQLite hands it back as a NAIVE datetime/TEXT
    (see :func:`lmchat.utils.clock.ensure_utc`); ``julianday()`` parses that
    stored wall-clock value literally rather than reinterpreting it as local
    time, so it yields the correct UTC epoch with no host-tz skew. Postgres
    stores ``created_at`` as ``TIMESTAMPTZ``, so ``EXTRACT(EPOCH FROM ...)``
    is timezone-correct regardless of session timezone.

    ``tiebreak_expr`` is ``created_at``'s epoch, used as a deterministic
    secondary sort key when two rows tie on ``recency_expr``.

    Returns:
        ``(recency_expr, tiebreak_expr)`` — call ``.asc()`` for oldest-first
        (eviction) or ``.desc()`` for newest-first (recall candidate pool).
    """
    if dialect_name == "sqlite":
        created_epoch: ColumnElement = (
            func.julianday(memory_insights.c.created_at) - 2440587.5
        ) * 86400.0
    else:
        created_epoch = extract("epoch", memory_insights.c.created_at)
    recency = func.coalesce(
        memory_insights.c.last_active_epoch,
        memory_insights.c.last_used,
        created_epoch,
    )
    return recency, created_epoch


def _score_insight(row: Any, now: float) -> float:  # noqa: ANN401  — duck-typed row
    """Compute the ADR §6.5 compound score for one insight row.

    ``score = boost * decay_for_category * category_weight * bayesian``,
    clamped to ``max(score, SCORE_FLOOR)``. *now* is caller-supplied so tests
    can pin it deterministically.
    """
    category: str = row.category
    use_count: int = int(row.use_count or 0)
    ups: float = float(row.ups or 0.0)
    downs: float = float(row.downs or 0.0)
    last_used: float = float(row.last_used) if row.last_used is not None else now

    tau_days = CATEGORY_HALF_LIVES.get(category, CATEGORY_HALF_LIVES["context"])
    days = max(0.0, (now - last_used) / 86_400.0)
    decay = 1.0 / (1.0 + days / tau_days)

    boost = 1.0 + 0.3 * math.log(1 + use_count)

    # (ups + α) / (ups + downs + β); cold-start = α/β = 0.5. NOT the textbook
    # beta-binomial (ups + α) / (ups + downs + α + β), which gives 1/3.
    bayesian = (ups + LAPLACE_ALPHA) / (ups + downs + LAPLACE_BETA)

    cat_w = CATEGORY_WEIGHTS.get(category, 1.0)

    score = boost * decay * cat_w * bayesian
    return max(score, SCORE_FLOOR)


# ---------------------------------------------------------------------------
# Standalone embedding-model resolver (single implementation shared by
# MemoryService, documents_service, and quality_modes)
# ---------------------------------------------------------------------------

_EMBED_PREF_ADMIN_ID: Final[int] = 1  # mirrors _ADMIN_RECORD_ID in lm_studio_overrides_service

# LM Studio ships this model on first launch, so it's the de-facto local
# default: when no preference is pinned, the resolver deterministically
# prefers this key if loaded. Must NOT fall back to a different-dimension
# model (e.g. bge-m3 at 1024-dim vs nomic's 768) — that would silently
# corrupt recall. If not loaded and nothing is pinned, fail loud instead.
DEFAULT_EMBEDDING_MODEL_KEY: Final[str] = "text-embedding-nomic-embed-text-v1.5"


async def resolve_active_embedding_model_key(
    *,
    engine: AsyncEngine,
    models_service: ModelsService,
    persist_default: bool = True,
) -> str:
    """Return the stable, persisted embedding model key.

    Single source of truth for ALL new-indexing paths (``MemoryService``,
    ``documents_service``, ``quality_modes``) — NOT used by the
    project-PINNED retrieval path, which must stay on its pin to preserve
    corpus vector-space consistency.

    If a preference is stored and a loaded embedding model matches it (exact
    key, exact instance id, ``key + "@"`` prefix, or the reverse fallback of
    a stale pinned ``@quant`` whose bare key is loaded under a different
    quant — dimension-safe since quants of one model share output
    dimensions), that preference is returned. If a preference is stored but
    matches nothing loaded, or if nothing is loaded at all, raises
    :exc:`NoEmbeddingModelLoadedError` — FAIL LOUD rather than silently
    switch to a dimensionally-incompatible model. With no preference stored,
    deterministically prefers :data:`DEFAULT_EMBEDDING_MODEL_KEY` (nomic) if
    loaded, persisting it when ``persist_default`` is True.

    Returns a CATALOG KEY; wire-id resolution (the ``@quant`` suffix LM
    Studio needs when JIT is disabled) happens centrally in
    ``EmbeddingClient._resolve_wire_id`` — don't duplicate that here.

    Args:
        persist_default: When True (default) and no preference is stored,
                         persist the chosen key. Pass False in read-only
                         contexts.

    Returns:
        Catalog key string (e.g. ``"text-embedding-nomic-embed-text-v1.5"``).

    Raises:
        NoEmbeddingModelLoadedError: If the preferred model is not loaded,
            or if no embedding model is loaded at all.
    """
    preferred: str | None = None
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(
                    server_lm_studio_default.c.preferred_embedding_model_id
                ).where(server_lm_studio_default.c.id == _EMBED_PREF_ADMIN_ID)
            )
        ).first()
    if row is not None and row.preferred_embedding_model_id:
        preferred = str(row.preferred_embedding_model_id)

    loaded = await models_service.list_loaded()
    # A model is only ACTUALLY loaded when it has loaded_instance_ids —
    # filtering on type alone would let an unloaded embedder win _is_loaded,
    # the bug that pinned projects to a dimensionally-incompatible model.
    embedding_models = [
        m for m in loaded if m.type == "embedding" and m.loaded_instance_ids
    ]

    loaded_keys: set[str] = {m.key for m in embedding_models}
    loaded_instance_ids: set[str] = {
        iid for m in embedding_models for iid in m.loaded_instance_ids
    }

    def _is_loaded(key: str) -> bool:
        """Return True if *key* resolves to a currently-loaded embedding model.

        Matches the same ladder ``ModelsService.resolve_embedding_wire_id``
        (recall's resolver) uses, so the write and recall paths stay in
        lock-step: exact key, exact instance id, ``key + "@"`` prefix, and the
        reverse bare-``@``-strip fallback — without which a stale pinned
        quant would silently stop SAVING while recall kept READING via its
        own fallback.
        """
        if key in loaded_keys:
            return True
        if key in loaded_instance_ids:
            return True
        prefix = key + "@"
        if any(iid.startswith(prefix) for iid in loaded_instance_ids):
            return True
        # key itself pins a stale @quant no longer loaded, but the bare key
        # is (quants share output dimensions → dimension-safe fallback).
        if "@" in key:
            bare = key.split("@", 1)[0]
            if bare in loaded_keys:
                return True
            bare_prefix = bare + "@"
            return any(iid.startswith(bare_prefix) for iid in loaded_instance_ids)
        return False

    if preferred is not None:
        if _is_loaded(preferred):
            return preferred
        raise NoEmbeddingModelLoadedError(
            f"The selected embedding model '{preferred}' is not currently "
            "loaded in LM Studio. Load it, or choose a different embedding "
            "model in Settings.",
            reason=EMBEDDING_ERROR_REASON_PREFERRED_NOT_LOADED,
        )

    # No preference — deterministically prefer the canonical default. Never
    # fall back to a different-dimension model: that would silently corrupt recall.
    if not embedding_models:
        raise NoEmbeddingModelLoadedError(
            "No embedding model is currently loaded in LM Studio. "
            "Load an embedding model before indexing or recalling memories.",
            reason=EMBEDDING_ERROR_REASON_NONE_LOADED,
        )

    if not _is_loaded(DEFAULT_EMBEDDING_MODEL_KEY):
        # Nothing pinned and the canonical default isn't loaded — fail loud
        # rather than silently embed under a possibly wrong-dimension model.
        raise NoEmbeddingModelLoadedError(
            f"The default embedding model '{DEFAULT_EMBEDDING_MODEL_KEY}' is "
            "not currently loaded in LM Studio. Load it (LM Studio ships it on "
            "first launch), or choose a different embedding model in Settings.",
            reason=EMBEDDING_ERROR_REASON_DEFAULT_NOT_LOADED,
        )

    chosen_key = DEFAULT_EMBEDDING_MODEL_KEY

    if persist_default:
        await _persist_embedding_preference(engine, chosen_key)

    return chosen_key


async def _persist_embedding_preference(engine: AsyncEngine, model_key: str) -> None:
    """Write *model_key* as the preferred embedding model in the DB.

    Uses the same cross-dialect UPSERT pattern as
    ``LmStudioOverridesService._upsert_admin_row``.  Only writes
    ``preferred_embedding_model_id``; leaves all other columns untouched
    (on-conflict updates only that column).
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    log.info(
        "memory.embedding_preference_persisted",
        preferred_embedding_model_id=model_key,
    )
    dialect = engine.dialect.name
    async with engine.begin() as conn:
        if dialect == "postgresql":
            stmt = pg_insert(server_lm_studio_default).values(
                id=_EMBED_PREF_ADMIN_ID,
                preferred_embedding_model_id=model_key,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["id"],
                set_={"preferred_embedding_model_id": stmt.excluded.preferred_embedding_model_id},
            )
            await conn.execute(stmt)
        elif dialect == "sqlite":
            stmt = sqlite_insert(server_lm_studio_default).values(
                id=_EMBED_PREF_ADMIN_ID,
                preferred_embedding_model_id=model_key,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["id"],
                set_={"preferred_embedding_model_id": stmt.excluded.preferred_embedding_model_id},
            )
            await conn.execute(stmt)
        else:  # pragma: no cover — generic fallback
            existing = (
                await conn.execute(
                    select(server_lm_studio_default.c.id).where(
                        server_lm_studio_default.c.id == _EMBED_PREF_ADMIN_ID
                    )
                )
            ).first()
            if existing is None:
                await conn.execute(
                    insert(server_lm_studio_default).values(
                        id=_EMBED_PREF_ADMIN_ID,
                        preferred_embedding_model_id=model_key,
                    )
                )
            else:
                await conn.execute(
                    update(server_lm_studio_default)
                    .where(server_lm_studio_default.c.id == _EMBED_PREF_ADMIN_ID)
                    .values(preferred_embedding_model_id=model_key)
                )


# ---------------------------------------------------------------------------
# Corpus dimension lock: the memory corpus has exactly ONE embedding
# dimension at a time. Indexing or recalling with a different-dimension
# embedder corrupts recall, so the corpus dimension's single source of truth
# is the actual byte-length of a stored ``message_embeddings.embedding``
# vector — it can never drift from reality. Switching embedders requires a
# full re-index (POST /api/memory/reindex), which legitimately changes it.
# ---------------------------------------------------------------------------


async def corpus_embedding_dimension(engine: AsyncEngine) -> int | None:
    """Return the embedding dimension of the current memory corpus.

    Reads the byte-length of a single stored ``message_embeddings.embedding``
    blob (little-endian float32, 4 bytes/element) and divides by 4 — the
    actual stored vector length, so it can never drift from what was indexed.

    Returns:
        The corpus dimension (e.g. ``768`` for nomic), or ``None`` if the
        corpus is empty (no rows in ``message_embeddings``). An empty corpus
        accepts any embedder — the first index call sets the corpus dimension.
    """
    async with engine.connect() as conn:
        blob = (
            await conn.execute(
                select(message_embeddings.c.embedding).limit(1)
            )
        ).scalar()
    if blob is None:
        return None
    # Packed float32 → 4 bytes per element. memoryview handles both bytes and
    # the buffer types some drivers return for BLOB/BYTEA columns.
    return len(bytes(blob)) // 4


async def probe_embedding_dimension(
    embedding_client: EmbeddingClient, model_id: str
) -> int:
    """Return the output dimension of *model_id* by embedding a short probe.

    The catalog carries no dimension field — it's only known post-embed. Used
    by the SET-time guard to compare a candidate embedder's dimension against
    the corpus dimension before accepting a switch.

    Raises:
        EmbeddingError: On upstream embedding failure (e.g. model not loaded).
    """
    vector = await embedding_client.embed_one(text="dimension probe", model_id=model_id)
    return len(vector)

class MemoryService:
    """Embedding-backed memory service for lm-chat.

    Indexes message text as embeddings (``index_message``), recalls
    semantically similar messages (``recall``), manages user-pinned insights
    (``pin_insight`` / ``unpin_insight`` / ``list_pinned``), re-embeds the
    corpus under a new model (``reindex``), and exposes a batch-embed hook
    (``embed_texts``) plus an audit hook for message deletions
    (``handle_message_deleted``).

    Constructed at application lifespan start and injected into routes via
    ``app.state.memory_service``.
    """

    def __init__(
        self,
        *,
        engine: AsyncEngine,
        embedding_client: EmbeddingClient,
        models_service: ModelsService,
    ) -> None:
        self._engine = engine
        self._embedding_client = embedding_client
        self._models_service = models_service
        # StreamingService._safe_index_message swallows index_message
        # exceptions log-only; these fields let embedding_status() surface
        # repeated failures instead of them being invisible outside the logs.
        self._index_write_failure_count: int = 0
        self._index_write_last_error: str | None = None

    def record_index_write_failure(self, *, error: str) -> None:
        """Record a swallowed ``index_message`` write failure.

        Called by ``StreamingService._safe_index_message`` when
        ``index_message`` raises; makes the failure countable via
        :meth:`embedding_status` instead of log-only.
        """
        self._index_write_failure_count += 1
        self._index_write_last_error = error

    # ------------------------------------------------------------------
    # Incognito gate (defensive write-path guard)
    # ------------------------------------------------------------------

    async def _chat_is_incognito(self, chat_id: int) -> bool:
        """Return True when ``chats.incognito = 1`` for *chat_id*.

        Defensive guard called by ``index_message`` before INSERTing into
        ``message_embeddings`` so an incognito chat never leaks content into
        long-term memory. Returns False when the row doesn't exist (no
        privacy obligation to honour — the caller fails loudly next anyway).
        """
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(chats.c.incognito).where(chats.c.id == chat_id)
            )
            row = result.fetchone()
        if row is None:
            return False
        return bool(int(row[0]))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def embedding_status(self) -> dict[str, Any]:
        """Snapshot of which embedding model the service is using and
        how many messages have been indexed.

        Returned for admin visibility so the Settings UI can show
        the active embedding model + a last-indexed timestamp without
        running a reindex. Auth gating happens at the route layer.

        Returns:
            {
              "active_model_id": str | None,         # resolver-chosen model, or None
              "loaded_embedding_models": [str],      # LOADED embedding model keys only
              "total_indexed_messages": int,         # row count in message_embeddings
              "last_indexed_at": float | None,       # max(messages.created_at) over indexed rows
              "models_in_use": {model_id: count},    # historical embeds per model
            }

        ``loaded_embedding_models`` lists ONLY models with at least one
        loaded instance (``loaded_instance_ids`` non-empty).  LM Studio's
        catalog enumerates every *downloaded* quant variant as its own
        ``ModelInfo`` key, but only the loaded one(s) can actually embed —
        listing the downloaded-but-unloaded variants let the admin pin a
        not-loaded quant (e.g. ``…v1.5@q8_0`` while only ``…v1.5`` is loaded),
        which silently killed memory indexing.

        ``active_model_id`` is the model the index/recall path *actually*
        resolves to via :func:`resolve_active_embedding_model_key` — NOT
        merely the first entry in LM Studio's list order.  The two diverged
        before (reporting e.g. ``bge-m3`` while indexing used ``nomic``)
        because the snapshot took ``loaded[0]`` instead of consulting the
        resolver.
        """
        try:
            loaded = await self._models_service.list_loaded()
        except Exception:  # noqa: BLE001
            loaded = []
        # Only models with a live instance can embed; downloaded-but-unloaded
        # quant variants must NOT appear (pinning one breaks memory).
        loaded_embedding_keys = [
            m.key
            for m in loaded
            if m.type == "embedding" and m.loaded_instance_ids
        ]

        # active_model_id must match the model the index/recall path resolves
        # to (resolve_active_embedding_model_key), not list order. Read-only
        # here (persist_default=False) — embedding_status is a pure snapshot.
        active: str | None
        # active_model_error_reason preserves WHY resolution failed
        # (EMBEDDING_ERROR_REASON_* for NoEmbeddingModelLoadedError, or
        # "resolver_error" for a generic exception) — without this,
        # "preferred model not loaded" and "resolver raised" both
        # collapsed into the same active=None, indistinguishable to a
        # caller that needs to branch on the failure kind.
        active_error_reason: str | None = None
        try:
            active = await resolve_active_embedding_model_key(
                engine=self._engine,
                models_service=self._models_service,
                persist_default=False,
            )
        except NoEmbeddingModelLoadedError as exc:
            active = None
            active_error_reason = exc.reason
        except Exception:  # noqa: BLE001
            # A generic resolver error must not silently fall back to a
            # possibly wrong-dimension embedder — that would show a green
            # "active" card while indexing is dead (dimension mismatch).
            # Return None so the admin card reflects the real failure state.
            active = None
            active_error_reason = "resolver_error"

        async with self._engine.connect() as conn:
            total = (
                await conn.execute(select(func.count()).select_from(message_embeddings))
            ).scalar() or 0
            last_ts = (
                await conn.execute(
                    select(func.max(messages.c.created_at))
                    .select_from(
                        message_embeddings.join(
                            messages,
                            message_embeddings.c.message_id == messages.c.id,
                        )
                    )
                )
            ).scalar()
            in_use_rows = (
                await conn.execute(
                    select(
                        message_embeddings.c.embedding_model_id,
                        func.count().label("n"),
                    ).group_by(message_embeddings.c.embedding_model_id)
                )
            ).fetchall()

        # `created_at` round-trips as either a real number or an ISO/space
        # string depending on the writer; float() on the string would raise
        # and 500 the whole endpoint. Coerce safely: numbers pass through,
        # strings parse, anything else becomes None.
        last_indexed_at: float | None
        if last_ts is None:
            last_indexed_at = None
        elif isinstance(last_ts, (int, float)):
            last_indexed_at = float(last_ts)
        else:
            try:
                last_indexed_at = float(last_ts)
            except (TypeError, ValueError):
                try:
                    from datetime import datetime as _dt

                    # fromisoformat() yields a NAIVE datetime for SQLite's
                    # timezone-less read-back; ensure_utc() stamps it UTC
                    # before .timestamp() so it isn't misread as host-local
                    # time (see _recency_order_expr for the same bug class).
                    parsed = ensure_utc(
                        _dt.fromisoformat(str(last_ts).replace(" ", "T"))
                    )
                    assert parsed is not None  # fromisoformat never returns None
                    last_indexed_at = parsed.timestamp()
                except (TypeError, ValueError):
                    last_indexed_at = None

        return {
            "active_model_id": active,
            "active_model_error_reason": active_error_reason,
            "loaded_embedding_models": loaded_embedding_keys,
            "total_indexed_messages": int(total),
            "last_indexed_at": last_indexed_at,
            "models_in_use": {str(r[0]): int(r[1]) for r in in_use_rows},
            "write_failure_count": self._index_write_failure_count,
            "write_last_error": self._index_write_last_error,
        }

    async def _default_embedding_model(self) -> str:
        """Return the stable preferred embedding model key.

        Thin compatibility shim over :meth:`resolve_active_embedding_model_key`
        so existing internal callers (``index_message``, ``embed_texts``)
        need no changes.
        """
        return await self.resolve_active_embedding_model_key(persist_default=True)

    async def resolve_active_embedding_model_key(
        self,
        *,
        persist_default: bool = True,
    ) -> str:
        """Return the stable, persisted embedding model key.

        Delegates to the module-level :func:`resolve_active_embedding_model_key`
        (see its docstring for the full resolution algorithm), which is the
        single implementation shared by ``documents_service`` and
        ``quality_modes``.
        """
        return await resolve_active_embedding_model_key(
            engine=self._engine,
            models_service=self._models_service,
            persist_default=persist_default,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def index_message(self, message_id: int) -> None:
        """Embed a message and write a ``message_embeddings`` row.

        Idempotent (``INSERT OR IGNORE``). When the parent chat is incognito,
        the embedding row is NOT written — a second line of defence beyond
        the streaming path's own skip, so an accidental future caller can't
        leak incognito content into ``message_embeddings``.

        Raises:
            RuntimeError:                If the message row is not found.
            NoEmbeddingModelLoadedError: If no embedding model is loaded.
            EmbeddingError:              On upstream embedding failure.
        """
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(messages.c.content, messages.c.chat_id).where(
                    messages.c.id == message_id
                )
            )
            row = result.fetchone()

        if row is None:
            raise RuntimeError(f"message_id {message_id!r} not found in messages table")

        content: str = row.content
        chat_id: int = row.chat_id

        # Privacy invariant: incognito chats NEVER write embeddings.
        if await self._chat_is_incognito(chat_id):
            log.info(
                "memory.index_message.skipped_incognito",
                message_id=message_id,
                chat_id=chat_id,
            )
            return

        model_id = await self._default_embedding_model()
        vector = await self._embedding_client.embed_one(text=content, model_id=model_id)

        # Last line of defence against a mismatched vector (the SET-time
        # guard on the embedding-model PATCH route makes this unreachable in
        # normal use): never write a dimension that differs from the corpus.
        # An empty corpus (dim is None) accepts any dimension.
        corpus_dim = await corpus_embedding_dimension(self._engine)
        if corpus_dim is not None and len(vector) != corpus_dim:
            log.error(
                "memory.index_message.vec_dim_mismatch",
                message_id=message_id,
                chat_id=chat_id,
                embedding_model_id=model_id,
                vector_dim=len(vector),
                corpus_dim=corpus_dim,
            )
            return

        normalized = _normalize(content)
        t_hash = _text_hash(normalized)
        blob = _pack_embedding(vector)

        log.info(
            "memory.index_message",
            message_id=message_id,
            chat_id=chat_id,
            embedding_model_id=model_id,
            text_hash=t_hash,
            vector_dim=len(vector),
        )

        async def _insert() -> None:
            async with self._engine.begin() as conn:
                # PK is message_id, so a duplicate call for the same id no-ops.
                await conn.execute(
                    insert(message_embeddings).prefix_with("OR IGNORE").values(
                        message_id=message_id,
                        embedding_model_id=model_id,
                        embedding=blob,
                        text_hash=t_hash,
                    )
                )

        await with_write_retry(_insert)

    async def recall(
        self,
        *,
        user_id: int,
        query: str,
        top_k: int = 8,
        project_id: int | None = None,
    ) -> list[RecalledMessage]:
        """Return the *top_k* most semantically similar messages for *user_id*.

        Groups stored rows by ``embedding_model_id``, embeds the query once
        per distinct model, and cosine-compares only within the same
        model/dim; rows with NULL model (legacy) are excluded to avoid
        cross-space comparison. Each stored ``embedding_model_id`` is
        resolved through ``ModelsService.resolve_embedding_wire_id`` first —
        a row stored under a stale quant-suffixed id is remapped to the
        currently loaded wire id instead of being sent upstream unchanged
        (which LM Studio 400s on) — and the per-group embed call is isolated
        in a try/except so one stale/unloadable group can't kill recall for
        the rest (mirrors ``retrieval_service.retrieve``).

        Args:
            top_k:      Maximum number of results to return (default 8).
            project_id: When non-None, restrict to messages whose chat is in
                        this project. None applies no project filter
                        (user-scoped union, legacy behavior).

        Returns:
            List of :class:`RecalledMessage`, sorted by ``similarity``
            descending. Empty list if the corpus is empty or no usable
            embedding model is available for any stored row.

        Raises:
            NoEmbeddingModelLoadedError: If no embedding model is loaded.
        """
        stmt = (
            select(
                message_embeddings.c.message_id,
                message_embeddings.c.embedding,
                message_embeddings.c.embedding_model_id,
                messages.c.chat_id,
                messages.c.role,
                messages.c.content,
                messages.c.created_at,
            )
            .join(messages, message_embeddings.c.message_id == messages.c.id)
            .join(chats, messages.c.chat_id == chats.c.id)
            .where(chats.c.user_id == user_id)
        )
        if project_id is not None:
            stmt = stmt.where(chats.c.project_id == project_id)

        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).fetchall()

        if not rows:
            return []

        # Group rows by embedding_model_id (skip NULL / unknown).
        from collections import defaultdict

        rows_by_model: dict[str, list[Row]] = defaultdict(list)
        for row in rows:
            mid = row.embedding_model_id
            if mid:
                rows_by_model[mid].append(row)

        if not rows_by_model:
            return []

        scored: list[tuple[float, Row[Any]]] = []
        for mid, model_rows in rows_by_model.items():
            resolved_mid = await self._models_service.resolve_embedding_wire_id(mid)
            if resolved_mid is None:
                # Unloadable right now — skip rather than call embed_one with
                # an id LM Studio is guaranteed to 400 on (mirrors
                # retrieval_service.retrieve).
                log.warning(
                    "memory.recall.vec_embed_unresolvable",
                    embed_model_id=mid,
                    skipped_rows=len(model_rows),
                )
                continue
            try:
                qv = await self._embedding_client.embed_one(
                    text=query, model_id=resolved_mid
                )
            except EmbeddingError as exc:
                # This group's model can't be embedded right now — skip it
                # rather than failing the whole recall; other groups may
                # still resolve fine.
                log.warning(
                    "memory.recall.vec_embed_failed",
                    embed_model_id=mid,
                    resolved_model_id=resolved_mid,
                    error=str(exc),
                    skipped_rows=len(model_rows),
                )
                continue
            for row in model_rows:
                candidate_vector = _unpack_embedding(row.embedding)
                if len(qv) != len(candidate_vector):
                    log.warning(
                        "memory.recall.vec_dim_mismatch",
                        message_id=row.message_id,
                        embed_model_id=mid,
                        query_dim=len(qv),
                        stored_dim=len(candidate_vector),
                    )
                    continue
                sim = _cosine_similarity(qv, candidate_vector)
                scored.append((sim, row))

        scored.sort(key=lambda x: x[0], reverse=True)

        results: list[RecalledMessage] = []
        for sim, row in scored[:top_k]:
            results.append(
                RecalledMessage(
                    message_id=row.message_id,
                    chat_id=row.chat_id,
                    role=row.role,
                    content=row.content,
                    similarity=sim,
                    created_at=row.created_at,
                )
            )
        return results

    async def pin_insight(
        self,
        *,
        user_id: int,
        text: str,
        project_id: int | None = None,
    ) -> MemoryInsight:
        """Pin a text insight for *user_id*, returning the (possibly existing) row.

        Enforces the per-user pinned-insights cap from
        ``settings.lm_chat_pinned_insights_cap`` (default 100). Content-dedup:
        if the same normalized text is already pinned, the existing row is
        returned without creating a duplicate.

        Args:
            project_id: When set, the pinned row carries this project_id so
                        retrieval can scope per-project. Dedup is still by
                        (user_id, text_hash) — the same text pinned in two
                        projects coalesces into one row whose project_id
                        reflects the first writer. None preserves the legacy
                        un-projected behavior (project_id stays NULL).

        Returns:
            The :class:`MemoryInsight` row (new or existing).

        Raises:
            PinnedInsightsCapError: If the user has reached the cap.
        """
        settings = get_settings()
        cap: int = settings.lm_chat_pinned_insights_cap

        # text_hash computed before the cap check so a re-pin of existing
        # content short-circuits it — otherwise a user at the cap re-pinning
        # the same insight would see PinnedInsightsCapError instead of the
        # existing pin, which is surprising since re-pinning is idempotent.
        normalized = _normalize(text)
        t_hash = _text_hash(normalized)

        async with self._engine.connect() as conn:
            existing_result = await conn.execute(
                select(memory_insights).where(
                    memory_insights.c.user_id == user_id,
                    memory_insights.c.text_hash == t_hash,
                )
            )
            existing_row = existing_result.fetchone()
        if existing_row is not None:
            return MemoryInsight.model_validate(existing_row, from_attributes=True)

        async with self._engine.connect() as conn:
            count_result = await conn.execute(
                select(func.count()).where(
                    memory_insights.c.user_id == user_id,
                    memory_insights.c.pinned.is_(True),
                )
            )
            current_count: int = count_result.scalar_one()

        if current_count >= cap:
            raise PinnedInsightsCapError(
                f"User {user_id!r} has reached the pinned-insights cap of {cap}."
            )

        # Attempt insert; on UNIQUE violation, return the existing row.
        try:
            async def _insert_insight() -> int:
                async with self._engine.begin() as conn:
                    values: dict[str, Any] = {
                        "user_id": user_id,
                        "text": text,
                        "text_hash": t_hash,
                        "pinned": True,
                    }
                    if project_id is not None:
                        values["project_id"] = project_id
                    result = await conn.execute(
                        insert(memory_insights).values(**values)
                    )
                    pk = result.inserted_primary_key
                    if pk is None:
                        raise RuntimeError("INSERT into memory_insights returned no PK")
                    return int(pk[0])

            new_id = await with_write_retry(_insert_insight)

        except IntegrityError:
            # UNIQUE(user_id, text_hash) violated — fetch existing row.
            async with self._engine.connect() as conn:
                result = await conn.execute(
                    select(memory_insights).where(
                        memory_insights.c.user_id == user_id,
                        memory_insights.c.text_hash == t_hash,
                    )
                )
                existing = result.fetchone()
            if existing is None:
                raise RuntimeError(
                    f"memory_insights UNIQUE violation for user {user_id!r} / "
                    f"hash {t_hash!r} but row not found on re-select"
                ) from None
            return MemoryInsight.model_validate(existing, from_attributes=True)

        # Scoped by id AND user_id even for this internal post-insert fetch
        # (cross-user isolation invariant, LLM06 #168).
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(memory_insights).where(
                    memory_insights.c.id == new_id,
                    memory_insights.c.user_id == user_id,
                )
            )
            row = result.fetchone()

        if row is None:
            raise RuntimeError(
                f"memory_insights row {new_id!r} not found after INSERT — unexpected"
            )
        return MemoryInsight.model_validate(row, from_attributes=True)

    async def unpin_insight(self, insight_id: int, *, user_id: int) -> None:
        """Delete the ``memory_insights`` row with *insight_id*.

        Ownership-enforced: raises :class:`InsightNotFoundError` when the row
        is missing **or** does not belong to *user_id*.  This prevents IDOR
        (LLM06 finding #168): callers can only delete their own insights.

        Idempotent for the owning user: if the row is already gone the call
        is a no-op (but still verifies ownership first if the row exists).

        Args:
            insight_id: PK of the insight to remove.
            user_id:    Caller's user PK — MUST match the row's ``user_id``.

        Raises:
            InsightNotFoundError: When the row is missing or not owned by
                *user_id*.
        """

        async def _delete() -> None:
            async with self._engine.begin() as conn:
                result = await conn.execute(
                    delete(memory_insights)
                    .where(memory_insights.c.id == insight_id)
                    .where(memory_insights.c.user_id == user_id)
                )
                # rowcount == 0: missing or owned by another user — both 404.
                if result.rowcount == 0:
                    raise InsightNotFoundError(
                        f"insight {insight_id!r} not found or not owned by user {user_id!r}"
                    )

        await with_write_retry(_delete)

    async def list_pinned(
        self,
        user_id: int,
        project_id: int | None = None,
    ) -> list[MemoryInsight]:
        """Return all pinned insights for *user_id*, newest first.

        Args:
            project_id: Restrict to this project_id if given; None applies
                        no filter (every pin the user owns).

        Returns:
            List of :class:`MemoryInsight`, ordered by ``created_at`` DESC.
        """
        stmt = (
            select(memory_insights)
            .where(memory_insights.c.user_id == user_id)
            .where(memory_insights.c.pinned.is_(True))
            .order_by(memory_insights.c.created_at.desc())
        )
        if project_id is not None:
            stmt = stmt.where(memory_insights.c.project_id == project_id)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).fetchall()
        return [MemoryInsight.model_validate(r, from_attributes=True) for r in rows]

    async def list_auto(
        self,
        user_id: int,
        project_id: int | None = None,
    ) -> list[MemoryInsight]:
        """Return AUTO (distilled) insights for *user_id*, newest first.

        AUTO insights are machine-extracted profile facts from
        :meth:`distill_and_store` — ``pinned = False``, ``state = 'active'``.
        ``state = 'faded'`` rows (evicted past the cap, or decayed below the
        score floor) are excluded so the /memory view shows only memories
        that still influence recall.

        Args:
            project_id: Restrict to this project_id if given.

        Returns:
            List of :class:`MemoryInsight`, ordered by ``created_at`` DESC.
        """
        stmt = (
            select(memory_insights)
            .where(memory_insights.c.user_id == user_id)
            .where(memory_insights.c.pinned.is_(False))
            .where(memory_insights.c.state == "active")
            .order_by(memory_insights.c.created_at.desc())
        )
        if project_id is not None:
            stmt = stmt.where(memory_insights.c.project_id == project_id)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).fetchall()
        return [MemoryInsight.model_validate(r, from_attributes=True) for r in rows]

    async def save_auto_insight(
        self,
        *,
        user_id: int,
        text: str,
        existing_texts: list[str] | None = None,
        project_id: int | None = None,
    ) -> MemoryInsight | None:
        """Persist a single AUTO (distilled) durable fact about the user.

        The auto-memory counterpart to :meth:`pin_insight`: inserts with
        ``pinned = False``, stamps :data:`AUTO_INSIGHT_CATEGORY`, computes the
        blake2b ``text_hash``, increments
        :data:`~lmchat.metrics.MEMORY_DISTILLATIONS`, and enforces the
        per-user auto-memory cap by fading the oldest LRU auto rows when the
        cap is exceeded — which can immediately fade the row just inserted
        (see the eviction note below).

        Dedup is two-layer: (1) exact ``text_hash`` — a verbatim repeat
        returns ``None``, UNIQUE(user_id, text_hash) also backstops a race;
        (2) near-duplicate — an overlap check against *existing_texts* (the
        caller's current insight texts) so a paraphrase is dropped.

        No embedding row is written: ``recall_insights`` ranks by the
        compound decay/Bayesian score over ``state='active'`` rows, not
        vector similarity, so an AUTO insight is recallable the moment it's
        active and the embedding-dimension lock doesn't apply here.

        Args:
            text:           Durable third-person fact (e.g. "Name is Kevin").
            existing_texts: The user's current insight texts for the near-dup
                            check. None applies only exact-hash dedup
                            (callers that already filtered should pass []).
            project_id:     Optional project scope; NULL preserves the legacy
                            un-projected behavior.

        Returns:
            The newly-inserted :class:`MemoryInsight`, or ``None`` when the
            fact was a duplicate (exact or near), or when the auto-memory cap
            immediately faded this same row after insert — either way,
            nothing new is recallable.
        """
        normalized = _normalize(text)
        if not normalized:
            return None
        t_hash = _text_hash(normalized)

        # Exact-hash dedup against ANY existing row (pinned or auto) — never
        # shadow an admin-pinned fact with an auto row.
        async with self._engine.connect() as conn:
            existing_result = await conn.execute(
                select(memory_insights.c.id).where(
                    memory_insights.c.user_id == user_id,
                    memory_insights.c.text_hash == t_hash,
                )
            )
            if existing_result.fetchone() is not None:
                return None

        if existing_texts and _is_near_duplicate(text, existing_texts):
            log.info(
                "memory.distill.near_duplicate_skipped",
                user_id=user_id,
                text_preview=text[:80],
            )
            return None

        async def _insert() -> int | None:
            async with self._engine.begin() as conn:
                values: dict[str, Any] = {
                    "user_id": user_id,
                    "text": text,
                    "text_hash": t_hash,
                    "pinned": False,
                    "category": AUTO_INSIGHT_CATEGORY,
                    "state": "active",
                    # Never-recalled row: recency = its own creation time
                    # (see _recency_order_expr); _touch_insights bumps this
                    # on first recall.
                    "last_active_epoch": time.time(),
                }
                if project_id is not None:
                    values["project_id"] = project_id
                try:
                    result = await conn.execute(
                        insert(memory_insights).values(**values)
                    )
                except IntegrityError:
                    # UNIQUE(user_id, text_hash) lost a race — treat as dup.
                    return None
                pk = result.inserted_primary_key
                if pk is None:
                    raise RuntimeError(
                        "INSERT into memory_insights returned no PK"
                    )
                return int(pk[0])

        new_id = await with_write_retry(_insert)
        if new_id is None:
            return None

        MEMORY_DISTILLATIONS.inc()
        await self._evict_auto_over_cap(user_id=user_id)

        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(memory_insights).where(
                        memory_insights.c.id == new_id,
                        memory_insights.c.user_id == user_id,
                    )
                )
            ).fetchone()
        if row is None or row.state != "active":
            # Eviction fades, never deletes — this row can exist here with
            # state="faded" if the cap pushed the just-inserted insight back
            # out. Report both cases as "not stored" so the caller never
            # believes a faded (unrecallable) insight is live.
            return None
        return MemoryInsight.model_validate(row, from_attributes=True)

    async def _evict_auto_over_cap(self, *, user_id: int) -> None:
        """Fade the oldest least-recently-used AUTO rows past the cap.

        Keeps the newest-activity ``lm_chat_auto_memory_cap`` active AUTO
        insights and transitions the remainder to ``state='faded'`` — never
        destroyed, so a future reindex/restore could revive them. Pinned
        insights are exempt and never counted against the cap.

        "Least recently used" = :func:`_recency_order_expr` ascending — a
        never-recalled row ranks by ITS OWN creation time, not sorted first
        just because it hasn't been recalled (which would fade a fact the
        instant it was saved, before it ever had a chance to be recalled).
        The ordering (and the ``LIMIT``-free full scan below) is resolved
        entirely in SQL — see :func:`_recency_order_expr` for why this is
        safe on every dialect.
        """
        cap: int = get_settings().lm_chat_auto_memory_cap
        recency_expr, tiebreak_expr = _recency_order_expr(
            self._engine.dialect.name
        )

        async with self._engine.connect() as conn:
            active_rows_result = await conn.execute(
                select(memory_insights.c.id)
                .where(memory_insights.c.user_id == user_id)
                .where(memory_insights.c.pinned.is_(False))
                .where(memory_insights.c.state == "active")
                .order_by(recency_expr.asc(), tiebreak_expr.asc())
            )
            ordered_ids = [int(r.id) for r in active_rows_result.fetchall()]

        if len(ordered_ids) <= cap:
            return

        # The first (len - cap) rows are the oldest-LRU — fade them.
        to_fade = ordered_ids[: len(ordered_ids) - cap]

        async def _fade() -> None:
            async with self._engine.begin() as conn:
                await conn.execute(
                    update(memory_insights)
                    .where(memory_insights.c.id.in_(to_fade))
                    .where(memory_insights.c.user_id == user_id)
                    .values(state="faded")
                )

        await with_write_retry(_fade)
        log.info(
            "memory.distill.evicted_over_cap",
            user_id=user_id,
            faded_count=len(to_fade),
            cap=cap,
        )

    async def distill_and_store(
        self,
        *,
        user_id: int,
        facts: list[str],
        project_id: int | None = None,
    ) -> list[MemoryInsight]:
        """Store a batch of distilled durable facts as AUTO insights.

        Loads the user's current insight texts once (pinned + active auto),
        then for each candidate fact runs exact-hash + near-duplicate dedup
        and inserts survivors via :meth:`save_auto_insight`, folding each
        newly-stored fact into the running set so two paraphrases within one
        batch don't both land.

        Never raises on a per-fact failure — logged and skipped so the rest
        of the batch still persists (defence-in-depth on top of the caller's
        fire-and-forget wrapping).

        Returns:
            The list of :class:`MemoryInsight` rows actually stored (may be
            empty when every candidate was a duplicate).
        """
        if not facts:
            return []

        async with self._engine.connect() as conn:
            existing_rows = (
                await conn.execute(
                    select(memory_insights.c.text)
                    .where(memory_insights.c.user_id == user_id)
                    .where(memory_insights.c.state == "active")
                )
            ).fetchall()
        existing_texts: list[str] = [str(r.text) for r in existing_rows]

        stored: list[MemoryInsight] = []
        for fact in facts:
            try:
                insight = await self.save_auto_insight(
                    user_id=user_id,
                    text=fact,
                    existing_texts=existing_texts,
                    project_id=project_id,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "memory.distill.save_failed",
                    user_id=user_id,
                    text_preview=str(fact)[:80],
                    error=str(exc),
                )
                continue
            if insight is not None:
                stored.append(insight)
                # Fold the stored text in so an in-batch paraphrase is caught.
                existing_texts.append(insight.text)
        if stored:
            log.info(
                "memory.distill.stored",
                user_id=user_id,
                stored_count=len(stored),
                candidate_count=len(facts),
            )
        return stored

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Batch-embed *texts* using the current default embedding model.

        Public hook for convergence-cosine batch needs.

        Returns:
            List of embedding vectors, one per input, in input order.

        Raises:
            NoEmbeddingModelLoadedError: If no embedding model is loaded.
            EmbeddingError:              On upstream embedding failure.
        """
        model_id = await self._default_embedding_model()
        return await self._embedding_client.embed_batch(texts=texts, model_id=model_id)

    async def _insert_embedding_rows(
        self,
        rows: list[dict],  # type: ignore[type-arg]
    ) -> None:
        """Atomically swap each message's stale/absent row for its new one.

        For every row in *rows*, any existing embedding row for that
        ``message_id`` (under any model) is deleted and the freshly-embedded
        row inserted, both inside one ``engine.begin()`` transaction — delete
        and insert commit or roll back together, so a message is never
        observed with its old vector gone and its new one not yet written.
        The caller (``reindex``) only calls this once a batch has already
        embedded successfully, so a message whose batch fails or never runs
        simply keeps whatever row it already had.

        Args:
            rows: One dict per successfully embedded message, with keys
                  ``message_id``, ``embedding_model_id``, ``embedding``,
                  ``text_hash``.
        """
        if not rows:
            return

        message_ids = [row["message_id"] for row in rows]

        async def _do_swap() -> None:
            async with self._engine.begin() as conn:
                await conn.execute(
                    delete(message_embeddings).where(
                        message_embeddings.c.message_id.in_(message_ids)
                    )
                )
                await conn.execute(insert(message_embeddings), rows)

        await with_write_retry(_do_swap)

    async def reindex(self, *, embedding_model_id: str) -> dict:  # type: ignore[type-arg]
        """Re-embed all messages under *embedding_model_id*.

        Background-task entrypoint — the admin route typically runs this as
        ``asyncio.create_task(memory_service.reindex(...))``.

        After a fail-fast probe of the target model (see the DATA-LOSS GUARD
        below), selects messages with no ``message_embeddings`` row or a
        stale one, embeds in batches of :data:`_REINDEX_BATCH_SIZE`, and for
        each successful batch atomically swaps every message_id's row (see
        ``_insert_embedding_rows``). A batch that raises ``EmbeddingError``
        is skipped entirely (not retried per-message — the typical cause is
        the model being unloaded, which per-message retries would just repeat
        while adding upstream load) and its rows are left untouched; a future
        ``reindex`` call will retry them.

        Atomicity invariant: a message's stale row is deleted only inside the
        same transaction that inserts its replacement, and only after that
        message's batch has embedded successfully — so a message always has
        either its old (stale-but-searchable) vector or its new one, never
        neither. ``recall`` already groups rows by ``embedding_model_id`` and
        embeds the query once per group, so a corpus transiently split across
        old/new models mid-reindex is fully supported.

        Args:
            embedding_model_id: Target embedding model key.

        Returns:
            Snapshot dict:
            ``{embedding_model_id, processed, total, elapsed_s, failed}``.
        """
        start_time = time.monotonic()

        # Count all messages.
        async with self._engine.connect() as conn:
            total_result = await conn.execute(select(func.count()).select_from(messages))
            total: int = total_result.scalar_one()

        log.info(
            "memory.reindex.start",
            embedding_model_id=embedding_model_id,
            total_messages=total,
        )

        # DATA-LOSS GUARD: a throwaway embed proves the target model is
        # loaded and responds BEFORE any row is touched below; on failure we
        # raise without touching anything, so the corpus is left intact.
        try:
            await self._embedding_client.embed_batch(
                texts=["reindex precondition probe"],
                model_id=embedding_model_id,
            )
        except EmbeddingError as exc:
            log.error(
                "memory.reindex.precondition_failed",
                embedding_model_id=embedding_model_id,
                error=str(exc),
            )
            raise EmbeddingError(
                f"reindex aborted: target embedding model "
                f"'{embedding_model_id}' is not available (probe failed) — the "
                f"corpus was left untouched. {exc}"
            ) from exc

        # Messages with no row yet, or a stale one — stale rows are left in
        # place until their batch actually re-embeds successfully (see the
        # atomicity invariant above). ``ORDER BY messages.c.id`` makes batch
        # membership deterministic, which matters now that a batch's
        # success/failure has a message-level consequence.
        #
        # PRIVACY INVARIANT: JOIN + filter ``incognito = 0`` so this bulk
        # write path mirrors the same guarantee index_message's per-message
        # guard gives — an incognito row must never reach _insert_embedding_rows.
        needs_embed_stmt = (
            select(
                messages.c.id,
                messages.c.content,
            )
            .select_from(
                messages.join(chats, messages.c.chat_id == chats.c.id).outerjoin(
                    message_embeddings,
                    message_embeddings.c.message_id == messages.c.id,
                )
            )
            .where(
                chats.c.incognito == 0,
                or_(
                    message_embeddings.c.message_id.is_(None),
                    message_embeddings.c.embedding_model_id != embedding_model_id,
                ),
            )
            .order_by(messages.c.id)
        )

        async with self._engine.connect() as conn:
            all_rows = (await conn.execute(needs_embed_stmt)).fetchall()

        processed = 0
        total_failed = 0

        for batch_start in range(0, len(all_rows), _REINDEX_BATCH_SIZE):
            batch = all_rows[batch_start : batch_start + _REINDEX_BATCH_SIZE]
            batch_texts = [r.content for r in batch]
            batch_ids = [r.id for r in batch]
            batch_failed = 0
            batch_time = time.monotonic()

            try:
                vectors = await self._embedding_client.embed_batch(
                    texts=batch_texts,
                    model_id=embedding_model_id,
                )
            except EmbeddingError as exc:
                # Skip the whole batch; existing rows are left untouched
                # (see the atomicity invariant above).
                batch_failed = len(batch)
                total_failed += batch_failed
                elapsed_s = time.monotonic() - batch_time
                log.warning(
                    "memory.reindex.batch_failed",
                    embedding_model_id=embedding_model_id,
                    processed=processed,
                    total=total,
                    batch_size=len(batch),
                    elapsed_s=round(elapsed_s, 3),
                    failed=batch_failed,
                    error=str(exc),
                )
                log.info(
                    "memory.reindex.batch",
                    embedding_model_id=embedding_model_id,
                    processed=processed,
                    total=total,
                    batch_size=len(batch),
                    elapsed_s=round(elapsed_s, 3),
                    failed=batch_failed,
                )
                continue

            # Build the successfully-embedded rows for this batch.
            rows_to_insert = []
            for msg_id, content, vector in zip(batch_ids, batch_texts, vectors, strict=False):
                normalized = _normalize(content)
                t_hash = _text_hash(normalized)
                rows_to_insert.append(
                    {
                        "message_id": msg_id,
                        "embedding_model_id": embedding_model_id,
                        "embedding": _pack_embedding(vector),
                        "text_hash": t_hash,
                    }
                )

            await self._insert_embedding_rows(rows_to_insert)

            batch_processed = len(batch)
            processed += batch_processed
            MEMORY_REINDEXED.inc(batch_processed)

            elapsed_s = time.monotonic() - batch_time
            log.info(
                "memory.reindex.batch",
                embedding_model_id=embedding_model_id,
                processed=processed,
                total=total,
                batch_size=batch_processed,
                elapsed_s=round(elapsed_s, 3),
                failed=batch_failed,
            )

        # Reindex is the sanctioned way to change the corpus dimension, so
        # persist the target as preferred once re-embedded (subsequent
        # index/recall must resolve to the same model). DATA-LOSS GUARD: only
        # pin if something was actually re-embedded, or the corpus was empty
        # — if total>0 but processed==0 the model died mid-run after the
        # probe, and pinning would aim index/recall at a model that just failed.
        if processed > 0 or total == 0:
            await _persist_embedding_preference(self._engine, embedding_model_id)
        else:
            log.error(
                "memory.reindex.preference_not_pinned",
                embedding_model_id=embedding_model_id,
                total=total,
                processed=processed,
                failed=total_failed,
            )

        total_elapsed_s = time.monotonic() - start_time
        log.info(
            "memory.reindex.complete",
            embedding_model_id=embedding_model_id,
            processed=processed,
            total=total,
            elapsed_s=round(total_elapsed_s, 3),
            failed=total_failed,
        )

        return {
            "embedding_model_id": embedding_model_id,
            "processed": processed,
            "total": total,
            "elapsed_s": round(total_elapsed_s, 3),
            "failed": total_failed,
        }

    async def handle_message_deleted(self, message_id: int) -> None:
        """Emit an audit log event when a message is deleted.

        Called by ``chat_service.delete()``, ``chat_service.compact()``, and
        ``message_service.delete()`` for every affected ``message_id``. The
        FK cascade already removes the embedding row; this hook provides an
        audit trail and a future in-memory vector index subscription point.
        Idempotent: safe to call for a message with no embedding row.
        """
        # The FK cascade may have already deleted the row.
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(message_embeddings.c.embedding_model_id).where(
                    message_embeddings.c.message_id == message_id
                )
            )
            row = result.fetchone()

        embedding_model_id: str | None = row.embedding_model_id if row is not None else None

        log.info(
            "memory.message_deleted",
            message_id=message_id,
            embedding_model_id=embedding_model_id,
        )


    # ------------------------------------------------------------------
    # Moat-truth insight recall, scoring, decay & refinement.
    # ------------------------------------------------------------------

    async def recall_insights(
        self,
        *,
        user_id: int,
        top_k: int = 8,
        candidate_pool: int = 50,
        now: float | None = None,
        project_id: int | None = None,
    ) -> list[ScoredInsight]:
        """Return the top-k insights for *user_id* ranked by ADR §6.5 compound score.

        Pinned insights bypass scoring and are injected first; remaining
        slots are filled by ``state='active'`` rows ranked by
        ``_score_insight``. Every returned row has ``last_used`` bumped to
        ``now`` and ``use_count`` incremented (ADR §6.1 retrieval-as-signal).
        A low-frequency ``_fade_pass`` sweep (~2% of calls) transitions
        score < SCORE_FLOOR rows to ``state='faded'``.

        Args:
            candidate_pool: Upper bound on the active-row scan; the top-N
                            rows by :func:`_recency_order_expr` are scored and
                            re-sorted. A never-recalled row ranks by its own
                            creation time so it can still enter the pool.
                            Defaults to 50, comfortably above any realistic
                            ``top_k``.
            now:            Override "now" for deterministic tests.
            project_id:     Restrict to this project_id if given, applied to
                            BOTH the pinned and active-scoring branches so
                            cross-project leakage is impossible.

        Returns:
            List of :class:`ScoredInsight`, pinned-first, then scored
            descending, length ≤ ``top_k``.
        """
        if top_k <= 0:
            return []

        ts = time.time() if now is None else now

        results: list[ScoredInsight] = []

        # Pinned insights first (bypass scoring). project_id is applied here
        # AND on the active-row scan below so neither branch leaks cross-project.
        pinned_stmt = (
            select(memory_insights)
            .where(memory_insights.c.user_id == user_id)
            .where(memory_insights.c.pinned.is_(True))
            .order_by(memory_insights.c.created_at.desc())
            .limit(top_k)
        )
        if project_id is not None:
            pinned_stmt = pinned_stmt.where(
                memory_insights.c.project_id == project_id
            )
        async with self._engine.connect() as conn:
            pinned_rows = (await conn.execute(pinned_stmt)).fetchall()

        for row in pinned_rows:
            results.append(
                ScoredInsight(
                    id=int(row.id),
                    user_id=int(row.user_id),
                    text=row.text,
                    category=row.category,
                    score=float("inf"),  # pinned outranks everything
                    pinned=True,
                )
            )

        # Score active rows; pick the rest of top_k. SQL-level ORDER BY +
        # LIMIT — only the top candidate_pool rows are ever fetched (see
        # _recency_order_expr for why COALESCE is safe here).
        remaining = top_k - len(results)
        if remaining > 0:
            recency_expr, tiebreak_expr = _recency_order_expr(
                self._engine.dialect.name
            )
            active_stmt = (
                select(memory_insights)
                .where(memory_insights.c.user_id == user_id)
                .where(memory_insights.c.pinned.is_(False))
                .where(memory_insights.c.state == "active")
            )
            if project_id is not None:
                active_stmt = active_stmt.where(
                    memory_insights.c.project_id == project_id
                )
            active_stmt = active_stmt.order_by(
                recency_expr.desc(), tiebreak_expr.desc()
            ).limit(candidate_pool)
            async with self._engine.connect() as conn:
                active_rows = (await conn.execute(active_stmt)).fetchall()

            scored: list[tuple[float, Row[Any]]] = [
                (_score_insight(r, ts), r) for r in active_rows
            ]
            scored.sort(key=lambda x: x[0], reverse=True)

            for score, row in scored[:remaining]:
                results.append(
                    ScoredInsight(
                        id=int(row.id),
                        user_id=int(row.user_id),
                        text=row.text,
                        category=row.category,
                        score=score,
                        pinned=False,
                    )
                )

        if results:
            await self._touch_insights([r.id for r in results], now=ts)

        import random as _rand
        if _rand.random() < _FADE_PASS_PROBABILITY:
            try:
                await self._fade_pass(now=ts)
            except Exception as exc:  # noqa: BLE001
                log.warning("memory.fade_pass.failed", error=str(exc))

        return results

    async def _touch_insights(
        self,
        insight_ids: list[int],
        *,
        now: float | None = None,
    ) -> None:
        """Bump ``last_used`` to *now* and ``use_count`` by 1 for *insight_ids*.

        Also bumps ``last_active_epoch`` — the "row was recalled" half of
        keeping that column in sync (the other half is the INSERT in
        :meth:`save_auto_insight`); see :func:`_recency_order_expr`.
        Idempotent: missing rows are silently skipped.

        Scoped: cross-user — *insight_ids* are pre-resolved by callers that
        already filter by user_id; this only updates by PK (LLM06 #168: no
        user content is read, only scoring timestamps).
        """
        if not insight_ids:
            return
        ts = time.time() if now is None else now

        async def _do_update() -> None:
            async with self._engine.begin() as conn:
                await conn.execute(
                    # scoped: cross-user — insight_ids are pre-resolved (already
                    # user-scoped) by the caller; the touch is keyed by those ids.
                    update(memory_insights)
                    .where(memory_insights.c.id.in_(insight_ids))
                    .values(
                        last_used=ts,
                        last_active_epoch=ts,
                        use_count=memory_insights.c.use_count + 1,
                    )
                )

        await with_write_retry(_do_update)

    async def _fade_pass(self, *, now: float | None = None) -> int:
        """Transition active rows whose compound score < SCORE_FLOOR to 'faded'.

        ``_score_insight`` clamps to SCORE_FLOOR, so this checks the
        *pre-clamp* compound (computed inline) rather than the clamped score
        — otherwise the floor would mask the transition. Per ADR §6.5 the
        floor prevents permanent silencing; fading is a separate signal that
        a row is no longer in active recall.

        Returns:
            Count of rows transitioned.
        """
        ts = time.time() if now is None else now

        async with self._engine.connect() as conn:
            result = await conn.execute(
                # scoped: cross-user — background maintenance sweep (LLM06 #168
                # exemption): reads only scoring columns, no user content.
                select(memory_insights).where(
                    # LLM06 exemption: sweeps ALL rows (user_id=all), background only.
                    memory_insights.c.state == "active"
                )
            )
            rows = result.fetchall()

        to_fade: list[int] = []
        for row in rows:
            category: str = row.category
            use_count: int = int(row.use_count or 0)
            ups: float = float(row.ups or 0.0)
            downs: float = float(row.downs or 0.0)
            last_used: float = float(row.last_used) if row.last_used is not None else ts

            tau_days = CATEGORY_HALF_LIVES.get(category, CATEGORY_HALF_LIVES["context"])
            days = max(0.0, (ts - last_used) / 86_400.0)
            decay = 1.0 / (1.0 + days / tau_days)
            boost = 1.0 + 0.3 * math.log(1 + use_count)
            bayesian = (ups + LAPLACE_ALPHA) / (ups + downs + LAPLACE_BETA)
            cat_w = CATEGORY_WEIGHTS.get(category, 1.0)
            unclamped = boost * decay * cat_w * bayesian

            if unclamped < SCORE_FLOOR:
                to_fade.append(int(row.id))

        if not to_fade:
            return 0

        async def _do_update() -> None:
            async with self._engine.begin() as conn:
                await conn.execute(
                    update(memory_insights)
                    .where(memory_insights.c.id.in_(to_fade))
                    .values(state="faded")
                )

        await with_write_retry(_do_update)
        log.info("memory.fade_pass.completed", faded_count=len(to_fade))
        return len(to_fade)


    # ------------------------------------------------------------------
    # Memory edit + refine + restore
    # ------------------------------------------------------------------

    async def edit_insight(
        self,
        *,
        insight_id: int,
        user_id: int,
        text: str,
    ) -> MemoryInsight:
        """Update *insight_id*.text after verifying ownership.

        Re-normalises + re-hashes the text so a future re-pin of the same
        content is still deduplicated through the UNIQUE(user_id, text_hash)
        index — skipping the hash update would defeat dedup on later pins.

        Returns:
            The updated :class:`MemoryInsight` row.

        Raises:
            InsightNotFoundError: When the row is missing or not owned
                by *user_id*.
            ValueError: When *text* normalizes to an empty string.
        """
        normalized = _normalize(text)
        if normalized == "":
            raise ValueError("insight text may not be empty after normalisation")
        new_hash = _text_hash(normalized)

        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(memory_insights).where(
                        memory_insights.c.id == insight_id,
                        memory_insights.c.user_id == user_id,
                    )
                )
            ).fetchone()
        if row is None:
            raise InsightNotFoundError(
                f"insight {insight_id!r} not found or not owned by user {user_id!r}"
            )

        async def _do() -> None:
            async with self._engine.begin() as conn:
                await conn.execute(
                    update(memory_insights)
                    .where(memory_insights.c.id == insight_id)
                    .values(text=text, text_hash=new_hash)
                )

        try:
            await with_write_retry(_do)
        except IntegrityError as exc:
            # Edited text now matches another existing insight's content.
            raise ValueError(
                "edited text duplicates another pinned insight; "
                "delete the other first"
            ) from exc

        async with self._engine.connect() as conn:
            updated = (
                await conn.execute(
                    select(memory_insights).where(
                        memory_insights.c.id == insight_id,
                        memory_insights.c.user_id == user_id,
                    )
                )
            ).fetchone()
        assert updated is not None

        log.info("memory.edit_insight", insight_id=insight_id, user_id=user_id)
        try:
            from lmchat.services.audit_service import write_audit_event

            await write_audit_event(
                user_id=user_id,
                event="memory.insight.edited",
                ip=None,
                user_agent=None,
                detail={"insight_id": insight_id},
                engine=self._engine,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("memory.edit_insight.audit_failed", error=str(exc))
        return MemoryInsight.model_validate(updated, from_attributes=True)

    async def refine(
        self,
        *,
        user_id: int,
        refine_callable: Any,
        project_id: int | None = None,
    ) -> tuple[list[MemoryInsight], int]:
        """Refine the user's pinned insights via *refine_callable*.

        *refine_callable* is an async callable with signature
        ``async def call(items: list[str]) -> list[str]``: receives the
        current pinned-insight texts, returns the refined list. Production
        wires this to a single LM Studio chat call via the native
        ``/api/v1/chat`` surface; tests pass an in-memory stub.

        Before the destructive replace, the current snapshot is inserted into
        ``memory_insights_history`` so the admin can undo with
        ``restore_from_history``.

        When ``project_id`` is set, the scope (read, DELETE, re-insert) is
        restricted to that project's pins. When None, only the
        ``project_id IS NULL`` slice is refined — NOT the user-wide union.

        Args:
            project_id: Optional project scope. None = un-projected
                        pinned insights only.

        Returns:
            Tuple of (new insight rows, history_id).

        Raises:
            RefineUpstreamError: When the callable raises or returns an
                empty list (refine must be lossless; an empty output would
                silently delete pins).
        """
        scope_clause = (
            memory_insights.c.project_id == project_id
            if project_id is not None
            else memory_insights.c.project_id.is_(None)
        )
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(memory_insights).where(
                        memory_insights.c.user_id == user_id,
                        memory_insights.c.pinned.is_(True),
                        scope_clause,
                    )
                )
            ).fetchall()
        current = [
            {
                "id": int(r.id),
                "text": r.text,
                "created_at": r.created_at.isoformat()
                if r.created_at is not None
                else None,
                # Legacy snapshots without this key restore as un-projected.
                "project_id": getattr(r, "project_id", None),
            }
            for r in rows
        ]
        if not current:
            return ([], 0)

        try:
            refined_texts: list[str] = await refine_callable(
                [c["text"] for c in current]
            )
        except Exception as exc:
            raise RefineUpstreamError(
                f"refine call raised: {type(exc).__name__}: {exc}"
            ) from exc

        cleaned: list[str] = [
            t.strip() for t in refined_texts if isinstance(t, str) and t.strip() != ""
        ]
        if not cleaned:
            raise RefineUpstreamError(
                "refine call returned no usable output; refusing to drop pins"
            )

        history_pk_cell: list[int] = []

        async def _do() -> None:
            async with self._engine.begin() as conn:
                # Carries the scope so refine traces stay reconstructible.
                event_label = (
                    f"refine:project={project_id}"
                    if project_id is not None
                    else "refine"
                )
                history_result = await conn.execute(
                    insert(memory_insights_history).values(
                        user_id=user_id,
                        event=event_label,
                        insights_before=current,
                    )
                )
                pk = history_result.inserted_primary_key
                if pk is None:
                    raise RuntimeError("history INSERT returned no PK")
                history_pk_cell.append(int(pk[0]))

                # Scoped by project_id so refine of project A's pins doesn't
                # wipe project B's pins.
                await conn.execute(
                    delete(memory_insights).where(
                        memory_insights.c.user_id == user_id,
                        memory_insights.c.pinned.is_(True),
                        scope_clause,
                    )
                )

                for cleaned_text in cleaned:
                    norm = _normalize(cleaned_text)
                    h = _text_hash(norm)
                    try:
                        values: dict[str, Any] = {
                            "user_id": user_id,
                            "text": cleaned_text,
                            "text_hash": h,
                            "pinned": True,
                        }
                        if project_id is not None:
                            values["project_id"] = project_id
                        await conn.execute(
                            insert(memory_insights).values(**values)
                        )
                    except IntegrityError:
                        pass  # refined output contains a duplicate

        await with_write_retry(_do)
        history_id = history_pk_cell[0]

        async with self._engine.connect() as conn:
            new_rows = (
                await conn.execute(
                    select(memory_insights)
                    .where(
                        memory_insights.c.user_id == user_id,
                        memory_insights.c.pinned.is_(True),
                    )
                    .order_by(memory_insights.c.created_at.asc())
                )
            ).fetchall()

        result_models = [
            MemoryInsight.model_validate(r, from_attributes=True) for r in new_rows
        ]
        log.info(
            "memory.refine",
            user_id=user_id,
            before=len(current),
            after=len(result_models),
            history_id=history_id,
        )
        try:
            from lmchat.services.audit_service import write_audit_event

            await write_audit_event(
                user_id=user_id,
                event="memory.refine",
                ip=None,
                user_agent=None,
                detail={
                    "before": len(current),
                    "after": len(result_models),
                    "history_id": history_id,
                },
                engine=self._engine,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("memory.refine.audit_failed", error=str(exc))
        return (result_models, history_id)

    async def restore_from_history(
        self,
        *,
        user_id: int,
        history_id: int,
        project_id: int | None = None,
    ) -> list[MemoryInsight]:
        """Roll the user's pinned insights back to a history snapshot.

        Atomically replaces the current pinned-insight set with the rows in
        ``memory_insights_history.insights_before``. New PKs are minted
        since the prior IDs may have been freed or reclaimed since.

        When ``project_id`` is set, only snapshot entries whose project_id
        matches are re-inserted (and only those scopes get the DELETE) —
        mirroring :meth:`refine`. When None, all scopes in the snapshot are
        restored.

        Args:
            project_id: Optional partial-restore scope. None restores every
                        entry at its own original scope.

        Returns:
            The restored :class:`MemoryInsight` list (chronological by
            ``created_at``).

        Raises:
            HistoryNotFoundError: When the history row is missing or
                not owned by *user_id*.
        """
        async with self._engine.connect() as conn:
            history_row = (
                await conn.execute(
                    select(memory_insights_history).where(
                        memory_insights_history.c.id == history_id,
                        memory_insights_history.c.user_id == user_id,
                    )
                )
            ).fetchone()
        if history_row is None:
            raise HistoryNotFoundError(
                f"history {history_id!r} not found or not owned by user {user_id!r}"
            )

        snapshot_raw = history_row.insights_before
        # SQLAlchemy JSON returns list/dict; defensively decode strings.
        if isinstance(snapshot_raw, str):
            import json as _json

            try:
                snapshot = _json.loads(snapshot_raw)
            except ValueError:
                snapshot = []
        else:
            snapshot = snapshot_raw

        if not isinstance(snapshot, list):
            snapshot = []

        if project_id is not None:
            snapshot = [
                e
                for e in snapshot
                if isinstance(e, dict) and e.get("project_id") == project_id
            ]

        # Scopes present in the snapshot, so the DELETE below only touches
        # the project_ids being restored (never a user-wide wipe). Legacy
        # snapshots without a project_id key default to None (un-projected).
        snapshot_scopes: set[int | None] = set()
        for entry in snapshot:
            if isinstance(entry, dict):
                snapshot_scopes.add(entry.get("project_id"))

        async def _do() -> None:
            async with self._engine.begin() as conn:
                for scope_pid in snapshot_scopes:
                    scope_pred = (
                        memory_insights.c.project_id == scope_pid
                        if scope_pid is not None
                        else memory_insights.c.project_id.is_(None)
                    )
                    await conn.execute(
                        delete(memory_insights).where(
                            memory_insights.c.user_id == user_id,
                            memory_insights.c.pinned.is_(True),
                            scope_pred,
                        )
                    )
                for entry in snapshot:
                    if not isinstance(entry, dict):
                        continue
                    txt = entry.get("text")
                    if not isinstance(txt, str) or txt.strip() == "":
                        continue
                    entry_project_id = entry.get("project_id")
                    norm = _normalize(txt)
                    h = _text_hash(norm)
                    try:
                        values: dict[str, Any] = {
                            "user_id": user_id,
                            "text": txt,
                            "text_hash": h,
                            "pinned": True,
                        }
                        if entry_project_id is not None:
                            values["project_id"] = entry_project_id
                        await conn.execute(
                            insert(memory_insights).values(**values)
                        )
                    except IntegrityError:
                        pass  # duplicate inside the snapshot

        await with_write_retry(_do)

        async with self._engine.connect() as conn:
            restored = (
                await conn.execute(
                    select(memory_insights)
                    .where(
                        memory_insights.c.user_id == user_id,
                        memory_insights.c.pinned.is_(True),
                    )
                    .order_by(memory_insights.c.created_at.asc())
                )
            ).fetchall()

        log.info(
            "memory.restore",
            user_id=user_id,
            history_id=history_id,
            restored=len(restored),
        )
        try:
            from lmchat.services.audit_service import write_audit_event

            await write_audit_event(
                user_id=user_id,
                event="memory.restore",
                ip=None,
                user_agent=None,
                detail={
                    "history_id": history_id,
                    "restored": len(restored),
                },
                engine=self._engine,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("memory.restore.audit_failed", error=str(exc))
        return [
            MemoryInsight.model_validate(r, from_attributes=True) for r in restored
        ]
