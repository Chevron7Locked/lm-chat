# SPDX-License-Identifier: Apache-2.0
"""Tests for migration 0025 — ``messages.last_activity_at``.

Adds a nullable
``last_activity_at`` column to ``messages`` with a backfill of
``created_at`` for existing rows.

Pins:
1. Column is present after upgrade to 0025.
2. Existing rows are backfilled with ``created_at`` (not NULL).
3. New rows can be inserted with an explicit ``last_activity_at``.
4. ``downgrade()`` drops the column cleanly.
"""
from __future__ import annotations

import sqlite3
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


def _raw(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(db))


def test_0025_adds_last_activity_at_column(tmp_path: Path) -> None:
    """After upgrade to 0025, ``messages.last_activity_at`` exists."""
    db = tmp_path / "test_0025_upgrade.db"
    _upgrade(db, "0025")
    eng = sa.create_engine(f"sqlite:///{db}")
    try:
        assert "last_activity_at" in _columns(eng, "messages")
    finally:
        eng.dispose()


def test_0025_backfills_existing_rows(tmp_path: Path) -> None:
    """Existing message rows get last_activity_at = created_at after 0025."""
    db = tmp_path / "test_0025_backfill.db"

    # Upgrade to 0023b (one before 0025) and insert a message row.
    _upgrade(db, "0023b")
    con = _raw(db)
    try:
        # Insert minimal required rows (user → chat → message).
        con.execute(
            "INSERT INTO users (id, username, password_hash) VALUES (1, 'u', 'ph')"
        )
        con.execute(
            "INSERT INTO chats (id, user_id, title) VALUES (1, 1, 'tc')"
        )
        con.execute(
            "INSERT INTO messages (chat_id, role, content, state, created_at)"
            " VALUES (1, 'assistant', '', 'final', '2026-01-01 00:00:00.000000')"
        )
        con.commit()
    finally:
        con.close()

    # Apply 0024.
    _upgrade(db, "0025")

    con = _raw(db)
    try:
        row = con.execute(
            "SELECT created_at, last_activity_at FROM messages WHERE chat_id = 1"
        ).fetchone()
        assert row is not None
        created_at, last_activity_at = row
        # The backfill should have set last_activity_at = created_at.
        assert last_activity_at is not None
        assert last_activity_at == created_at
    finally:
        con.close()


def test_0025_new_row_accepts_explicit_last_activity_at(tmp_path: Path) -> None:
    """After 0024, messages can be inserted with an explicit last_activity_at."""
    db = tmp_path / "test_0025_insert.db"
    _upgrade(db, "0025")
    con = _raw(db)
    try:
        con.execute(
            "INSERT INTO users (id, username, password_hash) VALUES (1, 'u', 'ph')"
        )
        con.execute(
            "INSERT INTO chats (id, user_id, title) VALUES (1, 1, 'tc')"
        )
        ts = "2026-06-10 12:00:00.000000"
        con.execute(
            "INSERT INTO messages (chat_id, role, content, state, created_at, last_activity_at)"
            " VALUES (1, 'assistant', '', 'draft', :ts, :act)",
            {"ts": ts, "act": ts},
        )
        con.commit()
        row = con.execute(
            "SELECT last_activity_at FROM messages WHERE chat_id = 1"
        ).fetchone()
        assert row is not None
        assert row[0] == ts
    finally:
        con.close()


def test_0025_downgrade_drops_column(tmp_path: Path) -> None:
    """Downgrading from 0024 to 0023b drops last_activity_at."""
    db = tmp_path / "test_0025_downgrade.db"
    _upgrade(db, "0025")

    eng = sa.create_engine(f"sqlite:///{db}")
    try:
        assert "last_activity_at" in _columns(eng, "messages")
    finally:
        eng.dispose()

    _downgrade(db, "0023b")

    eng = sa.create_engine(f"sqlite:///{db}")
    try:
        assert "last_activity_at" not in _columns(eng, "messages")
    finally:
        eng.dispose()
