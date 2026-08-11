# SPDX-License-Identifier: Apache-2.0
"""Tests for migration 0033 — ``tool_policy`` column on ``mcp_servers``.

Covers:
1. Column exists after upgrade to 0033 (mcp_servers table present + new column).
2. A row with a non-null tool_policy can be inserted and read back.
3. Round-trip: upgrade to 0033 → downgrade to 0032 → upgrade to 0033: clean.
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


def test_0033_adds_tool_policy_column(tmp_path: Path) -> None:
    """After upgrade to 0033, mcp_servers has the tool_policy column."""
    db = tmp_path / "test_0033_upgrade.db"
    _upgrade(db, "0033")

    cols = _column_names(db, "mcp_servers")
    assert "tool_policy" in cols, f"tool_policy column missing; cols = {cols}"
    assert _get_version(db) == "0033"


def test_0033_tool_policy_row_insert(tmp_path: Path) -> None:
    """After upgrade to 0033, a row with tool_policy can be inserted and read back."""
    db = tmp_path / "test_0033_insert.db"
    _upgrade(db, "0033")

    policy = json.dumps(["firecrawl_scrape", "firecrawl_map"])
    con = _raw(db)
    try:
        con.execute(
            "INSERT INTO mcp_servers (slug, name, transport, tool_policy)"
            " VALUES (?, ?, ?, ?)",
            ("firecrawl", "Firecrawl", "stdio", policy),
        )
        con.commit()

        row = con.execute(
            "SELECT slug, tool_policy FROM mcp_servers WHERE slug='firecrawl'"
        ).fetchone()
        assert row is not None
        assert row[0] == "firecrawl"
        stored = json.loads(row[1])
        assert stored == ["firecrawl_scrape", "firecrawl_map"]
    finally:
        con.close()


def test_0033_null_tool_policy_allowed(tmp_path: Path) -> None:
    """After upgrade to 0033, a row with null tool_policy (allow-all) is valid."""
    db = tmp_path / "test_0033_null.db"
    _upgrade(db, "0033")

    con = _raw(db)
    try:
        con.execute(
            "INSERT INTO mcp_servers (slug, name, transport, tool_policy)"
            " VALUES (?, ?, ?, ?)",
            ("github", "GitHub", "stdio", None),
        )
        con.commit()

        row = con.execute(
            "SELECT tool_policy FROM mcp_servers WHERE slug='github'"
        ).fetchone()
        assert row is not None
        assert row[0] is None, f"Expected NULL tool_policy, got {row[0]!r}"
    finally:
        con.close()


def test_0033_round_trip(tmp_path: Path) -> None:
    """upgrade to 0033 → downgrade to 0032 → upgrade to 0033: round-trips cleanly."""
    db = tmp_path / "test_0033_roundtrip.db"
    _upgrade(db, "0033")

    cols_at_33 = _column_names(db, "mcp_servers")
    assert "tool_policy" in cols_at_33
    assert _get_version(db) == "0033"

    _downgrade(db, "0032")
    cols_at_32 = _column_names(db, "mcp_servers")
    assert "tool_policy" not in cols_at_32, (
        "tool_policy still present after downgrade to 0032"
    )
    assert _get_version(db) == "0032"

    _upgrade(db, "0033")
    cols_after = _column_names(db, "mcp_servers")
    assert "tool_policy" in cols_after
    assert _get_version(db) == "0033"
