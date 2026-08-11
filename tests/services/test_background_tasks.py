# SPDX-License-Identifier: Apache-2.0
"""Unit tests for background_tasks.run_incognito_ttl_purge."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import create_async_engine

from lmchat.db.schema import chats, metadata, users
from lmchat.services._active_streams import mark_active, mark_inactive
from lmchat.services.background_tasks import run_incognito_ttl_purge

_EXPIRED_TTL_OFFSET_SEC = 3600  # expired an hour ago


async def _make_engine(tmp_path: Path):
    eng = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/bg_tasks.db", pool_pre_ping=True
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


async def _insert_expired_incognito_chat(eng, user_id: int, title: str) -> int:
    async with eng.begin() as conn:
        result = await conn.execute(
            insert(chats).values(
                user_id=user_id,
                title=title,
                incognito=1,
                incognito_expires_at=time.time() - _EXPIRED_TTL_OFFSET_SEC,
            )
        )
        return int(result.inserted_primary_key[0])


async def _run_one_purge_cycle(eng) -> None:
    """Wake run_incognito_ttl_purge for (at least) one iteration, then stop it."""
    task = asyncio.create_task(run_incognito_ttl_purge(eng, interval_sec=0))
    await asyncio.sleep(0.2)
    task.cancel()
    await task


@pytest.mark.anyio
async def test_incognito_purge_skips_chat_with_active_stream(
    tmp_path: Path,
) -> None:
    """An expired incognito chat with a live stream must survive the purge.

    Deleting an incognito chat mid-turn kills a live conversation. The
    purge must consult the process-local active-stream registry
    (_active_streams.py) the same way the stream reaper does
    (_stream_reaper.py._finalize_stuck_drafts), skipping any chat_id
    that is in-flight — while still purging every other expired row.
    """
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        live_chat_id = await _insert_expired_incognito_chat(eng, uid, "live")
        dead_chat_id = await _insert_expired_incognito_chat(eng, uid, "dead")
        mark_active(live_chat_id)
        try:
            await _run_one_purge_cycle(eng)
        finally:
            mark_inactive(live_chat_id)

        async with eng.connect() as conn:
            remaining = (await conn.execute(select(chats.c.id))).scalars().all()
        assert live_chat_id in remaining
        assert dead_chat_id not in remaining
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_incognito_purge_deletes_expired_chat_with_no_active_stream(
    tmp_path: Path,
) -> None:
    """Baseline: an expired incognito chat with NO active stream is purged."""
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        chat_id = await _insert_expired_incognito_chat(eng, uid, "dead")
        await _run_one_purge_cycle(eng)

        async with eng.connect() as conn:
            remaining = (await conn.execute(select(chats.c.id))).scalars().all()
        assert chat_id not in remaining
    finally:
        await eng.dispose()
