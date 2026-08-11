# SPDX-License-Identifier: Apache-2.0
"""Tests for MemoryService insight deduplication via content-hash.

Tests for memory deduplication.

Covers:
- pin_insight deduplicates on text_hash — same normalized text returns the same row.
- text_hash normalization collapses whitespace and case-folds.
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

from lmchat.db.schema import memory_insights, metadata
from lmchat.embedding.client import EmbeddingClient
from lmchat.services.memory_service import MemoryService
from lmchat.services.models_service import Capabilities, ModelInfo, ModelsService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    """Yield a fresh per-test SQLite engine with the schema."""
    db_path = tmp_path / "test_memory_dedup.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}", pool_pre_ping=True)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
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


def _make_service(engine: AsyncEngine) -> MemoryService:
    mock_embedding_client = AsyncMock(spec=EmbeddingClient)
    mock_models_service = AsyncMock(spec=ModelsService)
    mock_model = ModelInfo(
        key="embed-model",
        type="embedding",
        capabilities=Capabilities(vision=False, trained_for_tool_use=False),
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


async def test_pin_insight_dedups_on_text_hash(engine: AsyncEngine) -> None:
    """Pinning the same normalized text twice returns the same row (no duplicate)."""
    await _insert_user(engine, 1)
    svc = _make_service(engine)

    result1 = await svc.pin_insight(user_id=1, text="Hello world")
    result2 = await svc.pin_insight(user_id=1, text="Hello world")

    # Both calls return the same row ID.
    assert result1.id == result2.id

    # Only one row in the DB.
    async with engine.connect() as conn:
        count_result = await conn.execute(
            select(memory_insights).where(memory_insights.c.user_id == 1)
        )
        rows = count_result.fetchall()
    assert len(rows) == 1


async def test_text_hash_normalization_collapses_whitespace_and_casefolds(
    engine: AsyncEngine,
) -> None:
    """'Hello   World' and 'hello world' produce the same text_hash."""
    await _insert_user(engine, 1)
    svc = _make_service(engine)

    result1 = await svc.pin_insight(user_id=1, text="Hello   World")
    result2 = await svc.pin_insight(user_id=1, text="hello world")

    # Same row — dedup worked.
    assert result1.id == result2.id

    # Verify the hash itself matches our reference implementation.
    normalized = re.sub(r"\s+", " ", "Hello   World").strip().casefold()
    expected_hash = hashlib.blake2b(normalized.encode(), digest_size=32).hexdigest()

    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(memory_insights.c.text_hash).where(memory_insights.c.id == result1.id)
            )
        ).fetchone()

    assert row is not None
    assert row.text_hash == expected_hash
