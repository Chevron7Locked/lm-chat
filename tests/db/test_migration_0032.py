# SPDX-License-Identifier: Apache-2.0
"""Tests for migration 0032 — ``mcp_servers`` table creation.

Covers:
1. Table and all key columns exist after upgrade to 0032.
2. A row can be inserted and read back (column types work).
3. Full round-trip: upgrade head → downgrade to 0031 → upgrade head.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import alembic.command
import alembic.config
import sqlalchemy as sa

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


def _column_names(db: Path, table: str) -> set[str]:
    eng = sa.create_engine(f"sqlite:///{db}")
    try:
        return {c["name"] for c in sa.inspect(eng).get_columns(table)}
    finally:
        eng.dispose()


def _table_names(db: Path) -> set[str]:
    eng = sa.create_engine(f"sqlite:///{db}")
    try:
        return set(sa.inspect(eng).get_table_names())
    finally:
        eng.dispose()


def _raw(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(db))


def _get_version(db: Path) -> str | None:
    con = _raw(db)
    try:
        row = con.execute(
            "SELECT version_num FROM alembic_version LIMIT 1"
        ).fetchone()
        return row[0] if row else None
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_0032_creates_mcp_servers_table(tmp_path: Path) -> None:
    """After upgrade to 0032, mcp_servers table exists with all key columns."""
    db = tmp_path / "test_0032_upgrade.db"
    _upgrade(db, "0032")

    assert "mcp_servers" in _table_names(db), "mcp_servers table missing"

    cols = _column_names(db, "mcp_servers")
    expected = {
        "id",
        "slug",
        "name",
        "transport",
        "command",
        "args",
        "url",
        "secrets_enc",
        "enabled",
        "source",
        "trust",
        "consented",
        "created_at",
        "updated_at",
    }
    missing = expected - cols
    assert not missing, f"Missing columns after upgrade to 0032: {missing}"

    assert _get_version(db) == "0032"


def test_0032_new_row_insert(tmp_path: Path) -> None:
    """After upgrade to 0032, a row can be inserted and read back."""
    db = tmp_path / "test_0032_insert.db"
    _upgrade(db, "0032")

    con = _raw(db)
    try:
        secrets_enc = json.dumps([{"key": "GITHUB_TOKEN", "enc_value": "enc$v1$fake"}])
        con.execute(
            "INSERT INTO mcp_servers (slug, name, transport, command, args, secrets_enc)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                "github",
                "GitHub",
                "stdio",
                "npx",
                json.dumps(["-y", "@modelcontextprotocol/server-github"]),
                secrets_enc,
            ),
        )
        con.commit()

        row = con.execute(
            "SELECT slug, name, transport, command, secrets_enc FROM mcp_servers"
            " WHERE slug='github'"
        ).fetchone()
        assert row is not None, "Inserted row not found"
        assert row[0] == "github"
        assert row[1] == "GitHub"
        assert row[2] == "stdio"
        assert row[3] == "npx"
        stored = json.loads(row[4])
        assert stored[0]["key"] == "GITHUB_TOKEN"
    finally:
        con.close()


def test_0032_round_trip(tmp_path: Path) -> None:
    """upgrade to 0032 → downgrade to 0031 → upgrade to 0032: round-trips cleanly."""
    db = tmp_path / "test_0032_roundtrip.db"
    _upgrade(db, "0032")

    assert "mcp_servers" in _table_names(db)
    assert _get_version(db) == "0032"

    _downgrade(db, "0031")
    assert "mcp_servers" not in _table_names(db), (
        "mcp_servers still present after downgrade to 0031"
    )
    assert _get_version(db) == "0031"

    _upgrade(db, "0032")
    assert "mcp_servers" in _table_names(db)
    assert _get_version(db) == "0032"
