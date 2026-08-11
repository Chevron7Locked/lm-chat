# SPDX-License-Identifier: Apache-2.0
"""Last-admin lockout guard (audit 2026-07-11).

`_is_last_admin` blocks demote/delete operations that would leave the
deployment with zero admins — an unrecoverable lockout (registration is closed
and POST /api/admin/invite itself requires admin). Every invite grants full
admin, so this helper is the sole safety net.

Tested at the helper level with a controlled DB because the integration harness
shares one backend where many admins accumulate (so the "sole admin" state
can't be constructed there).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import metadata
from lmchat.db.schema import users as users_table
from lmchat.routes.admin import _is_last_admin, _not_sole_admin_clause


@pytest.fixture()
async def engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'guard.db'}", pool_pre_ping=True)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


async def _add_user(engine: AsyncEngine, uid: int, *, is_admin: bool) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, username, password_hash, is_admin)"
                " VALUES (:id, :u, 'scrypt$dummy', :a)"
            ),
            {"id": uid, "u": f"u{uid}", "a": 1 if is_admin else 0},
        )


async def test_sole_admin_is_last_admin(engine: AsyncEngine) -> None:
    """The only admin IS the last admin → demote/delete must be blocked."""
    await _add_user(engine, 1, is_admin=True)
    assert await _is_last_admin(engine, 1) is True


async def test_two_admins_neither_is_last(engine: AsyncEngine) -> None:
    """With two admins, demoting either still leaves one → not blocked."""
    await _add_user(engine, 1, is_admin=True)
    await _add_user(engine, 2, is_admin=True)
    assert await _is_last_admin(engine, 1) is False
    assert await _is_last_admin(engine, 2) is False


async def test_regular_user_is_not_last_admin(engine: AsyncEngine) -> None:
    """A non-admin is never 'the last admin', even when only one admin exists."""
    await _add_user(engine, 1, is_admin=True)
    await _add_user(engine, 2, is_admin=False)
    assert await _is_last_admin(engine, 2) is False


async def test_unknown_user_is_not_last_admin(engine: AsyncEngine) -> None:
    """An id that doesn't exist is not the last admin (no false-positive block)."""
    await _add_user(engine, 1, is_admin=True)
    assert await _is_last_admin(engine, 99999) is False


# --- Atomic backstop (race-safe) — _not_sole_admin_clause -------------------
# The pre-check (_is_last_admin) is check-then-act; these verify the atomic
# WHERE fragment that closes the TOCTOU (two concurrent demotions to zero).


async def test_atomic_clause_blocks_demoting_sole_admin(engine: AsyncEngine) -> None:
    """A demotion UPDATE carrying the clause touches ZERO rows for the sole
    admin — so even if two demotions raced past the pre-check, the second's
    UPDATE (under the write lock) would see count==1 and refuse."""
    await _add_user(engine, 1, is_admin=True)
    async with engine.begin() as conn:
        result = await conn.execute(
            update(users_table)
            .where(users_table.c.id == 1, _not_sole_admin_clause())
            .values(is_admin=False)
        )
    assert result.rowcount == 0  # blocked — sole admin
    assert await _is_last_admin(engine, 1) is True  # still an admin


async def test_atomic_clause_allows_demotion_when_another_admin_exists(
    engine: AsyncEngine,
) -> None:
    """With two admins, the clause allows demoting one (rowcount 1)."""
    await _add_user(engine, 1, is_admin=True)
    await _add_user(engine, 2, is_admin=True)
    async with engine.begin() as conn:
        result = await conn.execute(
            update(users_table)
            .where(users_table.c.id == 1, _not_sole_admin_clause())
            .values(is_admin=False)
        )
    assert result.rowcount == 1  # allowed — another admin remains
