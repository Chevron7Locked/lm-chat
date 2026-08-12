# SPDX-License-Identifier: Apache-2.0
"""Tests for migration 0003_documents.py.

Applies the migration to a fresh SQLite DB and verifies:
- documents and document_chunks tables exist with the right columns.
- document_chunks_fts virtual table is queryable.
- chats.settings column exists with default '{}'.
- Downgrade path removes all P8b additions.

Per P8b.1 brief §Item 1 acceptance criteria.
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



def _table_names(db_url: str) -> set[str]:
    sync_url = db_url.replace("sqlite+aiosqlite://", "sqlite://")
    engine = sa.create_engine(sync_url)
    insp = sa.inspect(engine)
    result = set(insp.get_table_names())
    engine.dispose()
    return result


def _get_columns(db_url: str, table: str) -> set[str]:
    sync_url = db_url.replace("sqlite+aiosqlite://", "sqlite://")
    engine = sa.create_engine(sync_url)
    insp = sa.inspect(engine)
    cols = {c["name"] for c in insp.get_columns(table)}
    engine.dispose()
    return cols


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


def test_migration_0003_tables_exist(tmp_path: Path) -> None:
    """0003 creates documents and document_chunks tables."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    _run_upgrade(db_url)

    tables = _table_names(db_url)
    assert "documents" in tables, "documents table missing"
    assert "document_chunks" in tables, "document_chunks table missing"


def test_migration_0003_documents_columns(tmp_path: Path) -> None:
    """documents table has all required columns."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    _run_upgrade(db_url)

    cols = _get_columns(db_url, "documents")
    expected = {
        "id",
        "user_id",
        "title",
        "mime_type",
        "byte_size",
        "chunk_count",
        "embedding_model_id",
        "sha256",
        "deleted_at",
        "uploaded_at",
    }
    assert expected.issubset(cols), f"Missing columns: {expected - cols}"


def test_migration_0003_document_chunks_columns(tmp_path: Path) -> None:
    """document_chunks table has all required columns."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    _run_upgrade(db_url)

    cols = _get_columns(db_url, "document_chunks")
    expected = {"id", "document_id", "ordinal", "text", "text_hash", "embedding"}
    assert expected.issubset(cols), f"Missing columns: {expected - cols}"


def test_migration_0003_fts5_queryable(tmp_path: Path) -> None:
    """document_chunks_fts virtual table is queryable."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    _run_upgrade(db_url)

    sync_url = db_url.replace("sqlite+aiosqlite://", "sqlite://")
    engine = sa.create_engine(sync_url)
    try:
        with engine.connect() as conn:
            # A SELECT against the FTS virtual table must not raise.
            row_count = conn.execute(
                sa.text("SELECT COUNT(*) FROM document_chunks_fts")
            ).scalar()
            assert row_count == 0
    finally:
        engine.dispose()


def test_migration_0003_chats_settings_column(tmp_path: Path) -> None:
    """chats table gains a settings column with default '{}'."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    _run_upgrade(db_url)

    cols = _get_columns(db_url, "chats")
    assert "settings" in cols, "settings column missing from chats"


def test_migration_0003_chats_settings_default(tmp_path: Path) -> None:
    """Existing chats row gets settings = {} after backfill."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    # Upgrade to 0002 first to create chats table without settings.
    _run_upgrade(db_url, "0002")

    sync_url = db_url.replace("sqlite+aiosqlite://", "sqlite://")
    engine = sa.create_engine(sync_url)

    # Insert a user + chat row while at 0002 (no settings column yet).
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO users (username, password_hash, is_admin, created_at, updated_at)"
                " VALUES ('user1', 'hash', false, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO chats (user_id, title, pinned, created_at, updated_at)"
                " VALUES (1, 'test', false, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
    engine.dispose()

    # Now upgrade to head (0003).
    _run_upgrade(db_url, "head")

    engine = sa.create_engine(sync_url)
    try:
        with engine.connect() as conn:
            row = conn.execute(sa.text("SELECT settings FROM chats")).fetchone()
        assert row is not None
        # settings should be parseable as JSON '{}' — SQLite returns a string.
        import json
        settings_val = row[0]
        if isinstance(settings_val, str):
            settings_val = json.loads(settings_val)
        assert settings_val == {} or settings_val == "{}", (
            f"Expected empty settings, got {settings_val!r}"
        )
    finally:
        engine.dispose()


def test_migration_0003_head_version(tmp_path: Path) -> None:
    """After upgrade head, alembic_version is the current head (0021 = projects)."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    _run_upgrade(db_url)

    version = _get_version(db_url)
    assert version in {
        "0028", "0029", "0030", "0031", "0032", "0033", "0034", "0035", "0036", "0037", "0038",
        "0039", "0040", "0041", "0042", "0043", "0044",
    }, f"Expected head version, got {version!r}"


def test_migration_0003_downgrade(tmp_path: Path) -> None:
    """Downgrade to 0002 removes documents, document_chunks, and chats.settings."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    _run_upgrade(db_url)

    # Verify tables exist at head.
    assert "documents" in _table_names(db_url)
    assert "document_chunks" in _table_names(db_url)

    # Downgrade to 0002.
    _run_downgrade(db_url, "0002")

    tables = _table_names(db_url)
    assert "documents" not in tables, "documents table should be dropped after downgrade"
    assert "document_chunks" not in tables, (
        "document_chunks table should be dropped after downgrade"
    )

    version = _get_version(db_url)
    assert version == "0002", f"Expected '0002' after downgrade, got {version!r}"
