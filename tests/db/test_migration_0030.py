# SPDX-License-Identifier: Apache-2.0
"""Tests for migration 0030 — ``provider_configs.allowed_models``.

Covers:
1. Column is present after upgrade to 0030, nullable, JSON type.
2. Existing rows keep allowed_models NULL after upgrade (no backfill).
3. New rows can be inserted with an explicit allowed_models JSON list.
4. Full round-trip: upgrade head → downgrade -1 (0029) → upgrade head.
"""
from __future__ import annotations

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


def _column_info(db: Path, table: str) -> dict:
    eng = sa.create_engine(f"sqlite:///{db}")
    try:
        return {c["name"]: c for c in sa.inspect(eng).get_columns(table)}
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


def test_0030_adds_allowed_models_column(tmp_path: Path) -> None:
    """After upgrade to 0030, provider_configs.allowed_models exists and is nullable."""
    db = tmp_path / "test_0030_upgrade.db"
    _upgrade(db, "0030")
    cols = _column_info(db, "provider_configs")
    assert "allowed_models" in cols, (
        f"allowed_models column missing after upgrade to 0030; cols={list(cols)}"
    )
    assert cols["allowed_models"]["nullable"] is True


def test_0030_existing_rows_keep_null(tmp_path: Path) -> None:
    """Rows inserted before 0030 keep allowed_models NULL (no backfill)."""
    db = tmp_path / "test_0030_backfill.db"
    _upgrade(db, "0029")

    con = _raw(db)
    try:
        con.execute(
            "INSERT INTO provider_configs (provider, base_url, enabled)"
            " VALUES ('openrouter', 'https://openrouter.ai/api/v1', 1)"
        )
        con.commit()
    finally:
        con.close()

    _upgrade(db, "0030")

    con = _raw(db)
    try:
        row = con.execute(
            "SELECT allowed_models FROM provider_configs WHERE provider='openrouter'"
        ).fetchone()
        assert row is not None
        assert row[0] is None, f"Expected NULL, got {row[0]!r}"
    finally:
        con.close()


def test_0030_new_row_accepts_allowed_models(tmp_path: Path) -> None:
    """After upgrade to 0030, rows can be inserted with a JSON allowed_models list."""
    import json

    db = tmp_path / "test_0030_insert.db"
    _upgrade(db, "0030")
    con = _raw(db)
    try:
        con.execute(
            "INSERT INTO provider_configs (provider, base_url, enabled, allowed_models)"
            " VALUES ('openai', 'https://api.openai.com/v1', 1, ?)",
            (json.dumps(["gpt-4o", "gpt-4o-mini"]),),
        )
        con.commit()
        row = con.execute(
            "SELECT allowed_models FROM provider_configs WHERE provider='openai'"
        ).fetchone()
        assert row is not None
        stored = json.loads(row[0])
        assert stored == ["gpt-4o", "gpt-4o-mini"], f"Unexpected stored value: {stored!r}"
    finally:
        con.close()


def test_0030_round_trip_upgrade_downgrade_upgrade(tmp_path: Path) -> None:
    """head → downgrade -1 (0029) → head is clean; column drops and re-appears."""
    db = tmp_path / "test_0030_roundtrip.db"
    _upgrade(db, "head")
    assert "allowed_models" in _column_info(db, "provider_configs")
    assert _get_version(db) in {
        "0030", "0031", "0032", "0033", "0034", "0035", "0036",
        "0037", "0038", "0039", "0040", "0041", "0042", "0043", "0044", "0045", "0046",
    }

    _downgrade(db, "0029")
    assert "allowed_models" not in _column_info(db, "provider_configs"), (
        "Column still present after downgrade to 0029"
    )
    assert _get_version(db) == "0029"

    _upgrade(db, "head")
    assert "allowed_models" in _column_info(db, "provider_configs")
    assert _get_version(db) in {
        "0030", "0031", "0032", "0033", "0034", "0035", "0036",
        "0037", "0038", "0039", "0040", "0041", "0042", "0043", "0044", "0045", "0046",
    }
