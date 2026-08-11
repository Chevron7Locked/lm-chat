# SPDX-License-Identifier: Apache-2.0
"""Tests for migration 0022 — projects.embedding_model_id + default_model_id.

Pins the migration shape:
* both columns NULLABLE Text on the projects table
* default to NULL for existing projects (no backfill required)
* downgrade drops both columns cleanly
* fingerprint bump preserves the baseline-test contract
"""
from __future__ import annotations

from pathlib import Path

import alembic.command
import alembic.config
import sqlalchemy as sa

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _sync_url(db: Path) -> str:
    return f"sqlite:///{db}"


def _alembic_cfg(db_url: str) -> alembic.config.Config:
    cfg = alembic.config.Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    return cfg


def _upgrade(db: Path, rev: str = "head") -> None:
    # env.py uses the async engine driver; tests inspect via sync.
    alembic.command.upgrade(
        _alembic_cfg(f"sqlite+aiosqlite:///{db}"), rev
    )


def _downgrade(db: Path, rev: str) -> None:
    alembic.command.downgrade(
        _alembic_cfg(f"sqlite+aiosqlite:///{db}"), rev
    )


def _columns(engine: sa.Engine, table: str) -> dict[str, dict[str, object]]:
    """Return {col_name: {nullable, type_str}} for the given table."""
    insp = sa.inspect(engine)
    return {
        col["name"]: {
            "nullable": col["nullable"],
            "type": str(col["type"]),
        }
        for col in insp.get_columns(table)
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_0022_adds_two_nullable_text_columns(tmp_path: Path) -> None:
    """After upgrade head, projects has embedding_model_id +
    default_model_id, both NULLABLE TEXT."""
    db = tmp_path / "test_0022_columns.db"
    _upgrade(db)
    eng = sa.create_engine(_sync_url(db))
    try:
        cols = _columns(eng, "projects")
        assert "embedding_model_id" in cols, (
            f"embedding_model_id missing after 0022: {list(cols)}"
        )
        assert "default_model_id" in cols, (
            f"default_model_id missing after 0022: {list(cols)}"
        )
        assert cols["embedding_model_id"]["nullable"] is True
        assert cols["default_model_id"]["nullable"] is True
        # SQLite reports Text/VARCHAR/TEXT differently; just verify
        # the type string contains "TEXT" (or "VARCHAR" on some dialect
        # backings — we use sa.Text which renders TEXT on SQLite).
        assert "TEXT" in str(cols["embedding_model_id"]["type"]).upper()
        assert "TEXT" in str(cols["default_model_id"]["type"]).upper()
    finally:
        eng.dispose()


def test_0022_existing_projects_default_to_null(tmp_path: Path) -> None:
    """A project created BEFORE 0022 (simulated by inserting via raw
    SQL against the 0021 schema) reads back with NULL pins after the
    migration. No backfill needed."""
    db = tmp_path / "test_0022_null_default.db"
    _upgrade(db, "0021")
    # Insert a project at 0021 (no new columns).
    eng = sa.create_engine(_sync_url(db))
    with eng.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO users (id, username, password_hash) "
            "VALUES (1, 'alice', 'scrypt$dummy')"
        )
        conn.exec_driver_sql(
            "INSERT INTO projects (id, user_id, name, description, "
            "system_prompt, folders, created_at, updated_at) "
            "VALUES (10, 1, 'P', '', '', '[]', 0, 0)"
        )
    eng.dispose()
    # Upgrade through 0022.
    _upgrade(db)
    eng = sa.create_engine(_sync_url(db))
    try:
        with eng.connect() as conn:
            row = conn.exec_driver_sql(
                "SELECT embedding_model_id, default_model_id "
                "FROM projects WHERE id = 10"
            ).fetchone()
        assert row is not None
        assert row[0] is None, (
            f"embedding_model_id NOT NULL on pre-0022 row: {row[0]!r}"
        )
        assert row[1] is None, (
            f"default_model_id NOT NULL on pre-0022 row: {row[1]!r}"
        )
    finally:
        eng.dispose()


def test_0022_downgrade_drops_both_columns(tmp_path: Path) -> None:
    """``alembic downgrade 0021`` removes both new columns and the
    table shape matches 0021 again."""
    db = tmp_path / "test_0022_downgrade.db"
    _upgrade(db)  # head (incl. 0022)
    _downgrade(db, "0021")
    eng = sa.create_engine(_sync_url(db))
    try:
        cols = _columns(eng, "projects")
        assert "embedding_model_id" not in cols, (
            f"embedding_model_id present after downgrade: {list(cols)}"
        )
        assert "default_model_id" not in cols, (
            f"default_model_id present after downgrade: {list(cols)}"
        )
    finally:
        eng.dispose()
