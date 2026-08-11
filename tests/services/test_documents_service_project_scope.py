# SPDX-License-Identifier: Apache-2.0
"""list_documents project_id / unscoped filter."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import create_async_engine

from lmchat.db.schema import documents, metadata, projects, users
from lmchat.services.documents_service import list_documents


async def _make_engine(tmp_path: Path):
    eng = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/docscope.db", pool_pre_ping=True
    )
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return eng


async def _insert_user(eng, username: str = "alice") -> int:
    async with eng.begin() as conn:
        result = await conn.execute(
            insert(users).values(username=username, password_hash="x")
        )
        return int(result.inserted_primary_key[0])


async def _insert_project(eng, *, user_id: int, name: str = "P") -> int:
    async with eng.begin() as conn:
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
        return int(result.inserted_primary_key[0])


async def _insert_doc(
    eng, *, user_id: int, title: str, project_id: int | None
) -> int:
    async with eng.begin() as conn:
        result = await conn.execute(
            insert(documents).values(
                user_id=user_id,
                title=title,
                mime_type="text/plain",
                byte_size=1,
                chunk_count=0,
                embedding_model_id="",
                sha256=f"sha-{title}",
                project_id=project_id,
            )
        )
        return int(result.inserted_primary_key[0])


@pytest.mark.anyio
async def test_default_returns_user_scoped_union(tmp_path: Path) -> None:
    """project_id=None + unscoped=False → return every doc the user owns."""
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        pid = await _insert_project(eng, user_id=uid)
        await _insert_doc(eng, user_id=uid, title="un", project_id=None)
        await _insert_doc(eng, user_id=uid, title="in", project_id=pid)
        docs = await list_documents(user_id=uid, engine=eng)
        titles = {d.title for d in docs}
        assert titles == {"un", "in"}
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_project_id_filters(tmp_path: Path) -> None:
    """project_id=X → only docs in that project."""
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        p1 = await _insert_project(eng, user_id=uid, name="P1")
        p2 = await _insert_project(eng, user_id=uid, name="P2")
        await _insert_doc(eng, user_id=uid, title="un", project_id=None)
        await _insert_doc(eng, user_id=uid, title="a", project_id=p1)
        await _insert_doc(eng, user_id=uid, title="b", project_id=p2)
        docs = await list_documents(
            user_id=uid, engine=eng, project_id=p1
        )
        titles = {d.title for d in docs}
        assert titles == {"a"}
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_unscoped_returns_only_unprojected(tmp_path: Path) -> None:
    """unscoped=True + project_id=None → only docs with project_id IS NULL."""
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        pid = await _insert_project(eng, user_id=uid)
        await _insert_doc(eng, user_id=uid, title="un", project_id=None)
        await _insert_doc(eng, user_id=uid, title="in", project_id=pid)
        docs = await list_documents(
            user_id=uid, engine=eng, unscoped=True
        )
        titles = {d.title for d in docs}
        assert titles == {"un"}
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_project_id_precedence_over_unscoped(tmp_path: Path) -> None:
    """When project_id is set, unscoped is ignored — exact match wins."""
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        pid = await _insert_project(eng, user_id=uid)
        await _insert_doc(eng, user_id=uid, title="un", project_id=None)
        await _insert_doc(eng, user_id=uid, title="in", project_id=pid)
        docs = await list_documents(
            user_id=uid,
            engine=eng,
            project_id=pid,
            unscoped=True,
        )
        titles = {d.title for d in docs}
        assert titles == {"in"}
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_user_isolation_holds_in_project_filter(tmp_path: Path) -> None:
    """Project filter doesn't leak across users."""
    eng = await _make_engine(tmp_path)
    try:
        alice = await _insert_user(eng, "alice")
        bob = await _insert_user(eng, "bob")
        a_proj = await _insert_project(eng, user_id=alice)
        b_proj = await _insert_project(eng, user_id=bob)
        await _insert_doc(
            eng, user_id=alice, title="a-doc", project_id=a_proj
        )
        await _insert_doc(
            eng, user_id=bob, title="b-doc", project_id=b_proj
        )
        # Bob asking for Alice's project_id returns nothing (no docs
        # match the JOIN: documents.user_id=bob AND project_id=a_proj).
        docs = await list_documents(
            user_id=bob, engine=eng, project_id=a_proj
        )
        assert docs == []
    finally:
        await eng.dispose()
