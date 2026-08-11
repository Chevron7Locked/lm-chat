# SPDX-License-Identifier: Apache-2.0
"""Tests for documents_service — upload, chunk_and_embed, list, delete.

Covers:
- TXT upload round-trip (upload → list → chunks).
- MD upload round-trip.
- HTML upload (tag stripping).
- PDF upload (text extraction).
- Image-only PDF raises ImageOnlyPdfError.
- Unsupported MIME type raises UnsupportedMimeTypeError.
- SHA-256 dedup: same file for same user returns the existing row.
- Soft-delete: deleted document not in list_documents.
- Tenant isolation: delete by wrong user raises DocumentNotFoundError.
"""
from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import alembic.command
import alembic.config
import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import projects
from lmchat.services.documents_service import (
    DocumentNotFoundError,
    ImageOnlyPdfError,
    UnsupportedMimeTypeError,
    delete_document,
    get_document_chunks,
    list_documents,
    upload_document,
)
from lmchat.services.memory_service import DEFAULT_EMBEDDING_MODEL_KEY
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    """Yield a fresh per-test SQLite engine with the full schema + FTS5."""
    db_path = tmp_path / "test_docs.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"
    await asyncio.to_thread(_run_upgrade, db_url)
    eng = create_async_engine(db_url, pool_pre_ping=True)
    yield eng
    await eng.dispose()


def _fake_embedding(dim: int = 4) -> list[float]:
    return [0.1] * dim


def _make_models_service(model_key: str = DEFAULT_EMBEDDING_MODEL_KEY) -> MagicMock:
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


def _make_embedding_client(dim: int = 4) -> MagicMock:
    client = MagicMock()
    client.embed_batch = AsyncMock(return_value=[[0.1] * dim])
    client.embed_one = AsyncMock(return_value=[0.1] * dim)
    return client


async def _insert_user(engine: AsyncEngine, user_id: int) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO users (id, username, password_hash)"
                " VALUES (:id, :u, :ph)"
            ),
            {"id": user_id, "u": f"user{user_id}", "ph": "scrypt$dummy"},
        )


async def _insert_project(engine: AsyncEngine, *, user_id: int, name: str = "P") -> int:
    async with engine.begin() as conn:
        now = time.time()
        result = await conn.execute(
            insert(projects).values(
                user_id=user_id,
                name=name,
                description="",
                system_prompt="",
                created_at=now,
                updated_at=now,
            )
        )
        pk = result.inserted_primary_key
        if pk is None:
            raise RuntimeError("INSERT into projects returned no PK")
        return int(pk[0])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_txt_round_trip(engine: AsyncEngine) -> None:
    """TXT upload creates document + chunks; list returns it."""
    await _insert_user(engine, 1)
    client = _make_embedding_client()
    # Embed multiple chunks — return enough vectors.
    client.embed_batch = AsyncMock(side_effect=lambda texts, model_id: [[0.1] * 4] * len(texts))
    svc = _make_models_service()

    body = b"Hello world. This is a test document for the RAG pipeline."
    doc = await upload_document(
        user_id=1,
        filename="test.txt",
        content_type="text/plain",
        body_bytes=body,
        engine=engine,
        embedding_client=client,
        models_service=svc,
    )

    assert doc.id > 0
    assert doc.title == "test.txt"
    assert doc.mime_type == "text/plain"
    assert doc.byte_size == len(body)
    assert doc.chunk_count >= 1

    docs = await list_documents(user_id=1, engine=engine)
    assert len(docs) == 1
    assert docs[0].id == doc.id


@pytest.mark.asyncio
async def test_upload_md_round_trip(engine: AsyncEngine) -> None:
    """Markdown upload is treated as plain text."""
    await _insert_user(engine, 1)
    client = _make_embedding_client()
    client.embed_batch = AsyncMock(side_effect=lambda texts, model_id: [[0.1] * 4] * len(texts))
    svc = _make_models_service()

    body = b"# Heading\n\nSome **markdown** text."
    doc = await upload_document(
        user_id=1,
        filename="readme.md",
        content_type="text/markdown",
        body_bytes=body,
        engine=engine,
        embedding_client=client,
        models_service=svc,
    )

    assert doc.title == "readme.md"
    assert doc.chunk_count >= 1


@pytest.mark.asyncio
async def test_upload_html_strips_tags(engine: AsyncEngine) -> None:
    """HTML upload strips tags and creates chunks from visible text."""
    await _insert_user(engine, 1)
    client = _make_embedding_client()
    client.embed_batch = AsyncMock(side_effect=lambda texts, model_id: [[0.1] * 4] * len(texts))
    svc = _make_models_service()

    body = b"<html><body><h1>Hello</h1><p>World</p></body></html>"
    doc = await upload_document(
        user_id=1,
        filename="page.html",
        content_type="text/html",
        body_bytes=body,
        engine=engine,
        embedding_client=client,
        models_service=svc,
    )

    assert doc.title == "page.html"
    # At least one chunk must exist.
    assert doc.chunk_count >= 1
    chunks = await get_document_chunks(document_id=doc.id, user_id=1, engine=engine)
    # The chunk text should not contain HTML tags.
    full_text = " ".join(c["text_preview"] for c in chunks)
    assert "<h1>" not in full_text


@pytest.mark.asyncio
async def test_upload_image_only_pdf_raises(engine: AsyncEngine) -> None:
    """Image-only PDF (no extractable text) raises ImageOnlyPdfError."""
    await _insert_user(engine, 1)
    from unittest.mock import patch

    # Patch pypdf.PdfReader at the pypdf module level (imported inside
    # _extract_text lazily).
    class _FakePage:
        def extract_text(self) -> str:
            return ""

    class _FakeReader:
        def __init__(self, fp: object) -> None:
            self.pages = [_FakePage()]

    with patch("pypdf.PdfReader", _FakeReader):
        with pytest.raises(ImageOnlyPdfError):
            await upload_document(
                user_id=1,
                filename="scan.pdf",
                content_type="application/pdf",
                body_bytes=b"%PDF-fake",
                engine=engine,
                embedding_client=_make_embedding_client(),
                models_service=_make_models_service(),
            )


@pytest.mark.asyncio
async def test_upload_unsupported_mime_raises(engine: AsyncEngine) -> None:
    """Uploading an unsupported MIME type raises UnsupportedMimeTypeError."""
    await _insert_user(engine, 1)
    with pytest.raises(UnsupportedMimeTypeError) as exc_info:
        await upload_document(
            user_id=1,
            filename="file.bin",
            content_type="application/octet-stream",
            body_bytes=b"\x00\x01\x02",
            engine=engine,
            embedding_client=_make_embedding_client(),
            models_service=_make_models_service(),
        )
    assert "application/octet-stream" in exc_info.value.mime_type


@pytest.mark.asyncio
async def test_upload_dedup_same_file(engine: AsyncEngine) -> None:
    """Same file uploaded twice returns the existing document without re-inserting."""
    await _insert_user(engine, 1)
    client = _make_embedding_client()
    client.embed_batch = AsyncMock(side_effect=lambda texts, model_id: [[0.1] * 4] * len(texts))
    svc = _make_models_service()

    body = b"Deduplicated content here."
    doc1 = await upload_document(
        user_id=1,
        filename="dup.txt",
        content_type="text/plain",
        body_bytes=body,
        engine=engine,
        embedding_client=client,
        models_service=svc,
    )
    doc2 = await upload_document(
        user_id=1,
        filename="dup2.txt",
        content_type="text/plain",
        body_bytes=body,
        engine=engine,
        embedding_client=client,
        models_service=svc,
    )

    # Same row returned; embed_batch called only once (dedup short-circuit).
    assert doc1.id == doc2.id
    docs = await list_documents(user_id=1, engine=engine)
    assert len(docs) == 1


@pytest.mark.asyncio
async def test_upload_dedup_scoped_by_project_id(engine: AsyncEngine) -> None:
    """Same bytes uploaded into a DIFFERENT project get their OWN document
    row, actually chunked/embedded under that project's scope — dedup
    identity is (user_id, sha256, project_id), not (user_id, sha256) alone.
    A short-circuited cross-project dedup match would misattribute the
    document AND leave the target project's just-set embedding pin
    dangling with no matching document; re-uploading into the SAME
    project must still dedup as before.
    """
    await _insert_user(engine, 1)
    pid1 = await _insert_project(engine, user_id=1, name="P1")
    pid2 = await _insert_project(engine, user_id=1, name="P2")

    client = _make_embedding_client()
    client.embed_batch = AsyncMock(
        side_effect=lambda texts, model_id: [[0.1] * 4] * len(texts)
    )
    svc = _make_models_service()

    body = b"Cross-project dedup content."

    doc1 = await upload_document(
        user_id=1,
        filename="a.txt",
        content_type="text/plain",
        body_bytes=body,
        engine=engine,
        embedding_client=client,
        models_service=svc,
        project_id=pid1,
    )
    doc2 = await upload_document(
        user_id=1,
        filename="b.txt",
        content_type="text/plain",
        body_bytes=body,
        engine=engine,
        embedding_client=client,
        models_service=svc,
        project_id=pid2,
    )

    # Different projects → different rows, each actually indexed — not a
    # dedup short-circuit onto the first project's row.
    assert doc1.id != doc2.id
    assert doc1.chunk_count >= 1
    assert doc2.chunk_count >= 1
    assert doc1.project_id == pid1
    assert doc2.project_id == pid2

    docs_p1 = await list_documents(user_id=1, engine=engine, project_id=pid1)
    docs_p2 = await list_documents(user_id=1, engine=engine, project_id=pid2)
    assert [d.id for d in docs_p1] == [doc1.id]
    assert [d.id for d in docs_p2] == [doc2.id]

    # Both projects' embedding pins correspond to a REAL attached document —
    # no dangling pin left by a short-circuited dedup match.
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(projects.c.id, projects.c.embedding_model_id).where(
                    projects.c.id.in_([pid1, pid2])
                )
            )
        ).fetchall()
    pins = {r.id: r.embedding_model_id for r in rows}
    assert pins[pid1] is not None
    assert pins[pid2] is not None

    # Re-uploading into the SAME project still dedups (unchanged behavior).
    doc3 = await upload_document(
        user_id=1,
        filename="a-again.txt",
        content_type="text/plain",
        body_bytes=body,
        engine=engine,
        embedding_client=client,
        models_service=svc,
        project_id=pid1,
    )
    assert doc3.id == doc1.id


@pytest.mark.asyncio
async def test_soft_delete_excludes_from_list(engine: AsyncEngine) -> None:
    """Deleted document not returned by list_documents."""
    await _insert_user(engine, 1)
    client = _make_embedding_client()
    client.embed_batch = AsyncMock(side_effect=lambda texts, model_id: [[0.1] * 4] * len(texts))
    svc = _make_models_service()

    doc = await upload_document(
        user_id=1,
        filename="del.txt",
        content_type="text/plain",
        body_bytes=b"Content to delete.",
        engine=engine,
        embedding_client=client,
        models_service=svc,
    )
    assert len(await list_documents(user_id=1, engine=engine)) == 1

    await delete_document(document_id=doc.id, user_id=1, engine=engine)
    assert len(await list_documents(user_id=1, engine=engine)) == 0


@pytest.mark.asyncio
async def test_delete_wrong_user_raises(engine: AsyncEngine) -> None:
    """Deleting another user's document raises DocumentNotFoundError."""
    await _insert_user(engine, 1)
    await _insert_user(engine, 2)
    client = _make_embedding_client()
    client.embed_batch = AsyncMock(side_effect=lambda texts, model_id: [[0.1] * 4] * len(texts))
    svc = _make_models_service()

    doc = await upload_document(
        user_id=1,
        filename="private.txt",
        content_type="text/plain",
        body_bytes=b"User 1 only.",
        engine=engine,
        embedding_client=client,
        models_service=svc,
    )

    with pytest.raises(DocumentNotFoundError):
        await delete_document(document_id=doc.id, user_id=2, engine=engine)


# ---------------------------------------------------------------------------
# Regression — orphaned chunk_count=0 row when embedding fails.
#
# The document INSERT and chunk_and_embed run in SEPARATE engine.begin()
# transactions. If the embed stage raises, the row was left committed at
# chunk_count=0 with no recovery path — and the sha256 dedup predicate
# (deleted_at IS NULL) still matched it, so re-uploading the same file
# returned the dead row instead of re-processing it. upload_document now
# compensates by SOFT-deleting the orphan on any embed failure and
# re-raising, so a retry of the same bytes re-inserts + re-indexes.
# ---------------------------------------------------------------------------


async def _fetch_doc_row(engine: AsyncEngine, doc_id: int) -> Any:
    """Return the raw documents row (incl. deleted_at) regardless of soft-delete."""
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT id, chunk_count, deleted_at FROM documents WHERE id = :id"
                ),
                {"id": doc_id},
            )
        ).fetchone()


@pytest.mark.asyncio
async def test_upload_embed_failure_soft_deletes_orphan_and_propagates(
    engine: AsyncEngine,
) -> None:
    """An embed failure soft-deletes the just-inserted row and re-raises."""
    from lmchat.embedding import EmbeddingError

    await _insert_user(engine, 1)
    client = _make_embedding_client()
    # Realistic failure mode: the embedding model is not loaded.
    client.embed_batch = AsyncMock(
        side_effect=EmbeddingError("embedding model not loaded")
    )
    svc = _make_models_service()

    body = b"Content that will fail to embed on the first attempt."

    # 1. The original exception propagates out of upload_document.
    with pytest.raises(EmbeddingError):
        await upload_document(
            user_id=1,
            filename="orphan.txt",
            content_type="text/plain",
            body_bytes=body,
            engine=engine,
            embedding_client=client,
            models_service=svc,
        )

    # 2. The inserted row is soft-deleted (deleted_at set), so it is
    #    auditable but excluded from active listings.
    row = await _fetch_doc_row(engine, 1)
    assert row is not None, "the orphan row must remain for audit"
    assert row.deleted_at is not None, (
        "embed failure must soft-delete the orphaned row"
    )
    assert len(await list_documents(user_id=1, engine=engine)) == 0

    # 3. Re-uploading the SAME bytes must NOT return the dead row via the
    #    sha256 dedup; it re-inserts a fresh row and indexes it.
    client.embed_batch = AsyncMock(
        side_effect=lambda texts, model_id: [[0.1] * 4] * len(texts)
    )
    doc2 = await upload_document(
        user_id=1,
        filename="orphan.txt",
        content_type="text/plain",
        body_bytes=body,
        engine=engine,
        embedding_client=client,
        models_service=svc,
    )

    assert doc2.id != 1, "retry must create a fresh row, not reuse the dead one"
    assert doc2.chunk_count > 0, "retry must actually index the document"
    docs = await list_documents(user_id=1, engine=engine)
    assert len(docs) == 1
    assert docs[0].id == doc2.id


@pytest.mark.asyncio
async def test_upload_dimension_mismatch_soft_deletes_orphan(
    engine: AsyncEngine,
) -> None:
    """The dimension-mismatch RuntimeError path is also compensated."""
    await _insert_user(engine, 1)
    client = _make_embedding_client()
    # Return vectors of inconsistent dimension to trip the dimension guard.
    client.embed_batch = AsyncMock(
        side_effect=lambda texts, model_id: [
            [0.1] * (4 if i == 0 else 8) for i in range(len(texts))
        ]
    )
    svc = _make_models_service()

    body = b"First chunk. " + (b"More text to force a second chunk. " * 200)

    with pytest.raises(RuntimeError, match="dimension mismatch"):
        await upload_document(
            user_id=1,
            filename="mismatch.txt",
            content_type="text/plain",
            body_bytes=body,
            engine=engine,
            embedding_client=client,
            models_service=svc,
        )

    row = await _fetch_doc_row(engine, 1)
    assert row is not None
    assert row.deleted_at is not None, (
        "dimension-mismatch failure must also soft-delete the orphan"
    )
    assert len(await list_documents(user_id=1, engine=engine)) == 0
