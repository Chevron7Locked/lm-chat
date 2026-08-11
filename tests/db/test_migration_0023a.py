# SPDX-License-Identifier: Apache-2.0
"""Tests for migration 0023a — detach + RAG mode + projects.meta archival."""
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
    alembic.command.upgrade(
        _alembic_cfg(f"sqlite+aiosqlite:///{db}"), rev
    )


def _downgrade(db: Path, rev: str) -> None:
    alembic.command.downgrade(
        _alembic_cfg(f"sqlite+aiosqlite:///{db}"), rev
    )


def _columns(engine: sa.Engine, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns(table)}


# ─── Tests ────────────────────────────────────────────────────────────────


def test_0023a_adds_chats_detached_from_project_meta(tmp_path: Path) -> None:
    """``chats.detached_from_project_meta`` JSON nullable."""
    db = tmp_path / "test_0023a_chats.db"
    _upgrade(db)
    eng = sa.create_engine(_sync_url(db))
    try:
        assert "detached_from_project_meta" in _columns(eng, "chats")
    finally:
        eng.dispose()


def test_0023a_adds_projects_rag_threshold_and_meta(tmp_path: Path) -> None:
    """``projects.rag_threshold`` Integer + ``projects.meta`` JSON."""
    db = tmp_path / "test_0023a_projects.db"
    _upgrade(db)
    eng = sa.create_engine(_sync_url(db))
    try:
        cols = _columns(eng, "projects")
        assert "rag_threshold" in cols, cols
        assert "meta" in cols, cols
    finally:
        eng.dispose()


def test_0023a_archives_folders_into_meta(tmp_path: Path) -> None:
    """A project with ``folders=["A", "B"]`` BEFORE 0023a reads back
    with ``meta = {"folders": ["A", "B"]}`` AFTER. Bind via json.dumps,
    not the decoded list."""
    db = tmp_path / "test_0023a_archive.db"
    _upgrade(db, "0022b")
    eng = sa.create_engine(_sync_url(db))
    with eng.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO users (id, username, password_hash) "
            "VALUES (1, 'alice', 'scrypt$dummy')"
        )
        conn.exec_driver_sql(
            "INSERT INTO projects (id, user_id, name, description, "
            "system_prompt, folders, created_at, updated_at) "
            "VALUES (10, 1, 'P', '', '', '[\"A\", \"B\"]', 0, 0)"
        )
    eng.dispose()

    _upgrade(db)
    eng = sa.create_engine(_sync_url(db))
    try:
        with eng.connect() as conn:
            row = conn.exec_driver_sql(
                "SELECT json_extract(meta, '$.folders') "
                "FROM projects WHERE id = 10"
            ).fetchone()
        assert row is not None
        # SQLite json_extract returns the JSON-encoded string of the
        # extracted value; for a list it returns '["A","B"]'.
        import json as _json

        decoded = _json.loads(row[0])
        assert decoded == ["A", "B"], (
            f"archive lost data: {row[0]!r}"
        )
    finally:
        eng.dispose()


def test_0023a_downgrade_drops_three_columns(tmp_path: Path) -> None:
    """``alembic downgrade 0022b`` removes the three new columns."""
    db = tmp_path / "test_0023a_downgrade.db"
    _upgrade(db)
    _downgrade(db, "0022b")
    eng = sa.create_engine(_sync_url(db))
    try:
        assert "detached_from_project_meta" not in _columns(eng, "chats")
        assert "rag_threshold" not in _columns(eng, "projects")
        assert "meta" not in _columns(eng, "projects")
    finally:
        eng.dispose()
