# SPDX-License-Identifier: Apache-2.0
"""Unit tests for FolderService."""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import create_async_engine

from lmchat.db.schema import chats, metadata, users
from lmchat.services.folder_service import (
    FolderConflictError,
    FolderService,
    InvalidFolderNameError,
)


async def _make_engine(tmp_path: Path):
    eng = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/folders.db", pool_pre_ping=True
    )
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return eng


async def _insert_user(eng, username: str = "alice") -> int:
    async with eng.begin() as conn:
        result = await conn.execute(
            insert(users).values(username=username, password_hash="scrypt$dummy")
        )
        return int(result.inserted_primary_key[0])


@pytest.mark.anyio
async def test_list_folders_empty_for_new_user(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        svc = FolderService(engine=eng)
        assert await svc.list_folders(user_id=uid) == []
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_add_folder_persists_in_prefs(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        svc = FolderService(engine=eng)
        out = await svc.add_folder(user_id=uid, name="Drafts")
        assert out == ["Drafts"]
        assert await svc.list_folders(user_id=uid) == ["Drafts"]
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_list_unions_chats_and_prefs(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        async with eng.begin() as conn:
            await conn.execute(
                insert(chats).values(user_id=uid, title="c1", folder="Work")
            )
            await conn.execute(
                insert(chats).values(user_id=uid, title="c2", folder=None)
            )
        svc = FolderService(engine=eng)
        await svc.add_folder(user_id=uid, name="Archive")
        out = await svc.list_folders(user_id=uid)
        assert out == ["Archive", "Work"]
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_rename_migrates_chats_and_prefs(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        async with eng.begin() as conn:
            await conn.execute(
                insert(chats).values(user_id=uid, title="c1", folder="Old")
            )
            await conn.execute(
                insert(chats).values(user_id=uid, title="c2", folder="Old")
            )
        svc = FolderService(engine=eng)
        out = await svc.rename_folder(
            user_id=uid, old_name="Old", new_name="New"
        )
        assert out == ["New"]
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_rename_collision_raises_FolderConflictError(tmp_path: Path) -> None:
    """Rename refuses to silently merge into an existing folder.

    User has two folders 'Personal' and 'Work'; renaming Personal → Work must
    raise FolderConflictError (route layer surfaces 409) rather than silently
    merging the two via set deduplication.
    """

    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        async with eng.begin() as conn:
            await conn.execute(
                insert(chats).values(user_id=uid, title="c1", folder="Personal")
            )
            await conn.execute(
                insert(chats).values(user_id=uid, title="c2", folder="Work")
            )
        svc = FolderService(engine=eng)

        with pytest.raises(FolderConflictError):
            await svc.rename_folder(
                user_id=uid, old_name="Personal", new_name="Work"
            )

        # And confirm both folders are intact (no destructive side effect).
        result = await svc.list_folders(user_id=uid)
        assert sorted(result) == ["Personal", "Work"]
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_rename_to_same_name_is_no_op(tmp_path: Path) -> None:
    """Renaming X → X must NOT raise FolderConflictError (trivial no-op)."""
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        async with eng.begin() as conn:
            await conn.execute(
                insert(chats).values(user_id=uid, title="c1", folder="X")
            )
        svc = FolderService(engine=eng)
        out = await svc.rename_folder(user_id=uid, old_name="X", new_name="X")
        assert "X" in out
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_delete_unfolders_chats(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        async with eng.begin() as conn:
            await conn.execute(
                insert(chats).values(user_id=uid, title="c1", folder="X")
            )
        svc = FolderService(engine=eng)
        await svc.add_folder(user_id=uid, name="X")
        out = await svc.delete_folder(user_id=uid, name="X")
        assert out == []
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_add_folder_validates_name(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        svc = FolderService(engine=eng)
        with pytest.raises(InvalidFolderNameError):
            await svc.add_folder(user_id=uid, name="")
        with pytest.raises(InvalidFolderNameError):
            await svc.add_folder(user_id=uid, name="   ")
        with pytest.raises(InvalidFolderNameError):
            await svc.add_folder(user_id=uid, name="x" * 200)
        with pytest.raises(InvalidFolderNameError):
            await svc.add_folder(user_id=uid, name="a\nb")
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_folders_isolated_per_user(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        alice = await _insert_user(eng, "alice")
        bob = await _insert_user(eng, "bob")
        svc = FolderService(engine=eng)
        await svc.add_folder(user_id=alice, name="A")
        assert await svc.list_folders(user_id=alice) == ["A"]
        assert await svc.list_folders(user_id=bob) == []
    finally:
        await eng.dispose()


# The per-project folder
# methods + ``_ProjectFolderSource`` were REMOVED. All tests that
# exercised them (the 9 deleted from this file) used
# ``list_folders_for_project`` / ``add_folder_to_project`` /
# ``rename_folder_in_project`` / ``delete_folder_from_project`` —
# none of which exist now. The route-layer 410 GONE contract is
# tested in ``tests/routes/test_folders_project_410.py``.
