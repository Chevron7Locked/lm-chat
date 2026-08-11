# SPDX-License-Identifier: Apache-2.0
"""Migration 0031 — ``user_prefs.preset_models`` column.

Round-trip tests proving:
- upgrade to 0031 adds the ``preset_models`` nullable JSON column to
  ``user_prefs``.
- downgrade to 0030 drops it.
- re-upgrade restores it.
- the column accepts a JSON dict and NULL correctly.
"""
from __future__ import annotations

import json
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
    ini = _REPO_ROOT / "alembic.ini"
    cfg = alembic.config.Config(str(ini))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    return cfg


def _run_upgrade(db_url: str, revision: str = "head") -> None:
    alembic.command.upgrade(_alembic_cfg(db_url), revision)


def _run_downgrade(db_url: str, revision: str) -> None:
    alembic.command.downgrade(_alembic_cfg(db_url), revision)


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


def _columns(db_url: str, table: str) -> set[str]:
    sync_url = db_url.replace("sqlite+aiosqlite://", "sqlite://")
    engine = sa.create_engine(sync_url)
    try:
        insp = sa.inspect(engine)
        return {c["name"] for c in insp.get_columns(table)}
    finally:
        engine.dispose()


def _insert_user(db_url: str, user_id: int = 1) -> None:
    sync_url = db_url.replace("sqlite+aiosqlite://", "sqlite://")
    engine = sa.create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT OR IGNORE INTO users (id, username, password_hash, is_admin) "
                    "VALUES (:id, :u, :pw, 0)"
                ),
                {"id": user_id, "u": f"user{user_id}", "pw": "x"},
            )
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_0031_upgrade_adds_preset_models_column(tmp_path: Path) -> None:
    """Upgrade to 0031 (head) adds preset_models column to user_prefs."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test_0031.db"
    _run_upgrade(db_url, "0031")

    cols = _columns(db_url, "user_prefs")
    assert "preset_models" in cols, (
        f"expected preset_models in user_prefs columns after 0031, got {cols!r}"
    )
    # Pre-existing columns must still be present.
    assert {"user_id", "folders", "updated_at"}.issubset(cols)
    assert _get_version(db_url) in {
        "0031", "0032", "0033", "0034", "0035", "0036",
        "0037", "0038", "0039", "0040", "0041", "0042",
    }


def test_0031_downgrade_drops_preset_models_column(tmp_path: Path) -> None:
    """Downgrade from 0031 to 0030 removes preset_models from user_prefs."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test_0031_down.db"
    _run_upgrade(db_url, "0031")
    assert "preset_models" in _columns(db_url, "user_prefs")

    _run_downgrade(db_url, "0030")
    cols = _columns(db_url, "user_prefs")
    assert "preset_models" not in cols, (
        f"preset_models should be absent after downgrade to 0030, got {cols!r}"
    )
    assert _get_version(db_url) == "0030"


def test_0031_round_trip(tmp_path: Path) -> None:
    """upgrade 0031 → downgrade 0030 → upgrade head: column round-trips cleanly."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test_0031_rt.db"
    _run_upgrade(db_url, "0031")
    assert "preset_models" in _columns(db_url, "user_prefs")

    _run_downgrade(db_url, "0030")
    assert "preset_models" not in _columns(db_url, "user_prefs")

    _run_upgrade(db_url, "head")
    assert "preset_models" in _columns(db_url, "user_prefs")
    assert _get_version(db_url) in {
        "0031", "0032", "0033", "0034", "0035", "0036",
        "0037", "0038", "0039", "0040", "0041", "0042", "0043",
    }


def test_0031_column_accepts_json_dict(tmp_path: Path) -> None:
    """preset_models accepts a JSON dict and NULL correctly."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test_0031_data.db"
    _run_upgrade(db_url, "0031")
    _insert_user(db_url, user_id=1)

    sync_url = db_url.replace("sqlite+aiosqlite://", "sqlite://")
    engine = sa.create_engine(sync_url)
    mapping = {
        "general": {"provider": "lmstudio", "model_id": "phi-4"},
        "research": {"provider": "openrouter", "model_id": "qwen/qwq"},
    }
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO user_prefs (user_id, folders, preset_models, updated_at) "
                    "VALUES (:uid, '[]', :pm, datetime('now'))"
                ),
                {"uid": 1, "pm": json.dumps(mapping)},
            )
        with engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT preset_models FROM user_prefs WHERE user_id = 1")
            ).fetchone()
        assert row is not None
        raw = row[0]
        if isinstance(raw, str):
            parsed = json.loads(raw)
        else:
            parsed = raw
        assert parsed == mapping, f"round-trip mismatch: {parsed!r}"
    finally:
        engine.dispose()


def test_0031_column_nullable_accepts_null(tmp_path: Path) -> None:
    """preset_models is nullable — NULL is accepted without error."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test_0031_null.db"
    _run_upgrade(db_url, "0031")
    _insert_user(db_url, user_id=2)

    sync_url = db_url.replace("sqlite+aiosqlite://", "sqlite://")
    engine = sa.create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO user_prefs (user_id, folders, preset_models, updated_at) "
                    "VALUES (:uid, '[]', NULL, datetime('now'))"
                ),
                {"uid": 2},
            )
        with engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT preset_models FROM user_prefs WHERE user_id = 2")
            ).fetchone()
        assert row is not None
        assert row[0] is None, f"expected NULL, got {row[0]!r}"
    finally:
        engine.dispose()
