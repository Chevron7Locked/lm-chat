# SPDX-License-Identifier: Apache-2.0
"""Integration tests for unconditional pinned-insight injection.

Per REMEDIATION-SPEC ISSUE-12 + REMEDIATION-PLAN PR-C.

Pinned insights are explicit user preferences set via the Memory page.
They must reach the LLM prompt even when ``chats.settings.rag_enabled``
is false (semantic-recall is opt-in; pinned items are not).

Coverage:
  (1) rag_enabled=false + pinned exists → pinned section injected,
      no semantic-recall section.
  (2) rag_enabled=true + pinned exists → pinned section AND retrieved
      context both injected; pinned section appears first.
  (3) pinned count > settings.lm_chat_pinned_insights_cap → only the
      cap-count are injected, in recency order (newest first).
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import metadata
from lmchat.services.memory_service import MemoryService
from lmchat.services.rag_service import augment_prompt

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def engine(tmp_path: Path):  # type: ignore[return]
    """Yield a fresh in-process SQLite engine with the full schema."""
    db_path = tmp_path / "test_pinned_memory_injection.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}", pool_pre_ping=True)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


async def _insert_user(eng: AsyncEngine, user_id: int) -> None:
    async with eng.begin() as conn:
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO users (id, username, password_hash)"
                " VALUES (:id, :u, :ph)"
            ),
            {"id": user_id, "u": f"user{user_id}", "ph": "scrypt$dummy"},
        )


async def _insert_chat(
    eng: AsyncEngine,
    chat_id: int,
    user_id: int,
    *,
    rag_enabled: bool,
) -> None:
    settings_json = (
        '{"rag_enabled": true}' if rag_enabled else '{"rag_enabled": false}'
    )
    async with eng.begin() as conn:
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO chats (id, user_id, title, settings)"
                " VALUES (:id, :uid, :t, :s)"
            ),
            {"id": chat_id, "uid": user_id, "t": "Test chat", "s": settings_json},
        )


def _make_embedding_client() -> MagicMock:
    client = MagicMock()
    client.embed_one = AsyncMock(return_value=[0.1, 0.2])
    client.embed_batch = AsyncMock(return_value=[[0.1, 0.2]])
    return client


def _make_models_service() -> MagicMock:
    svc = MagicMock()
    svc.list_loaded = AsyncMock(return_value=[])
    return svc


def _make_memory_service_with_pins(
    eng: AsyncEngine, *, recalled: list | None = None
) -> MemoryService:
    """Build a real MemoryService against the test engine.

    Pin storage lives in ``memory_insights``; ``list_pinned`` reads from
    that table directly, so we can exercise the real method end-to-end.
    The ``recall`` method (semantic) is patched out via the returned
    object so we control its result independently of the embedding stack.
    """
    svc = MemoryService(
        engine=eng,
        embedding_client=_make_embedding_client(),
        models_service=_make_models_service(),
    )
    svc.recall = AsyncMock(return_value=recalled or [])  # type: ignore[method-assign]
    return svc


def _chunk_hit(
    document_id: int = 10,
    ordinal: int = 0,
    content: str = "document chunk text",
    title: str = "doc.pdf",
) -> object:
    from lmchat.services.retrieval_service import ChunkHit
    return ChunkHit(
        document_id=document_id,
        document_title=title,
        ordinal=ordinal,
        content=content,
        score=0.75,
    )


# ---------------------------------------------------------------------------
# (1) rag_enabled=false + pinned exists → pinned injected
# ---------------------------------------------------------------------------


async def test_pinned_inject_when_rag_disabled(engine: AsyncEngine) -> None:
    """Pinned insight reaches the context block even when rag_enabled=false."""
    await _insert_user(engine, 1)
    await _insert_chat(engine, 1, 1, rag_enabled=False)

    svc = _make_memory_service_with_pins(engine)
    await svc.pin_insight(user_id=1, text="user prefers concise answers")

    with patch(
        "lmchat.services.rag_service.retrieve",
        new_callable=AsyncMock,
        return_value=[],
    ):
        result = await augment_prompt(
            chat_id=1,
            user_id=1,
            current_message="anything",
            engine=engine,
            embedding_client=_make_embedding_client(),
            models_service=_make_models_service(),
            memory_service=svc,
        )

    assert result.pinned_hits == 1
    assert result.memory_hits == 0
    assert result.doc_hits == 0
    assert "## Pinned context" in result.context_block
    assert "user prefers concise answers" in result.context_block
    # Retrieved-context header MUST NOT appear when neither semantic surface
    # produced content.
    assert "## Retrieved context" not in result.context_block
    # Semantic recall must not have been called when rag is off.
    svc.recall.assert_not_called()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# (2) rag_enabled=true + pinned exists → both present, pinned first
# ---------------------------------------------------------------------------


async def test_pinned_inject_alongside_rag_when_enabled(engine: AsyncEngine) -> None:
    """Pinned section + retrieved-context section co-exist; pinned appears first."""
    await _insert_user(engine, 1)
    await _insert_chat(engine, 1, 1, rag_enabled=True)

    svc = _make_memory_service_with_pins(engine)
    await svc.pin_insight(user_id=1, text="always cite sources")

    with patch(
        "lmchat.services.rag_service.retrieve",
        new_callable=AsyncMock,
        return_value=[_chunk_hit(content="doc body text", title="notes.txt")],
    ):
        result = await augment_prompt(
            chat_id=1,
            user_id=1,
            current_message="some query",
            engine=engine,
            embedding_client=_make_embedding_client(),
            models_service=_make_models_service(),
            memory_service=svc,
        )

    assert result.pinned_hits == 1
    assert result.doc_hits == 1
    block = result.context_block
    assert "## Pinned context" in block
    assert "always cite sources" in block
    assert "## Retrieved context" in block
    assert "### Documents" in block
    # Pinned section must precede retrieved context — pinned is the user's
    # explicit standing preference, semantic recall is supplemental.
    assert block.index("## Pinned context") < block.index("## Retrieved context")


# ---------------------------------------------------------------------------
# (3) pin count > cap → only cap-count injected, recency order
# ---------------------------------------------------------------------------


async def test_pinned_cap_respected(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If pinned count exceeds the cap, only the newest cap-many are injected."""
    await _insert_user(engine, 1)
    await _insert_chat(engine, 1, 1, rag_enabled=False)

    # Force a tiny cap so the test is deterministic without inserting hundreds
    # of rows. The cap is read via get_settings() inside rag_service.
    from lmchat.config import get_settings

    base_settings = get_settings()
    base_settings_dict = base_settings.model_dump()
    base_settings_dict["lm_chat_pinned_insights_cap"] = 2
    cap_value: int = base_settings_dict["lm_chat_pinned_insights_cap"]

    class _StubSettings:
        lm_chat_pinned_insights_cap = cap_value

    monkeypatch.setattr(
        "lmchat.services.rag_service.get_settings",
        lambda: _StubSettings(),
    )

    svc = _make_memory_service_with_pins(engine)
    # Insert 3 pins with explicit, distinct created_at timestamps so the
    # newest-first ORDER BY is deterministic (server-default NOW() can
    # collide on millisecond-granular clocks across rapid inserts).
    from datetime import UTC, datetime, timedelta

    base_dt = datetime(2026, 1, 1, tzinfo=UTC)
    async with engine.begin() as conn:
        for i, label in enumerate(["oldest pin", "middle pin", "newest pin"]):
            await conn.execute(
                text(
                    "INSERT INTO memory_insights"
                    " (user_id, text, text_hash, pinned, created_at)"
                    " VALUES (:uid, :t, :h, 1, :ts)"
                ),
                {
                    "uid": 1,
                    "t": label,
                    "h": f"hash{i}",
                    "ts": (base_dt + timedelta(seconds=i)).isoformat(),
                },
            )

    with patch(
        "lmchat.services.rag_service.retrieve",
        new_callable=AsyncMock,
        return_value=[],
    ):
        result = await augment_prompt(
            chat_id=1,
            user_id=1,
            current_message="anything",
            engine=engine,
            embedding_client=_make_embedding_client(),
            models_service=_make_models_service(),
            memory_service=svc,
        )

    assert result.pinned_hits == 2
    block = result.context_block
    assert "newest pin" in block
    assert "middle pin" in block
    assert "oldest pin" not in block
