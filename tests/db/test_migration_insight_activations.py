# SPDX-License-Identifier: Apache-2.0
"""Tests for migration 0010_p12h_moat_truth_memory.py.

Applies the migration to a fresh SQLite DB and verifies:
- ``insight_activations`` table exists with the right columns + indexes.
- ``memory_insights`` gains the seven P12h score columns with sane defaults.
- Downgrade from 0010 → 0009 cleanly removes everything.
- Round-trip: upgrade head → downgrade -1 → upgrade head leaves the DB clean.

Per P12h brief §"Alembic migration: insight_activations table".
"""
from __future__ import annotations

import sys
from pathlib import Path

import alembic.command
import alembic.config
import sqlalchemy as sa

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _alembic_cfg(db_url: str) -> alembic.config.Config:
    ini = _REPO_ROOT / "alembic.ini"
    cfg = alembic.config.Config(str(ini))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    return cfg


def _run_upgrade(db_url: str, target: str = "head") -> None:
    alembic.command.upgrade(_alembic_cfg(db_url), target)


def _run_downgrade(db_url: str, target: str) -> None:
    alembic.command.downgrade(_alembic_cfg(db_url), target)


def _sync_url(db_url: str) -> str:
    return db_url.replace("sqlite+aiosqlite://", "sqlite://")


def _table_names(db_url: str) -> set[str]:
    engine = sa.create_engine(_sync_url(db_url))
    insp = sa.inspect(engine)
    result = set(insp.get_table_names())
    engine.dispose()
    return result


def _columns(db_url: str, table: str) -> set[str]:
    engine = sa.create_engine(_sync_url(db_url))
    insp = sa.inspect(engine)
    cols = {c["name"] for c in insp.get_columns(table)}
    engine.dispose()
    return cols


def _index_names(db_url: str, table: str) -> set[str]:
    engine = sa.create_engine(_sync_url(db_url))
    insp = sa.inspect(engine)
    idx: set[str] = set()
    for i in insp.get_indexes(table):
        name = i.get("name")
        if name:
            idx.add(name)
    engine.dispose()
    return idx


def _get_version(db_url: str) -> str | None:
    engine = sa.create_engine(_sync_url(db_url))
    try:
        with engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT version_num FROM alembic_version LIMIT 1")
            ).fetchone()
            return row[0] if row else None
    finally:
        engine.dispose()


def test_migration_0010_creates_insight_activations(tmp_path: Path) -> None:
    """0010 creates the insight_activations table."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    _run_upgrade(db_url)

    assert "insight_activations" in _table_names(db_url)


def test_migration_0010_activations_columns(tmp_path: Path) -> None:
    """insight_activations table has all required columns."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    _run_upgrade(db_url)

    cols = _columns(db_url, "insight_activations")
    expected = {"id", "message_id", "insight_id", "created_at"}
    assert expected.issubset(cols), f"Missing columns: {expected - cols}"


def test_migration_0010_activations_indexes(tmp_path: Path) -> None:
    """insight_activations has the message_id + insight_id indexes."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    _run_upgrade(db_url)

    idx = _index_names(db_url, "insight_activations")
    assert "idx_activations_message" in idx
    assert "idx_activations_insight" in idx


def test_migration_0010_memory_insights_score_columns(tmp_path: Path) -> None:
    """memory_insights gains the seven P12h score-bearing columns."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    _run_upgrade(db_url)

    cols = _columns(db_url, "memory_insights")
    p12h_new_cols = {
        "category",
        "use_count",
        "ups",
        "downs",
        "last_used",
        "last_feedback_at",
        "state",
    }
    assert p12h_new_cols.issubset(cols), f"Missing P12h columns: {p12h_new_cols - cols}"


def test_migration_0010_head_version(tmp_path: Path) -> None:
    """After upgrade head, alembic_version is the current head (0021 = projects)."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    _run_upgrade(db_url)

    version = _get_version(db_url)
    assert version in {
        "0028", "0029", "0030", "0031", "0032", "0033", "0034", "0035", "0036", "0037", "0038", "0039", "0040", "0041", "0042", "0043", "0044"
    }, f"Expected head version, got {version!r}"


def test_migration_0010_downgrade(tmp_path: Path) -> None:
    """Downgrade from 0010 → 0009 drops insight_activations + removes P12h columns."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    _run_upgrade(db_url)
    assert "insight_activations" in _table_names(db_url)

    _run_downgrade(db_url, "0009")

    tables = _table_names(db_url)
    assert "insight_activations" not in tables, "table should be dropped after downgrade"

    cols = _columns(db_url, "memory_insights")
    p12h_new_cols = {
        "category",
        "use_count",
        "ups",
        "downs",
        "last_used",
        "last_feedback_at",
        "state",
    }
    assert not (p12h_new_cols & cols), (
        f"P12h columns should be removed after downgrade; still present: "
        f"{p12h_new_cols & cols}"
    )

    version = _get_version(db_url)
    assert version == "0009", f"Expected '0009' after downgrade, got {version!r}"


def test_migration_0010_round_trip(tmp_path: Path) -> None:
    """upgrade head → downgrade to 0009 → upgrade head: 0010 round-trips cleanly.

    Asserts the 0010 migration (insight_activations + memory_insights score
    columns) is reversible even with subsequent migrations (0011 P13i
    incognito + 0012 P13j chat_shares + 0013 P13g LM Studio overrides + 0014
    P13k admin_invites + 0015 P13h integration default-on + 0016 P13l
    user_prefs + 0017 P13l memory_insights_history) sitting on top.
    """
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    _run_upgrade(db_url, "head")
    assert "insight_activations" in _table_names(db_url)
    assert _get_version(db_url) in {"0028", "0029", "0030", "0031", "0032", "0033", "0034", "0035", "0036", "0037", "0038", "0039", "0040", "0041", "0042", "0043", "0044"}

    # Downgrade past 0010 to 0009 — drops head → 0010 in order, including
    # the insight_activations table + memory_insights score columns.
    _run_downgrade(db_url, "0009")
    assert "insight_activations" not in _table_names(db_url)
    assert _get_version(db_url) == "0009"

    _run_upgrade(db_url, "head")
    assert "insight_activations" in _table_names(db_url)
    assert _get_version(db_url) in {"0028", "0029", "0030", "0031", "0032", "0033", "0034", "0035", "0036", "0037", "0038", "0039", "0040", "0041", "0042", "0043", "0044"}
