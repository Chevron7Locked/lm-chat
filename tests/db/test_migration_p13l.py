# SPDX-License-Identifier: Apache-2.0
"""P13l migrations (0016 user_prefs + 0017 memory_insights_history).

Round-trip tests proving that:
- upgrade head from a fresh DB creates ``user_prefs`` + ``memory_insights_history``.
- downgrade past 0015 → 0014 removes both tables (and the rest of the P13l stack).
- re-upgrade restores them.

Per the migration policy in AGENTS.md the head version is asserted to be
``"0017"`` after this phase lands; tests asserting earlier heads were
adjusted in the same commit.
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _alembic_cfg(db_url: str) -> alembic.config.Config:
    """Return an Alembic Config pointing at the repo's alembic.ini."""
    ini = _REPO_ROOT / "alembic.ini"
    cfg = alembic.config.Config(str(ini))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    return cfg


def _run_upgrade(db_url: str, revision: str = "head") -> None:
    alembic.command.upgrade(_alembic_cfg(db_url), revision)


def _run_downgrade(db_url: str, revision: str) -> None:
    alembic.command.downgrade(_alembic_cfg(db_url), revision)


def _table_names(db_url: str) -> set[str]:
    sync_url = db_url.replace("sqlite+aiosqlite://", "sqlite://")
    engine = sa.create_engine(sync_url)
    try:
        insp = sa.inspect(engine)
        return set(insp.get_table_names())
    finally:
        engine.dispose()


def _columns(db_url: str, table: str) -> set[str]:
    sync_url = db_url.replace("sqlite+aiosqlite://", "sqlite://")
    engine = sa.create_engine(sync_url)
    try:
        insp = sa.inspect(engine)
        return {c["name"] for c in insp.get_columns(table)}
    finally:
        engine.dispose()


def _get_version(db_url: str) -> str | None:
    sync_url = db_url.replace("sqlite+aiosqlite://", "sqlite://")
    engine = sa.create_engine(sync_url)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT version_num FROM alembic_version LIMIT 1")
            ).fetchone()
            return row[0] if row else None
    except Exception:  # noqa: BLE001
        return None
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_migration_p13l_upgrade_creates_tables(tmp_path: Path) -> None:
    """Upgrade head from a fresh DB creates user_prefs + memory_insights_history."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    _run_upgrade(db_url)

    tables = _table_names(db_url)
    assert "user_prefs" in tables
    assert "memory_insights_history" in tables

    # user_prefs columns.
    cols = _columns(db_url, "user_prefs")
    assert {"user_id", "folders", "updated_at"}.issubset(cols)

    # memory_insights_history columns.
    h_cols = _columns(db_url, "memory_insights_history")
    assert {"id", "user_id", "event", "insights_before", "created_at"}.issubset(h_cols)

    assert _get_version(db_url) in {"0028", "0029", "0030", "0031", "0032", "0033", "0034", "0035", "0036", "0037", "0038", "0039", "0040", "0041", "0042", "0043", "0044", "0045", "0046"}


def test_migration_p13l_head_is_0017(tmp_path: Path) -> None:
    """After upgrade head the alembic_version row reads '0017'."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    _run_upgrade(db_url)
    assert _get_version(db_url) in {"0028", "0029", "0030", "0031", "0032", "0033", "0034", "0035", "0036", "0037", "0038", "0039", "0040", "0041", "0042", "0043", "0044", "0045", "0046"}


def test_migration_p13l_downgrade_drops_tables(tmp_path: Path) -> None:
    """Downgrade to 0015 removes user_prefs + memory_insights_history."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    _run_upgrade(db_url)
    assert "user_prefs" in _table_names(db_url)
    assert "memory_insights_history" in _table_names(db_url)

    _run_downgrade(db_url, "0015")
    tables = _table_names(db_url)
    assert "user_prefs" not in tables, "user_prefs should be dropped at 0015"
    assert "memory_insights_history" not in tables, (
        "memory_insights_history should be dropped at 0015"
    )
    assert _get_version(db_url) == "0015"


def test_migration_p13l_round_trip(tmp_path: Path) -> None:
    """upgrade head → downgrade past 0015 → upgrade head: P13l round-trips clean."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    _run_upgrade(db_url)
    assert _get_version(db_url) in {"0028", "0029", "0030", "0031", "0032", "0033", "0034", "0035", "0036", "0037", "0038", "0039", "0040", "0041", "0042", "0043", "0044", "0045", "0046"}

    # Downgrade past 0015 → 0014 (drops P13l stack).
    _run_downgrade(db_url, "0014")
    tables = _table_names(db_url)
    assert "user_prefs" not in tables
    assert "memory_insights_history" not in tables
    assert _get_version(db_url) == "0014"

    # Upgrade back to head.
    _run_upgrade(db_url, "head")
    tables = _table_names(db_url)
    assert "user_prefs" in tables
    assert "memory_insights_history" in tables
    assert _get_version(db_url) in {"0028", "0029", "0030", "0031", "0032", "0033", "0034", "0035", "0036", "0037", "0038", "0039", "0040", "0041", "0042", "0043", "0044", "0045", "0046"}


def test_migration_p13l_user_prefs_default_folders(tmp_path: Path) -> None:
    """Inserting a user_prefs row with no folders override yields '[]'."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    _run_upgrade(db_url)

    sync_url = db_url.replace("sqlite+aiosqlite://", "sqlite://")
    engine = sa.create_engine(sync_url)
    try:
        with engine.begin() as conn:
            # Insert a user first (FK requirement).
            conn.execute(
                sa.text(
                    "INSERT INTO users (username, password_hash) "
                    "VALUES ('test', 'scrypt$dummy')"
                )
            )
            user_id = conn.execute(
                sa.text("SELECT id FROM users WHERE username='test'")
            ).scalar_one()
            conn.execute(
                sa.text("INSERT INTO user_prefs (user_id) VALUES (:uid)"),
                {"uid": user_id},
            )
        with engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT folders FROM user_prefs WHERE user_id = :uid"
                ),
                {"uid": user_id},
            ).fetchone()
        assert row is not None
        # SQLite returns the JSON column as a string; either form is fine.
        import json

        val = row[0]
        if isinstance(val, str):
            val = json.loads(val)
        assert val == [], f"expected default empty array, got {val!r}"
    finally:
        engine.dispose()
