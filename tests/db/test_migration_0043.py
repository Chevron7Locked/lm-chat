# SPDX-License-Identifier: Apache-2.0
"""Tests for migration 0043 — ``memory_insights.last_active_epoch``.

Adds a nullable Float ``last_active_epoch`` column and backfills existing
rows with ``COALESCE(last_used, epoch(created_at))``.

Pins:
1. Column is present after upgrade to 0043.
2. A pre-existing row with ``last_used`` set is backfilled to that value
   (unaffected by ``created_at``).
3. A pre-existing row with ``last_used`` NULL is backfilled from its own
   ``created_at``, treated as UTC — proven under a forced non-UTC ``TZ``
   so a reintroduction of the naive-datetime-as-local-time bug
   (``ensure_utc``'s Python-side fix; see
   ``lmchat.services.memory_service._recency_order_expr``) would fail
   this test even on a CI box that already runs in UTC.
4. ``downgrade()`` drops the column cleanly.
"""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

import alembic.command
import alembic.config
import pytest
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


def _insert_user_and_insight(
    con: sqlite3.Connection,
    *,
    insight_id: int,
    last_used: float | None,
    created_at: str,
) -> None:
    con.execute(
        "INSERT OR IGNORE INTO users (id, username, password_hash)"
        " VALUES (1, 'u', 'ph')"
    )
    con.execute(
        "INSERT INTO memory_insights (id, user_id, text, text_hash, pinned,"
        " category, use_count, ups, downs, last_used, last_feedback_at,"
        " state, created_at)"
        " VALUES (:id, 1, 't', :th, 0, 'context', 0, 0, 0, :lu, NULL,"
        " 'active', :ca)",
        {
            "id": insight_id,
            "th": f"{insight_id:064x}",
            "lu": last_used,
            "ca": created_at,
        },
    )
    con.commit()


def test_0043_adds_last_active_epoch_column(tmp_path: Path) -> None:
    """After upgrade to 0043, ``memory_insights.last_active_epoch`` exists."""
    db = tmp_path / "test_0043_upgrade.db"
    _upgrade(db, "0043")
    eng = sa.create_engine(f"sqlite:///{db}")
    try:
        assert "last_active_epoch" in _columns(eng, "memory_insights")
    finally:
        eng.dispose()


def test_0043_backfills_from_last_used_when_set(tmp_path: Path) -> None:
    """A row with last_used set backfills last_active_epoch to that value,
    NOT to created_at's epoch — last_used always wins when present.
    """
    db = tmp_path / "test_0043_backfill_last_used.db"
    _upgrade(db, "0042")

    last_used = 1_700_000_000.0
    # created_at is deliberately far from last_used so a wrong backfill
    # (using created_at instead of last_used) is unambiguous.
    con = _raw(db)
    try:
        _insert_user_and_insight(
            con,
            insight_id=1,
            last_used=last_used,
            created_at="2020-01-01 00:00:00.000000",
        )
    finally:
        con.close()

    _upgrade(db, "0043")

    con = _raw(db)
    try:
        row = con.execute(
            "SELECT last_active_epoch FROM memory_insights WHERE id = 1"
        ).fetchone()
        assert row is not None
        assert row[0] == last_used
    finally:
        con.close()


def test_0043_backfills_from_created_at_when_last_used_null(
    tmp_path: Path,
) -> None:
    """A row that was never recalled (last_used NULL) backfills
    last_active_epoch from its own created_at, interpreted as UTC.

    Forces a non-UTC host TZ so this would fail if the backfill
    reintroduced the naive-datetime-as-local-time bug (the SQL-side
    equivalent of the fix `lmchat.utils.clock.ensure_utc` applies on the
    Python side) — a host already running in UTC would not otherwise
    exercise this at all.
    """
    db = tmp_path / "test_0043_backfill_created_at.db"
    _upgrade(db, "0042")

    original_tz = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Shanghai"  # UTC+8, no DST.
    time.tzset()
    try:
        created_at_str = "2026-03-15 08:00:00.000000"
        con = _raw(db)
        try:
            _insert_user_and_insight(
                con,
                insight_id=1,
                last_used=None,
                created_at=created_at_str,
            )
        finally:
            con.close()

        _upgrade(db, "0043")

        con = _raw(db)
        try:
            row = con.execute(
                "SELECT last_active_epoch FROM memory_insights WHERE id = 1"
            ).fetchone()
        finally:
            con.close()
    finally:
        if original_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_tz
        time.tzset()

    assert row is not None
    assert row[0] is not None

    # The stored text is a naive UTC wall-clock value (matching what
    # SQLite hands back for a func.now()-written DateTime(timezone=True)
    # column) — the correct epoch treats it AS UTC, not as UTC+8 local.
    expected_epoch = (
        datetime.strptime(created_at_str, "%Y-%m-%d %H:%M:%S.%f")
        .replace(tzinfo=UTC)
        .timestamp()
    )
    assert row[0] == pytest.approx(expected_epoch, abs=1.0)


def test_0043_downgrade_drops_column(tmp_path: Path) -> None:
    """Downgrading from 0043 to 0042 drops last_active_epoch."""
    db = tmp_path / "test_0043_downgrade.db"
    _upgrade(db, "0043")

    eng = sa.create_engine(f"sqlite:///{db}")
    try:
        assert "last_active_epoch" in _columns(eng, "memory_insights")
    finally:
        eng.dispose()

    _downgrade(db, "0042")

    eng = sa.create_engine(f"sqlite:///{db}")
    try:
        assert "last_active_epoch" not in _columns(eng, "memory_insights")
    finally:
        eng.dispose()
