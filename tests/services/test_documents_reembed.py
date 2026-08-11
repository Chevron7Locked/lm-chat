# SPDX-License-Identifier: Apache-2.0
"""``re_embed_project_documents`` — re-embed all chunks under the new active model.

Covers the admin-facing
"Re-embed all" affordance the `ReembedBanner` originally pointed at.

Contract:
* Pins ``projects.embedding_model_id`` to the new active model id
  BEFORE rewriting any chunk embeddings (interleaved-upload guard).
* Rewrites every chunk's ``embedding`` blob under the new model.
* Updates every doc's ``embedding_model_id``.
* Returns ``{documents_re_embedded, chunks_re_embedded,
  active_embedding_model_id}``.
* Raises ``RuntimeError`` when no embedding model is loaded (route
  layer maps to 503).
"""
from __future__ import annotations

import struct
import time
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import create_async_engine

from lmchat.db.schema import (
    document_chunks,
    documents,
    metadata,
    projects,
    users,
)
from lmchat.embedding.client import EmbeddingClient
from lmchat.services.documents_service import re_embed_project_documents
from lmchat.services.memory_service import DEFAULT_EMBEDDING_MODEL_KEY
from lmchat.services.models_service import ModelsService

# The "new" model the re-embed switches TO. It must be a genuinely-loaded
# embedder that the stable resolver will return: with no admin preference set,
# the resolver returns the canonical default key when it is loaded. Using that
# key here keeps the test's intent identical (a real old → new model switch)
# while satisfying the fail-loud resolver contract.
_NEW_MODEL = DEFAULT_EMBEDDING_MODEL_KEY


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


async def _make_engine(tmp_path: Path):
    eng = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/reembed.db",
        pool_pre_ping=True,
    )
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return eng


async def _seed_project_with_two_docs(
    engine, *, pinned: str = "old-model"
) -> tuple[int, int, list[int], list[int]]:
    """Insert a user + project (pinned to OLD model) + 2 docs +
    a handful of chunks each, all under the OLD model.
    Returns (user_id, project_id, [doc_ids], [chunk_ids])."""
    now = time.time()
    async with engine.begin() as conn:
        u = await conn.execute(
            insert(users).values(username="alice", password_hash="x")
        )
        pk_u = u.inserted_primary_key
        assert pk_u is not None
        uid = int(pk_u[0])
        p = await conn.execute(
            insert(projects).values(
                user_id=uid,
                name="P",
                description="",
                system_prompt="",
                embedding_model_id=pinned,
                created_at=now,
                updated_at=now,
            )
        )
        pk_p = p.inserted_primary_key
        assert pk_p is not None
        pid = int(pk_p[0])
        doc_ids: list[int] = []
        chunk_ids: list[int] = []
        for di, title in enumerate(["a.txt", "b.txt"]):
            dr = await conn.execute(
                insert(documents).values(
                    user_id=uid,
                    project_id=pid,
                    title=title,
                    sha256=f"sha-{di}",
                    mime_type="text/plain",
                    byte_size=100,
                    chunk_count=2,
                    embedding_model_id=pinned,
                )
            )
            pk_dr = dr.inserted_primary_key
            assert pk_dr is not None
            did = int(pk_dr[0])
            doc_ids.append(did)
            for ord_ in range(2):
                cr = await conn.execute(
                    insert(document_chunks).values(
                        document_id=did,
                        ordinal=ord_,
                        text=f"chunk-{di}-{ord_}",
                        text_hash=f"h-{di}-{ord_}",
                        embedding=_pack([0.5, 0.5, 0.5, 0.5]),
                    )
                )
                pk_cr = cr.inserted_primary_key
                assert pk_cr is not None
                chunk_ids.append(int(pk_cr[0]))
    return uid, pid, doc_ids, chunk_ids


def _models_service(loaded: list[str]) -> ModelsService:
    svc = MagicMock()
    # Genuinely loaded — the resolver filters embedders on loaded_instance_ids,
    # so an entry without instance ids would be treated as not-loaded.
    entries = [
        MagicMock(
            key=mid,
            type="embedding",
            loaded_instance_ids=[f"{mid}@q8_0"],
        )
        for mid in loaded
    ]
    svc.list_loaded = AsyncMock(return_value=entries)
    return cast(ModelsService, svc)


def _embedding_client(new_vec: list[float]) -> EmbeddingClient:
    """Mock embedding client that returns a distinct vector per call
    so we can tell the re-embed actually happened."""
    cli = MagicMock()

    async def _embed_batch(*, texts: list[str], model_id: str) -> list[list[float]]:
        return [list(new_vec) for _ in texts]

    cli.embed_batch = _embed_batch
    return cast(EmbeddingClient, cli)


# ─── Tests ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_re_embed_rewrites_all_chunks_under_new_model(
    tmp_path: Path,
) -> None:
    """Project pinned to ``old-model`` + active model is the new
    (default) embedder. After re-embed: every chunk's embedding blob
    reflects the new vector AND every doc + the project itself are
    pinned to the new model (``_NEW_MODEL``)."""
    eng = await _make_engine(tmp_path)
    uid, pid, doc_ids, chunk_ids = await _seed_project_with_two_docs(
        eng, pinned="old-model"
    )
    new_vec = [0.9, 0.1, 0.2, 0.7]
    result = await re_embed_project_documents(
        user_id=uid,
        project_id=pid,
        engine=eng,
        embedding_client=_embedding_client(new_vec),
        models_service=_models_service([_NEW_MODEL]),
    )

    assert result == {
        "documents_re_embedded": 2,
        "chunks_re_embedded": 4,
        "active_embedding_model_id": _NEW_MODEL,
    }

    async with eng.connect() as conn:
        proj_pin = (
            await conn.execute(
                select(projects.c.embedding_model_id).where(
                    projects.c.id == pid
                )
            )
        ).scalar_one()
        assert proj_pin == _NEW_MODEL

        for did in doc_ids:
            doc_pin = (
                await conn.execute(
                    select(documents.c.embedding_model_id).where(
                        documents.c.id == did
                    )
                )
            ).scalar_one()
            assert doc_pin == _NEW_MODEL

        for cid in chunk_ids:
            blob = (
                await conn.execute(
                    select(document_chunks.c.embedding).where(
                        document_chunks.c.id == cid
                    )
                )
            ).scalar_one()
            assert _unpack(blob) == pytest.approx(new_vec, rel=1e-5), (
                f"chunk {cid} embedding not rewritten: {_unpack(blob)!r}"
            )

    await eng.dispose()


@pytest.mark.asyncio
async def test_re_embed_raises_when_no_embedding_model_loaded(
    tmp_path: Path,
) -> None:
    """No embedding model loaded → RuntimeError (route layer maps to 503)."""
    eng = await _make_engine(tmp_path)
    uid, pid, _, _ = await _seed_project_with_two_docs(eng)
    with pytest.raises(RuntimeError, match="No embedding model"):
        await re_embed_project_documents(
            user_id=uid,
            project_id=pid,
            engine=eng,
            embedding_client=_embedding_client([0.0, 0.0, 0.0, 0.0]),
            models_service=_models_service([]),
        )
    await eng.dispose()


@pytest.mark.asyncio
async def test_re_embed_skips_empty_documents_but_updates_pin(
    tmp_path: Path,
) -> None:
    """A document with zero chunks still gets its ``embedding_model_id``
    pointer updated — without that the next attach to the same project
    would conflict on a now-stale pin."""
    eng = await _make_engine(tmp_path)
    now = time.time()
    async with eng.begin() as conn:
        u = await conn.execute(
            insert(users).values(username="alice", password_hash="x")
        )
        pk_u = u.inserted_primary_key
        assert pk_u is not None
        uid = int(pk_u[0])
        p = await conn.execute(
            insert(projects).values(
                user_id=uid,
                name="P",
                description="",
                system_prompt="",
                embedding_model_id="old-model",
                created_at=now,
                updated_at=now,
            )
        )
        pk_p = p.inserted_primary_key
        assert pk_p is not None
        pid = int(pk_p[0])
        dr = await conn.execute(
            insert(documents).values(
                user_id=uid,
                project_id=pid,
                title="empty.txt",
                sha256="sha-empty",
                mime_type="text/plain",
                byte_size=0,
                chunk_count=0,
                embedding_model_id="old-model",
            )
        )
        pk_dr = dr.inserted_primary_key
        assert pk_dr is not None
        did = int(pk_dr[0])

    result = await re_embed_project_documents(
        user_id=uid,
        project_id=pid,
        engine=eng,
        embedding_client=_embedding_client([0.1] * 4),
        models_service=_models_service([_NEW_MODEL]),
    )

    assert result["documents_re_embedded"] == 1
    assert result["chunks_re_embedded"] == 0
    assert result["active_embedding_model_id"] == _NEW_MODEL

    async with eng.connect() as conn:
        doc_pin = (
            await conn.execute(
                select(documents.c.embedding_model_id).where(
                    documents.c.id == did
                )
            )
        ).scalar_one()
        assert doc_pin == _NEW_MODEL
    await eng.dispose()


@pytest.mark.asyncio
async def test_re_embed_does_not_touch_other_users_or_projects(
    tmp_path: Path,
) -> None:
    """Defense-in-depth: the user_id + project_id WHERE clauses must
    scope the writes correctly."""
    eng = await _make_engine(tmp_path)
    uid_a, pid_a, _, chunk_ids_a = await _seed_project_with_two_docs(
        eng, pinned="old-model"
    )
    # A second user + project, also pinned to old-model.
    now = time.time()
    async with eng.begin() as conn:
        u = await conn.execute(
            insert(users).values(username="bob", password_hash="x")
        )
        pk_ub = u.inserted_primary_key
        assert pk_ub is not None
        uid_b = int(pk_ub[0])
        p = await conn.execute(
            insert(projects).values(
                user_id=uid_b,
                name="other",
                description="",
                system_prompt="",
                embedding_model_id="old-model",
                created_at=now,
                updated_at=now,
            )
        )
        pk_pb = p.inserted_primary_key
        assert pk_pb is not None
        pid_b = int(pk_pb[0])
        dr = await conn.execute(
            insert(documents).values(
                user_id=uid_b,
                project_id=pid_b,
                title="b.txt",
                sha256="sha-b",
                mime_type="text/plain",
                byte_size=10,
                chunk_count=1,
                embedding_model_id="old-model",
            )
        )
        pk_drb = dr.inserted_primary_key
        assert pk_drb is not None
        did_b = int(pk_drb[0])
        cr = await conn.execute(
            insert(document_chunks).values(
                document_id=did_b,
                ordinal=0,
                text="other-user chunk",
                text_hash="h-b",
                embedding=_pack([0.5, 0.5, 0.5, 0.5]),
            )
        )
        pk_crb = cr.inserted_primary_key
        assert pk_crb is not None
        cid_b = int(pk_crb[0])

    new_vec = [0.9, 0.9, 0.9, 0.9]
    await re_embed_project_documents(
        user_id=uid_a,
        project_id=pid_a,
        engine=eng,
        embedding_client=_embedding_client(new_vec),
        models_service=_models_service([_NEW_MODEL]),
    )

    async with eng.connect() as conn:
        # Other user's project pin untouched.
        other_pin = (
            await conn.execute(
                select(projects.c.embedding_model_id).where(
                    projects.c.id == pid_b
                )
            )
        ).scalar_one()
        assert other_pin == "old-model"
        # Other user's doc pin untouched.
        other_doc_pin = (
            await conn.execute(
                select(documents.c.embedding_model_id).where(
                    documents.c.id == did_b
                )
            )
        ).scalar_one()
        assert other_doc_pin == "old-model"
        # Other user's chunk embedding untouched (still all-0.5).
        other_blob = (
            await conn.execute(
                select(document_chunks.c.embedding).where(
                    document_chunks.c.id == cid_b
                )
            )
        ).scalar_one()
        assert _unpack(other_blob) == pytest.approx(
            [0.5, 0.5, 0.5, 0.5], rel=1e-5
        )
        # Sanity: user A's chunks DID get rewritten.
        for cid in chunk_ids_a:
            blob_a = (
                await conn.execute(
                    select(document_chunks.c.embedding).where(
                        document_chunks.c.id == cid
                    )
                )
            ).scalar_one()
            assert _unpack(blob_a) == pytest.approx(new_vec, rel=1e-5)
    await eng.dispose()
