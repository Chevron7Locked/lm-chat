# SPDX-License-Identifier: Apache-2.0
"""Tests for MemoryService.pin_insight() cap enforcement.

Tests for the memory pin cap.

Covers:
- pin_insight at cap raises PinnedInsightsCapError.
- cap is per-user: userA at cap does not block userB.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import metadata
from lmchat.embedding.client import EmbeddingClient
from lmchat.services.memory_service import MemoryService, PinnedInsightsCapError
from lmchat.services.models_service import Capabilities, ModelInfo, ModelsService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    """Yield a fresh per-test SQLite engine with the schema."""
    db_path = tmp_path / "test_memory_pin_cap.db"
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


async def test_pin_insight_at_cap_raises_PinnedInsightsCapError(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the user is at the configured cap, pin_insight raises PinnedInsightsCapError."""
    # Set cap to 2 via monkeypatching LM_CHAT_PINNED_INSIGHTS_CAP.
    monkeypatch.setenv("LM_CHAT_PINNED_INSIGHTS_CAP", "2")

    from lmchat.config import get_settings

    get_settings.cache_clear()

    try:
        await _insert_user(engine, 1)
        svc = _make_service(engine)

        await svc.pin_insight(user_id=1, text="First insight")
        await svc.pin_insight(user_id=1, text="Second insight")

        # At cap — next distinct insight should raise.
        with pytest.raises(PinnedInsightsCapError):
            await svc.pin_insight(user_id=1, text="Third insight — over cap")
    finally:
        get_settings.cache_clear()


async def test_pin_insight_cap_is_per_user(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """userA at cap does not block userB from pinning."""
    monkeypatch.setenv("LM_CHAT_PINNED_INSIGHTS_CAP", "1")

    from lmchat.config import get_settings

    get_settings.cache_clear()

    try:
        await _insert_user(engine, 1)
        await _insert_user(engine, 2)

        svc = _make_service(engine)

        # User 1 reaches cap.
        await svc.pin_insight(user_id=1, text="User1 insight")

        # User 1 is at cap; raises.
        with pytest.raises(PinnedInsightsCapError):
            await svc.pin_insight(user_id=1, text="User1 second insight")

        # User 2 is independent; should succeed.
        result = await svc.pin_insight(user_id=2, text="User2 insight")
        assert result.user_id == 2

    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Re-pin of same text at cap returns existing
# ---------------------------------------------------------------------------


async def test_pin_insight_at_cap_returns_existing_for_idempotent_repin(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user at cap who re-pins the SAME content gets the existing pin,
    NOT PinnedInsightsCapError.

    pin_insight dedup-short-circuits BEFORE checking the cap.
    """
    monkeypatch.setenv("LM_CHAT_PINNED_INSIGHTS_CAP", "2")

    from lmchat.config import get_settings

    get_settings.cache_clear()

    try:
        await _insert_user(engine, 1)
        svc = _make_service(engine)

        first = await svc.pin_insight(user_id=1, text="My note")
        await svc.pin_insight(user_id=1, text="Another note")

        # User is at cap. Re-pinning "My note" must return the existing
        # row, not raise PinnedInsightsCapError. text_hash equality is
        # what matters (whitespace-collapsed + casefolded).
        # Different case + extra whitespace — same normalized hash.
        repin = await svc.pin_insight(user_id=1, text="my note  ")
        assert repin.id == first.id
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# NoEmbeddingModelLoadedError coverage
# ---------------------------------------------------------------------------


async def test_index_message_raises_no_embedding_model_loaded_error(
    engine: AsyncEngine,
) -> None:
    """When ModelsService has no embedding model in its cache, any code
    path that needs the default embedding model raises
    NoEmbeddingModelLoadedError. This guard had zero coverage before.
    """
    from lmchat.services.memory_service import NoEmbeddingModelLoadedError

    mock_embedding_client = AsyncMock(spec=EmbeddingClient)
    mock_models_service = AsyncMock(spec=ModelsService)
    # No embedding-type model in the cache — only chat models.
    chat_model = ModelInfo(
        key="chat-model",
        type="llm",
        capabilities=Capabilities(vision=False, trained_for_tool_use=False),
    )
    mock_models_service.list_loaded.return_value = [chat_model]

    svc = MemoryService(
        engine=engine,
        embedding_client=mock_embedding_client,
        models_service=mock_models_service,
    )

    # Any embed_texts call triggers _default_embedding_model() which
    # iterates models_service.list_loaded() and raises if no
    # type=="embedding" entry is found.
    with pytest.raises(NoEmbeddingModelLoadedError):
        await svc.embed_texts(["any text"])
