# SPDX-License-Identifier: Apache-2.0
"""chats.model_id seed on ChatService.create.

Pins the contract: when
``model_id`` kwarg is supplied, ChatService.create writes it into the
chats row on INSERT; when None, the column stays NULL (legacy).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from lmchat.db.schema import chats, metadata, users
from lmchat.services.chat_service import ChatService


async def _make_engine(tmp_path: Path):
    eng = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/b2_seed.db", pool_pre_ping=True
    )
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return eng


async def _insert_user(eng) -> int:
    from sqlalchemy import insert

    async with eng.begin() as conn:
        r = await conn.execute(
            insert(users).values(username="alice", password_hash="x")
        )
        return int(r.inserted_primary_key[0])


def _svc(eng) -> ChatService:
    return ChatService(
        engine=eng,
        memory_service=AsyncMock(),
        models_service=AsyncMock(),
        chat_locks={},
    )


@pytest.mark.asyncio
async def test_create_with_model_id_writes_to_chat_row(
    tmp_path: Path,
) -> None:
    """``model_id="qwen3.6-35b-a3b"`` on create lands in chats.model_id."""
    eng = await _make_engine(tmp_path)
    uid = await _insert_user(eng)
    svc = _svc(eng)

    chat = await svc.create(
        user_id=uid, title="t", model_id="qwen3.6-35b-a3b"
    )

    async with eng.connect() as conn:
        row = (
            await conn.execute(
                select(chats.c.model_id).where(chats.c.id == chat.id)
            )
        ).fetchone()
    assert row is not None and row[0] == "qwen3.6-35b-a3b"
    await eng.dispose()


@pytest.mark.asyncio
async def test_create_without_model_id_leaves_column_null(
    tmp_path: Path,
) -> None:
    """``model_id=None`` (default) preserves the legacy behavior:
    chats.model_id stays NULL on INSERT. Stream-time resolution picks
    up the user's global default — unchanged."""
    eng = await _make_engine(tmp_path)
    uid = await _insert_user(eng)
    svc = _svc(eng)

    chat = await svc.create(user_id=uid, title="t")

    async with eng.connect() as conn:
        row = (
            await conn.execute(
                select(chats.c.model_id).where(chats.c.id == chat.id)
            )
        ).fetchone()
    assert row is not None and row[0] is None
    await eng.dispose()


@pytest.mark.asyncio
async def test_create_with_model_id_and_project_id_composes(
    tmp_path: Path,
) -> None:
    """Both kwargs compose: chat carries project_id AND model_id."""
    import time as _time

    from sqlalchemy import insert

    from lmchat.db.schema import projects

    eng = await _make_engine(tmp_path)
    uid = await _insert_user(eng)
    async with eng.begin() as conn:
        p = await conn.execute(
            insert(projects).values(
                user_id=uid,
                name="P",
                description="",
                system_prompt="",

                created_at=_time.time(),
                updated_at=_time.time(),
            )
        )
        pk_p = p.inserted_primary_key
        assert pk_p is not None
        pid = int(pk_p[0])

    svc = _svc(eng)
    chat = await svc.create(
        user_id=uid,
        title="t",
        project_id=pid,
        model_id="qwen3.6-35b-a3b",
    )

    async with eng.connect() as conn:
        row = (
            await conn.execute(
                select(chats.c.project_id, chats.c.model_id).where(
                    chats.c.id == chat.id
                )
            )
        ).fetchone()
    assert row is not None
    assert row[0] == pid
    assert row[1] == "qwen3.6-35b-a3b"
    await eng.dispose()
