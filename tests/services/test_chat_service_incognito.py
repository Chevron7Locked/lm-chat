# SPDX-License-Identifier: Apache-2.0
"""P13i — ChatService incognito-mode unit tests.

Covers the service-layer half of audit O-04 (incognito chats):

- ``create(incognito=True)`` sets ``chats.incognito=1`` and a non-NULL
  ``incognito_expires_at`` (UTC seconds + TTL).
- ``set_incognito`` toggles the flag ONLY when no messages exist;
  raises ``ValueError`` once at least one message row is present
  (privacy invariant: P13i scope §1, R-15).
- ``is_shareable`` returns False for incognito chats, True otherwise
  (P13i scope §7 hook for P13j).
- ``purge_user_incognito`` deletes every incognito chat for a user
  and leaves non-incognito chats alone (logout-sweep contract).
"""
from __future__ import annotations

import time
from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.pragmas import apply_sqlite_pragmas
from lmchat.db.schema import chats, metadata
from lmchat.services.chat_service import ChatService

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures (mirror tests/services/test_chat_service.py)
# ---------------------------------------------------------------------------


@pytest.fixture()
async def engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    """Per-test SQLite engine with FK enforcement enabled."""
    db_path = tmp_path / "test_chat_service_incognito.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}", pool_pre_ping=True)

    @event.listens_for(eng.sync_engine, "connect")
    def _on_connect(dbapi_conn: object, _rec: object) -> None:
        apply_sqlite_pragmas(dbapi_conn)

    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


async def _insert_user(eng: AsyncEngine, user_id: int = 1, username: str = "alice") -> None:
    async with eng.begin() as conn:
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO users (id, username, password_hash)"
                " VALUES (:id, :u, :ph)"
            ),
            {"id": user_id, "u": username, "ph": "scrypt$dummy"},
        )


def _make_service(eng: AsyncEngine) -> ChatService:
    memory_mock = MagicMock()
    memory_mock.handle_message_deleted = AsyncMock(return_value=None)
    models_mock = MagicMock()
    models_mock.get_capabilities = AsyncMock(side_effect=KeyError("no model"))
    return ChatService(
        engine=eng,
        memory_service=memory_mock,
        models_service=models_mock,
        chat_locks={},
    )


async def _insert_message(eng: AsyncEngine, chat_id: int, content: str = "hi") -> int:
    async with eng.begin() as conn:
        result = await conn.execute(
            text(
                "INSERT INTO messages (chat_id, role, content) "
                "VALUES (:cid, 'user', :c)"
            ),
            {"cid": chat_id, "c": content},
        )
        return int(result.lastrowid)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# create()
# ---------------------------------------------------------------------------


async def test_create_default_is_non_incognito(engine: AsyncEngine) -> None:
    """create() without flag → incognito=0, incognito_expires_at=NULL."""
    await _insert_user(engine)
    svc = _make_service(engine)

    chat = await svc.create(user_id=1, title="ordinary")

    assert chat.incognito is False
    assert chat.incognito_expires_at is None

    async with engine.connect() as conn:
        row = (
            await conn.execute(select(chats).where(chats.c.id == chat.id))
        ).fetchone()
    assert row is not None
    assert int(row.incognito) == 0
    assert row.incognito_expires_at is None


async def test_create_with_incognito_sets_flag_and_ttl(engine: AsyncEngine) -> None:
    """create(incognito=True) sets the row to incognito with non-NULL TTL."""
    await _insert_user(engine)
    svc = _make_service(engine)

    before = time.time()
    chat = await svc.create(
        user_id=1, title="secret", incognito=True, incognito_ttl_seconds=60
    )

    assert chat.incognito is True
    assert chat.incognito_expires_at is not None
    assert chat.incognito_expires_at >= before + 60 - 1  # tolerate clock skew

    # Verify the row on disk.
    async with engine.connect() as conn:
        row = (
            await conn.execute(select(chats).where(chats.c.id == chat.id))
        ).fetchone()
    assert row is not None
    assert int(row.incognito) == 1


# ---------------------------------------------------------------------------
# is_shareable
# ---------------------------------------------------------------------------


async def test_is_shareable_false_for_incognito(engine: AsyncEngine) -> None:
    """P13j hook: is_shareable returns False for incognito chats."""
    await _insert_user(engine)
    svc = _make_service(engine)
    chat = await svc.create(user_id=1, title="x", incognito=True)
    assert await svc.is_shareable(chat.id, user_id=1) is False


async def test_is_shareable_true_for_regular(engine: AsyncEngine) -> None:
    await _insert_user(engine)
    svc = _make_service(engine)
    chat = await svc.create(user_id=1, title="x")
    assert await svc.is_shareable(chat.id, user_id=1) is True


# ---------------------------------------------------------------------------
# set_incognito (privacy invariant)
# ---------------------------------------------------------------------------


async def test_set_incognito_toggle_on_empty_chat(engine: AsyncEngine) -> None:
    """Empty chat → set_incognito toggles freely."""
    await _insert_user(engine)
    svc = _make_service(engine)
    chat = await svc.create(user_id=1, title="x")

    updated = await svc.set_incognito(chat.id, user_id=1, incognito=True)
    assert updated.incognito is True
    assert updated.incognito_expires_at is not None

    # Flipping back off clears the TTL.
    updated2 = await svc.set_incognito(chat.id, user_id=1, incognito=False)
    assert updated2.incognito is False
    assert updated2.incognito_expires_at is None


async def test_set_incognito_rejected_when_messages_exist(
    engine: AsyncEngine,
) -> None:
    """Privacy invariant: cannot toggle incognito once messages exist."""
    await _insert_user(engine)
    svc = _make_service(engine)
    chat = await svc.create(user_id=1, title="x")
    await _insert_message(engine, chat.id)

    with pytest.raises(ValueError, match="incognito flag is immutable"):
        await svc.set_incognito(chat.id, user_id=1, incognito=True)

    # Flag must remain unchanged.
    async with engine.connect() as conn:
        row = (
            await conn.execute(select(chats).where(chats.c.id == chat.id))
        ).fetchone()
    assert row is not None
    assert int(row.incognito) == 0


# ---------------------------------------------------------------------------
# purge_user_incognito (logout-sweep)
# ---------------------------------------------------------------------------


async def test_purge_user_incognito_deletes_only_incognito_chats(
    engine: AsyncEngine,
) -> None:
    """logout-sweep deletes incognito chats, preserves regular ones."""
    await _insert_user(engine)
    svc = _make_service(engine)

    regular = await svc.create(user_id=1, title="regular")
    incog_a = await svc.create(user_id=1, title="a", incognito=True)
    incog_b = await svc.create(user_id=1, title="b", incognito=True)

    deleted = await svc.purge_user_incognito(user_id=1)
    assert deleted == 2

    async with engine.connect() as conn:
        rows = (await conn.execute(select(chats.c.id))).fetchall()
    surviving_ids = {int(r[0]) for r in rows}
    assert regular.id in surviving_ids
    assert incog_a.id not in surviving_ids
    assert incog_b.id not in surviving_ids


async def test_purge_user_incognito_other_user_untouched(
    engine: AsyncEngine,
) -> None:
    """Logout sweep affects only the calling user's chats."""
    await _insert_user(engine, user_id=1, username="alice")
    await _insert_user(engine, user_id=2, username="bob")
    svc = _make_service(engine)

    a_inc = await svc.create(user_id=1, title="a-inc", incognito=True)
    b_inc = await svc.create(user_id=2, title="b-inc", incognito=True)

    deleted = await svc.purge_user_incognito(user_id=1)
    assert deleted == 1

    async with engine.connect() as conn:
        rows = (await conn.execute(select(chats.c.id))).fetchall()
    surviving = {int(r[0]) for r in rows}
    assert a_inc.id not in surviving
    assert b_inc.id in surviving

