# SPDX-License-Identifier: Apache-2.0
"""Tests for MemoryService.reindex().

Tests for memory re-indexing.

Covers:
- reindex emits memory.reindex.start / batch / complete with contract fields.
- reindex deletes stale rows (different model_id) and inserts new ones.
- reindex increments MEMORY_REINDEXED counter per processed message.
- reindex skips a failed batch, records its count, and continues next batch.
"""
from __future__ import annotations

import struct
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import message_embeddings, metadata
from lmchat.embedding.client import EmbeddingClient
from lmchat.embedding.errors import EmbeddingError
from lmchat.services.memory_service import MemoryService
from lmchat.services.models_service import Capabilities, ModelInfo, ModelsService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    """Yield a fresh per-test SQLite engine with the schema."""
    db_path = tmp_path / "test_memory_reindex.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}", pool_pre_ping=True)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


async def _insert_user_chat(engine: AsyncEngine) -> None:
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


async def _insert_message(engine: AsyncEngine, msg_id: int, content: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO messages (id, chat_id, role, content)"
                " VALUES (:id, 1, 'user', :c)"
            ),
            {"id": msg_id, "c": content},
        )


async def _insert_embedding(
    engine: AsyncEngine, msg_id: int, model_id: str, vec: list[float]
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO message_embeddings"
                " (message_id, embedding_model_id, embedding, text_hash)"
                " VALUES (:mid, :model, :emb, :th)"
            ),
            {
                "mid": msg_id,
                "model": model_id,
                "emb": _pack(vec),
                "th": "a" * 64,
            },
        )


def _make_service(
    engine: AsyncEngine,
    *,
    embed_batch_return: list[list[float]] | None = None,
    embed_batch_side_effect: Any = None,
    model_id: str = "new-embed-model",
) -> tuple[MemoryService, AsyncMock]:
    mock_embedding_client = AsyncMock(spec=EmbeddingClient)
    if embed_batch_side_effect is not None:
        mock_embedding_client.embed_batch.side_effect = embed_batch_side_effect
    elif embed_batch_return is not None:
        mock_embedding_client.embed_batch.return_value = embed_batch_return

    mock_models_service = AsyncMock(spec=ModelsService)
    mock_model = ModelInfo(
        key=model_id,
        type="embedding",
        capabilities=Capabilities(vision=False, trained_for_tool_use=False),
    )
    mock_models_service.list_loaded.return_value = [mock_model]

    svc = MemoryService(
        engine=engine,
        embedding_client=mock_embedding_client,
        models_service=mock_models_service,
    )
    return svc, mock_embedding_client


# ---------------------------------------------------------------------------
# Event-capture helper
# ---------------------------------------------------------------------------


class _EventCapture:
    """Captures structlog info() calls by replacing the module-level log."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def info(self, event: str, **kwargs: Any) -> None:
        self.events.append({"event": event, **kwargs})

    def warning(self, event: str, **kwargs: Any) -> None:
        self.events.append({"event": event, "_level": "warning", **kwargs})

    def error(self, event: str, **kwargs: Any) -> None:
        self.events.append({"event": event, "_level": "error", **kwargs})

    def get(self, event_name: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["event"] == event_name]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_reindex_emits_start_batch_complete_events_with_contract_fields(
    engine: AsyncEngine,
) -> None:
    """reindex emits start/batch/complete with the exactly-specified contract fields."""
    await _insert_user_chat(engine)
    await _insert_message(engine, 1, "msg one")
    await _insert_message(engine, 2, "msg two")

    svc, _ = _make_service(
        engine,
        embed_batch_return=[[0.1, 0.2], [0.3, 0.4]],
        model_id="new-embed-model",
    )

    capture = _EventCapture()

    import lmchat.services.memory_service as _mod

    original_log = _mod.log
    _mod.log = capture  # type: ignore[assignment]
    try:
        result = await svc.reindex(embedding_model_id="new-embed-model")
    finally:
        _mod.log = original_log

    # Check start event.
    start_events = capture.get("memory.reindex.start")
    assert len(start_events) == 1
    start = start_events[0]
    assert "embedding_model_id" in start
    assert "total_messages" in start
    assert start["embedding_model_id"] == "new-embed-model"

    # Check batch event.
    batch_events = capture.get("memory.reindex.batch")
    assert len(batch_events) >= 1
    batch = batch_events[0]
    for field in ("embedding_model_id", "processed", "total", "batch_size", "elapsed_s", "failed"):
        assert field in batch, f"batch event missing field: {field}"

    # Check complete event.
    complete_events = capture.get("memory.reindex.complete")
    assert len(complete_events) == 1
    complete = complete_events[0]
    for field in ("embedding_model_id", "processed", "total", "elapsed_s", "failed"):
        assert field in complete, f"complete event missing field: {field}"
    assert complete["embedding_model_id"] == "new-embed-model"

    # Return value also carries the contract fields.
    assert result["embedding_model_id"] == "new-embed-model"
    assert "processed" in result
    assert "total" in result
    assert "elapsed_s" in result
    assert "failed" in result


async def test_reindex_deletes_stale_rows_and_inserts_new(engine: AsyncEngine) -> None:
    """reindex deletes rows with old model_id and inserts fresh rows for new model_id."""
    await _insert_user_chat(engine)
    await _insert_message(engine, 1, "hello")
    await _insert_message(engine, 2, "world")

    # Insert stale embeddings under the old model.
    await _insert_embedding(engine, 1, "old-model", [0.5, 0.5])
    await _insert_embedding(engine, 2, "old-model", [0.6, 0.4])

    svc, _ = _make_service(
        engine,
        embed_batch_return=[[0.1, 0.2], [0.3, 0.4]],
        model_id="new-model",
    )

    await svc.reindex(embedding_model_id="new-model")

    # Stale rows (old-model) should be gone.
    async with engine.connect() as conn:
        old_rows = (
            await conn.execute(
                select(message_embeddings).where(
                    message_embeddings.c.embedding_model_id == "old-model"
                )
            )
        ).fetchall()
        new_rows = (
            await conn.execute(
                select(message_embeddings).where(
                    message_embeddings.c.embedding_model_id == "new-model"
                )
            )
        ).fetchall()

    assert old_rows == []
    assert len(new_rows) == 2


async def test_reindex_persists_target_model_as_preferred(
    engine: AsyncEngine,
) -> None:
    """reindex records the target model as the preferred
    embedding model so subsequent index/recall resolve to the SAME model the
    corpus was just re-embedded under (no mid-corpus dimension drift).
    """
    from lmchat.db.schema import server_lm_studio_default

    await _insert_user_chat(engine)
    await _insert_message(engine, 1, "hello")
    await _insert_embedding(engine, 1, "old-model", [0.5, 0.5])

    svc, _ = _make_service(
        engine,
        embed_batch_return=[[0.1, 0.2]],
        model_id="new-model",
    )

    await svc.reindex(embedding_model_id="new-model")

    async with engine.connect() as conn:
        stored = (
            await conn.execute(
                select(
                    server_lm_studio_default.c.preferred_embedding_model_id
                ).where(server_lm_studio_default.c.id == 1)
            )
        ).scalar()

    assert stored == "new-model"


async def test_reindex_increments_memory_reindexed_counter(engine: AsyncEngine) -> None:
    """reindex increments MEMORY_REINDEXED by the number of successfully processed messages."""
    await _insert_user_chat(engine)
    await _insert_message(engine, 1, "msg a")
    await _insert_message(engine, 2, "msg b")
    await _insert_message(engine, 3, "msg c")

    svc, _ = _make_service(
        engine,
        embed_batch_return=[[0.1], [0.2], [0.3]],
        model_id="embed-v2",
    )

    # Snapshot counter before.
    # prometheus_client exposes Counter samples with the suffix "_total" on the
    # sample name, while metric.name omits the "_total" suffix internally.
    def _counter_value() -> float:
        for metric in REGISTRY.collect():
            for sample in metric.samples:
                if sample.name == "lm_chat_memory_reindexed_total":
                    return sample.value
        return 0.0

    before = _counter_value()

    await svc.reindex(embedding_model_id="embed-v2")

    after = _counter_value()
    assert after - before == pytest.approx(3.0)


async def test_reindex_skips_failed_batch_records_count(engine: AsyncEngine) -> None:
    """Skipped batch is counted as failed; next batch still processes."""
    await _insert_user_chat(engine)
    # Insert 2 messages; we'll make the first batch fail, second succeed.
    # With batch_size=50, both fit in one batch — we need to use a patched batch size.
    # Instead: insert 3 messages, side_effect = [EmbeddingError, [[...], [...], [...]]]
    # But with batch_size=50 it's all in one batch.
    # Patch _REINDEX_BATCH_SIZE to 1 for this test via monkeypatching the module constant.
    import lmchat.services.memory_service as _mod

    original_batch_size = _mod._REINDEX_BATCH_SIZE
    _mod._REINDEX_BATCH_SIZE = 1  # type: ignore[assignment]

    try:
        await _insert_message(engine, 1, "msg 1")
        await _insert_message(engine, 2, "msg 2")

        call_count = 0

        async def _side_effect(*, texts: list[str], model_id: str) -> list[list[float]]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # call 1 = the data-loss precondition probe — must succeed so the
                # reindex proceeds to the real batch that fails below.
                return [[0.0]]
            if call_count == 2:
                raise EmbeddingError("upstream down")
            return [[0.1] * len(texts[0].split())]

        svc, _ = _make_service(engine, embed_batch_side_effect=_side_effect, model_id="m")

        capture = _EventCapture()
        original_log = _mod.log
        _mod.log = capture  # type: ignore[assignment]
        try:
            result = await svc.reindex(embedding_model_id="m")
        finally:
            _mod.log = original_log

    finally:
        _mod._REINDEX_BATCH_SIZE = original_batch_size  # type: ignore[assignment]

    # failed count = 1 (the skipped batch had 1 message)
    assert result["failed"] == 1
    # processed = 1 (the second batch succeeded)
    assert result["processed"] == 1

    # Verify the complete event carries the right fields.
    complete = capture.get("memory.reindex.complete")
    assert len(complete) == 1
    assert complete[0]["failed"] == 1
    assert complete[0]["processed"] == 1


async def test_reindex_unloaded_target_does_not_delete_corpus(engine: AsyncEngine) -> None:
    """DATA-LOSS regression: reindexing to a model that can't
    embed must NOT delete the existing corpus.

    Before the fix, `_delete_stale` ran unconditionally BEFORE any embedding, so
    an unloaded/wrong-dimension target deleted every vector and then failed every
    re-embed batch — destroying all memory. The precondition probe now fails
    first and reindex raises BEFORE any delete, leaving the corpus intact.
    """
    await _insert_user_chat(engine)
    await _insert_message(engine, 1, "keep me")
    await _insert_message(engine, 2, "and me")
    await _insert_embedding(engine, 1, "old-model", [0.5, 0.5])
    await _insert_embedding(engine, 2, "old-model", [0.6, 0.4])

    # Target model can't embed (unloaded / wrong dim) — every call raises.
    svc, _ = _make_service(
        engine,
        embed_batch_side_effect=EmbeddingError("model not loaded"),
        model_id="dead-model",
    )

    with pytest.raises(EmbeddingError):
        await svc.reindex(embedding_model_id="dead-model")

    # The corpus is UNTOUCHED — the old-model rows are still present.
    async with engine.connect() as conn:
        rows = (await conn.execute(select(message_embeddings))).fetchall()
    assert len(rows) == 2
    assert {r.embedding_model_id for r in rows} == {"old-model"}


async def test_reindex_does_not_pin_preference_when_nothing_processed(
    engine: AsyncEngine,
) -> None:
    """DATA-LOSS regression: if the probe passes but every real batch then fails
    (model died mid-run), processed==0 and the preference must NOT be pinned to
    the failing target — otherwise future index/recall would aim at a dead model.
    """
    from lmchat.db.schema import server_lm_studio_default

    await _insert_user_chat(engine)
    await _insert_message(engine, 1, "msg")

    call_count = 0

    async def _side_effect(*, texts: list[str], model_id: str) -> list[list[float]]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [[0.0]]  # probe passes
        raise EmbeddingError("model unloaded mid-run")  # every real batch fails

    svc, _ = _make_service(engine, embed_batch_side_effect=_side_effect, model_id="target")

    result = await svc.reindex(embedding_model_id="target")

    assert result["processed"] == 0
    assert result["total"] == 1

    async with engine.connect() as conn:
        stored = (
            await conn.execute(
                select(server_lm_studio_default.c.preferred_embedding_model_id).where(
                    server_lm_studio_default.c.id == 1
                )
            )
        ).scalar()
    # Preference was NOT pinned to the failing target.
    assert stored != "target"


async def test_reindex_never_leaves_message_with_zero_rows_on_partial_failure(
    engine: AsyncEngine,
) -> None:
    """Data-loss-WINDOW regression (a follow-up to the fix above).

    That earlier fix closed the catastrophic case (probe fails -> corpus
    untouched). The residual risk it left open: the delete of stale rows
    happened ALL-UP-FRONT for the WHOLE corpus, before any re-embedding.
    If the embedding model died PARTWAY through the batch loop (i.e. AFTER
    the precondition probe passed), messages in a batch that hadn't run yet
    were already stripped of their old row and would gain no new one --
    left with ZERO rows in ``message_embeddings`` until a future reindex
    happened to succeed. That's a silent recall-miss window, not a
    corpus wipe, but it's still data loss.

    The fix makes the swap atomic PER MESSAGE: a message's stale row is
    deleted only inside the same transaction that inserts its replacement,
    and only once ITS OWN batch has actually embedded successfully. This
    test kills the embedding client partway through (batch 1 of 2
    succeeds, batch 2 fails) and asserts every message ends up with
    EXACTLY one row -- either its old vector (batch never ran) or its new
    one (batch succeeded) -- never neither.
    """
    import lmchat.services.memory_service as _mod

    original_batch_size = _mod._REINDEX_BATCH_SIZE
    _mod._REINDEX_BATCH_SIZE = 2  # type: ignore[assignment]  # 2 batches for 4 msgs

    try:
        await _insert_user_chat(engine)
        for msg_id, content in ((1, "aaa"), (2, "bbb"), (3, "ccc"), (4, "ddd")):
            await _insert_message(engine, msg_id, content)
            await _insert_embedding(engine, msg_id, "old-model", [0.1, 0.1])

        call_count = 0

        async def _side_effect(*, texts: list[str], model_id: str) -> list[list[float]]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Precondition probe -- must succeed so reindex proceeds.
                return [[0.0]]
            if call_count == 2:
                # First real batch (messages 1, 2) succeeds.
                return [[0.5, 0.5] for _ in texts]
            # Second real batch (messages 3, 4): model dies mid-run.
            raise EmbeddingError("model unloaded mid-run")

        svc, _ = _make_service(
            engine, embed_batch_side_effect=_side_effect, model_id="new-model"
        )

        result = await svc.reindex(embedding_model_id="new-model")

        assert result["processed"] == 2
        assert result["failed"] == 2

        async with engine.connect() as conn:
            rows = (await conn.execute(select(message_embeddings))).fetchall()

        rows_by_id = {r.message_id: r for r in rows}

        # THE INVARIANT: every message still has EXACTLY one row -- never
        # zero. If this fails, a message was left with its stale row
        # deleted but no replacement written.
        assert set(rows_by_id) == {1, 2, 3, 4}, (
            "a message was left with NO embedding row after a mid-run "
            "reindex failure -- the stale-delete and the re-insert were "
            "not atomic per message"
        )

        # Messages 1 & 2 (successful batch) were swapped to the new model.
        assert rows_by_id[1].embedding_model_id == "new-model"
        assert rows_by_id[2].embedding_model_id == "new-model"

        # Messages 3 & 4 (failed batch) kept their OLD, stale-but-present
        # vector -- untouched, since their batch never embedded.
        assert rows_by_id[3].embedding_model_id == "old-model"
        assert rows_by_id[4].embedding_model_id == "old-model"
    finally:
        _mod._REINDEX_BATCH_SIZE = original_batch_size  # type: ignore[assignment]
