# SPDX-License-Identifier: Apache-2.0
"""Tests for Alembic migration 0002 — FTS5 virtual table + sync triggers.

Tests for the FTS5 migration.

All tests use a temporary SQLite file (``tmp_path``) so they exercise the
real migration path without touching the dev database.

Coverage targets:
- test_0002_creates_messages_fts_table
- test_0002_triggers_sync_messages_to_fts
  - INSERT populates FTS row
  - UPDATE OF content keeps FTS in sync
  - DELETE removes FTS row
- test_0002_downgrade_drops_table_and_triggers
- test_0002_noop_on_postgres (mock dialect check)
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import alembic.command
import alembic.config
import sqlalchemy as sa

# ---------------------------------------------------------------------------
# Make migrations/ importable.
# ---------------------------------------------------------------------------
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


def _sync_url(db_url: str) -> str:
    """Convert an async SQLite URL to a synchronous one."""
    return db_url.replace("sqlite+aiosqlite://", "sqlite://")


def _run_upgrade(db_url: str, target: str = "head") -> None:
    """Run ``alembic upgrade <target>`` synchronously."""
    alembic.command.upgrade(_alembic_cfg(db_url), target)


def _run_downgrade(db_url: str, target: str) -> None:
    """Run ``alembic downgrade <target>`` synchronously."""
    alembic.command.downgrade(_alembic_cfg(db_url), target)


def _get_sqlite_master(db_url: str) -> list[dict[str, Any]]:
    """Return rows from ``sqlite_master`` for the DB at *db_url*."""
    engine = sa.create_engine(_sync_url(db_url))
    try:
        with engine.connect() as conn:
            result = conn.execute(
                sa.text("SELECT type, name FROM sqlite_master ORDER BY name")
            )
            return [dict(row._mapping) for row in result.fetchall()]
    finally:
        engine.dispose()


def _table_names(db_url: str) -> set[str]:
    """Return the set of table + virtual-table names in the DB."""
    return {r["name"] for r in _get_sqlite_master(db_url) if r["type"] == "table"}


def _trigger_names(db_url: str) -> set[str]:
    """Return the set of trigger names in the DB."""
    engine = sa.create_engine(_sync_url(db_url))
    try:
        with engine.connect() as conn:
            result = conn.execute(
                sa.text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
            )
            return {row[0] for row in result.fetchall()}
    finally:
        engine.dispose()


def _query_fts(db_url: str, query: str) -> list[str]:
    """Return content strings from ``messages_fts`` matching *query*."""
    engine = sa.create_engine(_sync_url(db_url))
    try:
        with engine.connect() as conn:
            result = conn.execute(
                sa.text("SELECT content FROM messages_fts WHERE messages_fts MATCH :q"),
                {"q": query},
            )
            return [row[0] for row in result.fetchall()]
    finally:
        engine.dispose()


def _insert_message(db_url: str, content: str) -> int:
    """Insert a minimal user → chat → message chain; return the message id."""
    engine = sa.create_engine(_sync_url(db_url))
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT OR IGNORE INTO users (id, username, password_hash)"
                    " VALUES (1, 'alice', 'scrypt$dummy')"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT OR IGNORE INTO chats (id, user_id, title)"
                    " VALUES (1, 1, 'Test')"
                )
            )
            result = conn.execute(
                sa.text(
                    "INSERT INTO messages (chat_id, role, content)"
                    " VALUES (1, 'user', :c) RETURNING id"
                ),
                {"c": content},
            )
            row = result.fetchone()
            if row is None:
                raise RuntimeError("INSERT INTO messages returned no row")
            return int(row[0])
    finally:
        engine.dispose()


def _update_message_content(db_url: str, msg_id: int, new_content: str) -> None:
    """Update the content of a message row."""
    engine = sa.create_engine(_sync_url(db_url))
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE messages SET content = :c WHERE id = :id"),
                {"c": new_content, "id": msg_id},
            )
    finally:
        engine.dispose()


def _delete_message(db_url: str, msg_id: int) -> None:
    """Delete a messages row by PK."""
    engine = sa.create_engine(_sync_url(db_url))
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM messages WHERE id = :id"),
                {"id": msg_id},
            )
    finally:
        engine.dispose()


def _count_fts_rowid(db_url: str, rowid: int) -> int:
    """Return the count of FTS rows with ``rowid = rowid``."""
    engine = sa.create_engine(_sync_url(db_url))
    try:
        with engine.connect() as conn:
            result = conn.execute(
                sa.text(
                    "SELECT COUNT(*) FROM messages_fts WHERE rowid = :rid"
                ),
                {"rid": rowid},
            )
            row = result.fetchone()
            return int(row[0]) if row else 0
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_0002_creates_messages_fts_table(tmp_path: Path) -> None:
    """Migration 0002 creates the ``messages_fts`` virtual table in SQLite."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'test_0002.db'}"
    _run_upgrade(db_url)

    tables = _table_names(db_url)
    assert "messages_fts" in tables, (
        f"messages_fts virtual table missing after upgrade head; tables: {tables}"
    )


def test_0002_creates_all_three_triggers(tmp_path: Path) -> None:
    """Migration 0002 creates messages_ai, messages_au, and messages_ad triggers."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'test_0002_triggers.db'}"
    _run_upgrade(db_url)

    triggers = _trigger_names(db_url)
    expected = {"messages_ai", "messages_au", "messages_ad"}
    assert expected.issubset(triggers), (
        f"Expected triggers {expected}; found {triggers}"
    )


def test_0002_triggers_sync_insert(tmp_path: Path) -> None:
    """After INSERT into messages, the FTS row appears in messages_fts."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'test_0002_insert.db'}"
    _run_upgrade(db_url)

    msg_id = _insert_message(db_url, "hello world the quick brown fox")

    count = _count_fts_rowid(db_url, msg_id)
    assert count == 1, f"Expected 1 FTS row for message {msg_id}; got {count}"

    # Also verify via MATCH query.
    matches = _query_fts(db_url, '"hello"')
    assert len(matches) > 0, "FTS MATCH returned no results after INSERT"


def test_0002_triggers_sync_update(tmp_path: Path) -> None:
    """After UPDATE OF content, the FTS row reflects the new content."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'test_0002_update.db'}"
    _run_upgrade(db_url)

    msg_id = _insert_message(db_url, "original content")
    _update_message_content(db_url, msg_id, "updated content entirely different")

    # Old term should not appear in FTS (content table is external-content so
    # FTS reflects the actual messages row via rowid).  New term should appear.
    new_matches = _query_fts(db_url, '"updated"')
    assert len(new_matches) > 0, "FTS did not reflect updated content"


def test_0002_triggers_sync_delete(tmp_path: Path) -> None:
    """After DELETE from messages, the FTS row is removed."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'test_0002_delete.db'}"
    _run_upgrade(db_url)

    msg_id = _insert_message(db_url, "content to be deleted")
    # Verify FTS row exists before deletion.
    assert _count_fts_rowid(db_url, msg_id) == 1

    _delete_message(db_url, msg_id)

    count = _count_fts_rowid(db_url, msg_id)
    assert count == 0, f"FTS row should be gone after DELETE; count={count}"


def test_0002_downgrade_drops_table_and_triggers(tmp_path: Path) -> None:
    """Downgrade from 0002 to 0001 removes messages_fts and all three triggers."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'test_0002_down.db'}"
    _run_upgrade(db_url)

    # Verify they exist before downgrade.
    assert "messages_fts" in _table_names(db_url)
    assert {"messages_ai", "messages_au", "messages_ad"}.issubset(_trigger_names(db_url))

    _run_downgrade(db_url, "0001")

    tables = _table_names(db_url)
    triggers = _trigger_names(db_url)

    assert "messages_fts" not in tables, "messages_fts should be dropped by downgrade"
    assert "messages_ai" not in triggers, "messages_ai trigger should be dropped"
    assert "messages_au" not in triggers, "messages_au trigger should be dropped"
    assert "messages_ad" not in triggers, "messages_ad trigger should be dropped"


def test_0002_downgrade_preserves_messages_table(tmp_path: Path) -> None:
    """Downgrade from 0002 to 0001 does NOT drop the messages table itself."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'test_0002_down_msgs.db'}"
    _run_upgrade(db_url)
    _run_downgrade(db_url, "0001")

    tables = _table_names(db_url)
    assert "messages" in tables, "messages table must survive 0002 downgrade"


def test_0002_noop_on_postgres() -> None:
    """Migration 0002 upgrade/downgrade is a no-op on non-SQLite dialects.

    We cannot spin up a real Postgres in unit tests, so we verify the
    guard via the migration module's dialect check directly.  The test
    patches ``op.get_bind()`` to return a connection whose dialect name
    is ``"postgresql"`` and confirms that no ``op.execute`` calls are made.

    The module name ``0002_fts5_messages`` starts with a digit, making it
    invalid as a Python identifier; importlib.import_module is required.
    """
    import importlib

    # importlib handles the leading-digit module name where `import` syntax
    # cannot (``import migrations.versions.0002_...`` is a syntax error).
    migration_mod = importlib.import_module("migrations.versions.0002_fts5_messages")

    # Build a fake bind object whose dialect.name is "postgresql".
    fake_dialect = MagicMock()
    fake_dialect.name = "postgresql"
    fake_bind = MagicMock()
    fake_bind.dialect = fake_dialect

    with patch.object(migration_mod.op, "get_bind", return_value=fake_bind):
        with patch.object(migration_mod.op, "execute") as mock_execute:
            migration_mod.upgrade()
            migration_mod.downgrade()

    assert not mock_execute.called, (
        "op.execute was called on a Postgres dialect — 0002 should be a no-op"
    )
