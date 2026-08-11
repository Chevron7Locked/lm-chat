# SPDX-License-Identifier: Apache-2.0
"""Tests for migration 0021 — Projects v1 schema additions.

Coverage targets:

1. test_projects_table_created — after upgrade, the projects table
   exists with the expected columns and the user_id index.
2. test_project_id_columns_added — chats / documents / memory_insights
   each gained a nullable project_id column with its index.
3. test_existing_rows_default_to_null — pre-existing chats /
   documents / memory_insights rows have project_id IS NULL after
   the upgrade.
4. test_set_null_on_project_delete — the SQLite ON DELETE SET NULL
   verification gate from the spec. Insert a project, set chats /
   documents / memory_insights.project_id to it, DELETE FROM
   projects, assert all three FKs flipped to NULL.
5. test_downgrade_restores_pre_state — downgrade drops the columns
   and the projects table; upgrading again from the same fresh DB
   ends up at the same final state.
6. test_chats_indexes_survive_batch_alter — chats has multiple
   pre-existing indexes (ix_chats_user_id, etc.). batch_alter_table
   recreation must preserve them.
"""
from __future__ import annotations

import sys
import time
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


def _sync_url(db_file: Path) -> str:
    return f"sqlite:///{db_file}"


def _upgrade(db_file: Path, rev: str = "head") -> None:
    alembic.command.upgrade(
        _alembic_cfg(f"sqlite+aiosqlite:///{db_file}"), rev
    )


def _downgrade(db_file: Path, rev: str) -> None:
    alembic.command.downgrade(
        _alembic_cfg(f"sqlite+aiosqlite:///{db_file}"), rev
    )


def _columns(engine: sa.Engine, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns(table)}


def _indexes(engine: sa.Engine, table: str) -> set[str]:
    # get_indexes can return name=None for anonymous unique constraints;
    # filter those out — every index we care about is named.
    names: set[str] = set()
    for i in sa.inspect(engine).get_indexes(table):
        name = i.get("name")
        if isinstance(name, str):
            names.add(name)
    return names


def _enable_fks(conn: sa.Connection) -> None:
    """PRAGMA foreign_keys=ON — lifespan does this in prod; tests must too."""
    conn.exec_driver_sql("PRAGMA foreign_keys = ON")


def test_projects_table_created(tmp_path: Path) -> None:
    """After upgrade head, projects exists with the expected columns + index."""
    db = tmp_path / "test.db"
    _upgrade(db)
    eng = sa.create_engine(_sync_url(db))
    try:
        assert "projects" in sa.inspect(eng).get_table_names()
        cols = _columns(eng, "projects")
        # Migration 0022 added the ``embedding_model_id`` +
        # ``default_model_id`` nullable Text columns. Migration 0023a
        # added ``rag_threshold`` Integer + ``meta`` JSON. Migration
        # 0023b DROPPED ``folders``. Migration 0038 added
        # ``archived_at``. Migration 0039 added the rolling auto-summary
        # columns. The test asserts AFTER head; the column set reflects
        # 0021 + 0022 + 0023a + 0023b + 0038 + 0039.
        assert cols == {
            "id",
            "user_id",
            "name",
            "description",
            "system_prompt",
            "embedding_model_id",
            "default_model_id",
            "rag_threshold",
            "meta",
            "created_at",
            "updated_at",
            "archived_at",
            "summary",
            "summary_updated_at",
            "summary_message_watermark",
        }, f"projects columns mismatch: {cols}"
        assert "ix_projects_user_id" in _indexes(eng, "projects")
    finally:
        eng.dispose()


def test_project_id_columns_added(tmp_path: Path) -> None:
    """All three child tables gained nullable project_id + an index."""
    db = tmp_path / "test.db"
    _upgrade(db)
    eng = sa.create_engine(_sync_url(db))
    try:
        for table, idx in (
            ("chats", "ix_chats_project_id"),
            ("documents", "ix_documents_project_id"),
            ("memory_insights", "ix_memory_insights_project_id"),
        ):
            assert "project_id" in _columns(eng, table), (
                f"{table} missing project_id column"
            )
            assert idx in _indexes(eng, table), (
                f"{table} missing {idx} index"
            )
    finally:
        eng.dispose()


def test_existing_rows_default_to_null(tmp_path: Path) -> None:
    """Pre-existing rows in ALL three child tables get project_id IS NULL.

    The original test only checked chats; bugs specific to the
    documents or memory_insights batch_alter_table column-add would
    have passed silently. This now exercises all three.
    """
    db = tmp_path / "test.db"
    _upgrade(db, rev="0020")
    eng = sa.create_engine(_sync_url(db))
    try:
        with eng.begin() as conn:
            _enable_fks(conn)
            conn.exec_driver_sql(
                "INSERT INTO users (username, password_hash, created_at, is_admin) "
                "VALUES ('alice', 'x', CURRENT_TIMESTAMP, 0)"
            )
            user_id = conn.exec_driver_sql(
                "SELECT id FROM users WHERE username='alice'"
            ).scalar_one()
            conn.exec_driver_sql(
                "INSERT INTO chats (user_id, title, pinned, "
                "created_at, updated_at, settings, display_order, "
                "model_id, incognito, incognito_expires_at) "
                "VALUES (:uid, 'chat', 0, CURRENT_TIMESTAMP, "
                "CURRENT_TIMESTAMP, '{}', 0, NULL, 0, NULL)",
                {"uid": user_id},
            )
            conn.exec_driver_sql(
                "INSERT INTO documents (user_id, title, mime_type, "
                "byte_size, chunk_count, embedding_model_id, sha256, "
                "deleted_at, uploaded_at) "
                "VALUES (:uid, 'doc', 'text/plain', 1, 0, '', "
                "'sha', NULL, CURRENT_TIMESTAMP)",
                {"uid": user_id},
            )
            conn.exec_driver_sql(
                "INSERT INTO memory_insights (user_id, text, text_hash, "
                "pinned, created_at, category) "
                "VALUES (:uid, 'i', 'ih', 0, CURRENT_TIMESTAMP, 'context')",
                {"uid": user_id},
            )
    finally:
        eng.dispose()
    _upgrade(db)
    eng = sa.create_engine(_sync_url(db))
    try:
        with eng.connect() as conn:
            for table in ("chats", "documents", "memory_insights"):
                row = conn.exec_driver_sql(
                    f"SELECT project_id FROM {table} LIMIT 1"
                ).fetchone()
                assert row is not None, f"no rows in {table} after migration"
                assert row[0] is None, (
                    f"Pre-existing {table} row must have project_id IS NULL "
                    f"after migration, got {row[0]!r}"
                )
    finally:
        eng.dispose()


def test_set_null_on_project_delete(tmp_path: Path) -> None:
    """Spec verification gate: deleting a project flips all FKs to NULL."""
    db = tmp_path / "test.db"
    _upgrade(db)
    eng = sa.create_engine(_sync_url(db))
    try:
        with eng.begin() as conn:
            _enable_fks(conn)
            # User → project → child rows referencing project.
            conn.exec_driver_sql(
                "INSERT INTO users (username, password_hash, created_at, is_admin) "
                "VALUES ('bob', 'x', CURRENT_TIMESTAMP, 0)"
            )
            user_id = conn.exec_driver_sql(
                "SELECT id FROM users WHERE username='bob'"
            ).scalar_one()
            now = time.time()
            # The folders column was dropped in migration 0023b;
            # INSERT no longer mentions it.
            conn.exec_driver_sql(
                "INSERT INTO projects (user_id, name, description, "
                "system_prompt, created_at, updated_at) "
                "VALUES (:uid, 'P', '', '', :now, :now)",
                {"uid": user_id, "now": now},
            )
            project_id = conn.exec_driver_sql(
                "SELECT id FROM projects WHERE name='P'"
            ).scalar_one()
            conn.exec_driver_sql(
                "INSERT INTO chats (user_id, title, pinned, created_at, "
                "updated_at, settings, display_order, model_id, incognito, "
                "incognito_expires_at, project_id) "
                "VALUES (:uid, 'c', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, "
                "'{}', 0, NULL, 0, NULL, :pid)",
                {"uid": user_id, "pid": project_id},
            )
            conn.exec_driver_sql(
                "INSERT INTO documents (user_id, title, mime_type, "
                "byte_size, chunk_count, embedding_model_id, sha256, "
                "deleted_at, uploaded_at, project_id) "
                "VALUES (:uid, 'd', 'text/plain', 1, 0, '', "
                "'sha', NULL, CURRENT_TIMESTAMP, :pid)",
                {"uid": user_id, "pid": project_id},
            )
            conn.exec_driver_sql(
                "INSERT INTO memory_insights (user_id, text, text_hash, "
                "pinned, created_at, category, project_id) "
                "VALUES (:uid, 't', 'th', 0, CURRENT_TIMESTAMP, "
                "'context', :pid)",
                {"uid": user_id, "pid": project_id},
            )

            # Sanity: all three FKs point at the project.
            assert conn.exec_driver_sql(
                "SELECT project_id FROM chats WHERE title='c'"
            ).scalar() == project_id
            assert conn.exec_driver_sql(
                "SELECT project_id FROM documents WHERE title='d'"
            ).scalar() == project_id
            assert conn.exec_driver_sql(
                "SELECT project_id FROM memory_insights WHERE text='t'"
            ).scalar() == project_id

        # Delete the project — FK cascade should null all three references.
        with eng.begin() as conn:
            _enable_fks(conn)
            conn.exec_driver_sql(
                "DELETE FROM projects WHERE id = :pid",
                {"pid": project_id},
            )

        with eng.connect() as conn:
            _enable_fks(conn)
            assert conn.exec_driver_sql(
                "SELECT project_id FROM chats WHERE title='c'"
            ).scalar() is None, (
                "chats.project_id should be NULL after project delete"
            )
            assert conn.exec_driver_sql(
                "SELECT project_id FROM documents WHERE title='d'"
            ).scalar() is None, (
                "documents.project_id should be NULL after project delete"
            )
            assert conn.exec_driver_sql(
                "SELECT project_id FROM memory_insights WHERE text='t'"
            ).scalar() is None, (
                "memory_insights.project_id should be NULL after project delete"
            )
    finally:
        eng.dispose()


def test_downgrade_restores_pre_state(tmp_path: Path) -> None:
    """Downgrade 0021 → 0020 drops the new columns and projects table."""
    db = tmp_path / "test.db"
    _upgrade(db)
    _downgrade(db, "0020")
    eng = sa.create_engine(_sync_url(db))
    try:
        tables = set(sa.inspect(eng).get_table_names())
        assert "projects" not in tables
        for table in ("chats", "documents", "memory_insights"):
            assert "project_id" not in _columns(eng, table), (
                f"{table}.project_id should be dropped after downgrade"
            )
    finally:
        eng.dispose()


def test_upgrade_after_downgrade_idempotent(tmp_path: Path) -> None:
    """Upgrade head → downgrade 0020 → upgrade head reaches the same shape.

    Re-running upgrade after a downgrade must restore the same final
    state. If index recreation collides with a leftover name, this
    test catches it.
    """
    db = tmp_path / "test.db"
    _upgrade(db)
    _downgrade(db, "0020")
    _upgrade(db)
    eng = sa.create_engine(_sync_url(db))
    try:
        assert "projects" in sa.inspect(eng).get_table_names()
        for table, idx in (
            ("chats", "ix_chats_project_id"),
            ("documents", "ix_documents_project_id"),
            ("memory_insights", "ix_memory_insights_project_id"),
        ):
            assert "project_id" in _columns(eng, table)
            assert idx in _indexes(eng, table)
    finally:
        eng.dispose()


def test_chats_indexes_survive_batch_alter(tmp_path: Path) -> None:
    """batch_alter_table must preserve pre-existing indexes on chats.

    SQLite batch_alter does table-recreation; if mishandled, indexes
    silently drop. Verify every chats index that existed in 0020 still
    exists in 0021.
    """
    # Build the pre-0021 baseline index set.
    db_pre = tmp_path / "pre.db"
    _upgrade(db_pre, rev="0020")
    eng_pre = sa.create_engine(_sync_url(db_pre))
    try:
        pre_indexes = _indexes(eng_pre, "chats")
    finally:
        eng_pre.dispose()

    # Now run head; assert all pre-0021 indexes survive.
    db_post = tmp_path / "post.db"
    _upgrade(db_post)
    eng_post = sa.create_engine(_sync_url(db_post))
    try:
        post_indexes = _indexes(eng_post, "chats")
        missing = pre_indexes - post_indexes
        assert not missing, (
            f"batch_alter_table dropped pre-existing chats indexes: {missing}"
        )
        # And the new project_id index is present.
        assert "ix_chats_project_id" in post_indexes
    finally:
        eng_post.dispose()
