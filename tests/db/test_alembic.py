# SPDX-License-Identifier: Apache-2.0
"""Tests for the Alembic baseline migration and startup stamp logic.

All tests use a temporary SQLite file
(``tmp_path``) so they exercise the real migration path without touching
the dev database.

Coverage targets:
- test_baseline_upgrade_on_fresh_sqlite — fresh DB, upgrade head, verify tables.
- test_baseline_idempotent — running upgrade head twice is a no-op.
- test_baseline_fingerprint_matches_schema — SCHEMA_FINGERPRINT constant equals
  schema_fingerprint(metadata) for the current schema.py.
- test_ensure_schema_ready_fresh_db — startup logic on fresh DB runs upgrade.
- test_ensure_schema_ready_existing_consistent_db — consistent DB is a no-op.
"""
from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import alembic.command
import alembic.config
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from lmchat.db.fingerprint import schema_fingerprint
from lmchat.db.schema import metadata

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EXPECTED_TABLES = {
    "users",
    "sessions",
    "chats",
    "messages",
    "message_embeddings",
    "memory_insights",
    "audit_log",
    # Tables added by migration 0003.
    "documents",
    "document_chunks",
    # Table added by migration 0004.
    "prompts",
    # Tables added by migration 0005.
    "quotas",
    "quota_usage",
}

# Make migrations/ importable from the repo root even when sys.path
# doesn't include it yet (pytest is run from various CWDs in CI).
# tests/db/test_alembic.py → parents[0]=tests/db, [1]=tests, [2]=repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _alembic_cfg(db_url: str) -> alembic.config.Config:
    """Return an Alembic Config for *db_url* pointing at the repo's alembic.ini.

    Sets ``script_location`` as an absolute path so tests work regardless
    of the pytest working directory.
    """
    ini = _REPO_ROOT / "alembic.ini"
    cfg = alembic.config.Config(str(ini))
    cfg.set_main_option("sqlalchemy.url", db_url)
    # Absolute path so Alembic finds migrations/ regardless of CWD.
    cfg.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    return cfg


def _run_upgrade(db_url: str) -> None:
    """Run ``alembic upgrade head`` against *db_url* synchronously."""
    alembic.command.upgrade(_alembic_cfg(db_url), "head")


def _get_tables(db_url: str) -> set[str]:
    """Return the set of table names in the SQLite DB at *db_url*."""
    # Use the sync URL for inspection.
    sync_url = db_url.replace("sqlite+aiosqlite://", "sqlite://")
    engine = sa.create_engine(sync_url)
    insp = sa.inspect(engine)
    tables = set(insp.get_table_names())
    engine.dispose()
    return tables


def _get_alembic_version(db_url: str) -> str | None:
    """Return the current alembic_version or None if the table is absent."""
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


def test_baseline_fingerprint_matches_schema() -> None:
    """SCHEMA_FINGERPRINT in 0001_baseline.py must equal schema_fingerprint(metadata)."""
    mod = importlib.import_module("migrations.versions.0001_baseline")
    expected = schema_fingerprint(metadata)
    assert mod.SCHEMA_FINGERPRINT == expected, (
        f"Baseline fingerprint {mod.SCHEMA_FINGERPRINT!r} != "
        f"live metadata fingerprint {expected!r}. "
        "The migration is out of sync with db/schema.py."
    )


def test_baseline_upgrade_on_fresh_sqlite(tmp_path: Path) -> None:
    """Fresh SQLite DB: upgrade head creates all nine tables."""
    db_file = tmp_path / "test.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"

    _run_upgrade(db_url)

    tables = _get_tables(db_url)
    assert _EXPECTED_TABLES.issubset(tables), (
        f"Missing tables after upgrade: {_EXPECTED_TABLES - tables}"
    )

    # After migration 0014 (admin_invites) the head version is "0014" —
    # bumped to "0015" once the integration-default-on migration lands.
    version = _get_alembic_version(db_url)
    assert version in {"0014", "0015", "0016", "0017", "0018", "0019", "0020", "0021", "0022", "0022b", "0023a", "0023b", "0024", "0025", "0026", "0027", "0028", "0029", "0030", "0031", "0032", "0033", "0034", "0035", "0036", "0037", "0038", "0039", "0040", "0041", "0042", "0043", "0044", "0045", "0046"}, f"Expected head, got {version!r}"


def test_baseline_idempotent(tmp_path: Path) -> None:
    """Running upgrade head twice does not raise."""
    db_file = tmp_path / "test_idem.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"

    _run_upgrade(db_url)
    # Second call must be a no-op — already at head.
    _run_upgrade(db_url)

    version = _get_alembic_version(db_url)
    assert version in {"0014", "0015", "0016", "0017", "0018", "0019", "0020", "0021", "0022", "0022b", "0023a", "0023b", "0024", "0025", "0026", "0027", "0028", "0029", "0030", "0031", "0032", "0033", "0034", "0035", "0036", "0037", "0038", "0039", "0040", "0041", "0042", "0043", "0044", "0045", "0046"}  # head = 0044


@pytest.mark.asyncio
async def test_ensure_schema_ready_fresh_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ensure_schema_ready on a fresh DB runs upgrade and verifies fingerprint."""
    db_file = tmp_path / "fresh.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"

    # Patch get_settings() so startup.py uses our temp URL.
    import lmchat.config as config_mod
    import lmchat.db.startup as startup_mod

    class _FakeSettings:
        database_url = db_url

    monkeypatch.setattr(config_mod, "get_settings", lambda: _FakeSettings())
    # Also patch in startup module's import path.
    monkeypatch.setattr(startup_mod, "_build_alembic_cfg", lambda: _alembic_cfg(db_url))

    engine = create_async_engine(db_url)
    try:
        await startup_mod.ensure_schema_ready(engine)
    finally:
        await engine.dispose()

    tables = _get_tables(db_url)
    assert _EXPECTED_TABLES.issubset(tables)
    version = _get_alembic_version(db_url)
    assert version in {"0014", "0015", "0016", "0017", "0018", "0019", "0020", "0021", "0022", "0022b", "0023a", "0023b", "0024", "0025", "0026", "0027", "0028", "0029", "0030", "0031", "0032", "0033", "0034", "0035", "0036", "0037", "0038", "0039", "0040", "0041", "0042", "0043", "0044", "0045", "0046"}  # head = 0044


@pytest.mark.asyncio
async def test_ensure_schema_ready_existing_consistent_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ensure_schema_ready on a consistent existing DB is a no-op (no upgrade call)."""
    db_file = tmp_path / "existing.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"

    # Bootstrap the DB first.  Must be run in a thread because this is an
    # async test — asyncio.run() inside _run_upgrade → env.py would conflict
    # with the already-running pytest-asyncio event loop.
    await asyncio.to_thread(_run_upgrade, db_url)

    import lmchat.config as config_mod
    import lmchat.db.startup as startup_mod

    class _FakeSettings:
        database_url = db_url

    monkeypatch.setattr(config_mod, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(startup_mod, "_build_alembic_cfg", lambda: _alembic_cfg(db_url))

    # Track whether _run_upgrade_head was invoked.
    upgrade_called = False

    def _noop_upgrade() -> None:
        nonlocal upgrade_called
        upgrade_called = True

    monkeypatch.setattr(startup_mod, "_run_upgrade_head", _noop_upgrade)

    engine = create_async_engine(db_url)
    try:
        await startup_mod.ensure_schema_ready(engine)
    finally:
        await engine.dispose()

    assert not upgrade_called, (
        "ensure_schema_ready must not run upgrade on a consistent existing DB"
    )
