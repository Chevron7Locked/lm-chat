# SPDX-License-Identifier: Apache-2.0
"""Tests for MemoryService.handle_message_deleted().

Tests for memory compaction cascade.

Covers:
- handle_message_deleted emits a structured log event with the expected fields.
- handle_message_deleted is idempotent when no embedding row exists.
"""
from __future__ import annotations

import struct
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import metadata
from lmchat.embedding.client import EmbeddingClient
from lmchat.services.memory_service import MemoryService
from lmchat.services.models_service import Capabilities, ModelInfo, ModelsService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    """Yield a fresh per-test SQLite engine with the schema."""
    db_path = tmp_path / "test_compaction_cascade.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}", pool_pre_ping=True)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


async def _seed(engine: AsyncEngine, *, message_id: int, content: str, model_id: str) -> None:
    """Insert the full chain: user → chat → message → message_embedding."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO users (id, username, password_hash)"
                " VALUES (1, 'alice', 'scrypt$dummy')"
            )
        )
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO chats (id, user_id, title)"
                " VALUES (1, 1, 'Test')"
            )
        )
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO messages (id, chat_id, role, content)"
                " VALUES (:mid, 1, 'user', :c)"
            ),
            {"mid": message_id, "c": content},
        )
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO message_embeddings"
                " (message_id, embedding_model_id, embedding, text_hash)"
                " VALUES (:mid, :model, :emb, :th)"
            ),
            {
                "mid": message_id,
                "model": model_id,
                "emb": _pack([0.1, 0.2, 0.3]),
                "th": "a" * 64,
            },
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


async def test_handle_message_deleted_emits_structured_log(engine: AsyncEngine) -> None:
    """handle_message_deleted emits memory.message_deleted with message_id + embedding_model_id."""
    model_id = "embed-model-v1"
    await _seed(engine, message_id=42, content="test content", model_id=model_id)

    svc = _make_service(engine)

    emitted: list[dict[str, Any]] = []

    class _CapturingLogger:
        def info(self, event: str, **kwargs: Any) -> None:
            emitted.append({"event": event, **kwargs})

        def warning(self, event: str, **kwargs: Any) -> None:
            pass

        def error(self, event: str, **kwargs: Any) -> None:
            pass

    import lmchat.services.memory_service as _mod

    original_log = _mod.log
    _mod.log = _CapturingLogger()  # type: ignore[assignment]
    try:
        await svc.handle_message_deleted(message_id=42)
    finally:
        _mod.log = original_log

    deleted_events = [e for e in emitted if e["event"] == "memory.message_deleted"]
    assert len(deleted_events) == 1
    ev = deleted_events[0]
    assert ev["message_id"] == 42
    assert ev["embedding_model_id"] == model_id


async def test_handle_message_deleted_idempotent_when_no_embedding_exists(
    engine: AsyncEngine,
) -> None:
    """handle_message_deleted does not raise when no embedding row exists."""
    svc = _make_service(engine)

    # No setup — no embedding row for message_id=999.
    # Should complete without exception.
    await svc.handle_message_deleted(message_id=999)

    # Verify it still emits the log event (with embedding_model_id=None).
    emitted: list[dict[str, Any]] = []

    class _CapturingLogger:
        def info(self, event: str, **kwargs: Any) -> None:
            emitted.append({"event": event, **kwargs})

        def warning(self, event: str, **kwargs: Any) -> None:
            pass

        def error(self, event: str, **kwargs: Any) -> None:
            pass

    import lmchat.services.memory_service as _mod

    original_log = _mod.log
    _mod.log = _CapturingLogger()  # type: ignore[assignment]
    try:
        await svc.handle_message_deleted(message_id=999)
    finally:
        _mod.log = original_log

    deleted_events = [e for e in emitted if e["event"] == "memory.message_deleted"]
    assert len(deleted_events) == 1
    assert deleted_events[0]["message_id"] == 999
    assert deleted_events[0]["embedding_model_id"] is None
