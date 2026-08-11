# SPDX-License-Identifier: Apache-2.0
"""Tests for ProjectsService minimum name length (§1E — 54-"P" project gap).

Covers:
- ProjectsService.create(name="P") raises InvalidProjectFieldError
- ProjectsService.create(name="abc") succeeds
"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import metadata
from lmchat.services.projects_service import (
    InvalidProjectFieldError,
    ProjectsService,
)
from lmchat.utils.hashing import hash_password

_LOW_N: int = 2**10


async def _engine_for(tmp_path: Path) -> AsyncEngine:
    db_url = f"sqlite+aiosqlite:///{tmp_path}/projects_min_name_test.db"
    eng = create_async_engine(db_url, pool_pre_ping=True)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return eng


async def _insert_user(eng: AsyncEngine) -> int:
    pw_hash = hash_password("test-pw", n=_LOW_N, r=8, p=1)
    async with eng.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (username, password_hash, is_admin) "
                "VALUES (:u, :pw, 0)"
            ),
            {"u": "test_user", "pw": pw_hash},
        )
        row = (
            await conn.execute(
                text("SELECT id FROM users WHERE username = :u"),
                {"u": "test_user"},
            )
        ).fetchone()
        return int(row[0])  # type: ignore[index]


@pytest.mark.anyio
async def test_create_rejects_single_char_P(tmp_path: Path) -> None:
    """create(name='P') raises InvalidProjectFieldError (min_length=3)."""
    eng = await _engine_for(tmp_path)
    try:
        uid = await _insert_user(eng)
        svc = ProjectsService(engine=eng)
        with pytest.raises(InvalidProjectFieldError):
            await svc.create(user_id=uid, name="P")
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_create_accepts_three_char_abc(tmp_path: Path) -> None:
    """create(name='abc') succeeds (meets min_length=3)."""
    eng = await _engine_for(tmp_path)
    try:
        uid = await _insert_user(eng)
        svc = ProjectsService(engine=eng)
        project = await svc.create(user_id=uid, name="abc")
        assert project.name == "abc"
    finally:
        await eng.dispose()