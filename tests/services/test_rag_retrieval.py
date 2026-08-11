# SPDX-License-Identifier: Apache-2.0
"""Tests for retrieval_service — hybrid FTS5 + vector + RRF fusion.

Covers:
- retrieve returns top-k results from a fixture corpus.
- RRF fusion is stable (deterministic for the same inputs).
- Vector-only path: FTS returns nothing, vector results come through.
- FTS-only path: chunks match keyword but not vector.
- Tenant isolation: user A cannot retrieve user B's chunks.
- Empty corpus returns empty list.
"""
from __future__ import annotations

import asyncio
import struct
import sys
from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import alembic.command
import alembic.config
import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import document_chunks
from lmchat.services.models_service import Capabilities, ModelInfo
from lmchat.services.retrieval_service import retrieve

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _run_upgrade(db_url: str) -> None:
    ini = _REPO_ROOT / "alembic.ini"
    cfg = alembic.config.Config(str(ini))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    alembic.command.upgrade(cfg, "head")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pack(vec: list[float]) -> bytes:
    n = len(vec)
    return struct.pack(f"<{n}f", *vec)


def _make_models_service(
    model_key: str = "text-embedding-nomic-embed-text-v1.5",
) -> MagicMock:
    svc = MagicMock()
    info = ModelInfo(
        key=model_key,
        type="embedding",
        # loaded_instance_ids mirrors LM Studio's live response: the wire id
        # equals the catalog key for non-quantised test models.
        loaded_instance_ids=[model_key],
        capabilities=Capabilities(vision=False, trained_for_tool_use=False),
    )
    svc.list_loaded = AsyncMock(return_value=[info])
    # resolve_embedding_model_status consults auth_failed before a
    # force-reprobe; keep it False (bool) so the reprobe branch is skipped.
    svc.auth_failed = False
    # resolve_embedding_wire_id is called in retrieve() to normalise
    # catalog-key vs wire-id comparisons. For test models the key == wire id.
    # Returns the wire id only for the configured model_key; None for all others
    # (i.e. other models are not loaded — correct cross-model behaviour).
    async def _resolve_wire(model_id: str) -> str | None:
        return model_key if model_id == model_key else None
    svc.resolve_embedding_wire_id = _resolve_wire
    return svc


def _make_embedding_client(query_vec: list[float]) -> MagicMock:
    client = MagicMock()
    client.embed_one = AsyncMock(return_value=query_vec)
    client.embed_batch = AsyncMock(return_value=[query_vec])
    return client


@pytest.fixture()
async def engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    """Yield a fresh per-test SQLite engine with the full schema + FTS5."""
    db_path = tmp_path / "test_retrieval.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"
    # Use Alembic so the FTS5 virtual table and triggers are created.
    await asyncio.to_thread(_run_upgrade, db_url)
    eng = create_async_engine(db_url, pool_pre_ping=True)
    yield eng
    await eng.dispose()


async def _insert_user(engine: AsyncEngine, user_id: int) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO users (id, username, password_hash)"
                " VALUES (:id, :u, :ph)"
            ),
            {"id": user_id, "u": f"user{user_id}", "ph": "scrypt$dummy"},
        )


async def _insert_document(
    engine: AsyncEngine,
    user_id: int,
    doc_id: int,
    title: str,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO documents (id, user_id, title, mime_type, byte_size,"
                " chunk_count, embedding_model_id, sha256)"
                " VALUES (:id, :uid, :title, 'text/plain', 100, 0, 'nomic-embed',"
                " :sha)"
            ),
            {"id": doc_id, "uid": user_id, "title": title, "sha": f"sha256_{doc_id}"},
        )


async def _insert_chunk(
    engine: AsyncEngine,
    chunk_id: int,
    doc_id: int,
    ordinal: int,
    text_content: str,
    vec: list[float],
    embedding_model_id: str | None = "nomic-embed",
) -> None:
    import re
    from hashlib import blake2b

    def _normalize(t: str) -> str:
        return re.sub(r"\s+", " ", t).strip().casefold()

    t_hash = blake2b(_normalize(text_content).encode(), digest_size=32).hexdigest()
    blob = _pack(vec)

    async with engine.begin() as conn:
        await conn.execute(
            insert(document_chunks).values(
                id=chunk_id,
                document_id=doc_id,
                ordinal=ordinal,
                text=text_content,
                text_hash=t_hash,
                embedding=blob,
                # Chunks record which embedding model produced the
                # vector. Default to the loaded fixture model so the vector
                # stage (which skips NULL-model rows) actually runs; pass an
                # explicit value to exercise the cross-model path.
                embedding_model_id=embedding_model_id,
            )
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retrieve_empty_corpus(engine: AsyncEngine) -> None:
    """retrieve on empty DB returns empty list."""
    await _insert_user(engine, 1)
    svc = _make_models_service()
    client = _make_embedding_client([0.1, 0.0, 0.0, 0.0])

    hits = await retrieve(
        query="anything",
        user_id=1,
        top_k=5,
        engine=engine,
        embedding_client=client,
        models_service=svc,
    )
    assert hits == []


@pytest.mark.asyncio
async def test_retrieve_vector_ranking(engine: AsyncEngine) -> None:
    """Vector search returns the most similar chunk in top position."""
    await _insert_user(engine, 1)
    await _insert_document(engine, 1, 1, "doc1")

    # chunk 1: very similar to query (high dot product)
    # chunk 2: dissimilar (orthogonal)
    await _insert_chunk(engine, 1, 1, 0, "highly relevant text", [1.0, 0.0, 0.0, 0.0])
    await _insert_chunk(engine, 2, 1, 1, "unrelated text about cats", [0.0, 1.0, 0.0, 0.0])

    # Query vector aligns with chunk 1.
    svc = _make_models_service()
    client = _make_embedding_client([1.0, 0.0, 0.0, 0.0])

    hits = await retrieve(
        query="relevant",
        user_id=1,
        top_k=2,
        engine=engine,
        embedding_client=client,
        models_service=svc,
    )

    assert len(hits) >= 1
    # The first result should be chunk 1 (most similar vector).
    assert hits[0].ordinal == 0
    assert "relevant" in hits[0].content


@pytest.mark.asyncio
async def test_retrieve_rrf_fusion_stable(engine: AsyncEngine) -> None:
    """RRF scores are deterministic for the same corpus and query."""
    await _insert_user(engine, 1)
    await _insert_document(engine, 1, 1, "doc1")

    # 5 chunks with varying similarity.
    for i in range(5):
        vec = [float(i == 0), float(i == 1), float(i == 2), float(i == 3)]
        await _insert_chunk(engine, i + 1, 1, i, f"chunk {i} text content", vec)

    svc = _make_models_service()
    client = _make_embedding_client([1.0, 0.0, 0.0, 0.0])

    hits1 = await retrieve(
        query="text",
        user_id=1,
        top_k=5,
        engine=engine,
        embedding_client=client,
        models_service=svc,
    )
    hits2 = await retrieve(
        query="text",
        user_id=1,
        top_k=5,
        engine=engine,
        embedding_client=client,
        models_service=svc,
    )

    assert [h.ordinal for h in hits1] == [h.ordinal for h in hits2]
    assert [h.score for h in hits1] == [h.score for h in hits2]


@pytest.mark.asyncio
async def test_retrieve_tenant_isolation(engine: AsyncEngine) -> None:
    """User A cannot retrieve User B's chunks."""
    await _insert_user(engine, 1)
    await _insert_user(engine, 2)

    # User 2's document.
    await _insert_document(engine, 2, 1, "user2_doc")
    await _insert_chunk(engine, 1, 1, 0, "secret user2 content", [1.0, 0.0, 0.0, 0.0])

    svc = _make_models_service()
    client = _make_embedding_client([1.0, 0.0, 0.0, 0.0])

    # User 1 queries — must get no results.
    hits = await retrieve(
        query="secret",
        user_id=1,
        top_k=5,
        engine=engine,
        embedding_client=client,
        models_service=svc,
    )
    assert hits == [], "User 1 must not see User 2's chunks"


@pytest.mark.asyncio
async def test_retrieve_top_k_limit(engine: AsyncEngine) -> None:
    """retrieve respects top_k and returns at most top_k results."""
    await _insert_user(engine, 1)
    await _insert_document(engine, 1, 1, "doc1")

    # Insert 10 chunks.
    for i in range(10):
        vec = [float(i == j) for j in range(4)] if i < 4 else [0.1, 0.1, 0.1, 0.1]
        await _insert_chunk(engine, i + 1, 1, i, f"chunk {i} relevant content", vec)

    svc = _make_models_service()
    client = _make_embedding_client([1.0, 0.0, 0.0, 0.0])

    hits = await retrieve(
        query="relevant",
        user_id=1,
        top_k=3,
        engine=engine,
        embedding_client=client,
        models_service=svc,
    )
    assert len(hits) <= 3


@pytest.mark.asyncio
async def test_retrieve_no_embedding_model_falls_back_to_fts(engine: AsyncEngine) -> None:
    """With no embedding model loaded, retrieve no longer
    returns []; it degrades to FTS-only keyword retrieval (the vector stage is
    skipped, the query is never embedded)."""
    await _insert_user(engine, 1)
    await _insert_document(engine, 1, 1, "doc1")
    await _insert_chunk(engine, 1, 1, 0, "some content", [1.0, 0.0, 0.0, 0.0])

    svc = MagicMock()
    svc.list_loaded = AsyncMock(return_value=[])  # No models loaded.
    svc.auth_failed = False
    client = _make_embedding_client([1.0, 0.0, 0.0, 0.0])

    hits = await retrieve(
        query="content",
        user_id=1,
        top_k=5,
        engine=engine,
        embedding_client=client,
        models_service=svc,
    )
    # FTS keyword match on "content" still surfaces the chunk.
    assert len(hits) == 1
    assert "content" in hits[0].content
    client.embed_one.assert_not_called()


@pytest.mark.asyncio
async def test_retrieve_cross_model_document(engine: AsyncEngine) -> None:
    """Cross-model regression: a doc embedded under model A is retrieved by a
    natural-language query even when model B is the currently-loaded model.

    Pre-fix, the read path embedded the query under the loaded model and
    compared against all chunks regardless of their write-time model — so a
    chunk written under a different model was compared in the wrong vector
    space (or skipped entirely). The fix groups chunks by their stored
    ``embedding_model_id`` and embeds the query once per distinct model.
    """
    await _insert_user(engine, 1)
    await _insert_document(engine, 1, 1, "doc1")
    # Chunk written under model-A. Its text shares NO token with the query,
    # so the ONLY way to retrieve it is the vector stage under model-A.
    await _insert_chunk(
        engine, 1, 1, 0, "alpha beta gamma", [1.0, 0.0, 0.0, 0.0],
        embedding_model_id="model-A",
    )

    # The canonical default (nomic) is the currently-loaded embedding model,
    # so the no-preference resolver resolves it and the vector
    # stage runs. The chunk's write-time model (model-A) differs from the
    # loaded model — the per-model grouping must still embed the query under
    # model-A to retrieve it.
    svc = _make_models_service()  # nomic default loaded
    client = _make_embedding_client([1.0, 0.0, 0.0, 0.0])

    hits = await retrieve(
        query="zeta",  # no FTS overlap with the chunk text
        user_id=1,
        top_k=5,
        engine=engine,
        embedding_client=client,
        models_service=svc,
    )

    assert len(hits) == 1, "cross-model chunk must be retrieved via per-model embedding"
    assert hits[0].document_id == 1
    # The query was embedded under the chunk's write-time model (model-A),
    # not only the loaded model (nomic).
    called_models = {c.kwargs.get("model_id") for c in client.embed_one.call_args_list}
    assert "model-A" in called_models


@pytest.mark.asyncio
async def test_retrieve_null_model_chunk_keyword_only(engine: AsyncEngine) -> None:
    """A chunk with NULL ``embedding_model_id`` (unknown provenance) is
    excluded from the vector stage but still retrievable via the FTS keyword
    stage — the natural-language keyword fix carries it."""
    await _insert_user(engine, 1)
    await _insert_document(engine, 1, 1, "doc1")
    await _insert_chunk(
        engine, 1, 1, 0, "the BLUEFALCON project budget", [1.0, 0.0, 0.0, 0.0],
        embedding_model_id=None,
    )

    svc = _make_models_service(model_key="nomic-embed")
    # Orthogonal query vector: even if the vector stage somehow ran, it would
    # not be the reason this chunk is found.
    client = _make_embedding_client([0.0, 1.0, 0.0, 0.0])

    hits = await retrieve(
        query="What is BLUEFALCON",  # natural-language → keyword match
        user_id=1,
        top_k=5,
        engine=engine,
        embedding_client=client,
        models_service=svc,
    )

    assert len(hits) == 1, "NULL-model chunk must still be found via FTS keyword"
    assert "BLUEFALCON" in hits[0].content


@pytest.mark.asyncio
async def test_retrieve_fts_only_when_no_embedding_model(engine: AsyncEngine) -> None:
    """With NO embedding model loaded, retrieve() must still
    return FTS keyword hits — it must not block keyword search on the embedding
    gate (the cold-cache-after-401 failure mode)."""
    await _insert_user(engine, 1)
    await _insert_document(engine, 1, 1, "doc1")
    await _insert_chunk(
        engine, 1, 1, 0, "the ORCHIDKEY hardware budget is 12000", [1.0, 0.0, 0.0, 0.0],
        embedding_model_id="nomic-embed",
    )

    # models_service reporting NO embedding model loaded, and not auth-failed
    # (so the force-reprobe path is not taken).
    svc = MagicMock()
    svc.list_loaded = AsyncMock(return_value=[])
    svc.auth_failed = False
    client = _make_embedding_client([1.0, 0.0, 0.0, 0.0])

    hits = await retrieve(
        query="What is ORCHIDKEY",
        user_id=1,
        top_k=5,
        engine=engine,
        embedding_client=client,
        models_service=svc,
    )

    assert len(hits) == 1, "FTS keyword retrieval must work with no embedding model"
    assert "ORCHIDKEY" in hits[0].content
    # The vector stage was skipped entirely → the query was never embedded.
    client.embed_one.assert_not_called()
