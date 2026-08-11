# SPDX-License-Identifier: Apache-2.0
"""Tests for MemoryService.recall().

Tests for memory recall.

Covers:
- recall returns top_k results sorted by similarity descending.
- recall filters by embedding_model_id — different-model rows are excluded.
- recall filters by user_id via the chat JOIN.
- recall on empty DB returns an empty list.
- recall with top_k less than available rows truncates correctly.
"""
from __future__ import annotations

import struct
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import metadata
from lmchat.embedding.client import EmbeddingClient
from lmchat.embedding.errors import EmbeddingUpstreamError
from lmchat.services.memory_service import MemoryService
from lmchat.services.models_service import Capabilities, ModelInfo, ModelsService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    """Yield a fresh per-test SQLite engine with the schema."""
    db_path = tmp_path / "test_memory_recall.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}", pool_pre_ping=True)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


async def _insert_user(engine: AsyncEngine, user_id: int, username: str = "") -> None:
    uname = username or f"user{user_id}"
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO users (id, username, password_hash)"
                " VALUES (:id, :u, :ph)"
            ),
            {"id": user_id, "u": uname, "ph": "scrypt$dummy"},
        )


async def _insert_chat(engine: AsyncEngine, chat_id: int, user_id: int) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO chats (id, user_id, title)"
                " VALUES (:id, :uid, :t)"
            ),
            {"id": chat_id, "uid": user_id, "t": "Test chat"},
        )


async def _insert_message(
    engine: AsyncEngine,
    message_id: int,
    chat_id: int,
    content: str,
    role: str = "user",
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO messages (id, chat_id, role, content)"
                " VALUES (:id, :cid, :r, :c)"
            ),
            {"id": message_id, "cid": chat_id, "r": role, "c": content},
        )


async def _insert_embedding(
    engine: AsyncEngine,
    message_id: int,
    embedding_model_id: str,
    vector: list[float],
    text_hash: str = "a" * 64,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO message_embeddings"
                " (message_id, embedding_model_id, embedding, text_hash)"
                " VALUES (:mid, :model, :emb, :th)"
            ),
            {
                "mid": message_id,
                "model": embedding_model_id,
                "emb": _pack(vector),
                "th": text_hash,
            },
        )


def _make_service(
    engine: AsyncEngine,
    *,
    query_vector: list[float],
    model_id: str = "embed-model-v1",
) -> MemoryService:
    """Return a MemoryService whose embed_one returns query_vector for the given model_id."""
    mock_embedding_client = AsyncMock(spec=EmbeddingClient)
    mock_embedding_client.embed_one.return_value = query_vector

    mock_models_service = AsyncMock(spec=ModelsService)
    mock_model = ModelInfo(
        key=model_id,
        type="embedding",
        capabilities=Capabilities(vision=False, trained_for_tool_use=False),
    )
    mock_models_service.list_loaded.return_value = [mock_model]
    # recall() resolves the stored embedding_model_id through
    # resolve_embedding_wire_id() before embedding the query. Default
    # to identity passthrough so existing
    # tests (which store rows under already-"loaded" model ids) keep
    # embed_one seeing the same id they inserted under; individual tests
    # override this side_effect to exercise resolution/fallback behavior.
    mock_models_service.resolve_embedding_wire_id = AsyncMock(
        side_effect=lambda mid: mid
    )

    return MemoryService(
        engine=engine,
        embedding_client=mock_embedding_client,
        models_service=mock_models_service,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_recall_returns_top_k_sorted_by_similarity(engine: AsyncEngine) -> None:
    """recall returns at most top_k results, sorted by cosine similarity descending."""
    await _insert_user(engine, 1)
    await _insert_chat(engine, 1, user_id=1)

    # Insert 5 messages with known vectors.
    vectors = [
        [1.0, 0.0, 0.0],
        [0.9, 0.1, 0.0],
        [0.5, 0.5, 0.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.0, 1.0],
    ]
    for i, vec in enumerate(vectors, start=1):
        await _insert_message(engine, i, chat_id=1, content=f"msg {i}")
        await _insert_embedding(engine, i, "embed-model-v1", vec, text_hash="a" * 63 + str(i))

    # Query is aligned with the first vector.
    query_vec = [1.0, 0.0, 0.0]
    svc = _make_service(engine, query_vector=query_vec)

    results = await svc.recall(user_id=1, query="anything", top_k=3)

    assert len(results) == 3
    # First result should be the [1.0, 0.0, 0.0] vector (cosine = 1.0).
    assert results[0].similarity >= results[1].similarity >= results[2].similarity
    # The most similar message is the one with vector [1.0, 0.0, 0.0].
    assert results[0].similarity == pytest.approx(1.0, abs=1e-5)


async def test_recall_returns_rows_across_different_embedding_models(
    engine: AsyncEngine,
) -> None:
    """Rows embedded under a DIFFERENT model ARE returned in recall.

    Previously, recall filtered by a single ``embedding_model_id``
    (the currently-loaded model), so rows indexed under model A were
    invisible when model B was loaded.  The fix groups rows by their stored
    ``embedding_model_id``, embeds the query once per distinct model, and
    compares within the same vector space.  This test proves cross-model
    retrieval works.
    """
    await _insert_user(engine, 1)
    await _insert_chat(engine, 1, user_id=1)
    await _insert_message(engine, 1, chat_id=1, content="in-model message")
    await _insert_message(engine, 2, chat_id=1, content="other-model message")

    # Message 1: stored under "embed-model-v1".
    await _insert_embedding(engine, 1, "embed-model-v1", [1.0, 0.0], text_hash="a" * 64)
    # Message 2: stored under "other-model" (different model).
    await _insert_embedding(engine, 2, "other-model", [1.0, 0.0], text_hash="b" * 64)

    svc = _make_service(engine, query_vector=[1.0, 0.0], model_id="embed-model-v1")

    results = await svc.recall(user_id=1, query="anything", top_k=10)

    # Both rows should be returned because recall groups by model
    # and embeds the query per model.  Both have the same vector [1.0, 0.0]
    # and the mock returns that for any model_id, so both score 1.0.
    assert len(results) == 2
    assert {r.message_id for r in results} == {1, 2}


async def test_recall_filters_by_user_id_via_chat_join(engine: AsyncEngine) -> None:
    """recall excludes messages from chats belonging to a different user_id."""
    await _insert_user(engine, 1)
    await _insert_user(engine, 2)
    await _insert_chat(engine, 1, user_id=1)
    await _insert_chat(engine, 2, user_id=2)

    # Message 1 belongs to user 1.
    await _insert_message(engine, 1, chat_id=1, content="user1 message")
    await _insert_embedding(engine, 1, "embed-model-v1", [1.0, 0.0], text_hash="a" * 64)

    # Message 2 belongs to user 2.
    await _insert_message(engine, 2, chat_id=2, content="user2 message")
    await _insert_embedding(engine, 2, "embed-model-v1", [1.0, 0.0], text_hash="b" * 64)

    svc = _make_service(engine, query_vector=[1.0, 0.0])

    results = await svc.recall(user_id=1, query="anything", top_k=10)

    assert len(results) == 1
    assert results[0].message_id == 1


async def test_recall_empty_db_returns_empty_list(engine: AsyncEngine) -> None:
    """recall on a corpus with no embedding rows returns an empty list."""
    svc = _make_service(engine, query_vector=[0.5, 0.5])
    results = await svc.recall(user_id=1, query="anything", top_k=5)
    assert results == []


async def test_recall_with_top_k_less_than_available(engine: AsyncEngine) -> None:
    """recall with top_k=2 returns exactly 2 results when 5 are available."""
    await _insert_user(engine, 1)
    await _insert_chat(engine, 1, user_id=1)

    for i in range(1, 6):
        await _insert_message(engine, i, chat_id=1, content=f"msg {i}")
        await _insert_embedding(
            engine, i, "embed-model-v1", [float(i), 0.0], text_hash="a" * 63 + str(i)
        )

    svc = _make_service(engine, query_vector=[1.0, 0.0])
    results = await svc.recall(user_id=1, query="anything", top_k=2)

    assert len(results) == 2


async def test_recall_isolates_failing_embedding_model_group(
    engine: AsyncEngine,
) -> None:
    """A stale/unloadable embedding_model_id group must not kill
    recall for every other group.

    Previously, the per-group ``embed_one`` call in ``recall()`` was not
    wrapped in try/except, so one ``EmbeddingUpstreamError`` (e.g. a row
    stored under a quant-suffixed id that's no longer loaded) propagated
    out of the whole method and killed recall entirely — surfacing as
    ``memory_hits=0`` for the turn even though a perfectly good group of
    rows (embedded under the currently-loaded bare model) existed.
    """
    await _insert_user(engine, 1)
    await _insert_chat(engine, 1, user_id=1)
    await _insert_message(engine, 1, chat_id=1, content="bare-model message")
    await _insert_message(engine, 2, chat_id=1, content="stale-quant message")

    bare_id = "text-embedding-nomic-embed-text-v1.5"
    stale_id = "text-embedding-nomic-embed-text-v1.5@q8_0"

    await _insert_embedding(engine, 1, bare_id, [1.0, 0.0], text_hash="a" * 64)
    await _insert_embedding(engine, 2, stale_id, [1.0, 0.0], text_hash="b" * 64)

    svc = _make_service(engine, query_vector=[1.0, 0.0], model_id=bare_id)

    async def _embed_one(*, model_id: str, **_: object) -> list[float]:
        if model_id == stale_id:
            raise EmbeddingUpstreamError("model not loaded", status_code=400)
        return [1.0, 0.0]

    cast(AsyncMock, svc._embedding_client.embed_one).side_effect = _embed_one

    results = await svc.recall(user_id=1, query="anything", top_k=10)

    assert len(results) == 1
    assert results[0].message_id == 1


async def test_recall_resolves_stored_wire_id_before_embed(
    engine: AsyncEngine,
) -> None:
    """recall must resolve a stored embedding_model_id through
    ModelsService.resolve_embedding_wire_id BEFORE calling embed_one, so a
    stale quant-suffixed id is remapped to the currently loaded wire id
    instead of being sent upstream raw (which LM Studio 400s on).
    """
    await _insert_user(engine, 1)
    await _insert_chat(engine, 1, user_id=1)
    await _insert_message(engine, 1, chat_id=1, content="stale-quant message")

    stale_id = "text-embedding-nomic-embed-text-v1.5@q8_0"
    bare_id = "text-embedding-nomic-embed-text-v1.5"

    await _insert_embedding(engine, 1, stale_id, [1.0, 0.0], text_hash="a" * 64)

    svc = _make_service(engine, query_vector=[1.0, 0.0], model_id=bare_id)
    svc._models_service.resolve_embedding_wire_id = AsyncMock(
        side_effect=lambda mid: bare_id if mid == stale_id else mid
    )

    results = await svc.recall(user_id=1, query="anything", top_k=10)

    assert len(results) == 1
    cast(AsyncMock, svc._embedding_client.embed_one).assert_awaited_once_with(
        text="anything", model_id=bare_id
    )


async def test_recall_skips_group_when_resolver_reports_unresolvable(
    engine: AsyncEngine,
) -> None:
    """When resolve_embedding_wire_id reports a group's
    embedding_model_id as unresolvable (returns None), recall must SKIP
    that group rather than falling back to the raw stored id and calling
    embed_one with it — a guaranteed-doomed call LM Studio 400s on. Other
    groups must still recall normally.
    """
    await _insert_user(engine, 1)
    await _insert_chat(engine, 1, user_id=1)
    await _insert_message(engine, 1, chat_id=1, content="unresolvable-model message")
    await _insert_message(engine, 2, chat_id=1, content="resolvable message")

    unresolvable_id = "text-embedding-ghost-model-v1"
    resolvable_id = "text-embedding-nomic-embed-text-v1.5"

    await _insert_embedding(engine, 1, unresolvable_id, [1.0, 0.0], text_hash="a" * 64)
    await _insert_embedding(engine, 2, resolvable_id, [1.0, 0.0], text_hash="b" * 64)

    svc = _make_service(engine, query_vector=[1.0, 0.0], model_id=resolvable_id)
    svc._models_service.resolve_embedding_wire_id = AsyncMock(
        side_effect=lambda mid: None if mid == unresolvable_id else mid
    )

    results = await svc.recall(user_id=1, query="anything", top_k=10)

    # The resolvable group still recalls successfully.
    assert len(results) == 1
    assert results[0].message_id == 2

    # embed_one must NEVER be called with the unresolvable id — the group
    # is skipped BEFORE the doomed embed call, not caught after it 400s.
    embed_calls = cast(AsyncMock, svc._embedding_client.embed_one).await_args_list
    called_model_ids = [c.kwargs.get("model_id") for c in embed_calls]
    assert unresolvable_id not in called_model_ids
