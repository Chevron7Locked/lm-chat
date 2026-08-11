# SPDX-License-Identifier: Apache-2.0
"""retrieve() project_id scoping.

Tests both the FTS5 keyword stage and the vector stage filter by the
same `documents.project_id` predicate. Without the gate on both, RRF
fusion would mix in-project + out-of-project hits.
"""
from __future__ import annotations

import struct
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import create_async_engine

from lmchat.db.schema import (
    document_chunks,
    documents,
    metadata,
    projects,
    users,
)
from lmchat.services.retrieval_service import retrieve


async def _make_engine(tmp_path: Path):
    eng = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/retr.db", pool_pre_ping=True
    )
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
        # Mirror the FTS5 virtual table the production migration creates.
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
    return eng


def _mock_models_service():
    m = SimpleNamespace(
        key="embed-1",
        type="embedding",
        # loaded_instance_ids mirrors LM Studio's live response.
        loaded_instance_ids=["embed-1"],
    )
    svc = AsyncMock()
    svc.list_loaded = AsyncMock(return_value=[m])
    # Keep auth_failed False so the force-reprobe branch is skipped.
    svc.auth_failed = False
    # resolve_embedding_wire_id is called in retrieve() for normalisation.
    # For test models the key equals the wire id ("embed-1" == "embed-1").
    svc.resolve_embedding_wire_id = AsyncMock(return_value="embed-1")
    return svc


def _mock_embedding_client(vec: list[float]):
    c = AsyncMock()
    c.embed_one = AsyncMock(return_value=vec)
    return c


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


async def _insert_user(eng, name: str = "alice") -> int:
    async with eng.begin() as conn:
        r = await conn.execute(
            insert(users).values(username=name, password_hash="x")
        )
        return int(r.inserted_primary_key[0])


async def _insert_project(eng, *, user_id: int, name: str = "P") -> int:
    async with eng.begin() as conn:
        now = time.time()
        r = await conn.execute(
            insert(projects).values(
                user_id=user_id,
                name=name,
                description="",
                system_prompt="",

                created_at=now,
                updated_at=now,
            )
        )
        return int(r.inserted_primary_key[0])


async def _insert_doc(
    eng, *, user_id: int, title: str, project_id: int | None
) -> int:
    async with eng.begin() as conn:
        r = await conn.execute(
            insert(documents).values(
                user_id=user_id,
                title=title,
                mime_type="text/plain",
                byte_size=1,
                chunk_count=1,
                embedding_model_id="embed-1",
                sha256=f"sha-{title}",
                project_id=project_id,
            )
        )
        return int(r.inserted_primary_key[0])


async def _insert_chunk(
    eng, *, document_id: int, text_: str, ordinal: int = 0
) -> int:
    """Chunk rows don't carry embedding_model_id; the parent document does."""
    async with eng.begin() as conn:
        r = await conn.execute(
            insert(document_chunks).values(
                document_id=document_id,
                ordinal=ordinal,
                text=text_,
                text_hash=f"h-{text_}",
                embedding=_pack([1.0, 0.0]),
            )
        )
        return int(r.inserted_primary_key[0])


@pytest.mark.anyio
async def test_retrieve_default_returns_all_chunks(tmp_path: Path) -> None:
    """project_id=None → every chunk the user owns (legacy behavior)."""
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        pid = await _insert_project(eng, user_id=uid)
        d_un = await _insert_doc(
            eng, user_id=uid, title="un", project_id=None
        )
        d_in = await _insert_doc(
            eng, user_id=uid, title="in", project_id=pid
        )
        await _insert_chunk(eng, document_id=d_un, text_="apple")
        await _insert_chunk(eng, document_id=d_in, text_="apple")
        hits = await retrieve(
            query="apple",
            user_id=uid,
            engine=eng,
            embedding_client=_mock_embedding_client([1.0, 0.0]),
            models_service=_mock_models_service(),
        )
        titles = {h.document_title for h in hits}
        assert titles == {"un", "in"}
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_retrieve_with_project_id_filters(tmp_path: Path) -> None:
    """project_id=X → only chunks from documents in that project."""
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        pid = await _insert_project(eng, user_id=uid)
        d_un = await _insert_doc(
            eng, user_id=uid, title="un", project_id=None
        )
        d_in = await _insert_doc(
            eng, user_id=uid, title="in", project_id=pid
        )
        await _insert_chunk(eng, document_id=d_un, text_="apple")
        await _insert_chunk(eng, document_id=d_in, text_="apple")
        hits = await retrieve(
            query="apple",
            user_id=uid,
            engine=eng,
            embedding_client=_mock_embedding_client([1.0, 0.0]),
            models_service=_mock_models_service(),
            project_id=pid,
        )
        titles = {h.document_title for h in hits}
        assert titles == {"in"}
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_retrieve_user_isolation_intact_under_project_filter(
    tmp_path: Path,
) -> None:
    """Project filter never overrides user_id isolation."""
    eng = await _make_engine(tmp_path)
    try:
        alice = await _insert_user(eng, "alice")
        bob = await _insert_user(eng, "bob")
        a_proj = await _insert_project(eng, user_id=alice)
        a_doc = await _insert_doc(
            eng, user_id=alice, title="alice-doc", project_id=a_proj
        )
        await _insert_chunk(eng, document_id=a_doc, text_="apple")
        # Bob asks for Alice's project_id — should get nothing.
        hits = await retrieve(
            query="apple",
            user_id=bob,
            engine=eng,
            embedding_client=_mock_embedding_client([1.0, 0.0]),
            models_service=_mock_models_service(),
            project_id=a_proj,
        )
        assert hits == []
    finally:
        await eng.dispose()
