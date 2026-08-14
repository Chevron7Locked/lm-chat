# SPDX-License-Identifier: Apache-2.0
"""Migration 0036 — ``server_lm_studio_default`` app-level override columns.

Round-trip tests proving:
- upgrade to 0036 (head) adds the four override columns to
  ``server_lm_studio_default``.
- downgrade to 0035 drops them.
- re-upgrade restores them.
- the columns accept NULL and non-NULL values correctly.
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


def _insert_default_row(db_url: str) -> None:
    sync_url = db_url.replace("sqlite+aiosqlite://", "sqlite://")
    engine = sa.create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text("INSERT OR IGNORE INTO server_lm_studio_default (id) VALUES (1)"),
            )
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_0036_upgrade_adds_override_columns(tmp_path: Path) -> None:
    """Upgrade to 0036 (head) adds four override columns to server_lm_studio_default."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test_0036.db"
    _run_upgrade(db_url, "0036")

    cols = _columns(db_url, "server_lm_studio_default")
    expected = {
        "id",
        "memory_distillation_enabled",
        "subsession_memory_distillation_enabled",
        "web_search_provider",
        "searxng_url",
    }
    assert expected.issubset(cols), (
        f"expected {expected} in server_lm_studio_default columns after 0036, got {cols!r}"
    )
    assert _get_version(db_url) == "0036"


def test_0036_downgrade_drops_override_columns(tmp_path: Path) -> None:
    """Downgrade from 0036 to 0035 removes the four override columns."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test_0036_down.db"
    _run_upgrade(db_url, "0036")
    assert "memory_distillation_enabled" in _columns(db_url, "server_lm_studio_default")

    _run_downgrade(db_url, "0035")
    cols = _columns(db_url, "server_lm_studio_default")
    assert "memory_distillation_enabled" not in cols, (
        f"memory_distillation_enabled should be absent after downgrade to 0035, got {cols!r}"
    )
    assert _get_version(db_url) == "0035"


def test_0036_round_trip(tmp_path: Path) -> None:
    """upgrade 0036 → downgrade 0035 → upgrade head: columns round-trip cleanly."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test_0036_rt.db"
    _run_upgrade(db_url, "0036")
    assert "memory_distillation_enabled" in _columns(db_url, "server_lm_studio_default")

    _run_downgrade(db_url, "0035")
    assert "memory_distillation_enabled" not in _columns(db_url, "server_lm_studio_default")

    _run_upgrade(db_url, "head")
    assert "memory_distillation_enabled" in _columns(db_url, "server_lm_studio_default")
    # head advanced to 0037 (hybrid compaction) — accept either the 0036 that
    # this migration introduced or the current head.
    assert _get_version(db_url) in {
        "0036", "0037", "0038", "0039", "0040", "0041", "0042", "0043", "0044", "0045", "0046",
    }


def test_0036_columns_accept_null(tmp_path: Path) -> None:
    """Override columns are nullable — NULL is accepted without error."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test_0036_null.db"
    _run_upgrade(db_url, "0036")
    _insert_default_row(db_url)

    sync_url = db_url.replace("sqlite+aiosqlite://", "sqlite://")
    engine = sa.create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT OR IGNORE INTO server_lm_studio_default (id, "
                    "memory_distillation_enabled, subsession_memory_distillation_enabled, "
                    "web_search_provider, searxng_url) "
                    "VALUES (1, NULL, NULL, NULL, NULL)"
                ),
            )
        with engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT memory_distillation_enabled, subsession_memory_distillation_enabled, "
                    "web_search_provider, searxng_url "
                    "FROM server_lm_studio_default WHERE id = 1"
                )
            ).fetchone()
        assert row is not None
        assert row[0] is None
        assert row[1] is None
        assert row[2] is None
        assert row[3] is None
    finally:
        engine.dispose()


def test_0036_columns_accept_non_null_values(tmp_path: Path) -> None:
    """Override columns accept non-NULL values correctly."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test_0036_data.db"
    _run_upgrade(db_url, "0036")
    _insert_default_row(db_url)

    sync_url = db_url.replace("sqlite+aiosqlite://", "sqlite://")
    engine = sa.create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT OR REPLACE INTO server_lm_studio_default (id, "
                    "memory_distillation_enabled, subsession_memory_distillation_enabled, "
                    "web_search_provider, searxng_url) "
                    "VALUES (1, 1, 0, 'ddg', 'https://searxng.example.com/search')"
                ),
            )
        with engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT memory_distillation_enabled, subsession_memory_distillation_enabled, "
                    "web_search_provider, searxng_url "
                    "FROM server_lm_studio_default WHERE id = 1"
                )
            ).fetchone()
        assert row is not None
        assert row[0] == 1
        assert row[1] == 0
        assert row[2] == "ddg"
        assert row[3] == "https://searxng.example.com/search"
    finally:
        engine.dispose()
