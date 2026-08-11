# SPDX-License-Identifier: Apache-2.0
"""Read-time wiring — embedding model resolution at retrieve time.

NULL-fallback contract for the READ side. Resolution order:

1. project_id set AND projects.embedding_model_id non-NULL AND that
   model IS loaded → use the pinned model.
2. project_id set + pinned but pinned model NOT loaded → skip
   retrieval (None; graceful no-op).
3. project_id set + no pin (NULL) → fall back to user's active.
4. project_id None → user's active.
5. No active embedding model loaded → None (skip retrieval).
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import insert, update
from sqlalchemy.ext.asyncio import create_async_engine

from lmchat.db.schema import metadata, projects, users
from lmchat.services.models_service import ModelsService
from lmchat.services.retrieval_service import (
    EMBED_STATUS_NO_EMBEDDING_MODEL,
    EMBED_STATUS_OK,
    _resolve_embedding_model_id,
    resolve_embedding_model_status,
)


async def _make_engine(tmp_path: Path):
    eng = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/retrieval_resolve.db",
        pool_pre_ping=True,
    )
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return eng


async def _seed_project(eng, *, pinned: str | None = None) -> tuple[int, int]:
    async with eng.begin() as conn:
        u = await conn.execute(
            insert(users).values(username="alice", password_hash="x")
        )
        uid = int(u.inserted_primary_key[0])
        now = time.time()
        p = await conn.execute(
            insert(projects).values(
                user_id=uid,
                name="P",
                description="",
                system_prompt="",

                created_at=now,
                updated_at=now,
            )
        )
        pid = int(p.inserted_primary_key[0])
        if pinned is not None:
            await conn.execute(
                update(projects)
                .where(projects.c.id == pid)
                .values(embedding_model_id=pinned)
            )
    return uid, pid


def _models_service(
    loaded_embedding_ids: list[str],
    *,
    key_to_instance: dict[str, str] | None = None,
) -> ModelsService:
    """Build a mock ModelsService for resolution tests.

    Args:
        loaded_embedding_ids: catalog keys of loaded embedding models.
            Used as BOTH the ``key`` AND ``loaded_instance_ids[0]``
            unless overridden by *key_to_instance*.
        key_to_instance: optional override mapping catalog key →
            loaded instance id (the @quant-suffixed wire id).  Use
            this to test the case where the loaded id differs from
            the catalog key (e.g. ``"nomic@q8_0"`` ≠ ``"nomic"``).
    """
    svc = MagicMock()
    loaded = []
    for mid in loaded_embedding_ids:
        wire_id = (key_to_instance or {}).get(mid, mid)
        m = MagicMock(key=mid, type="embedding", loaded_instance_ids=[wire_id])
        loaded.append(m)
    svc.list_loaded = AsyncMock(return_value=loaded)
    # Keep auth_failed False (bool) so the force-reprobe branch is skipped
    # in these resolution unit tests.
    svc.auth_failed = False
    return cast(ModelsService, svc)


# ─── Resolution paths ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_uses_pinned_model_when_loaded(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    uid, pid = await _seed_project(eng, pinned="embed-A")
    ms = _models_service(["embed-A", "embed-B"])

    resolved = await _resolve_embedding_model_id(
        project_id=pid, user_id=uid, engine=eng, models_service=ms
    )
    assert resolved == "embed-A"
    await eng.dispose()


@pytest.mark.asyncio
async def test_resolve_skips_when_pinned_model_not_loaded(
    tmp_path: Path,
) -> None:
    """Pinned model isn't currently loaded — graceful skip (None)
    rather than embedding under a different model and producing
    wrong-vector-space cosine results."""
    eng = await _make_engine(tmp_path)
    uid, pid = await _seed_project(eng, pinned="embed-A")
    ms = _models_service(["embed-B"])  # A NOT loaded

    resolved = await _resolve_embedding_model_id(
        project_id=pid, user_id=uid, engine=eng, models_service=ms
    )
    assert resolved is None
    await eng.dispose()


@pytest.mark.asyncio
async def test_resolve_falls_back_to_user_active_when_no_pin(
    tmp_path: Path,
) -> None:
    """Project with no pin (NULL) → user's active embedding model.

    Uses the canonical default (nomic) as the loaded embedder so the
    no-preference resolver selects it deterministically.
    """
    eng = await _make_engine(tmp_path)
    uid, pid = await _seed_project(eng, pinned=None)
    active_key = "text-embedding-nomic-embed-text-v1.5"
    ms = _models_service([active_key], key_to_instance={active_key: active_key})

    resolved = await _resolve_embedding_model_id(
        project_id=pid, user_id=uid, engine=eng, models_service=ms
    )
    assert resolved == active_key
    await eng.dispose()


@pytest.mark.asyncio
async def test_resolve_no_project_id_uses_user_active(
    tmp_path: Path,
) -> None:
    """project_id=None → user's active embedding model (legacy
    un-projected behavior unchanged).

    Uses the canonical default (nomic) as the loaded embedder so the
    no-preference resolver selects it deterministically.
    """
    eng = await _make_engine(tmp_path)
    uid, _ = await _seed_project(eng, pinned=None)
    active_key = "text-embedding-nomic-embed-text-v1.5"
    ms = _models_service([active_key], key_to_instance={active_key: active_key})

    resolved = await _resolve_embedding_model_id(
        project_id=None, user_id=uid, engine=eng, models_service=ms
    )
    assert resolved == active_key
    await eng.dispose()


@pytest.mark.asyncio
async def test_resolve_returns_none_when_no_embedding_loaded(
    tmp_path: Path,
) -> None:
    """No embedding model loaded → None (retrieval skipped)."""
    eng = await _make_engine(tmp_path)
    uid, pid = await _seed_project(eng, pinned=None)
    ms = _models_service([])

    resolved = await _resolve_embedding_model_id(
        project_id=pid, user_id=uid, engine=eng, models_service=ms
    )
    assert resolved is None
    await eng.dispose()


# ─── Wire-id (quant-suffix) resolution ───────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_returns_wire_id_when_quant_suffix_present(
    tmp_path: Path,
) -> None:
    """Configured key lacks the quant suffix; loaded instance has it.

    The resolver must return the @quant-suffixed wire id so that the
    embeddings endpoint doesn't 400-reject the unsuffixed catalog key.

    Scenario: configured embedding model is
    ``text-embedding-nomic-embed-text-v1.5`` (the catalog key), but LM
    Studio loaded it as ``text-embedding-nomic-embed-text-v1.5@q8_0``
    (the wire id).  The pinned project embeds with the catalog key;
    the resolved wire id must be the instance id.
    """
    eng = await _make_engine(tmp_path)
    catalog_key = "text-embedding-nomic-embed-text-v1.5"
    wire_id = "text-embedding-nomic-embed-text-v1.5@q8_0"
    uid, pid = await _seed_project(eng, pinned=catalog_key)
    ms = _models_service([catalog_key], key_to_instance={catalog_key: wire_id})

    resolved_id, status = await resolve_embedding_model_status(
        project_id=pid, user_id=uid, engine=eng, models_service=ms
    )
    assert status == EMBED_STATUS_OK
    assert resolved_id == wire_id, (
        "Must return the loaded instance wire id, not the catalog key"
    )
    await eng.dispose()


async def test_resolve_falls_back_to_any_loaded_embedder(
    tmp_path: Path,
) -> None:
    """No preference + only a NON-default embedder loaded → no_embedding_model.

    The resolver no longer silently
    falls back to "any loaded embedder". With no preference pinned and the
    canonical default (nomic) NOT loaded, it must NOT use a different model
    (e.g. bge-m3, which is a different dimension) — that would silently corrupt
    recall. Instead it surfaces ``no_embedding_model`` so the admin loads
    nomic (LM Studio's default), and retrieval degrades to FTS-keyword-only
    rather than embedding under the wrong model.
    """
    eng = await _make_engine(tmp_path)
    # Only bge-m3 loaded; nomic (the default) is NOT loaded.
    other_key = "text-embedding-bge-m3"
    # No project pin → user_active path; no preference persisted.
    uid, pid = await _seed_project(eng, pinned=None)
    ms = _models_service([other_key], key_to_instance={other_key: other_key})

    resolved_id, status = await resolve_embedding_model_status(
        project_id=pid, user_id=uid, engine=eng, models_service=ms
    )
    # Fail-loud → skip signal, NOT a silent swap to bge-m3.
    assert status == EMBED_STATUS_NO_EMBEDDING_MODEL
    assert resolved_id is None
    await eng.dispose()


@pytest.mark.asyncio
async def test_resolve_returns_skip_signal_when_no_embedding_loaded(
    tmp_path: Path,
) -> None:
    """No embedding model loaded at all → (None, no_embedding_model).

    Existing skip-retrieval-cleanly behavior must be preserved: the caller
    gates the embedding call on ``model_id is not None`` so no EmbeddingError
    is raised and RAG degrades to FTS-keyword-only instead of hard-erroring.
    """
    eng = await _make_engine(tmp_path)
    uid, pid = await _seed_project(eng, pinned=None)
    ms = _models_service([])  # nothing loaded

    resolved_id, status = await resolve_embedding_model_status(
        project_id=pid, user_id=uid, engine=eng, models_service=ms
    )
    assert status == EMBED_STATUS_NO_EMBEDDING_MODEL
    assert resolved_id is None
    await eng.dispose()


# ─── Reuse query_vector when catalog key resolves to wire id ─────────────────


@pytest.mark.asyncio
async def test_retrieve_reuses_query_vector_for_quant_suffixed_model(
    tmp_path: Path,
) -> None:
    """Chunks stored under the bare catalog key reuse query_vector at retrieve time.

    The vector group-by previously compared stored chunk
    embedding_model_id (a CATALOG key, e.g. "nomic-embed-v1.5") against
    model_id (a resolved WIRE id, e.g. "nomic-embed-v1.5@q8_0"): they never
    matched → embed_one was called once per RAG turn even when the same model
    was in use. After the fix, both are resolved to wire ids before comparison
    so the already-computed query_vector is reused.

    Reverting the normalized comparison back to `mid == model_id`
    → embed_one will be called once extra (total 2: once for the initial
    query embedding, once for the redundant per-group re-embed).
    """
    import struct
    from unittest.mock import AsyncMock, MagicMock, patch

    from sqlalchemy import insert
    from sqlalchemy.ext.asyncio import create_async_engine

    from lmchat.db.schema import document_chunks, documents, metadata, users
    from lmchat.services.retrieval_service import retrieve

    catalog_key = "nomic-embed-v1.5"
    wire_id = "nomic-embed-v1.5@q8_0"

    # Build a real SQLite DB with one chunk stored under the catalog key
    # (not the wire id — the pre-fix writing path).
    eng = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/fix_f_test.db",
        pool_pre_ping=True,
    )
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
        # Mirror the FTS5 virtual table + trigger that the production migration creates.
        await conn.exec_driver_sql(
            "CREATE VIRTUAL TABLE IF NOT EXISTS document_chunks_fts "
            "USING fts5(text, content='document_chunks', content_rowid='id')"
        )
        await conn.exec_driver_sql(
            "CREATE TRIGGER IF NOT EXISTS document_chunks_ai "
            "AFTER INSERT ON document_chunks BEGIN "
            "INSERT INTO document_chunks_fts(rowid, text) "
            "VALUES (new.id, new.text); END"
        )

    # Seed: user → document → chunk with a 4-dim embedding and
    # embedding_model_id = catalog_key (bare key, as stored before wire-id normalisation).
    _vec = [1.0, 0.0, 0.0, 0.0]
    _packed = struct.pack(f"{len(_vec)}f", *_vec)
    async with eng.begin() as conn:
        u = await conn.execute(
            insert(users).values(username="alice_fix_f", password_hash="x")
        )
        assert u.inserted_primary_key is not None
        uid = int(u.inserted_primary_key[0])
        d = await conn.execute(
            insert(documents).values(
                user_id=uid,
                title="fix-f-doc.txt",
                mime_type="text/plain",
                byte_size=11,
                chunk_count=1,
                embedding_model_id=catalog_key,
                sha256="abc123",
            )
        )
        assert d.inserted_primary_key is not None
        did = int(d.inserted_primary_key[0])
        await conn.execute(
            insert(document_chunks).values(
                document_id=did,
                ordinal=0,
                text="hello world",
                text_hash="h-hello world",
                embedding=_packed,
                embedding_model_id=catalog_key,  # stored as bare key
            )
        )

    # Build a mock ModelsService that:
    # - list_loaded() returns the model with its wire id in loaded_instance_ids.
    # - resolve_embedding_wire_id() maps both catalog_key and wire_id → wire_id.
    model_mock = MagicMock(
        key=catalog_key,
        type="embedding",
        loaded_instance_ids=[wire_id],
    )
    ms = MagicMock()
    ms.list_loaded = AsyncMock(return_value=[model_mock])
    ms.auth_failed = False

    async def _resolve_wire(model_id: str) -> str | None:
        if model_id in (catalog_key, wire_id):
            return wire_id
        return None

    ms.resolve_embedding_wire_id = _resolve_wire
    ms._refresh_if_loaded_cache_stale = AsyncMock()

    # Build a mock EmbeddingClient that:
    # - embed_one() called for the initial query embed (model_id = wire_id → returns _vec).
    # - must NOT be called again for the catalog_key group (the fix).
    ec = MagicMock()
    embed_one_call_count = 0

    async def _embed_one(*, text: str, model_id: str) -> list[float]:
        nonlocal embed_one_call_count
        embed_one_call_count += 1
        return _vec

    ec.embed_one = _embed_one

    # `_resolve_embedding_model_id` reads from the DB (no project pin → active
    # user model). We patch it to return the wire_id directly (the value that
    # resolve_active_embedding_model_key would return after wire-id normalisation).
    with patch(
        "lmchat.services.retrieval_service._resolve_embedding_model_id",
        new=AsyncMock(return_value=wire_id),
    ):
        hits = await retrieve(
            query="hello",
            user_id=uid,
            top_k=5,
            engine=eng,
            embedding_client=ec,  # type: ignore[arg-type]
            models_service=ms,  # type: ignore[arg-type]
        )

    await eng.dispose()

    # The chunk should have been found (FTS5 + vector).
    assert len(hits) >= 1, f"expected at least one hit, got {hits}"

    # CRITICAL assertion: embed_one must be called exactly ONCE (for
    # the initial query embedding). The catalog_key group must reuse
    # query_vector rather than triggering a second embed_one call.
    assert embed_one_call_count == 1, (
        f"Fix F regression: embed_one was called {embed_one_call_count} times. "
        "Expected 1 (initial query embed only). The catalog-key group must "
        "reuse query_vector when it resolves to the same wire id."
    )
