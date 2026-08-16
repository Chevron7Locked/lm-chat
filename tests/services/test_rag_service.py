# SPDX-License-Identifier: Apache-2.0
"""Unit tests for rag_service.augment_prompt.

Verifies:
  (a) rag_enabled=False  → empty AugmentedPrompt, no retrieval calls.
  (b) memory recall succeeds + doc retrieval succeeds → context_block has
      both ## Memory and ## Documents sections.
  (c) memory recall raises + doc retrieval succeeds → context_block has
      only ## Documents section; no exception propagates.
  (d) memory recall succeeds + doc retrieval raises → context_block has
      only ## Memory section; no exception propagates.
  (e) both fail → empty AugmentedPrompt, no exception propagates.
  (f) chat_id not found → empty AugmentedPrompt immediately.
  (g) context block format — ## Retrieved context header, separator ---.
  (h) top_k forwarded to both retrieval surfaces.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import document_chunks, metadata
from lmchat.services.rag_mode_resolver import (
    resolve_rag_mode as _real_resolve_rag_mode,
)
from lmchat.services.rag_service import AugmentedPrompt, augment_prompt

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def engine(tmp_path: Path):  # type: ignore[return]
    """Yield a fresh in-process SQLite engine with the full schema."""
    db_path = tmp_path / "test_rag_service.db"
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


async def _insert_chat(
    engine: AsyncEngine,
    chat_id: int,
    user_id: int,
    *,
    rag_enabled: bool | None = True,
) -> None:
    # rag_enabled=None → no explicit setting (exercises the smart default).
    if rag_enabled is None:
        settings_json = "{}"
    else:
        settings_json = (
            '{"rag_enabled": true}' if rag_enabled else '{"rag_enabled": false}'
        )
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO chats (id, user_id, title, settings)"
                " VALUES (:id, :uid, :t, :s)"
            ),
            {"id": chat_id, "uid": user_id, "t": "Test chat", "s": settings_json},
        )


async def _insert_document(
    engine: AsyncEngine,
    user_id: int,
    doc_id: int,
    *,
    project_id: int | None = None,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO documents (id, user_id, title, mime_type, byte_size,"
                " chunk_count, embedding_model_id, sha256, project_id)"
                " VALUES (:id, :uid, 'd.txt', 'text/plain', 10, 1, 'nomic-embed',"
                " :sha, :pid)"
            ),
            {"id": doc_id, "uid": user_id, "sha": f"sha_{doc_id}", "pid": project_id},
        )


def _pack(vec: list[float]) -> bytes:
    n = len(vec)
    return struct.pack(f"<{n}f", *vec)


async def _insert_chunk(
    engine: AsyncEngine,
    *,
    document_id: int,
    text_: str,
    ordinal: int = 0,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            insert(document_chunks).values(
                document_id=document_id,
                ordinal=ordinal,
                text=text_,
                text_hash=f"h-{document_id}-{ordinal}",
                embedding=_pack([1.0, 0.0]),
            )
        )


def _make_memory_service(
    *,
    recalled: list | None = None,
    raise_exc: Exception | None = None,
) -> MagicMock:
    svc = MagicMock()
    if raise_exc is not None:
        svc.recall = AsyncMock(side_effect=raise_exc)
    else:
        hits = recalled or []
        svc.recall = AsyncMock(return_value=hits)
    # recall_insights is unconditional (ungated from rag_enabled); default
    # to returning an empty list so test mocks don't fail on await.
    svc.recall_insights = AsyncMock(return_value=[])
    # list_pinned is also always called; default to empty.
    svc.list_pinned = AsyncMock(return_value=[])
    return svc


def _make_embedding_client() -> MagicMock:
    client = MagicMock()
    client.embed_one = AsyncMock(return_value=[0.1, 0.2])
    client.embed_batch = AsyncMock(return_value=[[0.1, 0.2]])
    return client


def _make_models_service() -> MagicMock:
    svc = MagicMock()
    svc.list_loaded = AsyncMock(return_value=[])
    return svc


def _recalled_hit(
    message_id: int = 1,
    chat_id: int = 1,
    content: str = "past message text",
) -> object:
    from datetime import UTC, datetime

    from lmchat.services.memory_service import RecalledMessage
    return RecalledMessage(
        message_id=message_id,
        chat_id=chat_id,
        role="user",
        content=content,
        similarity=0.87,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


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
# (a) rag_enabled=False
# ---------------------------------------------------------------------------


async def test_rag_disabled_returns_empty_no_calls(engine: AsyncEngine) -> None:
    """rag_enabled=False returns empty AugmentedPrompt; neither retrieval is called."""
    await _insert_user(engine, 1)
    await _insert_chat(engine, 1, 1, rag_enabled=False)

    memory_svc = _make_memory_service()
    embedding_client = _make_embedding_client()
    models_svc = _make_models_service()

    result = await augment_prompt(
        chat_id=1,
        user_id=1,
        current_message="hello",
        engine=engine,
        embedding_client=embedding_client,
        models_service=models_svc,
        memory_service=memory_svc,
    )

    assert result == AugmentedPrompt(context_block="", memory_hits=0, doc_hits=0)
    memory_svc.recall.assert_not_called()


# ---------------------------------------------------------------------------
# (b) Both recall surfaces succeed
# ---------------------------------------------------------------------------


async def test_both_surfaces_succeed_produces_full_context(engine: AsyncEngine) -> None:
    """When both recall and retrieve succeed, the context block has both sections."""
    await _insert_user(engine, 1)
    await _insert_chat(engine, 1, 1, rag_enabled=True)

    memory_svc = _make_memory_service(recalled=[_recalled_hit()])
    embedding_client = _make_embedding_client()
    models_svc = _make_models_service()

    with patch(
        "lmchat.services.rag_service.retrieve",
        new_callable=AsyncMock,
        return_value=[_chunk_hit()],
    ):
        result = await augment_prompt(
            chat_id=1,
            user_id=1,
            current_message="test query",
            engine=engine,
            embedding_client=embedding_client,
            models_service=models_svc,
            memory_service=memory_svc,
        )

    assert result.memory_hits == 1
    assert result.doc_hits == 1
    assert "## Retrieved context" in result.context_block
    assert "### Memory (past conversations)" in result.context_block
    assert "### Documents" in result.context_block
    assert "---" in result.context_block


# ---------------------------------------------------------------------------
# (c) Memory recall raises; doc retrieval succeeds
# ---------------------------------------------------------------------------


async def test_memory_fails_docs_succeed_returns_doc_context(engine: AsyncEngine) -> None:
    """If memory recall raises, the error is swallowed and docs section still appears."""
    await _insert_user(engine, 1)
    await _insert_chat(engine, 1, 1, rag_enabled=True)

    memory_svc = _make_memory_service(raise_exc=RuntimeError("embedding model offline"))
    embedding_client = _make_embedding_client()
    models_svc = _make_models_service()

    with patch(
        "lmchat.services.rag_service.retrieve",
        new_callable=AsyncMock,
        return_value=[_chunk_hit()],
    ):
        result = await augment_prompt(
            chat_id=1,
            user_id=1,
            current_message="test query",
            engine=engine,
            embedding_client=embedding_client,
            models_service=models_svc,
            memory_service=memory_svc,
        )

    assert result.memory_hits == 0
    assert result.doc_hits == 1
    assert "### Documents" in result.context_block
    assert "### Memory" not in result.context_block
    assert result.degraded_surfaces == ["memory_recall"]


# ---------------------------------------------------------------------------
# (d) Memory recall succeeds; doc retrieval raises
# ---------------------------------------------------------------------------


async def test_memory_succeeds_docs_fail_returns_memory_context(engine: AsyncEngine) -> None:
    """If doc retrieval raises, the error is swallowed and memory section still appears."""
    await _insert_user(engine, 1)
    await _insert_chat(engine, 1, 1, rag_enabled=True)

    memory_svc = _make_memory_service(recalled=[_recalled_hit()])
    embedding_client = _make_embedding_client()
    models_svc = _make_models_service()

    with patch(
        "lmchat.services.rag_service.retrieve",
        new_callable=AsyncMock,
        side_effect=RuntimeError("no embedding model loaded"),
    ):
        result = await augment_prompt(
            chat_id=1,
            user_id=1,
            current_message="test query",
            engine=engine,
            embedding_client=embedding_client,
            models_service=models_svc,
            memory_service=memory_svc,
        )

    assert result.memory_hits == 1
    assert result.doc_hits == 0
    assert "### Memory (past conversations)" in result.context_block
    assert "### Documents" not in result.context_block
    assert result.degraded_surfaces == ["documents"]


# ---------------------------------------------------------------------------
# (e) Both fail → empty, no exception
# ---------------------------------------------------------------------------


async def test_both_fail_returns_empty_no_exception(engine: AsyncEngine) -> None:
    """If both retrieval surfaces raise, AugmentedPrompt is empty and no exception propagates."""
    await _insert_user(engine, 1)
    await _insert_chat(engine, 1, 1, rag_enabled=True)

    memory_svc = _make_memory_service(raise_exc=RuntimeError("memory down"))
    embedding_client = _make_embedding_client()
    models_svc = _make_models_service()

    with patch(
        "lmchat.services.rag_service.retrieve",
        new_callable=AsyncMock,
        side_effect=RuntimeError("retrieval down"),
    ):
        result = await augment_prompt(
            chat_id=1,
            user_id=1,
            current_message="test query",
            engine=engine,
            embedding_client=embedding_client,
            models_service=models_svc,
            memory_service=memory_svc,
        )

    assert result.context_block == ""
    assert result.memory_hits == 0
    assert result.doc_hits == 0
    assert result.pinned_hits == 0
    # Both surfaces raised — the swallow still holds (no exception
    # propagated), but degraded_surfaces distinguishes this from a
    # genuinely empty corpus.
    assert sorted(result.degraded_surfaces) == ["documents", "memory_recall"]


# ---------------------------------------------------------------------------
# (f) chat_id not found → empty immediately
# ---------------------------------------------------------------------------


async def test_chat_not_found_returns_empty(engine: AsyncEngine) -> None:
    """Unknown chat_id returns empty AugmentedPrompt without calling retrieval."""
    memory_svc = _make_memory_service()
    embedding_client = _make_embedding_client()
    models_svc = _make_models_service()

    result = await augment_prompt(
        chat_id=999,
        user_id=1,
        current_message="anything",
        engine=engine,
        embedding_client=embedding_client,
        models_service=models_svc,
        memory_service=memory_svc,
    )

    assert result == AugmentedPrompt(context_block="", memory_hits=0, doc_hits=0)
    memory_svc.recall.assert_not_called()


# ---------------------------------------------------------------------------
# (g) Context block format
# ---------------------------------------------------------------------------


async def test_context_block_format(engine: AsyncEngine) -> None:
    """Context block starts with '## Retrieved context' and ends with '---'."""
    await _insert_user(engine, 1)
    await _insert_chat(engine, 1, 1, rag_enabled=True)

    memory_svc = _make_memory_service(recalled=[_recalled_hit(content="hello from memory")])
    embedding_client = _make_embedding_client()
    models_svc = _make_models_service()

    with patch(
        "lmchat.services.rag_service.retrieve",
        new_callable=AsyncMock,
        return_value=[_chunk_hit(content="hello from docs", title="notes.txt")],
    ):
        result = await augment_prompt(
            chat_id=1,
            user_id=1,
            current_message="query",
            engine=engine,
            embedding_client=embedding_client,
            models_service=models_svc,
            memory_service=memory_svc,
        )

    lines = result.context_block.splitlines()
    assert lines[0] == "## Retrieved context"
    assert lines[-1] == "---"
    assert "hello from memory" in result.context_block
    assert "hello from docs" in result.context_block
    # Source lines.
    assert "source: message:1 | chat:1" in result.context_block
    assert "source: doc:10 | chunk:0 | title: notes.txt" in result.context_block


# ---------------------------------------------------------------------------
# (h) Both surfaces return empty results → empty context block
# ---------------------------------------------------------------------------


async def test_both_surfaces_empty_returns_empty(engine: AsyncEngine) -> None:
    """Empty results from both surfaces produce empty context block without header."""
    await _insert_user(engine, 1)
    await _insert_chat(engine, 1, 1, rag_enabled=True)

    memory_svc = _make_memory_service(recalled=[])
    embedding_client = _make_embedding_client()
    models_svc = _make_models_service()

    with patch(
        "lmchat.services.rag_service.retrieve",
        new_callable=AsyncMock,
        return_value=[],
    ):
        result = await augment_prompt(
            chat_id=1,
            user_id=1,
            current_message="query",
            engine=engine,
            embedding_client=embedding_client,
            models_service=models_svc,
            memory_service=memory_svc,
        )

    assert result == AugmentedPrompt(context_block="", memory_hits=0, doc_hits=0)
    assert result.degraded_surfaces == []


# ---------------------------------------------------------------------------
# (h2) Empty corpus (no exception) must NOT be flagged as degraded — the
# empty-vs-degraded distinction: a failed surface and an empty corpus both
# produce empty *_sections, but only the former sets degraded_surfaces.
# ---------------------------------------------------------------------------


async def test_empty_corpus_yields_no_degraded_surfaces(engine: AsyncEngine) -> None:
    """Genuinely empty results (no exception raised) yield degraded_surfaces == []."""
    await _insert_user(engine, 1)
    await _insert_chat(engine, 1, 1, rag_enabled=True)

    memory_svc = _make_memory_service(recalled=[])
    embedding_client = _make_embedding_client()
    models_svc = _make_models_service()

    with patch(
        "lmchat.services.rag_service.retrieve",
        new_callable=AsyncMock,
        return_value=[],
    ):
        result = await augment_prompt(
            chat_id=1,
            user_id=1,
            current_message="query",
            engine=engine,
            embedding_client=embedding_client,
            models_service=models_svc,
            memory_service=memory_svc,
        )

    assert result.degraded_surfaces == []


# ---------------------------------------------------------------------------
# (i) top_k forwarded to memory_service.recall
# ---------------------------------------------------------------------------


async def test_top_k_forwarded_to_recall(engine: AsyncEngine) -> None:
    """top_k is forwarded correctly to memory_service.recall."""
    await _insert_user(engine, 1)
    await _insert_chat(engine, 1, 1, rag_enabled=True)

    memory_svc = _make_memory_service(recalled=[])
    embedding_client = _make_embedding_client()
    models_svc = _make_models_service()

    with patch(
        "lmchat.services.rag_service.retrieve",
        new_callable=AsyncMock,
        return_value=[],
    ):
        await augment_prompt(
            chat_id=1,
            user_id=1,
            current_message="hello",
            engine=engine,
            embedding_client=embedding_client,
            models_service=models_svc,
            memory_service=memory_svc,
            top_k=7,
        )

    # rag_service forwards chat.project_id (None for the
    # un-projected chat in this fixture) to recall as a keyword arg.
    memory_svc.recall.assert_called_once_with(
        user_id=1, query="hello", top_k=7, project_id=None
    )


# ---------------------------------------------------------------------------
# augment_prompt propagates chat.project_id
# ---------------------------------------------------------------------------


async def _insert_project(engine: AsyncEngine, project_id: int, user_id: int) -> None:
    import time as _time

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO projects (id, user_id, name, description, "
                "system_prompt, created_at, updated_at) "
                "VALUES (:id, :uid, 'P', '', '', :now, :now)"
            ),
            {"id": project_id, "uid": user_id, "now": _time.time()},
        )


async def _insert_chat_in_project(
    engine: AsyncEngine,
    chat_id: int,
    user_id: int,
    project_id: int,
    *,
    rag_enabled: bool = True,
    model_id: str | None = None,
) -> None:
    settings_json = '{"rag_enabled": true}' if rag_enabled else '{"rag_enabled": false}'
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO chats (id, user_id, title, "
                "settings, project_id, model_id) "
                "VALUES (:id, :uid, :t, :s, :pid, :mid)"
            ),
            {
                "id": chat_id,
                "uid": user_id,
                "t": "in-project chat",
                "s": settings_json,
                "pid": project_id,
                "mid": model_id,
            },
        )


async def test_augment_prompt_forwards_project_id_to_all_four_calls(
    engine: AsyncEngine,
) -> None:
    """When chat.project_id is set, augment_prompt passes it as a kwarg
    to list_pinned, recall, recall_insights, AND retrieve. All four
    chat-time retrieval surfaces must honor project scope or the
    in-project chat's RAG context would mix in out-of-project content.
    """
    await _insert_user(engine, 1)
    await _insert_project(engine, project_id=99, user_id=1)
    await _insert_chat_in_project(engine, 1, 1, project_id=99)

    memory_svc = _make_memory_service(recalled=[])
    memory_svc.list_pinned = AsyncMock(return_value=[])
    memory_svc.recall_insights = AsyncMock(return_value=[])
    embedding_client = _make_embedding_client()
    models_svc = _make_models_service()

    with patch(
        "lmchat.services.rag_service.retrieve",
        new_callable=AsyncMock,
        return_value=[],
    ) as mock_retrieve:
        await augment_prompt(
            chat_id=1,
            user_id=1,
            current_message="hello",
            engine=engine,
            embedding_client=embedding_client,
            models_service=models_svc,
            memory_service=memory_svc,
            top_k=7,
        )

    memory_svc.list_pinned.assert_called_once_with(
        user_id=1, project_id=99
    )
    memory_svc.recall.assert_called_once_with(
        user_id=1, query="hello", top_k=7, project_id=99
    )
    memory_svc.recall_insights.assert_called_once_with(
        user_id=1, top_k=7, project_id=99
    )
    _, kwargs = mock_retrieve.call_args
    assert kwargs.get("project_id") == 99


async def test_augment_prompt_reads_project_id_from_chat_row(
    engine: AsyncEngine,
) -> None:
    """End-to-end: augment_prompt reads
    chats.project_id from the DB (not from a kwarg) and forwards it.

    Two chats — one un-projected, one in project 77 — exercise both
    paths from the same engine fixture, proving the wire-up is real
    (not always-pass-None).
    """
    await _insert_user(engine, 1)
    await _insert_project(engine, project_id=77, user_id=1)
    await _insert_chat(engine, 100, 1, rag_enabled=True)
    await _insert_chat_in_project(
        engine, 200, 1, project_id=77, rag_enabled=True
    )

    memory_svc = _make_memory_service(recalled=[])
    memory_svc.list_pinned = AsyncMock(return_value=[])
    memory_svc.recall_insights = AsyncMock(return_value=[])

    with patch(
        "lmchat.services.rag_service.retrieve",
        new_callable=AsyncMock,
        return_value=[],
    ) as mock_retrieve:
        # Un-projected chat → every retrieval call receives project_id=None.
        await augment_prompt(
            chat_id=100,
            user_id=1,
            current_message="x",
            engine=engine,
            embedding_client=_make_embedding_client(),
            models_service=_make_models_service(),
            memory_service=memory_svc,
            top_k=5,
        )
        assert memory_svc.list_pinned.call_args.kwargs["project_id"] is None
        assert memory_svc.recall.call_args.kwargs["project_id"] is None
        assert memory_svc.recall_insights.call_args.kwargs["project_id"] is None
        assert mock_retrieve.call_args.kwargs["project_id"] is None

        memory_svc.list_pinned.reset_mock()
        memory_svc.recall.reset_mock()
        memory_svc.recall_insights.reset_mock()
        mock_retrieve.reset_mock()

        # In-project chat → every retrieval call receives 77.
        await augment_prompt(
            chat_id=200,
            user_id=1,
            current_message="x",
            engine=engine,
            embedding_client=_make_embedding_client(),
            models_service=_make_models_service(),
            memory_service=memory_svc,
            top_k=5,
        )
        assert memory_svc.list_pinned.call_args.kwargs["project_id"] == 77
        assert memory_svc.recall.call_args.kwargs["project_id"] == 77
        assert memory_svc.recall_insights.call_args.kwargs["project_id"] == 77
        assert mock_retrieve.call_args.kwargs["project_id"] == 77


async def test_augment_prompt_rejects_cross_user_chat(
    engine: AsyncEngine,
) -> None:
    """Defense-in-depth: the chat SELECT now
    gates on chats.user_id == user_id, so a buggy upstream caller
    passing alice's chat_id with bob's user_id gets the same
    "chat not found" path as a truly missing chat — no foreign
    project_id leak.
    """
    await _insert_user(engine, 1)  # alice
    await _insert_user(engine, 2)  # bob
    await _insert_project(engine, project_id=55, user_id=1)
    await _insert_chat_in_project(
        engine, 1, 1, project_id=55, rag_enabled=True
    )

    memory_svc = _make_memory_service(recalled=[])
    memory_svc.list_pinned = AsyncMock(return_value=[])
    memory_svc.recall_insights = AsyncMock(return_value=[])

    with patch(
        "lmchat.services.rag_service.retrieve",
        new_callable=AsyncMock,
        return_value=[],
    ) as mock_retrieve:
        # Bob asks for Alice's chat → empty AugmentedPrompt, no
        # downstream retrieval calls (chat_not_found short-circuit).
        result = await augment_prompt(
            chat_id=1,
            user_id=2,
            current_message="x",
            engine=engine,
            embedding_client=_make_embedding_client(),
            models_service=_make_models_service(),
            memory_service=memory_svc,
            top_k=5,
        )
        assert result.context_block == ""
        assert result.memory_hits == 0
        assert result.doc_hits == 0
        memory_svc.list_pinned.assert_not_called()
        memory_svc.recall.assert_not_called()
        memory_svc.recall_insights.assert_not_called()
        mock_retrieve.assert_not_called()


# ---------------------------------------------------------------------------
# Smart rag_enabled default: auto-enable when an embedding model is loaded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rag_smart_default_on_when_embedder_loaded(
    engine: AsyncEngine,
) -> None:
    """A chat with NO explicit rag_enabled defaults ON when an embedder is
    available — retrieval + memory recall run without a manual toggle."""
    await _insert_user(engine, 1)
    await _insert_chat(engine, 1, 1, rag_enabled=None)  # unset
    memory_svc = _make_memory_service(recalled=[])
    # Patch the resolver at its definition site (local import inside the
    # function body means the module-level patch path is memory_service).
    with patch(
        "lmchat.services.memory_service.resolve_active_embedding_model_key",
        new_callable=AsyncMock,
        return_value="text-embedding-nomic-embed-text-v1.5",
    ), patch(
        "lmchat.services.rag_service.retrieve",
        new_callable=AsyncMock,
        return_value=[],
    ) as mock_retrieve:
        await augment_prompt(
            chat_id=1,
            user_id=1,
            current_message="q",
            engine=engine,
            embedding_client=_make_embedding_client(),
            models_service=_make_models_service(),
            memory_service=memory_svc,
            top_k=5,
        )
        mock_retrieve.assert_called()
        memory_svc.recall.assert_called()


@pytest.mark.asyncio
async def test_rag_smart_default_off_when_no_embedder_loaded(
    engine: AsyncEngine,
) -> None:
    """A chat with NO explicit rag_enabled and NO embedder loaded stays OFF —
    no retrieval, no wasted per-turn query embedding."""
    await _insert_user(engine, 1)
    await _insert_chat(engine, 1, 1, rag_enabled=None)  # unset, no embedder
    memory_svc = _make_memory_service(recalled=[])
    from lmchat.services.memory_service import NoEmbeddingModelLoadedError

    # Patch the resolver at its definition site.
    with patch(
        "lmchat.services.memory_service.resolve_active_embedding_model_key",
        new_callable=AsyncMock,
        side_effect=NoEmbeddingModelLoadedError("no embedder"),
    ), patch(
        "lmchat.services.rag_service.retrieve",
        new_callable=AsyncMock,
        return_value=[],
    ) as mock_retrieve:
        await augment_prompt(
            chat_id=1,
            user_id=1,
            current_message="q",
            engine=engine,
            embedding_client=_make_embedding_client(),
            models_service=_make_models_service(),
            memory_service=memory_svc,
            top_k=5,
        )
        mock_retrieve.assert_not_called()
        memory_svc.recall.assert_not_called()


@pytest.mark.asyncio
async def test_rag_explicit_false_wins_over_embedder(
    engine: AsyncEngine,
) -> None:
    """An explicit rag_enabled=false always wins, even when an embedder is loaded."""
    await _insert_user(engine, 1)
    await _insert_chat(engine, 1, 1, rag_enabled=False)  # explicit off
    memory_svc = _make_memory_service(recalled=[])
    with patch(
        "lmchat.services.rag_service.retrieve",
        new_callable=AsyncMock,
        return_value=[],
    ) as mock_retrieve:
        await augment_prompt(
            chat_id=1,
            user_id=1,
            current_message="q",
            engine=engine,
            embedding_client=_make_embedding_client(),
            models_service=_make_models_service(),
            memory_service=memory_svc,
            top_k=5,
        )
        mock_retrieve.assert_not_called()
        memory_svc.recall.assert_not_called()


# ---------------------------------------------------------------------------
# recall_insights is unconditional (ungated from rag_enabled)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recall_insights_called_when_rag_disabled(engine: AsyncEngine) -> None:
    """recall_insights must be called even when rag_enabled=false.

    Auto-distilled insights are score-ranked and do not require an active
    embedder; they should reach the model regardless of the RAG toggle.
    """
    await _insert_user(engine, 1)
    await _insert_chat(engine, 1, 1, rag_enabled=False)
    memory_svc = _make_memory_service(recalled=[])

    with patch(
        "lmchat.services.rag_service.retrieve",
        new_callable=AsyncMock,
        return_value=[],
    ):
        await augment_prompt(
            chat_id=1,
            user_id=1,
            current_message="hello",
            engine=engine,
            embedding_client=_make_embedding_client(),
            models_service=_make_models_service(),
            memory_service=memory_svc,
        )

    # RAG is off → memory recall and retrieval must NOT run.
    memory_svc.recall.assert_not_called()
    # But recall_insights MUST run unconditionally.
    memory_svc.recall_insights.assert_called_once()


@pytest.mark.asyncio
async def test_recall_insights_called_when_rag_enabled(engine: AsyncEngine) -> None:
    """recall_insights is also called when rag_enabled=true (completeness check)."""
    await _insert_user(engine, 1)
    await _insert_chat(engine, 1, 1, rag_enabled=True)
    memory_svc = _make_memory_service(recalled=[])

    with patch(
        "lmchat.services.rag_service.retrieve",
        new_callable=AsyncMock,
        return_value=[],
    ):
        await augment_prompt(
            chat_id=1,
            user_id=1,
            current_message="hello",
            engine=engine,
            embedding_client=_make_embedding_client(),
            models_service=_make_models_service(),
            memory_service=memory_svc,
        )

    memory_svc.recall_insights.assert_called_once()


# ---------------------------------------------------------------------------
# projects.rag_threshold reaches resolve_rag_mode at inference time
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_augment_prompt_forwards_project_rag_threshold_override(
    engine: AsyncEngine,
) -> None:
    """A populated ``projects.rag_threshold`` must reach
    ``resolve_rag_mode(project_rag_threshold_override=...)``.

    Previously the column was writable-nowhere and read-nowhere at this
    call site — ``augment_prompt`` always passed the resolver's default
    (None → formula), so a per-project override could never change live
    RAG behavior. This guards the wire-up.
    """
    await _insert_user(engine, 1)
    await _insert_project(engine, project_id=42, user_id=1)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE projects SET rag_threshold = :t WHERE id = :id"),
            {"t": 500, "id": 42},
        )
    await _insert_chat_in_project(engine, 1, 1, project_id=42, rag_enabled=True)

    memory_svc = _make_memory_service(recalled=[])
    memory_svc.list_pinned = AsyncMock(return_value=[])
    memory_svc.recall_insights = AsyncMock(return_value=[])

    # Patch the resolver at its definition site — augment_prompt imports it
    # locally inside the function body, so a module-level patch on
    # rag_service wouldn't be seen (same pattern as the smart-default
    # rag_enabled tests above). `wraps=` keeps the real resolution logic
    # so `decision.mode` is still correctly computed.
    with patch(
        "lmchat.services.rag_service.retrieve",
        new_callable=AsyncMock,
        return_value=[],
    ), patch(
        "lmchat.services.rag_mode_resolver.resolve_rag_mode",
        wraps=_real_resolve_rag_mode,
    ) as mock_resolve:
        await augment_prompt(
            chat_id=1,
            user_id=1,
            current_message="hello",
            engine=engine,
            embedding_client=_make_embedding_client(),
            models_service=_make_models_service(),
            memory_service=memory_svc,
            top_k=5,
        )

    mock_resolve.assert_called_once()
    assert mock_resolve.call_args.kwargs["project_rag_threshold_override"] == 500


@pytest.mark.asyncio
async def test_augment_prompt_forwards_none_without_rag_threshold(
    engine: AsyncEngine,
) -> None:
    """No ``rag_threshold`` set on the project → override stays None
    (falls through to the formula, unchanged legacy behavior)."""
    await _insert_user(engine, 1)
    await _insert_project(engine, project_id=43, user_id=1)
    await _insert_chat_in_project(engine, 2, 1, project_id=43, rag_enabled=True)

    memory_svc = _make_memory_service(recalled=[])
    memory_svc.list_pinned = AsyncMock(return_value=[])
    memory_svc.recall_insights = AsyncMock(return_value=[])

    with patch(
        "lmchat.services.rag_service.retrieve",
        new_callable=AsyncMock,
        return_value=[],
    ), patch(
        "lmchat.services.rag_mode_resolver.resolve_rag_mode",
        wraps=_real_resolve_rag_mode,
    ) as mock_resolve:
        await augment_prompt(
            chat_id=2,
            user_id=1,
            current_message="hello",
            engine=engine,
            embedding_client=_make_embedding_client(),
            models_service=_make_models_service(),
            memory_service=memory_svc,
            top_k=5,
        )

    assert mock_resolve.call_args.kwargs["project_rag_threshold_override"] is None


# ---------------------------------------------------------------------------
# INLINE wire-up — augment_prompt
# actually produces INLINE-mode behavior for a small project corpus, and
# leaves the HYBRID retrieve() path unchanged for a large corpus. Before
# this fix, augment_prompt fed resolve_rag_mode a hardcoded
# project_corpus_tokens=None (and a hardcoded ctx_window=131_000), which
# structurally can never satisfy the resolver's INLINE condition — every
# project-scoped chat fell through to HYBRID regardless of corpus size.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_augment_prompt_inline_injects_ordered_project_corpus(
    engine: AsyncEngine,
) -> None:
    """A small project corpus + a real (known) ctx_window resolves to
    RagMode.INLINE and injects the ENTIRE project corpus — ordered by
    document then chunk ordinal — instead of calling retrieve().

    Against pre-fix code this fails, because retrieve()
    is unconditionally called (INLINE is structurally unreachable) and
    "| inline" never appears in the context block.
    """
    await _insert_user(engine, 1)
    await _insert_project(engine, project_id=61, user_id=1)
    await _insert_chat_in_project(
        engine, 1, 1, project_id=61, rag_enabled=True, model_id="local-model"
    )
    await _insert_document(engine, user_id=1, doc_id=201, project_id=61)
    await _insert_chunk(engine, document_id=201, text_="alpha chunk", ordinal=0)
    await _insert_chunk(engine, document_id=201, text_="beta chunk", ordinal=1)

    memory_svc = _make_memory_service(recalled=[])
    memory_svc.list_pinned = AsyncMock(return_value=[])
    memory_svc.recall_insights = AsyncMock(return_value=[])
    models_svc = _make_models_service()
    # A real, known context window — "alpha chunk" + "beta chunk" is a
    # handful of tokens, far under 100_000 * 0.061 (inline_fraction) ≈ 6100.
    models_svc.get_max_context_length = AsyncMock(return_value=100_000)

    with patch(
        "lmchat.services.rag_service.retrieve",
        new_callable=AsyncMock,
        return_value=[],
    ) as mock_retrieve:
        result = await augment_prompt(
            chat_id=1,
            user_id=1,
            current_message="what's in the corpus?",
            engine=engine,
            embedding_client=_make_embedding_client(),
            models_service=models_svc,
            memory_service=memory_svc,
            top_k=5,
        )

    mock_retrieve.assert_not_called()
    assert result.doc_hits == 2
    assert "alpha chunk" in result.context_block
    assert "beta chunk" in result.context_block
    assert "source: doc:201 | chunk:0 | inline" in result.context_block
    assert "source: doc:201 | chunk:1 | inline" in result.context_block
    assert result.degraded_surfaces == []


@pytest.mark.asyncio
async def test_augment_prompt_large_corpus_stays_hybrid_unchanged(
    engine: AsyncEngine,
) -> None:
    """A project corpus that EXCEEDS the resolved threshold — even with a
    known ctx_window — must still resolve to HYBRID and hit the existing
    retrieve() path unchanged. This is the behavior-preservation half of
    the INLINE fix: large corpora keep the legacy code path.
    """
    await _insert_user(engine, 1)
    await _insert_project(engine, project_id=62, user_id=1)
    await _insert_chat_in_project(
        engine, 2, 1, project_id=62, rag_enabled=True, model_id="local-model"
    )
    await _insert_document(engine, user_id=1, doc_id=202, project_id=62)
    # 8_000 * 0.061 (inline_fraction) ≈ 488 tokens ≈ 1_952 bytes threshold;
    # this chunk is ~5_000 bytes — comfortably over.
    await _insert_chunk(engine, document_id=202, text_="x" * 5_000, ordinal=0)

    memory_svc = _make_memory_service(recalled=[])
    memory_svc.list_pinned = AsyncMock(return_value=[])
    memory_svc.recall_insights = AsyncMock(return_value=[])
    models_svc = _make_models_service()
    models_svc.get_max_context_length = AsyncMock(return_value=8_000)

    with patch(
        "lmchat.services.rag_service.retrieve",
        new_callable=AsyncMock,
        return_value=[_chunk_hit(document_id=202, ordinal=0, content="hybrid hit")],
    ) as mock_retrieve:
        result = await augment_prompt(
            chat_id=2,
            user_id=1,
            current_message="what's in the corpus?",
            engine=engine,
            embedding_client=_make_embedding_client(),
            models_service=models_svc,
            memory_service=memory_svc,
            top_k=5,
        )

    mock_retrieve.assert_called_once()
    assert mock_retrieve.call_args.kwargs["project_id"] == 62
    assert result.doc_hits == 1
    assert "hybrid hit" in result.context_block
    assert "| inline" not in result.context_block


@pytest.mark.asyncio
async def test_augment_prompt_inline_injects_full_chunk_text_within_budget(
    engine: AsyncEngine,
) -> None:
    """INLINE must inject the FULL chunk text, not a 500-char head, while
    staying within the token budget ``rag_inject_budget()`` derives from
    the active model's context window.

    Against pre-fix code (``text[:500]``) this fails: a sentinel placed
    past char 500 of the (single, small) chunk never reaches the context
    block.
    """
    await _insert_user(engine, 1)
    await _insert_project(engine, project_id=71, user_id=1)
    await _insert_chat_in_project(
        engine, 11, 1, project_id=71, rag_enabled=True, model_id="local-model"
    )
    await _insert_document(engine, user_id=1, doc_id=301, project_id=71)
    long_text = ("alpha " * 100) + "SENTINEL_BEYOND_500_CHARS" + (" beta" * 20)
    assert len(long_text) > 500
    await _insert_chunk(engine, document_id=301, text_=long_text, ordinal=0)

    memory_svc = _make_memory_service(recalled=[])
    memory_svc.list_pinned = AsyncMock(return_value=[])
    memory_svc.recall_insights = AsyncMock(return_value=[])
    models_svc = _make_models_service()
    ctx_window = 100_000
    models_svc.get_max_context_length = AsyncMock(return_value=ctx_window)

    with patch(
        "lmchat.services.rag_service.retrieve",
        new_callable=AsyncMock,
        return_value=[],
    ) as mock_retrieve:
        result = await augment_prompt(
            chat_id=11,
            user_id=1,
            current_message="what's in the corpus?",
            engine=engine,
            embedding_client=_make_embedding_client(),
            models_service=models_svc,
            memory_service=memory_svc,
            top_k=5,
        )

    mock_retrieve.assert_not_called()
    assert "SENTINEL_BEYOND_500_CHARS" in result.context_block

    import tiktoken

    from lmchat.services.rag_mode_resolver import rag_inject_budget

    enc = tiktoken.get_encoding("cl100k_base")
    doc_section = result.context_block.split("### Documents", 1)[1]
    assert len(enc.encode(doc_section)) <= rag_inject_budget(ctx_window)


@pytest.mark.asyncio
async def test_augment_prompt_focused_injects_full_chunk_text_within_budget(
    engine: AsyncEngine,
) -> None:
    """FOCUSED must inject the FULL chunk text, not a 500-char head, and
    stay within ``rag_inject_budget()``'s ctx_window-derived bound.
    FOCUSED has no upstream eligibility check on corpus size (an
    admin-pinned document may be arbitrarily large), so this budget is
    its only defense against overflowing the model's context window.

    Against pre-fix code (``text[:500]``) this fails identically to the
    INLINE case above.
    """
    await _insert_user(engine, 1)
    await _insert_document(engine, user_id=1, doc_id=302)
    long_text = (
        ("gamma " * 100) + "SENTINEL_BEYOND_500_CHARS_FOCUSED" + (" delta" * 20)
    )
    assert len(long_text) > 500
    await _insert_chunk(engine, document_id=302, text_=long_text, ordinal=0)

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO chats (id, user_id, title, settings, model_id)"
                " VALUES (:id, :uid, :t, :s, :mid)"
            ),
            {
                "id": 12,
                "uid": 1,
                "t": "focused chat",
                "s": '{"rag_enabled": true, "focused_document_id": 302}',
                "mid": "local-model",
            },
        )

    memory_svc = _make_memory_service(recalled=[])
    memory_svc.list_pinned = AsyncMock(return_value=[])
    memory_svc.recall_insights = AsyncMock(return_value=[])
    models_svc = _make_models_service()
    ctx_window = 100_000
    models_svc.get_max_context_length = AsyncMock(return_value=ctx_window)

    with patch(
        "lmchat.services.rag_service.retrieve",
        new_callable=AsyncMock,
        return_value=[],
    ) as mock_retrieve:
        result = await augment_prompt(
            chat_id=12,
            user_id=1,
            current_message="what's in this doc?",
            engine=engine,
            embedding_client=_make_embedding_client(),
            models_service=models_svc,
            memory_service=memory_svc,
            top_k=5,
        )

    mock_retrieve.assert_not_called()
    assert "SENTINEL_BEYOND_500_CHARS_FOCUSED" in result.context_block
    assert "| focused" in result.context_block

    import tiktoken

    from lmchat.services.rag_mode_resolver import rag_inject_budget

    enc = tiktoken.get_encoding("cl100k_base")
    doc_section = result.context_block.split("### Documents", 1)[1]
    assert len(enc.encode(doc_section)) <= rag_inject_budget(ctx_window)


@pytest.mark.asyncio
async def test_augment_prompt_focused_enforces_budget_drops_overflow(
    engine: AsyncEngine,
) -> None:
    """When a focused document's chunks EXCEED ``rag_inject_budget()``,
    injection must stop at a token-bounded prefix of the offending
    chunk — not inject it uncapped — and later chunks must be dropped
    entirely rather than interleaved as fragments. Exercises the same
    ``_inject_full_text_chunks`` enforcement path INLINE shares.

    Uses a deliberately tiny ``ctx_window`` so the budget
    (``ctx_window * _RAG_CONTEXT_BUDGET_FRACTION`` = 200 * 0.25 = 50
    tokens; values verified against real tiktoken cl100k_base encoding)
    is small enough for a single chunk to overflow it. FOCUSED has no
    upstream eligibility gate on corpus size (unlike INLINE), so this
    budget is its ONLY defense — this is the test that would have
    caught an unbounded/no-op enforcement.
    """
    ctx_window = 200
    budget_tokens = 50  # int(200 * 0.25) — see rag_mode_resolver.rag_inject_budget

    await _insert_user(engine, 1)
    await _insert_document(engine, user_id=1, doc_id=402)

    # Chunk 0 alone (66 head tokens, verified via tiktoken) already
    # exceeds the 50-token budget — the sentinel below sits well past
    # the cut point.
    chunk0_text = (
        "PREFIX_MARKER_EARLY "
        + ("alpha " * 60)
        + "SENTINEL_PAST_BUDGET_MARKER"
        + (" beta" * 20)
    )
    await _insert_chunk(engine, document_id=402, text_=chunk0_text, ordinal=0)
    # Chunk 1 must never be reached — budget is exhausted by chunk 0.
    await _insert_chunk(
        engine,
        document_id=402,
        text_="CHUNK_TWO_SENTINEL_SHOULD_BE_DROPPED chunk two body text",
        ordinal=1,
    )

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO chats (id, user_id, title, settings, model_id)"
                " VALUES (:id, :uid, :t, :s, :mid)"
            ),
            {
                "id": 22,
                "uid": 1,
                "t": "focused overflow chat",
                "s": '{"rag_enabled": true, "focused_document_id": 402}',
                "mid": "local-model",
            },
        )

    memory_svc = _make_memory_service(recalled=[])
    memory_svc.list_pinned = AsyncMock(return_value=[])
    memory_svc.recall_insights = AsyncMock(return_value=[])
    models_svc = _make_models_service()
    models_svc.get_max_context_length = AsyncMock(return_value=ctx_window)

    with patch(
        "lmchat.services.rag_service.retrieve",
        new_callable=AsyncMock,
        return_value=[],
    ) as mock_retrieve:
        result = await augment_prompt(
            chat_id=22,
            user_id=1,
            current_message="what's in this doc?",
            engine=engine,
            embedding_client=_make_embedding_client(),
            models_service=models_svc,
            memory_service=memory_svc,
            top_k=5,
        )

    mock_retrieve.assert_not_called()
    # (1) The token-bounded prefix IS present...
    assert "PREFIX_MARKER_EARLY" in result.context_block
    # (2) ...but the sentinel placed past the budget is ABSENT — proves
    # this is a real token cut, not a pass-through of the full chunk.
    assert "SENTINEL_PAST_BUDGET_MARKER" not in result.context_block
    # (4) Chunk 1 is dropped entirely — not interleaved as a fragment.
    assert "CHUNK_TWO_SENTINEL_SHOULD_BE_DROPPED" not in result.context_block
    assert result.doc_hits == 1

    import tiktoken

    from lmchat.services.rag_mode_resolver import rag_inject_budget

    enc = tiktoken.get_encoding("cl100k_base")
    doc_section = result.context_block.split("### Documents", 1)[1]
    assert rag_inject_budget(ctx_window) == budget_tokens
    # (3) Encoded doc-section tokens land at ~budget plus a small, bounded
    # trailer overhead ("source: ..." line + section markers) — never
    # the ~94-token UNCAPPED chunk size this would be without
    # enforcement.
    assert len(enc.encode(doc_section)) <= budget_tokens + 20


# ---------------------------------------------------------------------------
# ctx_window threading (2026-08-15) — AugmentedPrompt exposes the LIVE-probed
# context window (the same value rag_inject_budget already used to build
# context_block) so trim_rag_context_for_model can reuse it instead of
# re-deriving a smaller static ModelProfile guess for the same turn.
# ---------------------------------------------------------------------------


async def test_ctx_window_reflects_live_probe_not_static_default(
    engine: AsyncEngine,
) -> None:
    """AugmentedPrompt.ctx_window carries the model's REAL loaded context
    length (262_144), sourced purely from the live probe
    (``ModelsService.get_max_context_length``) — ``ModelProfile`` no longer
    has a ``context_window`` field at all, so there is no static table to
    have fallen back to; this is the only source of truth.
    """
    await _insert_user(engine, 1)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO chats (id, user_id, title, settings, model_id)"
                " VALUES (:id, :uid, :t, :s, :mid)"
            ),
            {
                "id": 30,
                "uid": 1,
                "t": "live window chat",
                "s": '{"rag_enabled": true}',
                "mid": "some-unprofiled-model-id",
            },
        )

    memory_svc = _make_memory_service(recalled=[])
    memory_svc.list_pinned = AsyncMock(return_value=[])
    memory_svc.recall_insights = AsyncMock(return_value=[])
    models_svc = _make_models_service()
    # The operator's own fleet reports loaded_context_length: 262144 for a
    # real model with no static ModelProfile row — this is that shape.
    models_svc.get_max_context_length = AsyncMock(return_value=262_144)

    with patch(
        "lmchat.services.rag_service.retrieve",
        new_callable=AsyncMock,
        return_value=[],
    ):
        result = await augment_prompt(
            chat_id=30,
            user_id=1,
            current_message="hello",
            engine=engine,
            embedding_client=_make_embedding_client(),
            models_service=models_svc,
            memory_service=memory_svc,
            top_k=5,
        )

    assert result.ctx_window == 262_144


async def test_trim_uses_threaded_ctx_window_not_static_default(
    engine: AsyncEngine,
) -> None:
    """End-to-end regression for the fix: probe -> AugmentedPrompt.ctx_window
    -> trim_rag_context_for_model. The fixed "unknown window" floor
    (~12_288 chars) would trim a ~30K-char block; threading the live
    262_144-token window through (~196_608-char budget) lets the SAME block
    pass untouched — proportionally larger, not just numerically different,
    and driven entirely by the live number, not a model_id lookup.
    """
    from datetime import UTC, datetime

    from lmchat.services.memory_service import MemoryInsight
    from lmchat.services.rag_service import trim_rag_context_for_model

    await _insert_user(engine, 1)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO chats (id, user_id, title, settings, model_id)"
                " VALUES (:id, :uid, :t, :s, :mid)"
            ),
            {
                "id": 31,
                "uid": 1,
                "t": "live window chat 2",
                "s": '{"rag_enabled": true}',
                "mid": "some-unprofiled-model-id",
            },
        )

    # Pinned insights are injected verbatim (no per-hit excerpt cap, unlike
    # memory/doc hits) — the simplest way to build a single context block
    # large enough to actually exercise the trim boundary.
    big_insight = MemoryInsight(
        id=1,
        user_id=1,
        text="X" * 30_000,
        pinned=True,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    memory_svc = _make_memory_service(recalled=[])
    memory_svc.list_pinned = AsyncMock(return_value=[big_insight])
    memory_svc.recall_insights = AsyncMock(return_value=[])
    models_svc = _make_models_service()
    models_svc.get_max_context_length = AsyncMock(return_value=262_144)

    with patch(
        "lmchat.services.rag_service.retrieve",
        new_callable=AsyncMock,
        return_value=[],
    ):
        result = await augment_prompt(
            chat_id=31,
            user_id=1,
            current_message="hello",
            engine=engine,
            embedding_client=_make_embedding_client(),
            models_service=models_svc,
            memory_service=memory_svc,
            top_k=5,
        )

    assert result.ctx_window == 262_144
    assert len(result.context_block) > 12_288  # over the unresolved-floor budget

    # Unresolved-floor budget (as if ctx_window were never threaded
    # through, e.g. before this fix) — the fixed 16_384-token floor fires
    # the trim on this block.
    stale_trimmed, _, stale_fired = trim_rag_context_for_model(
        result.context_block, None
    )
    assert stale_fired is True

    # Threaded live window (the fix) — the same block passes through
    # untouched, and its untrimmed length is strictly greater than what the
    # unresolved-floor path kept.
    live_trimmed, _, live_fired = trim_rag_context_for_model(
        result.context_block, result.ctx_window
    )
    assert live_fired is False
    assert live_trimmed == result.context_block
    assert len(live_trimmed) > len(stale_trimmed)
