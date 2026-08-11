# SPDX-License-Identifier: Apache-2.0
"""Tests for migration 0022b — memory_insights_history project_id contract.

0022b is a no-op DDL — the
``memory_insights_history.insights_before`` JSON column accepts any
list-of-dicts shape, and the migration just advances the Alembic head
to record that the tuple shape extends from
``{id, text, created_at}`` to ``{id, text, created_at, project_id}``.

The contract this test pins is the BACKWARD-COMPAT read:
``MemoryService.restore_from_history`` uses ``entry.get("project_id")``,
so legacy snapshots written before the project_id key was added restore
as un-projected (``project_id IS NULL``). The migration must not break
that path — which means after 0022b runs, a JSON insert with the LEGACY
3-key shape must round-trip cleanly through the history table.
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


def _upgrade(db: Path, rev: str = "0022b") -> None:
    alembic.command.upgrade(
        _alembic_cfg(f"sqlite+aiosqlite:///{db}"), rev
    )


def _downgrade(db: Path, rev: str) -> None:
    alembic.command.downgrade(
        _alembic_cfg(f"sqlite+aiosqlite:///{db}"), rev
    )


def test_0022b_is_a_noop_ddl(tmp_path: Path) -> None:
    """Upgrade to 0022b adds no columns to memory_insights_history."""
    db = tmp_path / "test_0022b_noop.db"
    _upgrade(db, "0022")
    eng = sa.create_engine(f"sqlite:///{db}")
    try:
        before_cols = {
            c["name"]
            for c in sa.inspect(eng).get_columns(
                "memory_insights_history"
            )
        }
    finally:
        eng.dispose()

    _upgrade(db, "0022b")
    eng = sa.create_engine(f"sqlite:///{db}")
    try:
        after_cols = {
            c["name"]
            for c in sa.inspect(eng).get_columns(
                "memory_insights_history"
            )
        }
    finally:
        eng.dispose()

    assert before_cols == after_cols, (
        f"0022b added DDL: before={before_cols!r} after={after_cols!r}"
    )


def test_0022b_accepts_legacy_three_key_snapshot(tmp_path: Path) -> None:
    """Legacy snapshots use the 3-key shape
    ``{id, text, created_at}``. After 0022b, that shape must still
    round-trip through ``insights_before`` cleanly — the BC read
    in ``MemoryService.restore_from_history`` depends on it."""
    db = tmp_path / "test_0022b_legacy.db"
    _upgrade(db)
    eng = sa.create_engine(f"sqlite:///{db}")
    try:
        with eng.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO users (id, username, password_hash) "
                "VALUES (1, 'alice', 'scrypt$dummy')"
            )
            legacy_snapshot = [
                {"id": 1, "text": "alpha", "created_at": 100.0},
                {"id": 2, "text": "beta", "created_at": 101.0},
            ]
            conn.exec_driver_sql(
                "INSERT INTO memory_insights_history "
                "(user_id, event, insights_before, created_at) "
                "VALUES (1, 'refine', :ib, 0)",
                {"ib": json.dumps(legacy_snapshot)},
            )
        with eng.connect() as conn:
            row = conn.exec_driver_sql(
                "SELECT insights_before FROM memory_insights_history "
                "WHERE user_id = 1"
            ).fetchone()
        assert row is not None
        decoded = json.loads(row[0])
        assert decoded == legacy_snapshot
        for entry in decoded:
            assert "project_id" not in entry, (
                f"legacy snapshot leaked project_id: {entry!r}"
            )
    finally:
        eng.dispose()


def test_0022b_accepts_phase9_four_key_snapshot(tmp_path: Path) -> None:
    """Newer snapshots add the ``project_id`` key. The JSON column
    must accept the extended shape."""
    db = tmp_path / "test_0022b_phase9.db"
    _upgrade(db)
    eng = sa.create_engine(f"sqlite:///{db}")
    try:
        with eng.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO users (id, username, password_hash) "
                "VALUES (1, 'alice', 'scrypt$dummy')"
            )
            phase9_snapshot = [
                {
                    "id": 1,
                    "text": "alpha",
                    "created_at": 100.0,
                    "project_id": 42,
                },
                {
                    "id": 2,
                    "text": "beta",
                    "created_at": 101.0,
                    "project_id": None,
                },
            ]
            conn.exec_driver_sql(
                "INSERT INTO memory_insights_history "
                "(user_id, event, insights_before, created_at) "
                "VALUES (1, 'refine', :ib, 0)",
                {"ib": json.dumps(phase9_snapshot)},
            )
        with eng.connect() as conn:
            row = conn.exec_driver_sql(
                "SELECT insights_before FROM memory_insights_history "
                "WHERE user_id = 1"
            ).fetchone()
        assert row is not None
        decoded = json.loads(row[0])
        assert decoded == phase9_snapshot
    finally:
        eng.dispose()


def test_0022b_downgrade_to_0022_is_noop(tmp_path: Path) -> None:
    """Downgrading 0022b → 0022 is a no-op; snapshots that carry
    ``project_id`` still read cleanly (the key is just ignored)."""
    db = tmp_path / "test_0022b_downgrade.db"
    _upgrade(db)
    eng = sa.create_engine(f"sqlite:///{db}")
    try:
        with eng.connect() as conn:
            cols_before = {
                c["name"]
                for c in sa.inspect(conn).get_columns(
                    "memory_insights_history"
                )
            }
    finally:
        eng.dispose()

    _downgrade(db, "0022")
    eng = sa.create_engine(f"sqlite:///{db}")
    try:
        cols_after = {
            c["name"]
            for c in sa.inspect(eng).get_columns(
                "memory_insights_history"
            )
        }
    finally:
        eng.dispose()

    assert cols_before == cols_after
