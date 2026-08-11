# SPDX-License-Identifier: Apache-2.0
"""P13i — incognito memory-write invariant test.

PRIVACY INVARIANT (audit O-04): an incognito chat MUST NEVER cause a row
to land in any of:

- ``message_embeddings`` (no vectors persisted),
- ``memory_insights`` (no long-term insights).

This test sends 10 messages through ``MemoryService.index_message`` for
an incognito chat and asserts **zero** rows are added.  A second pass
through a NON-incognito chat confirms the same code path DOES write
rows when the privacy flag is absent — guarding against the
"off-by-one" failure mode where the guard short-circuits everything.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.pragmas import apply_sqlite_pragmas
from lmchat.db.schema import (
    memory_insights,
    message_embeddings,
    metadata,
)
from lmchat.embedding.client import EmbeddingClient
from lmchat.services.memory_service import MemoryService
from lmchat.services.models_service import Capabilities, ModelInfo, ModelsService

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    db_path = tmp_path / "test_incognito_memory_guard.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}", pool_pre_ping=True)
    from sqlalchemy import event

    @event.listens_for(eng.sync_engine, "connect")
    def _on_connect(dbapi_conn: object, _rec: object) -> None:
        apply_sqlite_pragmas(dbapi_conn)

    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


def _make_memory_service(eng: AsyncEngine) -> MemoryService:
    mock_embedding_client = AsyncMock(spec=EmbeddingClient)
    mock_embedding_client.embed_one.return_value = [0.1, 0.2, 0.3]

    mock_models_service = AsyncMock(spec=ModelsService)
    mock_model = ModelInfo(
        # Canonical default (nomic): index_message resolves the no-preference
        # auto-pick to the default embedder, so the loaded model must BE it.
        key="text-embedding-nomic-embed-text-v1.5",
        type="embedding",
        capabilities=Capabilities(vision=False, trained_for_tool_use=False),
        # Genuinely loaded — resolver filters on loaded_instance_ids.
        loaded_instance_ids=["text-embedding-nomic-embed-text-v1.5@q8_0"],
    )
    mock_models_service.list_loaded.return_value = [mock_model]

    return MemoryService(
        engine=eng,
        embedding_client=mock_embedding_client,
        models_service=mock_models_service,
    )


async def _seed_user(eng: AsyncEngine, user_id: int = 1) -> None:
    async with eng.begin() as conn:
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO users (id, username, password_hash) "
                "VALUES (:id, :u, :ph)"
            ),
            {"id": user_id, "u": f"u{user_id}", "ph": "scrypt$dummy"},
        )


async def _seed_chat(
    eng: AsyncEngine,
    *,
    chat_id: int,
    user_id: int = 1,
    incognito: bool = False,
) -> None:
    async with eng.begin() as conn:
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO chats (id, user_id, title, incognito) "
                "VALUES (:id, :uid, 'x', :inc)"
            ),
            {"id": chat_id, "uid": user_id, "inc": 1 if incognito else 0},
        )


async def _seed_messages(
    eng: AsyncEngine, *, chat_id: int, count: int, start_id: int = 1
) -> list[int]:
    """Insert *count* user messages and return their IDs."""
    ids: list[int] = []
    for i in range(count):
        mid = start_id + i
        async with eng.begin() as conn:
            await conn.execute(
                text(
                    "INSERT OR IGNORE INTO messages (id, chat_id, role, content) "
                    "VALUES (:id, :cid, 'user', :c)"
                ),
                {"id": mid, "cid": chat_id, "c": f"distill-worthy fact #{i}"},
            )
        ids.append(mid)
    return ids


# ---------------------------------------------------------------------------
# THE invariant test (P13i scope §3 + REPORT gates)
# ---------------------------------------------------------------------------


async def test_incognito_chat_writes_zero_memory_rows(engine: AsyncEngine) -> None:
    """Sending 10 messages in an incognito chat must NOT add memory rows.

    Asserts that:
    - ``message_embeddings`` count delta = 0
    - ``memory_insights`` count delta = 0
    """
    await _seed_user(engine)
    await _seed_chat(engine, chat_id=42, incognito=True)
    msg_ids = await _seed_messages(engine, chat_id=42, count=10)
    assert len(msg_ids) == 10

    svc = _make_memory_service(engine)

    # Baseline counts.
    async with engine.connect() as conn:
        emb_before = (
            await conn.execute(select(func.count()).select_from(message_embeddings))
        ).scalar_one()
        insights_before = (
            await conn.execute(select(func.count()).select_from(memory_insights))
        ).scalar_one()

    # Even messages that "look distill-worthy" must not produce any rows.
    for mid in msg_ids:
        await svc.index_message(mid)

    async with engine.connect() as conn:
        emb_after = (
            await conn.execute(select(func.count()).select_from(message_embeddings))
        ).scalar_one()
        insights_after = (
            await conn.execute(select(func.count()).select_from(memory_insights))
        ).scalar_one()

    assert emb_after == emb_before, (
        f"PRIVACY INVARIANT VIOLATED: message_embeddings grew by "
        f"{emb_after - emb_before} for an incognito chat"
    )
    assert insights_after == insights_before, (
        f"PRIVACY INVARIANT VIOLATED: memory_insights grew by "
        f"{insights_after - insights_before} for an incognito chat"
    )


async def test_non_incognito_chat_does_write_embedding_rows(engine: AsyncEngine) -> None:
    """Sanity check: a NON-incognito chat still writes embeddings.

    Guards against the off-by-one failure where the incognito guard
    short-circuits regardless of the flag value.
    """
    await _seed_user(engine)
    await _seed_chat(engine, chat_id=7, incognito=False)
    msg_ids = await _seed_messages(engine, chat_id=7, count=3, start_id=100)

    svc = _make_memory_service(engine)

    for mid in msg_ids:
        await svc.index_message(mid)

    async with engine.connect() as conn:
        emb_count = (
            await conn.execute(select(func.count()).select_from(message_embeddings))
        ).scalar_one()
    # 3 messages → 3 embedding rows.
    assert emb_count == 3



# ---------------------------------------------------------------------------
# CO-001 — reindex() incognito guard
# ---------------------------------------------------------------------------


async def test_reindex_does_not_write_incognito_chat_embeddings(
    engine: AsyncEngine,
) -> None:
    """CO-001 PRIVACY INVARIANT: MemoryService.reindex() MUST NOT write
    ``message_embeddings`` rows for incognito chats.

    The guarded ``index_message`` short-circuits per-message; the bulk
    reindex is a separate write path that must mirror the same guarantee.
    Before the CO-001 fix, ``reindex()`` selected messages without
    joining against ``chats`` — so an admin POST /api/memory/reindex
    would persist incognito chat embeddings.

    Setup:
    - 10 messages in an incognito chat (no prior embeddings).
    - Reindex pass with the test embedding model.
    Assert: ``message_embeddings`` rows for those messages = 0.
    """
    await _seed_user(engine)
    await _seed_chat(engine, chat_id=99, incognito=True)
    msg_ids = await _seed_messages(engine, chat_id=99, count=10, start_id=500)
    assert len(msg_ids) == 10

    svc = _make_memory_service(engine)
    # Configure embed_batch to return 10 deterministic vectors — these
    # would be written if the guard fails.
    svc._embedding_client.embed_batch.return_value = [  # type: ignore[attr-defined]
        [0.1, 0.2, 0.3] for _ in range(10)
    ]

    await svc.reindex(embedding_model_id="text-embed-ada-002")

    async with engine.connect() as conn:
        emb_count = (
            await conn.execute(
                select(func.count())
                .select_from(message_embeddings)
                .where(message_embeddings.c.message_id.in_(msg_ids))
            )
        ).scalar_one()

    assert emb_count == 0, (
        f"PRIVACY INVARIANT VIOLATED: reindex() wrote {emb_count} "
        f"message_embeddings rows for an incognito chat (must be 0)."
    )


async def test_reindex_does_write_non_incognito_chat_embeddings(
    engine: AsyncEngine,
) -> None:
    """Companion sanity check for CO-001: a NON-incognito chat's messages
    DO get embedded by reindex(). Guards against the off-by-one failure
    where the incognito filter short-circuits everything.
    """
    await _seed_user(engine)
    await _seed_chat(engine, chat_id=101, incognito=False)
    msg_ids = await _seed_messages(engine, chat_id=101, count=3, start_id=800)

    svc = _make_memory_service(engine)
    svc._embedding_client.embed_batch.return_value = [  # type: ignore[attr-defined]
        [0.1, 0.2, 0.3] for _ in range(3)
    ]

    await svc.reindex(embedding_model_id="text-embed-ada-002")

    async with engine.connect() as conn:
        emb_count = (
            await conn.execute(
                select(func.count())
                .select_from(message_embeddings)
                .where(message_embeddings.c.message_id.in_(msg_ids))
            )
        ).scalar_one()

    assert emb_count == 3, (
        f"reindex() should embed all 3 non-incognito messages; got {emb_count}"
    )
