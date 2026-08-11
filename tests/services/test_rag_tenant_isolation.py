# SPDX-License-Identifier: Apache-2.0
"""Regression tests for RAG tenant isolation.

Verifies that user A cannot retrieve user B's document chunks even
when guessing the document PK or chunk id directly.
"""
from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import alembic.command
import alembic.config
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.services.documents_service import (
    DocumentNotFoundError,
    delete_document,
    get_document_chunks,
    list_documents,
)
from lmchat.services.models_service import Capabilities, ModelInfo
from lmchat.services.retrieval_service import retrieve

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _run_upgrade(db_url: str) -> None:
    ini = _REPO_ROOT / "alembic.ini"
    cfg = alembic.config.Config(str(ini))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    alembic.command.upgrade(cfg, "head")


@pytest.fixture()
async def engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    db_path = tmp_path / "test_tenant.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"
    await asyncio.to_thread(_run_upgrade, db_url)
    eng = create_async_engine(db_url, pool_pre_ping=True)
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


def _make_models_service() -> MagicMock:
    svc = MagicMock()
    info = ModelInfo(
        # Canonical default (nomic): the no-preference resolver now requires the
        # loaded embedder to BE the default, so use its full catalog key here.
        key="text-embedding-nomic-embed-text-v1.5",
        type="embedding",
        # loaded_instance_ids mirrors LM Studio's live response: the wire id
        # equals the catalog key for non-quantised test models.
        loaded_instance_ids=["text-embedding-nomic-embed-text-v1.5"],
        capabilities=Capabilities(vision=False, trained_for_tool_use=False),
    )
    svc.list_loaded = AsyncMock(return_value=[info])
    # Keep auth_failed False so the force-reprobe branch is skipped.
    svc.auth_failed = False
    return svc


def _make_embedding_client() -> MagicMock:
    client = MagicMock()
    client.embed_batch = AsyncMock(side_effect=lambda texts, model_id: [[0.1] * 4] * len(texts))
    client.embed_one = AsyncMock(return_value=[0.1, 0.0, 0.0, 0.0])
    return client


@pytest.mark.asyncio
async def test_list_documents_tenant_isolation(engine: AsyncEngine) -> None:
    """list_documents only returns documents owned by the requesting user."""
    from lmchat.services.documents_service import upload_document

    await _insert_user(engine, 1)
    await _insert_user(engine, 2)

    client = _make_embedding_client()
    svc = _make_models_service()

    await upload_document(
        user_id=1,
        filename="user1.txt",
        content_type="text/plain",
        body_bytes=b"User 1 document.",
        engine=engine,
        embedding_client=client,
        models_service=svc,
    )
    await upload_document(
        user_id=2,
        filename="user2.txt",
        content_type="text/plain",
        body_bytes=b"User 2 document.",
        engine=engine,
        embedding_client=client,
        models_service=svc,
    )

    user1_docs = await list_documents(user_id=1, engine=engine)
    user2_docs = await list_documents(user_id=2, engine=engine)

    assert len(user1_docs) == 1
    assert user1_docs[0].title == "user1.txt"
    assert len(user2_docs) == 1
    assert user2_docs[0].title == "user2.txt"

    # Guessing document_id: user 2 cannot see user 1's chunks.
    user1_doc_id = user1_docs[0].id
    with pytest.raises(DocumentNotFoundError):
        await get_document_chunks(
            document_id=user1_doc_id,
            user_id=2,
            engine=engine,
        )


@pytest.mark.asyncio
async def test_delete_another_users_document_raises(engine: AsyncEngine) -> None:
    """Attempting to delete another user's document raises DocumentNotFoundError."""
    from lmchat.services.documents_service import upload_document

    await _insert_user(engine, 1)
    await _insert_user(engine, 2)

    client = _make_embedding_client()
    svc = _make_models_service()

    doc = await upload_document(
        user_id=1,
        filename="private.txt",
        content_type="text/plain",
        body_bytes=b"Private content.",
        engine=engine,
        embedding_client=client,
        models_service=svc,
    )

    with pytest.raises(DocumentNotFoundError):
        await delete_document(document_id=doc.id, user_id=2, engine=engine)

    # Doc still accessible by owner.
    docs = await list_documents(user_id=1, engine=engine)
    assert len(docs) == 1


@pytest.mark.asyncio
async def test_retrieve_tenant_isolation_guessed_doc(engine: AsyncEngine) -> None:
    """retrieve never returns chunks from another user even with matching vectors."""
    from lmchat.services.documents_service import upload_document

    await _insert_user(engine, 1)
    await _insert_user(engine, 2)

    client = _make_embedding_client()
    svc = _make_models_service()

    # User 1 uploads a document.
    await upload_document(
        user_id=1,
        filename="secret.txt",
        content_type="text/plain",
        body_bytes=b"Top secret confidential data.",
        engine=engine,
        embedding_client=client,
        models_service=svc,
    )

    # User 2 tries to retrieve — must get nothing.
    hits = await retrieve(
        query="secret confidential",
        user_id=2,
        top_k=10,
        engine=engine,
        embedding_client=client,
        models_service=svc,
    )
    assert hits == [], "User 2 must not retrieve User 1's document chunks"
