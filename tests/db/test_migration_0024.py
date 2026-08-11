# SPDX-License-Identifier: Apache-2.0
"""Tests for migration 0024 — add messages.tool_calls JSON column.

Pins:
1. Column absent before 0024, present after.
2. Existing rows get NULL (no backfill).
3. New rows can write JSON tool call lists.
4. downgrade removes the column cleanly.
"""
from __future__ import annotations

import json
from pathlib import Path

import alembic.command
import alembic.config
import sqlalchemy as sa

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _alembic_cfg(db_url: str) -> alembic.config.Config:
    cfg = alembic.config.Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    return cfg


def _upgrade(db: Path, rev: str = "head") -> None:
    alembic.command.upgrade(_alembic_cfg(f"sqlite+aiosqlite:///{db}"), rev)


def _downgrade(db: Path, rev: str) -> None:
    alembic.command.downgrade(_alembic_cfg(f"sqlite+aiosqlite:///{db}"), rev)


def _columns(engine: sa.Engine, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns(table)}


# ---------------------------------------------------------------------------
# Spec: test_messages_table_carries_tool_calls_json (alembic + ORM)
# ---------------------------------------------------------------------------


def test_0024_adds_tool_calls_column(tmp_path: Path) -> None:
    """After 0024, messages.tool_calls column exists."""
    db = tmp_path / "test_0024_add.db"
    _upgrade(db, "0023b")
    eng = sa.create_engine(f"sqlite:///{db}")
    try:
        cols_pre = _columns(eng, "messages")
        assert "tool_calls" not in cols_pre, (
            "tool_calls should not exist before 0024"
        )
    finally:
        eng.dispose()

    _upgrade(db, "0024")
    eng = sa.create_engine(f"sqlite:///{db}")
    try:
        cols_post = _columns(eng, "messages")
        assert "tool_calls" in cols_post, (
            f"tool_calls column missing after 0024; got {cols_post!r}"
        )
    finally:
        eng.dispose()


def test_0024_existing_rows_get_null(tmp_path: Path) -> None:
    """Existing message rows get NULL for tool_calls.

    Insert a message row before 0024, upgrade, verify tool_calls IS NULL.
    """
    db = tmp_path / "test_0024_null.db"

    # Upgrade to the revision just before 0024.
    _upgrade(db, "0023b")
    eng = sa.create_engine(f"sqlite:///{db}")
    try:
        with eng.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO users (id, username, password_hash) "
                "VALUES (1, 'alice', 'scrypt$dummy')"
            )
            conn.exec_driver_sql(
                "INSERT INTO chats (id, user_id, title, settings, display_order) "
                "VALUES (1, 1, 'pre-0024 chat', '{}', 0)"
            )
            conn.exec_driver_sql(
                "INSERT INTO messages (id, chat_id, role, content, state) "
                "VALUES (1, 1, 'assistant', 'hello', 'final')"
            )
    finally:
        eng.dispose()

    # Now upgrade to 0024.
    _upgrade(db, "0024")
    eng = sa.create_engine(f"sqlite:///{db}")
    try:
        with eng.connect() as conn:
            row = conn.exec_driver_sql(
                "SELECT tool_calls FROM messages WHERE id = 1"
            ).fetchone()
        assert row is not None
        assert row[0] is None, (
            f"Existing row should have tool_calls=NULL; got {row[0]!r}"
        )
    finally:
        eng.dispose()


def test_0024_new_rows_can_store_tool_calls(tmp_path: Path) -> None:
    """After 0024, new message rows can store JSON tool call lists."""
    db = tmp_path / "test_0024_write.db"
    _upgrade(db, "head")

    tool_calls_json = json.dumps([
        {
            "id": "tc_abc123",
            "name": "search_web",
            "arguments": {"query": "lm studio"},
            "status": "success",
            "result": "LM Studio is a desktop app...",
        }
    ])

    eng = sa.create_engine(f"sqlite:///{db}")
    try:
        with eng.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO users (id, username, password_hash) "
                "VALUES (2, 'bob', 'scrypt$dummy')"
            )
            conn.exec_driver_sql(
                "INSERT INTO chats (id, user_id, title, settings, display_order) "
                "VALUES (2, 2, 'tool chat', '{}', 0)"
            )
            conn.exec_driver_sql(
                f"INSERT INTO messages (id, chat_id, role, content, state, tool_calls) "
                f"VALUES (2, 2, 'assistant', 'ok', 'final', '{tool_calls_json}')"
            )
        with eng.connect() as conn:
            row = conn.exec_driver_sql(
                "SELECT tool_calls FROM messages WHERE id = 2"
            ).fetchone()
        assert row is not None
        stored = json.loads(row[0])
        assert len(stored) == 1
        assert stored[0]["name"] == "search_web"
        assert stored[0]["status"] == "success"
    finally:
        eng.dispose()


def test_0024_downgrade_drops_column(tmp_path: Path) -> None:
    """Downgrade from 0024 → 0023b removes the tool_calls column."""
    db = tmp_path / "test_0024_downgrade.db"
    _upgrade(db, "head")

    eng = sa.create_engine(f"sqlite:///{db}")
    try:
        assert "tool_calls" in _columns(eng, "messages")
    finally:
        eng.dispose()

    _downgrade(db, "0023b")
    eng = sa.create_engine(f"sqlite:///{db}")
    try:
        cols = _columns(eng, "messages")
        assert "tool_calls" not in cols, (
            f"tool_calls should be gone after downgrade; got {cols!r}"
        )
    finally:
        eng.dispose()
