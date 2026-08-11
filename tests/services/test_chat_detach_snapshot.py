# SPDX-License-Identifier: Apache-2.0
"""chats.detached_from_project_meta snapshot at move-out time.

The snapshot's shape:

    {project_id, name, detached_at, system_prompt_hash}

* Hash only — the system_prompt itself is kilobytes-per-chat at scale.
* Captured at detach time so the separator-turn UI can still render
  "Detached from X on Y" after the project is later deleted.
* Read + snapshot build + UPDATE all happen in ONE engine.begin()
  block. Tests run against the
  real ``ProjectsService`` so the in-transaction call path is
  exercised end-to-end, not stubbed.
"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import create_async_engine

from lmchat.db.schema import chats, metadata, projects, users
from lmchat.services.chat_service import ChatService
from lmchat.services.projects_service import ProjectsService


async def _make_engine(tmp_path: Path):
    eng = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/detach_snapshot.db",
        pool_pre_ping=True,
    )
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return eng


def _chat_svc(engine) -> ChatService:
    return ChatService(
        engine=engine,
        memory_service=AsyncMock(),
        models_service=AsyncMock(),
        chat_locks={},
    )


def _projects_svc(engine) -> ProjectsService:
    return ProjectsService(engine=engine)


async def _seed(engine) -> tuple[int, int, int]:
    """User + project (system_prompt='You are the project-x persona.') + chat in project."""
    async with engine.begin() as conn:
        u = await conn.execute(
            insert(users).values(username="alice", password_hash="x")
        )
        uid = int(u.inserted_primary_key[0])
        now = time.time()
        p = await conn.execute(
            insert(projects).values(
                user_id=uid,
                name="ProjX",
                description="",
                system_prompt="You are the project-x persona.",
                created_at=now,
                updated_at=now,
            )
        )
        pid = int(p.inserted_primary_key[0])
        c = await conn.execute(
            insert(chats).values(
                user_id=uid, title="t", project_id=pid
            )
        )
        cid = int(c.inserted_primary_key[0])
    return uid, pid, cid


async def _read_detach_meta(eng, chat_id: int) -> dict | None:
    async with eng.connect() as conn:
        row = (
            await conn.execute(
                select(
                    chats.c.project_id,
                    chats.c.detached_from_project_meta,
                ).where(chats.c.id == chat_id)
            )
        ).fetchone()
    assert row is not None
    return row.detached_from_project_meta


# ─── Tests ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_detach_writes_snapshot_with_hash_not_full_prompt(
    tmp_path: Path,
) -> None:
    """Detach (project_id=None) writes a snapshot whose
    system_prompt_hash matches sha256 of the project's
    system_prompt. The full text is NOT stored."""
    from hashlib import sha256

    eng = await _make_engine(tmp_path)
    uid, pid, cid = await _seed(eng)
    svc = _chat_svc(eng)
    ps = _projects_svc(eng)

    await svc.set_project_id(
        cid, user_id=uid, project_id=None, projects_service=ps
    )

    meta = await _read_detach_meta(eng, cid)
    assert isinstance(meta, dict), f"meta not written: {meta!r}"
    assert meta["project_id"] == pid
    assert meta["name"] == "ProjX"
    assert isinstance(meta["detached_at"], (int, float))

    expected_hash = (
        "sha256:"
        + sha256(
            b"You are the project-x persona."
        ).hexdigest()
    )
    assert meta["system_prompt_hash"] == expected_hash, (
        f"hash mismatch: {meta['system_prompt_hash']!r} vs "
        f"{expected_hash!r}"
    )

    # Crucially — the full prompt is NOT stored.
    assert "persona" not in str(meta).lower(), (
        f"full prompt leaked into meta: {meta!r}"
    )
    await eng.dispose()


@pytest.mark.asyncio
async def test_attach_between_projects_does_NOT_write_snapshot(
    tmp_path: Path,
) -> None:
    """Move-between-projects (e.g. PA → PB) does NOT write the
    detach snapshot — the chat's NEW project_id remains
    discoverable. Snapshot is only for move-to-None."""
    eng = await _make_engine(tmp_path)
    uid, _, cid = await _seed(eng)
    svc = _chat_svc(eng)
    ps = _projects_svc(eng)

    # Create a 2nd project to move into.
    async with eng.begin() as conn:
        p2 = await conn.execute(
            insert(projects).values(
                user_id=uid,
                name="ProjY",
                description="",
                system_prompt="",
                created_at=time.time(),
                updated_at=time.time(),
            )
        )
        pk_p2 = p2.inserted_primary_key
        assert pk_p2 is not None
        pid2 = int(pk_p2[0])

    await svc.set_project_id(
        cid, user_id=uid, project_id=pid2, projects_service=ps
    )

    meta = await _read_detach_meta(eng, cid)
    assert meta is None, (
        f"attach should not write detach snapshot: {meta!r}"
    )
    await eng.dispose()


@pytest.mark.asyncio
async def test_detach_unprojected_chat_writes_nothing(
    tmp_path: Path,
) -> None:
    """An un-projected chat being 're-detached' (project_id stays
    None) writes no snapshot — there's nothing to capture."""
    eng = await _make_engine(tmp_path)
    async with eng.begin() as conn:
        u = await conn.execute(
            insert(users).values(username="alice", password_hash="x")
        )
        pk_u = u.inserted_primary_key
        assert pk_u is not None
        uid = int(pk_u[0])
        c = await conn.execute(
            insert(chats).values(
                user_id=uid, title="t", project_id=None
            )
        )
        pk_c = c.inserted_primary_key
        assert pk_c is not None
        cid = int(pk_c[0])

    svc = _chat_svc(eng)
    ps = _projects_svc(eng)
    await svc.set_project_id(
        cid, user_id=uid, project_id=None, projects_service=ps
    )

    meta = await _read_detach_meta(eng, cid)
    assert meta is None
    await eng.dispose()


@pytest.mark.asyncio
async def test_detach_without_projects_service_skips_snapshot_gracefully(
    tmp_path: Path,
) -> None:
    """When the caller doesn't supply ``projects_service`` (legacy
    test fixture), the project_id clear still proceeds but the
    snapshot is skipped (with a warning log). No data corruption."""
    eng = await _make_engine(tmp_path)
    uid, _, cid = await _seed(eng)
    svc = _chat_svc(eng)

    await svc.set_project_id(
        cid, user_id=uid, project_id=None, projects_service=None
    )

    async with eng.connect() as conn:
        row = (
            await conn.execute(
                select(
                    chats.c.project_id,
                    chats.c.detached_from_project_meta,
                ).where(chats.c.id == cid)
            )
        ).fetchone()
    assert row is not None
    assert row.project_id is None  # clear still happened
    assert row.detached_from_project_meta is None  # no snapshot
    await eng.dispose()
