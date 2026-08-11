# SPDX-License-Identifier: Apache-2.0
"""Tests for migration 0028 — ``messages.tool_call_id``.

Adds a nullable String ``tool_call_id`` column to
``messages`` so Responses-API tool-result turns can persist the call_id
required for stateless-provider replay history reconstruction.

Pins:
1. Column is present after upgrade to 0028, nullable, String type.
2. Existing rows keep tool_call_id NULL after the upgrade (no backfill).
3. New role="tool" rows can be inserted with an explicit tool_call_id.
4. Full round-trip: upgrade head → downgrade -1 (drops column) →
   upgrade head (re-adds) without error.
5. MessageService.append() + list_for_chat() round-trip: tool_call_id
   persists and is returned on read.
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import alembic.command
import alembic.config
import pytest
import sqlalchemy as sa
from sqlalchemy import event as sa_event
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _alembic_cfg(db_url: str) -> alembic.config.Config:
    cfg = alembic.config.Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    return cfg


def _upgrade(db: Path, rev: str = "head") -> None:
    alembic.command.upgrade(_alembic_cfg(f"sqlite+aiosqlite:///{db}"), rev)


def _downgrade(db: Path, rev: str) -> None:
    alembic.command.downgrade(_alembic_cfg(f"sqlite+aiosqlite:///{db}"), rev)


def _column_info(engine: sa.Engine, table: str) -> dict[str, Any]:
    return {c["name"]: c for c in sa.inspect(engine).get_columns(table)}


def _raw(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(db))


# ---------------------------------------------------------------------------
# Schema-level tests
# ---------------------------------------------------------------------------


def test_0028_adds_nullable_string_tool_call_id(tmp_path: Path) -> None:
    """After upgrade to 0028, messages.tool_call_id exists, nullable, String/VARCHAR."""
    db = tmp_path / "test_0028_upgrade.db"
    _upgrade(db, "0028")
    eng = sa.create_engine(f"sqlite:///{db}")
    try:
        cols = _column_info(eng, "messages")
        assert "tool_call_id" in cols, "tool_call_id column missing after upgrade"
        assert cols["tool_call_id"]["nullable"] is True
        # SQLite maps VARCHAR/String to TEXT; just assert string-compatible.
        col_type = cols["tool_call_id"]["type"]
        assert isinstance(col_type, (sa.String, sa.Text, sa.VARCHAR)), (
            f"Unexpected column type: {col_type!r}"
        )
    finally:
        eng.dispose()


def test_0028_existing_rows_keep_null(tmp_path: Path) -> None:
    """Rows existing before 0028 keep tool_call_id NULL (no backfill)."""
    db = tmp_path / "test_0028_backfill.db"
    _upgrade(db, "0027")
    con = _raw(db)
    try:
        con.execute(
            "INSERT INTO users (id, username, password_hash) VALUES (1, 'u', 'ph')"
        )
        con.execute("INSERT INTO chats (id, user_id, title) VALUES (1, 1, 'tc')")
        con.execute(
            "INSERT INTO messages (chat_id, role, content, state, created_at)"
            " VALUES (1, 'tool', 'old result', 'final',"
            " '2026-01-01 00:00:00.000000')"
        )
        con.commit()
    finally:
        con.close()

    _upgrade(db, "0028")

    con = _raw(db)
    try:
        row = con.execute(
            "SELECT tool_call_id FROM messages WHERE chat_id = 1"
        ).fetchone()
        assert row is not None
        assert row[0] is None, f"Expected NULL, got {row[0]!r}"
    finally:
        con.close()


def test_0028_new_row_accepts_tool_call_id(tmp_path: Path) -> None:
    """After 0028, messages can be inserted with an explicit tool_call_id."""
    db = tmp_path / "test_0028_insert.db"
    _upgrade(db, "0028")
    con = _raw(db)
    try:
        con.execute(
            "INSERT INTO users (id, username, password_hash) VALUES (1, 'u', 'ph')"
        )
        con.execute("INSERT INTO chats (id, user_id, title) VALUES (1, 1, 'tc')")
        con.execute(
            "INSERT INTO messages (chat_id, role, content, state, created_at,"
            " tool_call_id) VALUES (1, 'tool', 'tool result', 'final',"
            " '2026-06-18 12:00:00.000000', 'call_abc123')"
        )
        con.commit()
        row = con.execute(
            "SELECT tool_call_id FROM messages WHERE chat_id = 1"
        ).fetchone()
        assert row is not None
        assert row[0] == "call_abc123", f"Expected 'call_abc123', got {row[0]!r}"
    finally:
        con.close()


def test_0028_round_trip_upgrade_downgrade_upgrade(tmp_path: Path) -> None:
    """head → downgrade -1 → head is clean; column drops and re-appears."""
    db = tmp_path / "test_0028_roundtrip.db"
    _upgrade(db, "head")

    eng = sa.create_engine(f"sqlite:///{db}")
    try:
        assert "tool_call_id" in _column_info(eng, "messages")
    finally:
        eng.dispose()

    _downgrade(db, "0027")
    eng = sa.create_engine(f"sqlite:///{db}")
    try:
        assert "tool_call_id" not in _column_info(eng, "messages"), (
            "Column still present after downgrade"
        )
    finally:
        eng.dispose()

    _upgrade(db, "head")
    eng = sa.create_engine(f"sqlite:///{db}")
    try:
        assert "tool_call_id" in _column_info(eng, "messages")
    finally:
        eng.dispose()


# ---------------------------------------------------------------------------
# MessageService round-trip test
# ---------------------------------------------------------------------------


@pytest.fixture()
async def svc_engine(tmp_path: Path) -> AsyncGenerator[tuple[Any, AsyncEngine]]:
    """Yield (MessageService, AsyncEngine) with head migrations applied."""
    from lmchat.db.pragmas import apply_sqlite_pragmas
    from lmchat.embedding.client import EmbeddingClient
    from lmchat.services.memory_service import MemoryService
    from lmchat.services.message_service import MessageService
    from lmchat.services.models_service import Capabilities, ModelInfo, ModelsService

    db_path = tmp_path / "test_svc_0028.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"

    await asyncio.to_thread(_upgrade, db_path, "head")
    eng = create_async_engine(db_url, pool_pre_ping=True)

    @sa_event.listens_for(eng.sync_engine, "connect")
    def _on_connect(dbapi_conn: object, _record: object) -> None:
        apply_sqlite_pragmas(dbapi_conn)

    mock_embedding = AsyncMock(spec=EmbeddingClient)
    mock_models = AsyncMock(spec=ModelsService)
    mock_model = ModelInfo(
        key="embed-model",
        type="embedding",
        capabilities=Capabilities(vision=False, trained_for_tool_use=False),
    )
    mock_models.list_loaded.return_value = [mock_model]
    mem_svc = MemoryService(
        engine=eng,
        embedding_client=mock_embedding,
        models_service=mock_models,
    )
    svc = MessageService(engine=eng, memory_service=mem_svc)
    yield svc, eng
    await eng.dispose()


@pytest.mark.asyncio
async def test_tool_call_id_round_trips_via_append_and_list(
    svc_engine: tuple[Any, AsyncEngine],
) -> None:
    """append(role='tool', tool_call_id=...) round-trips through list_for_chat."""
    from lmchat.services.message_service import MessageService

    svc: MessageService
    eng: AsyncEngine
    svc, eng = svc_engine

    # Seed user + chat.
    async with eng.begin() as conn:
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO users (id, username, password_hash)"
                " VALUES (1, 'tester', 'scrypt$dummy')"
            )
        )
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO chats (id, user_id, title)"
                " VALUES (1, 1, 'tool call id test')"
            )
        )

    call_id = "call_responses_api_xyz"
    appended = await svc.append(
        chat_id=1,
        user_id=1,
        role="tool",
        content="The weather in London is 18°C.",
        tool_call_id=call_id,
    )

    assert appended.tool_call_id == call_id, (
        f"append() returned tool_call_id={appended.tool_call_id!r}, expected {call_id!r}"
    )

    messages, _has_more = await svc.list_for_chat(chat_id=1, user_id=1)
    assert len(messages) == 1
    assert messages[0].tool_call_id == call_id, (
        f"list_for_chat() returned tool_call_id={messages[0].tool_call_id!r}, "
        f"expected {call_id!r}"
    )
