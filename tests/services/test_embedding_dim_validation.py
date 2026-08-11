# SPDX-License-Identifier: Apache-2.0
"""Tests for embedding dimension validation in chunk_and_embed.

A misconfigured embedding model that returns vectors of unexpected
dimensions must be rejected with a RuntimeError rather than persisting
corrupt blobs.
"""
from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import alembic.command
import alembic.config
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.services.documents_service import chunk_and_embed
from lmchat.services.models_service import Capabilities, ModelInfo

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _run_upgrade(db_url: str) -> None:
    ini = _REPO_ROOT / "alembic.ini"
    cfg = alembic.config.Config(str(ini))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    alembic.command.upgrade(cfg, "head")


@pytest.fixture()
async def engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    """Yield a fresh per-test SQLite engine with the full schema."""
    db_path = tmp_path / "test_dim.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"
    await asyncio.to_thread(_run_upgrade, db_url)
    eng = create_async_engine(db_url, pool_pre_ping=True)
    yield eng
    await eng.dispose()


def _make_models_service(
    model_key: str = "text-embedding-nomic-embed-text-v1.5",
) -> MagicMock:
    svc = MagicMock()
    info = ModelInfo(
        key=model_key,
        type="embedding",
        capabilities=Capabilities(vision=False, trained_for_tool_use=False),
        # Genuinely loaded — resolver filters on loaded_instance_ids.
        loaded_instance_ids=[f"{model_key}@q8_0"],
    )
    svc.list_loaded = AsyncMock(return_value=[info])
    return svc


@pytest.mark.asyncio
async def test_chunk_and_embed_rejects_mismatched_dim(engine: AsyncEngine) -> None:
    """chunk_and_embed raises RuntimeError when embed_batch returns
    vectors of inconsistent dimension.

    The first vector sets the expected dimension; any subsequent vector
    that differs must cause rejection before any chunk row is inserted.
    """
    # Arrange: mock embed_batch to return two vectors with different dims.
    client = MagicMock()
    # First vector: dim 4; second vector: dim 8 — dimension mismatch.
    client.embed_batch = AsyncMock(
        return_value=[[0.1] * 4, [0.1] * 8]
    )
    svc = _make_models_service()

    # We need a document row to embed chunks into.
    from sqlalchemy import text
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, username, password_hash)"
                " VALUES (1, 'u', 'scrypt$x')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO documents (id, user_id, title, mime_type,"
                " byte_size, chunk_count, embedding_model_id, sha256)"
                " VALUES (1, 1, 'test.txt', 'text/plain', 100, 0,"
                " 'nomic-embed', 'abc123')"
            )
        )

    # Two chunks of text so embed_batch returns two vectors.
    body = b"First chunk content. " + b"A " * 400 + b". Second chunk over here."

    with pytest.raises(RuntimeError, match="Embedding dimension mismatch"):
        await chunk_and_embed(
            document_id=1,
            body_bytes=body,
            mime_type="text/plain",
            engine=engine,
            embedding_client=client,
            models_service=svc,
        )
