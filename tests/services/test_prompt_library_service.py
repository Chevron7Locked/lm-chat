# SPDX-License-Identifier: Apache-2.0
"""Unit tests for PromptLibraryService.

Covers:
- list_for_user: empty, populated, cross-user isolation.
- create: happy path, name conflict raises PromptNameConflictError.
- update: rename, content change, no-op (neither field), name conflict.
- delete: happy path, cross-user 404.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import metadata, users
from lmchat.services.prompt_library_service import (
    PromptLibraryService,
    PromptNameConflictError,
    PromptNotFoundError,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def db_engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    db_path = tmp_path / "test_prompts.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}", pool_pre_ping=True)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


async def _insert_user(engine: AsyncEngine, username: str) -> int:
    async with engine.begin() as conn:
        result = await conn.execute(
            insert(users).values(username=username, password_hash="x")
        )
        pk = result.inserted_primary_key
        assert pk is not None
        return int(pk[0])


# ---------------------------------------------------------------------------
# list_for_user
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_empty(db_engine: AsyncEngine) -> None:
    uid = await _insert_user(db_engine, "alice")
    svc = PromptLibraryService(engine=db_engine)
    result = await svc.list_for_user(uid)
    assert result == []


@pytest.mark.asyncio
async def test_list_populated_sorted_by_name(db_engine: AsyncEngine) -> None:
    uid = await _insert_user(db_engine, "bob")
    svc = PromptLibraryService(engine=db_engine)
    await svc.create(user_id=uid, name="zebra", content="z")
    await svc.create(user_id=uid, name="alpha", content="a")
    result = await svc.list_for_user(uid)
    assert [p.name for p in result] == ["alpha", "zebra"]


@pytest.mark.asyncio
async def test_list_cross_user_isolation(db_engine: AsyncEngine) -> None:
    uid_a = await _insert_user(db_engine, "carol")
    uid_b = await _insert_user(db_engine, "dave")
    svc = PromptLibraryService(engine=db_engine)
    await svc.create(user_id=uid_a, name="p1", content="c")
    result_b = await svc.list_for_user(uid_b)
    assert result_b == []


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_happy_path(db_engine: AsyncEngine) -> None:
    uid = await _insert_user(db_engine, "eve")
    svc = PromptLibraryService(engine=db_engine)
    p = await svc.create(user_id=uid, name="greeting", content="Hello!")
    assert p.id > 0
    assert p.user_id == uid
    assert p.name == "greeting"
    assert p.content == "Hello!"


@pytest.mark.asyncio
async def test_create_name_conflict_raises(db_engine: AsyncEngine) -> None:
    uid = await _insert_user(db_engine, "frank")
    svc = PromptLibraryService(engine=db_engine)
    await svc.create(user_id=uid, name="dup", content="first")
    with pytest.raises(PromptNameConflictError):
        await svc.create(user_id=uid, name="dup", content="second")


@pytest.mark.asyncio
async def test_create_same_name_different_user_ok(db_engine: AsyncEngine) -> None:
    uid_a = await _insert_user(db_engine, "grace")
    uid_b = await _insert_user(db_engine, "henry")
    svc = PromptLibraryService(engine=db_engine)
    await svc.create(user_id=uid_a, name="shared", content="a")
    # Should NOT raise for a different user.
    p = await svc.create(user_id=uid_b, name="shared", content="b")
    assert p.user_id == uid_b


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_name(db_engine: AsyncEngine) -> None:
    uid = await _insert_user(db_engine, "iris")
    svc = PromptLibraryService(engine=db_engine)
    p = await svc.create(user_id=uid, name="old", content="c")
    updated = await svc.update(p.id, user_id=uid, name="new")
    assert updated.name == "new"
    assert updated.content == "c"


@pytest.mark.asyncio
async def test_update_content(db_engine: AsyncEngine) -> None:
    uid = await _insert_user(db_engine, "jack")
    svc = PromptLibraryService(engine=db_engine)
    p = await svc.create(user_id=uid, name="n", content="old content")
    updated = await svc.update(p.id, user_id=uid, content="new content")
    assert updated.content == "new content"


@pytest.mark.asyncio
async def test_update_noop_returns_prompt(db_engine: AsyncEngine) -> None:
    uid = await _insert_user(db_engine, "kate")
    svc = PromptLibraryService(engine=db_engine)
    p = await svc.create(user_id=uid, name="n", content="c")
    result = await svc.update(p.id, user_id=uid)
    assert result.id == p.id


@pytest.mark.asyncio
async def test_update_cross_user_404(db_engine: AsyncEngine) -> None:
    uid_a = await _insert_user(db_engine, "liam")
    uid_b = await _insert_user(db_engine, "mia")
    svc = PromptLibraryService(engine=db_engine)
    p = await svc.create(user_id=uid_a, name="n", content="c")
    with pytest.raises(PromptNotFoundError):
        await svc.update(p.id, user_id=uid_b, name="hacked")


@pytest.mark.asyncio
async def test_update_name_conflict_raises(db_engine: AsyncEngine) -> None:
    uid = await _insert_user(db_engine, "noah")
    svc = PromptLibraryService(engine=db_engine)
    await svc.create(user_id=uid, name="taken", content="c")
    p = await svc.create(user_id=uid, name="other", content="c")
    with pytest.raises(PromptNameConflictError):
        await svc.update(p.id, user_id=uid, name="taken")


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_happy_path(db_engine: AsyncEngine) -> None:
    uid = await _insert_user(db_engine, "olivia")
    svc = PromptLibraryService(engine=db_engine)
    p = await svc.create(user_id=uid, name="bye", content="x")
    await svc.delete(p.id, user_id=uid)
    result = await svc.list_for_user(uid)
    assert result == []


@pytest.mark.asyncio
async def test_delete_cross_user_404(db_engine: AsyncEngine) -> None:
    uid_a = await _insert_user(db_engine, "peter")
    uid_b = await _insert_user(db_engine, "quinn")
    svc = PromptLibraryService(engine=db_engine)
    p = await svc.create(user_id=uid_a, name="n", content="c")
    with pytest.raises(PromptNotFoundError):
        await svc.delete(p.id, user_id=uid_b)
