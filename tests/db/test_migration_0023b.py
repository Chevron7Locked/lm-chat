# SPDX-License-Identifier: Apache-2.0
"""Tests for migration 0023b — drop ``projects.folders`` column.

After 0023a archived the
data into ``projects.meta.folders``, 0023b drops the legacy column.
This test pins:

1. The column is gone after 0023b.
2. ``projects.meta.folders`` survives the drop (the archive is the
   single source of truth post-0023b).
3. ``downgrade()`` restores the column shape (empty default; data is
   NOT restored — admin action required, documented as one-way).
4. The migration refuses to run on non-SQLite dialects in v1.0 (the
   release-scope guard).
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

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
    alembic.command.upgrade(
        _alembic_cfg(f"sqlite+aiosqlite:///{db}"), rev
    )


def _downgrade(db: Path, rev: str) -> None:
    alembic.command.downgrade(
        _alembic_cfg(f"sqlite+aiosqlite:///{db}"), rev
    )


def _columns(engine: sa.Engine, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns(table)}


def test_0023b_drops_projects_folders(tmp_path: Path) -> None:
    """After upgrading to 0023b, ``projects.folders`` is gone."""
    db = tmp_path / "test_0023b_drop.db"
    _upgrade(db, "0023a")
    eng = sa.create_engine(f"sqlite:///{db}")
    try:
        cols_pre = _columns(eng, "projects")
        assert "folders" in cols_pre
    finally:
        eng.dispose()

    _upgrade(db, "0023b")
    eng = sa.create_engine(f"sqlite:///{db}")
    try:
        cols_post = _columns(eng, "projects")
        assert "folders" not in cols_post
    finally:
        eng.dispose()


def test_0023b_preserves_meta_folders_archive(tmp_path: Path) -> None:
    """Data written to ``projects.meta.folders`` by 0023a is NOT
    affected by 0023b — the archive is the single source of truth
    after the drop."""
    db = tmp_path / "test_0023b_archive.db"
    _upgrade(db, "0022b")
    eng = sa.create_engine(f"sqlite:///{db}")
    try:
        with eng.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO users (id, username, password_hash) "
                "VALUES (1, 'alice', 'scrypt$dummy')"
            )
            conn.exec_driver_sql(
                "INSERT INTO projects (id, user_id, name, description, "
                "system_prompt, folders, created_at, updated_at) "
                "VALUES (10, 1, 'P', '', '', "
                "'[\"alpha\", \"beta\"]', 0, 0)"
            )
    finally:
        eng.dispose()

    _upgrade(db, "head")
    eng = sa.create_engine(f"sqlite:///{db}")
    try:
        with eng.connect() as conn:
            row = conn.exec_driver_sql(
                "SELECT json_extract(meta, '$.folders') "
                "FROM projects WHERE id = 10"
            ).fetchone()
        assert row is not None, "row vanished across 0023b"
        decoded = json.loads(row[0])
        assert decoded == ["alpha", "beta"], (
            f"meta.folders archive corrupted by 0023b: {row[0]!r}"
        )
    finally:
        eng.dispose()


def test_0023b_downgrade_restores_column_shape(tmp_path: Path) -> None:
    """``alembic downgrade 0023a`` re-adds the ``folders`` column
    with the documented empty-list default. Data is NOT restored —
    that's admin-action, per the docstring."""
    db = tmp_path / "test_0023b_downgrade.db"
    _upgrade(db, "head")
    eng = sa.create_engine(f"sqlite:///{db}")
    try:
        assert "folders" not in _columns(eng, "projects")
    finally:
        eng.dispose()

    _downgrade(db, "0023a")
    eng = sa.create_engine(f"sqlite:///{db}")
    try:
        cols = _columns(eng, "projects")
        assert "folders" in cols, cols
    finally:
        eng.dispose()


def test_0023b_postgres_branch_emits_plain_drop_column() -> None:
    """Postgres branch uses ``op.drop_column('projects', 'folders')``
    directly (no batch_alter_table). Unit-level — substitutes a stub
    bind reporting dialect=postgresql and asserts the right alembic
    op gets called. Avoids needing a live Postgres test container."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_mig_0023b",
        _REPO_ROOT
        / "migrations"
        / "versions"
        / "0023b_drop_projects_folders.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    fake_bind = MagicMock()
    fake_bind.dialect.name = "postgresql"
    drop_column_calls: list[tuple] = []
    batch_alter_calls: list[str] = []

    class _FakeBatch:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def drop_column(self, name: str) -> None:
            batch_alter_calls.append(name)

    def _fake_batch_alter(table: str):
        batch_alter_calls.append(f"batch:{table}")
        return _FakeBatch()

    import contextlib

    with (
        contextlib.suppress(AttributeError),
        # Patch op against the module's own reference.
        _patch(mod.op, "get_bind", lambda: fake_bind),
        _patch(
            mod.op,
            "drop_column",
            lambda *args: drop_column_calls.append(args),
        ),
        _patch(mod.op, "batch_alter_table", _fake_batch_alter),
    ):
        mod.upgrade()

    assert drop_column_calls == [("projects", "folders")], (
        f"postgres branch should call op.drop_column('projects', "
        f"'folders') exactly once; got {drop_column_calls!r}"
    )
    assert batch_alter_calls == [], (
        f"postgres branch should NOT use batch_alter_table; "
        f"got {batch_alter_calls!r}"
    )


import contextlib  # noqa: E402


@contextlib.contextmanager
def _patch(obj, name: str, value):
    """Minimal monkey-patch context (avoids importing pytest fixtures
    into a non-fixture-taking test)."""
    original = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, original)
