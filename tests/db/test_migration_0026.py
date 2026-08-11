# SPDX-License-Identifier: Apache-2.0
"""Tests for migration 0026 — ``messages.stop_reason``.

Continue-chip closeout (audit 2026-06-10): adds a nullable TEXT
``stop_reason`` column to ``messages`` with NULL backfill (locked
decision 3 — historical rows render without a Continue chip).

Pins:
1. Column is present after upgrade head, nullable, type TEXT.
2. Existing rows keep stop_reason NULL after the upgrade.
3. New rows can be inserted with an explicit stop_reason.
4. Full round-trip: upgrade head → downgrade -1 (drops column) →
   upgrade head (re-adds) without error.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

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


def _column_info(engine: sa.Engine, table: str) -> dict[str, Any]:
    return {c["name"]: c for c in sa.inspect(engine).get_columns(table)}


def _raw(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(db))


def test_0026_adds_nullable_text_stop_reason(tmp_path: Path) -> None:
    """After upgrade to 0026, messages.stop_reason exists, nullable, TEXT."""
    db = tmp_path / "test_0026_upgrade.db"
    _upgrade(db, "0026")
    eng = sa.create_engine(f"sqlite:///{db}")
    try:
        cols = _column_info(eng, "messages")
        assert "stop_reason" in cols
        assert cols["stop_reason"]["nullable"] is True
        assert isinstance(cols["stop_reason"]["type"], sa.Text)
    finally:
        eng.dispose()


def test_0026_existing_rows_backfill_null(tmp_path: Path) -> None:
    """Rows existing before 0026 keep stop_reason NULL (locked decision 3)."""
    db = tmp_path / "test_0026_backfill.db"
    _upgrade(db, "0025")
    con = _raw(db)
    try:
        con.execute(
            "INSERT INTO users (id, username, password_hash) VALUES (1, 'u', 'ph')"
        )
        con.execute("INSERT INTO chats (id, user_id, title) VALUES (1, 1, 'tc')")
        con.execute(
            "INSERT INTO messages (chat_id, role, content, state, created_at)"
            " VALUES (1, 'assistant', 'old reply', 'final',"
            " '2026-01-01 00:00:00.000000')"
        )
        con.commit()
    finally:
        con.close()

    _upgrade(db, "0026")

    con = _raw(db)
    try:
        row = con.execute(
            "SELECT stop_reason FROM messages WHERE chat_id = 1"
        ).fetchone()
        assert row is not None
        assert row[0] is None
    finally:
        con.close()


def test_0026_new_row_accepts_stop_reason(tmp_path: Path) -> None:
    """After 0026, messages can be inserted with stop_reason='length'."""
    db = tmp_path / "test_0026_insert.db"
    _upgrade(db, "0026")
    con = _raw(db)
    try:
        con.execute(
            "INSERT INTO users (id, username, password_hash) VALUES (1, 'u', 'ph')"
        )
        con.execute("INSERT INTO chats (id, user_id, title) VALUES (1, 1, 'tc')")
        con.execute(
            "INSERT INTO messages (chat_id, role, content, state, created_at,"
            " stop_reason) VALUES (1, 'assistant', 'truncated…', 'final',"
            " '2026-06-10 12:00:00.000000', 'length')"
        )
        con.commit()
        row = con.execute(
            "SELECT stop_reason FROM messages WHERE chat_id = 1"
        ).fetchone()
        assert row is not None
        assert row[0] == "length"
    finally:
        con.close()


def test_0026_round_trip_upgrade_downgrade_upgrade(tmp_path: Path) -> None:
    """head → downgrade -1 → head is clean; column drops and re-appears."""
    db = tmp_path / "test_0026_roundtrip.db"
    _upgrade(db, "head")

    eng = sa.create_engine(f"sqlite:///{db}")
    try:
        assert "stop_reason" in _column_info(eng, "messages")
    finally:
        eng.dispose()

    _downgrade(db, "0025")
    eng = sa.create_engine(f"sqlite:///{db}")
    try:
        assert "stop_reason" not in _column_info(eng, "messages")
    finally:
        eng.dispose()

    _upgrade(db, "head")
    eng = sa.create_engine(f"sqlite:///{db}")
    try:
        assert "stop_reason" in _column_info(eng, "messages")
    finally:
        eng.dispose()
