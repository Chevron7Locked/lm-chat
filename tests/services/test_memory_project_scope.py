# SPDX-License-Identifier: Apache-2.0
"""project_id scoping on memory_service retrieval surfaces.

Covers three of the project-scoped retrieval surfaces:
- recall()
- list_pinned()
- recall_insights() (pinned branch + active-scoring branch)

The predicate invariant: project_id non-null → filter; project_id
None → no filter (legacy user-scoped union). All three sites share
the same shape.
"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import create_async_engine

from lmchat.db.schema import (
    chats,
    memory_insights,
    message_embeddings,
    messages,
    metadata,
    projects,
    users,
)
from lmchat.services.memory_service import MemoryService


async def _make_engine(tmp_path: Path):
    eng = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/memscope.db", pool_pre_ping=True
    )
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return eng


def _mock_models_service_with_embedding(model_id: str = "embed-1"):
    """Return a ModelsService stub that surfaces one embedding model."""
    from types import SimpleNamespace

    m = SimpleNamespace(key=model_id, type="embedding")
    svc = AsyncMock()
    svc.list_loaded = AsyncMock(return_value=[m])
    svc.refresh = AsyncMock()
    return svc


def _mock_embedding_client(vec: list[float]):
    """Embedding client always returning the same vector for any input."""
    c = AsyncMock()
    c.embed_one = AsyncMock(return_value=vec)
    c.embed_many = AsyncMock(return_value=[vec])
    return c


def _make_memory_service(eng) -> MemoryService:
    return MemoryService(
        engine=eng,
        embedding_client=_mock_embedding_client([1.0, 0.0]),
        models_service=_mock_models_service_with_embedding(),
    )


async def _insert_user(eng, username: str = "alice") -> int:
    async with eng.begin() as conn:
        r = await conn.execute(
            insert(users).values(
                username=username, password_hash="x"
            )
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


async def _insert_chat(
    eng, *, user_id: int, title: str, project_id: int | None
) -> int:
    async with eng.begin() as conn:
        r = await conn.execute(
            insert(chats).values(
                user_id=user_id,
                title=title,
                project_id=project_id,
            )
        )
        return int(r.inserted_primary_key[0])


async def _insert_message_with_embedding(
    eng,
    *,
    chat_id: int,
    content: str,
    embedding: bytes,
    embedding_model_id: str = "embed-1",
) -> int:
    from sqlalchemy.sql import insert as _insert

    async with eng.begin() as conn:
        msg_result = await conn.execute(
            _insert(messages).values(
                chat_id=chat_id,
                role="user",
                content=content,
            )
        )
        message_id = int(msg_result.inserted_primary_key[0])
        await conn.execute(
            _insert(message_embeddings).values(
                message_id=message_id,
                embedding_model_id=embedding_model_id,
                embedding=embedding,
                text_hash=f"h-{message_id}",
            )
        )
        return message_id


def _pack(vec: list[float]) -> bytes:
    """Pack a float list to bytes using the same convention as MemoryService."""
    import struct

    return struct.pack(f"<{len(vec)}f", *vec)


async def _insert_insight(
    eng,
    *,
    user_id: int,
    text: str,
    pinned: bool,
    project_id: int | None,
    state: str = "active",
) -> int:
    async with eng.begin() as conn:
        r = await conn.execute(
            insert(memory_insights).values(
                user_id=user_id,
                text=text,
                text_hash=f"h-{text}",
                pinned=pinned,
                category="context",
                state=state,
                project_id=project_id,
            )
        )
        return int(r.inserted_primary_key[0])


# ---------------------------------------------------------------------------
# recall()
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_recall_default_returns_user_scoped_union(
    tmp_path: Path,
) -> None:
    """project_id=None → every message the user owns (legacy behavior)."""
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        pid = await _insert_project(eng, user_id=uid)
        c_un = await _insert_chat(
            eng, user_id=uid, title="un", project_id=None
        )
        c_in = await _insert_chat(
            eng, user_id=uid, title="in", project_id=pid
        )
        vec = _pack([1.0, 0.0])
        await _insert_message_with_embedding(
            eng, chat_id=c_un, content="un-msg", embedding=vec
        )
        await _insert_message_with_embedding(
            eng, chat_id=c_in, content="in-msg", embedding=vec
        )
        svc = _make_memory_service(eng)
        out = await svc.recall(user_id=uid, query="x", top_k=10)
        contents = {h.content for h in out}
        assert contents == {"un-msg", "in-msg"}
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_recall_with_project_id_filters(tmp_path: Path) -> None:
    """project_id=X → only messages from chats in that project."""
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        pid = await _insert_project(eng, user_id=uid)
        c_un = await _insert_chat(
            eng, user_id=uid, title="un", project_id=None
        )
        c_in = await _insert_chat(
            eng, user_id=uid, title="in", project_id=pid
        )
        vec = _pack([1.0, 0.0])
        await _insert_message_with_embedding(
            eng, chat_id=c_un, content="un-msg", embedding=vec
        )
        await _insert_message_with_embedding(
            eng, chat_id=c_in, content="in-msg", embedding=vec
        )
        svc = _make_memory_service(eng)
        out = await svc.recall(
            user_id=uid, query="x", top_k=10, project_id=pid
        )
        contents = {h.content for h in out}
        assert contents == {"in-msg"}
    finally:
        await eng.dispose()


# ---------------------------------------------------------------------------
# list_pinned()
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_list_pinned_default_returns_all_pins(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        pid = await _insert_project(eng, user_id=uid)
        await _insert_insight(
            eng, user_id=uid, text="un", pinned=True, project_id=None
        )
        await _insert_insight(
            eng, user_id=uid, text="in", pinned=True, project_id=pid
        )
        svc = _make_memory_service(eng)
        out = await svc.list_pinned(uid)
        texts = {p.text for p in out}
        assert texts == {"un", "in"}
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_list_pinned_with_project_id_filters(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        pid = await _insert_project(eng, user_id=uid)
        await _insert_insight(
            eng, user_id=uid, text="un", pinned=True, project_id=None
        )
        await _insert_insight(
            eng, user_id=uid, text="in", pinned=True, project_id=pid
        )
        svc = _make_memory_service(eng)
        out = await svc.list_pinned(uid, project_id=pid)
        texts = {p.text for p in out}
        assert texts == {"in"}
    finally:
        await eng.dispose()


# ---------------------------------------------------------------------------
# recall_insights() — both pinned + active scoring branches
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_recall_insights_project_id_filters_both_branches(
    tmp_path: Path,
) -> None:
    """project_id gates BOTH the pinned branch AND the active-scoring branch.

    Without the filter on the active branch, scored (non-pinned)
    insights from other projects could leak in via the fan-out.
    """
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        pid = await _insert_project(eng, user_id=uid)
        # 1 pinned in each scope
        await _insert_insight(
            eng, user_id=uid, text="un-pin", pinned=True, project_id=None
        )
        await _insert_insight(
            eng, user_id=uid, text="in-pin", pinned=True, project_id=pid
        )
        # 1 active in each scope
        await _insert_insight(
            eng, user_id=uid, text="un-act", pinned=False,
            project_id=None,
        )
        await _insert_insight(
            eng, user_id=uid, text="in-act", pinned=False,
            project_id=pid,
        )
        svc = _make_memory_service(eng)
        out = await svc.recall_insights(
            user_id=uid, top_k=10, project_id=pid
        )
        texts = {s.text for s in out}
        # Only in-project items appear — and the un-project active
        # insight definitely doesn't leak in via the scored branch.
        assert texts <= {"in-pin", "in-act"}
        assert "un-pin" not in texts
        assert "un-act" not in texts
    finally:
        await eng.dispose()
