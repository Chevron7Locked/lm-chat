# SPDX-License-Identifier: Apache-2.0
"""Tests for MemoryService.index_message().

Tests for the memory index.

Covers:
- index_message writes a row with the expected text_hash.
- text_hash is blake2b(digest_size=32) of the normalized text.
- index_message is idempotent on the same message_id.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import message_embeddings, metadata
from lmchat.embedding.client import EmbeddingClient
from lmchat.services.memory_service import MemoryService
from lmchat.services.models_service import ModelsService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    """Yield a fresh per-test SQLite engine with the schema."""
    db_path = tmp_path / "test_memory_index.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}", pool_pre_ping=True)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


async def _seed_message(
    engine: AsyncEngine,
    *,
    user_id: int = 1,
    chat_id: int = 1,
    message_id: int = 1,
    content: str = "Hello world",
) -> None:
    """Insert user, chat, and message rows for FK satisfaction."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO users (id, username, password_hash)"
                " VALUES (:id, :u, :ph)"
            ),
            {"id": user_id, "u": f"user{user_id}", "ph": "scrypt$dummy"},
        )
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO chats (id, user_id, title)"
                " VALUES (:id, :uid, :t)"
            ),
            {"id": chat_id, "uid": user_id, "t": "Test chat"},
        )
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO messages (id, chat_id, role, content)"
                " VALUES (:id, :cid, :r, :c)"
            ),
            {"id": message_id, "cid": chat_id, "r": "user", "c": content},
        )


def _make_service(
    engine: AsyncEngine,
    *,
    embedding_vector: list[float] | None = None,
) -> MemoryService:
    """Return a MemoryService with mocked EmbeddingClient and ModelsService."""
    if embedding_vector is None:
        embedding_vector = [0.1, 0.2, 0.3]

    mock_embedding_client = AsyncMock(spec=EmbeddingClient)
    mock_embedding_client.embed_one.return_value = embedding_vector

    mock_models_service = AsyncMock(spec=ModelsService)
    # Simulate one embedding model loaded.
    from lmchat.services.models_service import Capabilities, ModelInfo

    mock_model = ModelInfo(
        # Canonical default (nomic): with no preference set, the resolver now
        # deterministically picks the default embedder, so the loaded model in
        # this fixture must BE the default for index_message to resolve it.
        key="text-embedding-nomic-embed-text-v1.5",
        type="embedding",
        capabilities=Capabilities(vision=False, trained_for_tool_use=False),
        # Genuinely loaded — the resolver filters on loaded_instance_ids, so a
        # realistic loaded embedder must carry at least one instance id.
        loaded_instance_ids=["text-embedding-nomic-embed-text-v1.5@q8_0"],
    )
    mock_models_service.list_loaded.return_value = [mock_model]

    return MemoryService(
        engine=engine,
        embedding_client=mock_embedding_client,
        models_service=mock_models_service,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_index_message_writes_row_with_text_hash(engine: AsyncEngine) -> None:
    """index_message writes a message_embeddings row with a non-empty text_hash."""
    await _seed_message(engine, content="Hello world")
    svc = _make_service(engine)

    await svc.index_message(message_id=1)

    async with engine.connect() as conn:
        result = await conn.execute(
            select(message_embeddings).where(message_embeddings.c.message_id == 1)
        )
        row = result.fetchone()

    assert row is not None
    assert row.text_hash is not None
    assert len(row.text_hash) == 64  # blake2b digest_size=32 → 64 hex chars
    assert row.embedding is not None
    assert row.embedding_model_id == "text-embedding-nomic-embed-text-v1.5"


async def test_index_message_text_hash_is_blake2b_digest_32_of_normalized_text(
    engine: AsyncEngine,
) -> None:
    """text_hash must equal blake2b(digest_size=32) of the normalized text."""
    content = "  Hello   World  "
    await _seed_message(engine, content=content)
    svc = _make_service(engine)

    await svc.index_message(message_id=1)

    # Compute expected hash using the same normalization as the service.
    normalized = re.sub(r"\s+", " ", content).strip().casefold()
    expected_hash = hashlib.blake2b(normalized.encode(), digest_size=32).hexdigest()

    async with engine.connect() as conn:
        result = await conn.execute(
            select(message_embeddings.c.text_hash).where(
                message_embeddings.c.message_id == 1
            )
        )
        row = result.fetchone()

    assert row is not None
    assert row.text_hash == expected_hash


async def test_index_message_idempotent_on_same_message_id(engine: AsyncEngine) -> None:
    """Calling index_message twice for the same message_id must not raise and must not duplicate."""
    await _seed_message(engine, content="Idempotency test")
    svc = _make_service(engine)

    await svc.index_message(message_id=1)
    await svc.index_message(message_id=1)  # second call — must be a no-op

    async with engine.connect() as conn:
        result = await conn.execute(
            select(message_embeddings).where(message_embeddings.c.message_id == 1)
        )
        rows = result.fetchall()

    assert len(rows) == 1  # exactly one row, not two


async def test_index_message_skips_on_dimension_mismatch_with_corpus(
    engine: AsyncEngine,
) -> None:
    """index_message FAILS LOUD (logs + skips) rather than
    storing a vector whose dimension differs from the existing corpus.

    The corpus is established at dim 4 by indexing message 1. Indexing message 2
    with an embedder that returns a dim-3 vector must NOT write a row — storing a
    mismatched vector silently corrupts recall (cross-dimension cosine).
    """
    # Message 1 → corpus dim = 4.
    await _seed_message(engine, message_id=1, content="first")
    svc4 = _make_service(engine, embedding_vector=[0.1, 0.2, 0.3, 0.4])
    await svc4.index_message(message_id=1)

    # Message 2 embedded under a model returning a dim-3 vector → mismatch.
    await _seed_message(engine, message_id=2, content="second")
    svc3 = _make_service(engine, embedding_vector=[0.1, 0.2, 0.3])
    await svc3.index_message(message_id=2)

    async with engine.connect() as conn:
        row1 = (
            await conn.execute(
                select(message_embeddings).where(
                    message_embeddings.c.message_id == 1
                )
            )
        ).fetchone()
        row2 = (
            await conn.execute(
                select(message_embeddings).where(
                    message_embeddings.c.message_id == 2
                )
            )
        ).fetchone()

    # The dim-4 corpus row is present; the mismatched dim-3 row was skipped.
    assert row1 is not None
    assert row2 is None
