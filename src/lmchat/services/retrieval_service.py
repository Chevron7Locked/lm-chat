# SPDX-License-Identifier: Apache-2.0
"""Hybrid FTS5 + vector retrieval service for lm-chat RAG pipeline.

Algorithm
---------
1. Embed *query* with the current default embedding model.
2. **FTS5 keyword search**: match against ``document_chunks_fts``
   (SQLite) or a simple ``ILIKE`` fallback (Postgres).  Returns ranked
   rows via ``document_chunks_fts.rank`` (BM25).
3. **Vector cosine search**: load all embeddings for the user's active
   chunks, compute cosine similarity in Python (same O(n) approach as
   ``memory_service.recall``), rank by similarity.
4. **Reciprocal Rank Fusion (RRF)**: combine keyword and vector ranks.
   Score = Σ 1/(k + rank_i) with canonical k=60 (env-overridable via
   ``LM_CHAT_RRF_K``).
5. Return top-k chunks with document metadata.

Tenant isolation
----------------
All paths JOIN ``document_chunks`` → ``documents`` and filter by
``documents.user_id == user_id``.  Cross-user access is structurally
impossible for an authenticated request.

Corpus size
-----------
The pure-Python vector scan is adequate for corpora up to ~50k chunks.
sqlite-vec / pgvector acceleration is future scope; the ``retrieve``
signature is stable so the swap will be transparent.

RRF reference
-------------
Cormack, Clarke & Buettner (2009) "Reciprocal Rank Fusion outperforms
Condorcet and individual Rank Learning Methods".  k=60 is the canonical
default from that paper and is well-established in the IR literature.
"""
from __future__ import annotations

import re
from typing import Any, Final

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Row, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from lmchat.config import get_settings
from lmchat.db.schema import document_chunks, documents, projects
from lmchat.embedding.client import EmbeddingClient
from lmchat.embedding.errors import EmbeddingError
from lmchat.embedding.vector_math import (
    cosine_similarity as _cosine_similarity,
)
from lmchat.embedding.vector_math import (
    unpack_embedding as _unpack_embedding,
)
from lmchat.logging import get_logger
from lmchat.services.memory_service import (
    NoEmbeddingModelLoadedError,
    resolve_active_embedding_model_key,
)
from lmchat.services.models_service import ModelsService

log = get_logger(__name__)

# Default RRF constant — overridable via LM_CHAT_RRF_K.
_DEFAULT_RRF_K: Final[int] = 60


def _escape_fts5_phrase(query: str) -> str:
    """Wrap *query* in double-quotes for an exact-phrase FTS5 MATCH.

    This is the **phrase-only** path.  The entire input string is treated
    as an exact phrase — it must appear verbatim in the indexed text.
    Use :func:`_build_fts5_keyword_query` for natural-language queries.

    Internal double-quotes are escaped with ``""`` per FTS5 convention.
    Empty input returns ``'""'`` (matches nothing rather than raising).
    """
    escaped = query.replace('"', '""')
    return f'"{escaped}"'


# Regex used by _build_fts5_keyword_query: match FTS5 operator chars.
# We drop these from individual tokens (they have no meaning once inside
# per-token quotes, but we strip them for predictability).
_FTS5_OP_CHARS: str = r'[()"*]'
_FTS5_OP_RE = re.compile(_FTS5_OP_CHARS)


def _build_fts5_keyword_query(query: str) -> str:
    """Tokenise *query* and build an ``OR``-of-quoted-terms FTS5 MATCH string.

    This is the **default keyword path** for natural-language queries.
    Each token is individually quoted so FTS5 treats it as a literal
    term (no operator injection).  Tokens are joined with `` OR `` so
    that a chunk matching any single token is a candidate (FTS5 BM25
    ranking then favours chunks matching multiple tokens).

    FTS5 operator characters (``()"*``) are stripped from each token;
    ``AND``, ``OR``, ``NOT`` and other reserved words lose their special
    meaning because they are inside double-quotes.

    Returns:
        FTS5-safe MATCH expression.  Empty input returns ``'""'``
        (matches nothing rather than raising).
    """
    if not query or not query.strip():
        return '""'

    tokens = query.split()
    quoted_terms: list[str] = []
    for token in tokens:
        # Strip FTS5 operator chars that have no meaning inside quotes.
        cleaned = _FTS5_OP_RE.sub("", token)
        if not cleaned:
            continue
        # Escape any internal double-quotes.
        escaped = cleaned.replace('"', '""')
        quoted_terms.append(f'"{escaped}"')

    if not quoted_terms:
        return '""'

    return " OR ".join(quoted_terms)


# ---------------------------------------------------------------------------
# Public models
# ---------------------------------------------------------------------------


class ChunkHit(BaseModel):
    """One result from the hybrid retrieval.

    Attributes:
        document_id:    PK of the parent document.
        document_title: Human-readable document title.
        ordinal:        Zero-based chunk index within the document.
        content:        Chunk text.
        score:          RRF fusion score (higher is better).
    """

    model_config = ConfigDict(from_attributes=True)

    document_id: int
    document_title: str
    ordinal: int
    content: str
    score: float


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


# _unpack_embedding / _cosine_similarity now live in
# lmchat.embedding.vector_math and are imported above under their original
# private names so all call sites in this module are unchanged. See that
# module's docstring for the storage format and the single
# dimension-mismatch policy (fail loud, no silent truncation).


def _rrf_score(ranks: list[int], k: int) -> float:
    """Compute the RRF fusion score from a list of ranks.

    Args:
        ranks: List of 1-based ranks from each retrieval system.
        k:     RRF constant (default 60; env-overridable).

    Returns:
        Sum of 1/(k + rank) over all ranks.
    """
    return sum(1.0 / (k + r) for r in ranks)


# ---------------------------------------------------------------------------
# Embedding-model resolution (read-time wiring)
# ---------------------------------------------------------------------------


# Embedding-resolution status codes — surfaced via
# ``GET /api/chats/{id}/rag_mode`` so the UI can show the operator WHY
# retrieval skipped instead of silently degrading.
EMBED_STATUS_OK = "ok"
EMBED_STATUS_PINNED_MODEL_UNAVAILABLE = "pinned_model_unavailable"
EMBED_STATUS_NO_EMBEDDING_MODEL = "no_embedding_model"


async def resolve_embedding_model_status(
    *,
    project_id: int | None,
    user_id: int,
    engine: AsyncEngine,
    models_service: ModelsService,
) -> tuple[str | None, str]:
    """Read-time embedding-model resolution.

    Returns ``(wire_id_or_none, status_code)``.  ``wire_id`` is the
    **loaded instance id** (e.g. ``text-embedding-nomic-embed-text-v1.5@q8_0``)
    that must be passed to the embeddings endpoint — NOT the catalog key.
    LM Studio routes by loaded instance name when JIT is disabled; the
    unsuffixed catalog key yields a 400 "Invalid model identifier" error.

    Resolution mirrors :meth:`ModelsService.resolve_to_loaded_or_fallback`
    for chat models:

    * exact key match against a loaded embedding model → use its
      ``loaded_instance_ids[0]`` (the @quant-suffixed wire id).
    * configured key is a prefix of a loaded instance id (e.g. the key
      ``text-embedding-nomic-embed-text-v1.5`` prefixes the loaded id
      ``text-embedding-nomic-embed-text-v1.5@q8_0``) → use that instance id.
      The prefix match requires the loaded id to start with ``key + "@"``
      so it can never accidentally match a different model family.

    The five status-code branches:

    * project pinned + pinned model loaded → ``(wire_id, "ok")``
    * project pinned + pinned model NOT loaded →
      ``(None, "pinned_model_unavailable")`` — skips retrieval so the
      query isn't embedded under a different model than the stored
      chunks (would produce wrong-vector-space cosine).
    * project not pinned (NULL pin) AND user has an active embedding
      model loaded → ``(wire_id, "ok")``.
    * no ``project_id`` (chat-level / un-projected) AND active model
      loaded → ``(wire_id, "ok")``.
    * no embedding model loaded at all →
      ``(None, "no_embedding_model")``.

    Single implementation; :func:`_resolve_embedding_model_id` is a
    thin wrapper that drops the status code.
    """
    loaded = await models_service.list_loaded()

    # Build a mapping: catalog key → wire id (loaded_instance_ids[0]).
    # Only include embedding models that actually have a loaded instance.
    # The wire id is the @quant-suffixed identifier LM Studio accepts.
    key_to_wire: dict[str, str] = {}
    for m in loaded:
        if m.type == "embedding" and m.loaded_instance_ids:
            key_to_wire[m.key] = m.loaded_instance_ids[0]

    # Also index by instance id itself (passthrough — caller already holds
    # a live instance id, or the configured value IS the full instance id).
    wire_by_instance: dict[str, str] = {}
    for m in loaded:
        if m.type == "embedding":
            for iid in m.loaded_instance_ids:
                wire_by_instance[iid] = iid

    def _resolve_wire(configured_key: str) -> str | None:
        """Return the wire id for *configured_key*, or None if not loaded.

        Resolution order (mirrors resolve_to_loaded_or_fallback):
        1. configured_key is itself a loaded instance id (passthrough).
        2. configured_key matches a loaded model's catalog key exactly.
        3. configured_key is a prefix of exactly one loaded instance id,
           connected by "@" (e.g. "nomic-embed-v1.5" → "nomic-embed-v1.5@q8_0").
           The "@" guard prevents cross-family prefix collisions.
        """
        # (1) Passthrough — the configured value is already a wire id.
        if configured_key in wire_by_instance:
            return configured_key
        # (2) Exact catalog-key match.
        if configured_key in key_to_wire:
            return key_to_wire[configured_key]
        # (3) Prefix match: configured_key + "@" must start a loaded instance id.
        prefix = configured_key + "@"
        matches = [iid for iid in wire_by_instance if iid.startswith(prefix)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            # Ambiguous prefix — take the first loaded instance id for the
            # best matching key (deterministic; warn to surface ambiguity).
            log.warning(
                "retrieval_service.embed_prefix_ambiguous",
                configured_key=configured_key,
                matches=matches,
            )
            return matches[0]
        # (4) Family fallback: the configured key pins a specific @quant that is
        # no longer loaded, but the SAME embedding model IS (a different or
        # un-suffixed quant). Quants of one embedding model share output
        # dimensions, so falling back to the same bare-key family is
        # dimension-safe — and far better than a dead memory/RAG pipeline when
        # LM Studio reloads the model under a different quant name.
        # Preferred pinned to ``…v1.5@q8_0`` while
        # only the bare ``…v1.5`` instance was loaded → resolver returned None →
        # ``no_embedding_model`` → memory silently stopped saving.
        if "@" in configured_key:
            bare = configured_key.split("@", 1)[0]
            if bare in key_to_wire:
                log.warning(
                    "retrieval_service.embed_quant_fallback",
                    configured_key=configured_key,
                    fell_back_to=key_to_wire[bare],
                )
                return key_to_wire[bare]
            bare_matches = [
                iid for iid in wire_by_instance if iid.startswith(bare + "@")
            ]
            if bare_matches:
                log.warning(
                    "retrieval_service.embed_quant_fallback",
                    configured_key=configured_key,
                    fell_back_to=bare_matches[0],
                )
                return bare_matches[0]
        return None

    # user_active_wire: wire id for the stable preferred embedding model.
    # Uses resolve_active_embedding_model_key (single source of truth) which
    # reads/writes the persisted preference so the choice is deterministic
    # even when multiple embedding models are loaded simultaneously.
    # persist_default=True: first call auto-persists the chosen key.
    user_active_wire: str | None
    try:
        _preferred_key = await resolve_active_embedding_model_key(
            engine=engine,
            models_service=models_service,
            persist_default=True,
        )
        user_active_wire = _resolve_wire(_preferred_key)
    except NoEmbeddingModelLoadedError:
        user_active_wire = None

    pinned: str | None = None
    if project_id is not None:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    select(projects.c.embedding_model_id).where(
                        projects.c.id == project_id,
                        projects.c.user_id == user_id,
                    )
                )
            ).fetchone()
        if row is not None:
            raw = row.embedding_model_id
            if raw:
                pinned = str(raw)

    if pinned is not None:
        pinned_wire = _resolve_wire(pinned)
        if pinned_wire is not None:
            return pinned_wire, EMBED_STATUS_OK
        log.warning(
            "retrieval_service.pinned_embedding_model_not_loaded",
            user_id=user_id,
            project_id=project_id,
            pinned_model_id=pinned,
        )
        return None, EMBED_STATUS_PINNED_MODEL_UNAVAILABLE

    if user_active_wire is None:
        # If auth is failing (likely a 401 that left the model cache empty),
        # force ONE storm-guarded reprobe before giving up — recovers a cold
        # start where the api_key was seeded/fixed after the initial probe,
        # without waiting out the 60s backoff.
        if models_service.auth_failed and await models_service.force_refresh():
            try:
                _retry_key = await resolve_active_embedding_model_key(
                    engine=engine,
                    models_service=models_service,
                    persist_default=True,
                )
                user_active_wire = _resolve_wire(_retry_key)
            except NoEmbeddingModelLoadedError:
                user_active_wire = None
        if user_active_wire is None:
            log.warning(
                "retrieval_service.no_embedding_model",
                user_id=user_id,
                project_id=project_id,
            )
            return None, EMBED_STATUS_NO_EMBEDDING_MODEL

    return user_active_wire, EMBED_STATUS_OK


async def _resolve_embedding_model_id(
    *,
    project_id: int | None,
    user_id: int,
    engine: AsyncEngine,
    models_service: ModelsService,
) -> str | None:
    """Returns just the resolved model id, dropping the status code.

    Thin wrapper around :func:`resolve_embedding_model_status` so the
    existing :func:`retrieve` call site can stay status-agnostic while
    the route layer reads the status sentinel.
    """
    model_id, _ = await resolve_embedding_model_status(
        project_id=project_id,
        user_id=user_id,
        engine=engine,
        models_service=models_service,
    )
    return model_id


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def retrieve(
    *,
    query: str,
    user_id: int,
    top_k: int = 8,
    engine: AsyncEngine,
    embedding_client: EmbeddingClient,
    models_service: ModelsService,
    project_id: int | None = None,
) -> list[ChunkHit]:
    """Run hybrid FTS5 + vector retrieval and return *top_k* chunks.

    Args:
        query:            Natural-language query string.
        user_id:          Filter results to chunks owned by this user.
        top_k:            Maximum number of results to return.
        engine:           Async SQLAlchemy engine.
        embedding_client: Embedding client.
        models_service:   For resolving the current embedding model.
        project_id:       When non-None, restrict
            results to chunks from documents in this project. When
            None (default), no project filter is applied (user-scoped
            union, the legacy behavior). Both the FTS5/ILIKE keyword
            stage and the vector stage filter by the same predicate
            so RRF fusion never mixes in-project + out-of-project hits.

    Returns:
        List of :class:`ChunkHit`, sorted by RRF score descending.
        Empty list when retrieval is skipped: corpus is empty, OR
        no embedding model is loaded (handled gracefully — see
        ``_resolve_embedding_model_id``), OR the project's pinned
        embedding model isn't currently loaded.

    Raises:
        EmbeddingError: On upstream embedding failure.
    """
    settings = get_settings()
    rrf_k: int = getattr(settings, "lm_chat_rrf_k", _DEFAULT_RRF_K)

    # Read-time embedding-model resolution. Resolution order with NULL
    # fallback:
    #   1. project_id set AND projects.embedding_model_id non-NULL
    #      AND that model IS loaded → use the pinned model
    #   2. project pinned but pinned model NOT loaded → skip retrieval
    #      (the chunks were embedded under model X; can't query under X
    #      if X isn't available; graceful no-op rather than wrong-vector-
    #      space cosine)
    #   3. project has no pin (NULL) → fall back to user's currently
    #      active embedding model
    #   4. No active embedding model loaded → skip retrieval
    model_id = await _resolve_embedding_model_id(
        project_id=project_id,
        user_id=user_id,
        engine=engine,
        models_service=models_service,
    )
    # Do NOT early-return when no embedding model resolves.
    # The FTS5 keyword stage below needs no embeddings and still surfaces docs
    # by exact term — important when the model cache is cold/empty after a 401.
    # Only the vector stage is gated on a resolved model + query embedding; the
    # existing fts_only second-pass materializes keyword-only hits.
    query_vector: list[float] | None = None
    if model_id is not None:
        query_vector = await embedding_client.embed_one(text=query, model_id=model_id)

    # ------------------------------------------------------------------
    # Step 1 — FTS5 keyword search (SQLite only; ILIKE fallback on PG)
    # ------------------------------------------------------------------
    bind_info_holder: list[str] = []
    async with engine.connect() as conn:
        dialect = conn.dialect.name
        bind_info_holder.append(dialect)

    dialect = bind_info_holder[0]

    # chunk_id → rank-in-list (1-based)
    fts_ranks: dict[int, int] = {}

    # Gate both keyword and vector stages on the same project_id
    # predicate so RRF fusion never mixes in-project + out-of-project
    # hits. The clause is appended via string format
    # only when project_id is set — otherwise the SQL stays
    # bit-identical to the legacy text() form so we don't churn the
    # query plan in the default path.
    _project_clause_sql = (
        " AND d.project_id = :project_id" if project_id is not None else ""
    )
    if dialect == "sqlite":
        # Only the constant _project_clause_sql is f-string-interpolated below;
        # every user value (query, user_id, project_id, limit) binds via
        # :params, so there is no injection surface.
        fts_sql = text(  # nosemgrep
            f"""
            SELECT dc.id
            FROM document_chunks_fts fts
            JOIN document_chunks dc ON dc.id = fts.rowid
            JOIN documents d ON d.id = dc.document_id
            WHERE fts.text MATCH :query
              AND d.user_id = :user_id
              AND d.deleted_at IS NULL
              {_project_clause_sql}
            ORDER BY fts.rank
            LIMIT :limit
        """)  # nosec B608
        params: dict[str, object] = {
            "query": _build_fts5_keyword_query(query),
            "user_id": user_id,
            "limit": top_k * 4,
        }
        if project_id is not None:
            params["project_id"] = project_id
        async with engine.connect() as conn:
            fts_rows = (await conn.execute(fts_sql, params)).fetchall()
        for rank_idx, row in enumerate(fts_rows, start=1):
            fts_ranks[row[0]] = rank_idx
    else:
        # Postgres fallback: per-token ILIKE (pg_trgm would be better
        # but requires the extension to be enabled; ILIKE is safe).
        # Tokenise the query and build OR-of-ILIKEs so that a row
        # matching any single token is a candidate — matches the FTS5
        # keyword behaviour above.
        # Only the constant _project_clause_sql is f-string-interpolated below;
        # every user value (user_id, project_id, limit) binds via :params,
        # so there is no injection surface.
        _tokens = [t for t in query.split() if t]
        # An empty/whitespace query yields no keyword candidates (mirrors the
        # SQLite empty-phrase MATCH which matches nothing). Skipping the query
        # also avoids an invalid ``WHERE ()`` when there are no tokens.
        if _tokens:
            _ilike_clauses = " OR ".join(
                f"dc.text ILIKE :pattern_{i}" for i in range(len(_tokens))
            )
            ilike_sql = text(  # nosemgrep
                f"""
                SELECT dc.id
                FROM document_chunks dc
                JOIN documents d ON d.id = dc.document_id
                WHERE ({_ilike_clauses})
                  AND d.user_id = :user_id
                  AND d.deleted_at IS NULL
                  {_project_clause_sql}
                LIMIT :limit
            """)  # nosec B608
            params = {
                f"pattern_{i}": f"%{token}%" for i, token in enumerate(_tokens)
            }
            params["user_id"] = user_id
            params["limit"] = top_k * 4
            if project_id is not None:
                params["project_id"] = project_id
            async with engine.connect() as conn:
                ilike_rows = (
                    await conn.execute(ilike_sql, params)
                ).fetchall()
            for rank_idx, row in enumerate(ilike_rows, start=1):
                fts_ranks[row[0]] = rank_idx

    # ------------------------------------------------------------------
    # Step 2 — Vector cosine search over user chunks, model-grouped
    # ------------------------------------------------------------------
    # Chunk embeddings may have been written under different
    # embedding models.  Group chunks by stored embedding_model_id;
    # embed the query once per distinct model; cosine-compare only
    # within the same model/dim.  Rows with NULL model (unknown)
    # are excluded from vector search to avoid cross-space comparison.
    vec_stmt = (
        select(
            document_chunks.c.id,
            document_chunks.c.document_id,
            document_chunks.c.ordinal,
            document_chunks.c.text,
            document_chunks.c.embedding,
            document_chunks.c.embedding_model_id,
            documents.c.title,
        )
        .join(documents, document_chunks.c.document_id == documents.c.id)
        .where(
            documents.c.user_id == user_id,
            documents.c.deleted_at.is_(None),
            document_chunks.c.embedding_model_id.isnot(None),
        )
    )
    if project_id is not None:
        vec_stmt = vec_stmt.where(documents.c.project_id == project_id)
    async with engine.connect() as conn:
        all_rows = (await conn.execute(vec_stmt)).fetchall()

    if model_id is None or query_vector is None or not all_rows:
        # No embedding model resolved (or empty corpus) → skip the vector
        # stage entirely; the FTS keyword stage above still carries results.
        vec_ranks = {}
        scored = []
    else:
        # Group rows by embedding_model_id.
        rows_by_model: dict[str, list[Row[Any]]] = {}
        for row in all_rows:
            mid = row.embedding_model_id
            if mid:
                rows_by_model.setdefault(mid, []).append(row)

        # Compute cosine similarity per model group.
        scored = []
        # Resolve model_id to its wire id once so the per-group comparison
        # below works regardless of whether stored embedding_model_id values
        # are bare catalog keys (e.g. "nomic-embed-v1.5") or wire ids
        # (e.g. "nomic-embed-v1.5@q8_0"). A direct mid == model_id comparison
        # NEVER matched when mid is a catalog key and model_id is a wire id →
        # always re-embedded (wasted round-trip).
        resolved_query_wire_id = await models_service.resolve_embedding_wire_id(model_id)
        for mid, model_rows in rows_by_model.items():
            # Reuse the already-computed query_vector when this group's
            # stored embedding_model_id resolves to the same wire id as
            # the current query vector was embedded under.
            # CRITICAL: only reuse when the resolved ids match exactly —
            # never reuse query_vector across genuinely different embedding
            # models (dimension mismatch).
            resolved_mid = await models_service.resolve_embedding_wire_id(mid)
            if resolved_mid is not None and resolved_mid == resolved_query_wire_id:
                qv = query_vector
            else:
                try:
                    qv = await embedding_client.embed_one(text=query, model_id=mid)
                except EmbeddingError as exc:
                    # The model these chunks were written under can't be
                    # embedded right now (e.g. JIT-load unavailable). Degrade
                    # to the keyword stage for this group rather than failing
                    # the whole turn — the FTS-only second pass still surfaces
                    # these chunks if they matched the keyword query.
                    log.warning(
                        "retrieval_service.vec_embed_failed",
                        embed_model_id=mid,
                        error=str(exc),
                        skipped_chunks=len(model_rows),
                    )
                    continue
            for row in model_rows:
                candidate = _unpack_embedding(row.embedding)
                # Dim guard: skip if dimensions differ (mismatched
                # models should not happen post-fix but guard anyway).
                if len(qv) != len(candidate):
                    log.warning(
                        "retrieval_service.vec_dim_mismatch",
                        chunk_id=row.id,
                        embed_model_id=mid,
                        query_dim=len(qv),
                        chunk_dim=len(candidate),
                    )
                    continue
                sim = _cosine_similarity(qv, candidate)
                scored.append((sim, row))

        scored.sort(key=lambda x: x[0], reverse=True)

        # Build vector rank dict (only top 4*top_k considered for RRF).
        vec_ranks = {}
        for rank_idx, (_, row) in enumerate(scored[: top_k * 4], start=1):
            vec_ranks[row.id] = rank_idx

    # ------------------------------------------------------------------
    # Step 3 — RRF fusion
    # ------------------------------------------------------------------
    # Union of chunk IDs from both result sets.
    all_chunk_ids = set(fts_ranks) | set(vec_ranks)

    rrf_scores: dict[int, float] = {}
    for chunk_id in all_chunk_ids:
        ranks: list[int] = []
        if chunk_id in fts_ranks:
            ranks.append(fts_ranks[chunk_id])
        if chunk_id in vec_ranks:
            ranks.append(vec_ranks[chunk_id])
        rrf_scores[chunk_id] = _rrf_score(ranks, rrf_k)

    # Sort by RRF score descending, take top_k.
    sorted_ids = sorted(rrf_scores, key=lambda cid: rrf_scores[cid], reverse=True)[:top_k]

    # Build result list from the cached rows (vec scan loaded all rows).
    row_by_id: dict[int, Row[Any]] = {row.id: row for _, row in scored}

    # Also index FTS-only rows (may not be in vector results if they were
    # outside the top 4*top_k vector window).  We need a second pass for those.
    fts_only_ids = set(fts_ranks) - set(vec_ranks)
    if fts_only_ids:
        fts_only_stmt = (
            select(
                document_chunks.c.id,
                document_chunks.c.document_id,
                document_chunks.c.ordinal,
                document_chunks.c.text,
                document_chunks.c.embedding,
                documents.c.title,
            )
            .join(documents, document_chunks.c.document_id == documents.c.id)
            .where(document_chunks.c.id.in_(list(fts_only_ids)))
        )
        async with engine.connect() as conn:
            extra_rows = (await conn.execute(fts_only_stmt)).fetchall()
        for row in extra_rows:
            row_by_id[row.id] = row

    hits: list[ChunkHit] = []
    for chunk_id in sorted_ids:
        row = row_by_id.get(chunk_id)
        if row is None:
            continue
        hits.append(
            ChunkHit(
                document_id=row.document_id,
                document_title=row.title,
                ordinal=row.ordinal,
                content=row.text,
                score=rrf_scores[chunk_id],
            )
        )

    log.info(
        "retrieval_service.retrieve",
        user_id=user_id,
        query_len=len(query),
        fts_candidates=len(fts_ranks),
        vec_candidates=len(vec_ranks),
        rrf_hits=len(hits),
        top_k=top_k,
    )
    return hits
