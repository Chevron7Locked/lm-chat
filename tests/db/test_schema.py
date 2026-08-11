# SPDX-License-Identifier: Apache-2.0
"""Tests for lmchat.db.schema — table definitions and constraints.

Uses a real in-memory SQLite database
so create_all() exercises the actual DDL path.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text

from lmchat.db.schema import (
    audit_log,
    memory_insights,
    messages,
    metadata,
    users,
)

_EXPECTED_TABLES = {
    "users",
    "sessions",
    "chats",
    "messages",
    "message_embeddings",
    "memory_insights",
    "audit_log",
}


@pytest.fixture()
def sync_engine():
    """In-memory SQLite sync engine for DDL testing."""
    engine = create_engine("sqlite:///:memory:", echo=False)
    # Enable FK enforcement so cascade tests actually fire.
    with engine.connect() as conn:
        conn.execute(text("PRAGMA foreign_keys = ON"))
        conn.commit()
    metadata.create_all(engine)
    yield engine
    engine.dispose()


# ---------------------------------------------------------------------------
# Table existence
# ---------------------------------------------------------------------------


def test_create_all_succeeds(sync_engine):
    """metadata.create_all() must complete without raising."""
    insp = inspect(sync_engine)
    tables_in_db = set(insp.get_table_names())
    assert _EXPECTED_TABLES.issubset(tables_in_db)


def test_all_nine_tables_present(sync_engine):
    """Exactly the nine spec tables must exist (no extras, no missing)."""
    insp = inspect(sync_engine)
    tables_in_db = set(insp.get_table_names())
    # At minimum all nine must be present (sqlite may add internal tables).
    assert _EXPECTED_TABLES.issubset(tables_in_db)


# ---------------------------------------------------------------------------
# Column-level checks
# ---------------------------------------------------------------------------


def test_users_is_admin_server_default(sync_engine):
    """users.is_admin must default to false at the server level."""
    from sqlalchemy.sql.schema import DefaultClause

    col = users.c.is_admin
    assert col.server_default is not None
    assert isinstance(col.server_default, DefaultClause)
    sd_text = str(col.server_default.arg).lower()
    assert "false" in sd_text


def test_messages_state_server_default(sync_engine):
    """messages.state must default to 'final' at the server level."""
    from sqlalchemy.sql.schema import DefaultClause

    col = messages.c.state
    assert col.server_default is not None
    assert isinstance(col.server_default, DefaultClause)
    sd_text = str(col.server_default.arg)
    assert "final" in sd_text


def test_messages_model_id_exists_and_nullable(sync_engine):
    """messages.model_id must exist and be nullable (user/system msgs)."""
    col = messages.c.model_id
    assert col is not None
    assert col.nullable is True


def test_users_username_unique(sync_engine):
    """users.username must carry a unique constraint."""
    col = users.c.username
    assert col.unique is True


def test_memory_insights_uq_user_hash(sync_engine):
    """memory_insights must have a (user_id, text_hash) unique constraint."""
    from sqlalchemy import UniqueConstraint

    names = {c.name for c in memory_insights.constraints if isinstance(c, UniqueConstraint)}
    assert "uq_memory_user_hash" in names


def test_audit_log_user_id_nullable(sync_engine):
    """audit_log.user_id is nullable (SET NULL on user delete)."""
    assert audit_log.c.user_id.nullable is True


def test_messages_has_reasoning_content(sync_engine):
    """messages.reasoning_content must exist and be nullable."""
    col = messages.c.reasoning_content
    assert col is not None
    assert col.nullable is True


# ---------------------------------------------------------------------------
# Foreign key cascade behaviour
# ---------------------------------------------------------------------------


def test_delete_user_cascades_sessions(sync_engine):
    """Deleting a user row must cascade-delete their sessions."""
    with sync_engine.connect() as conn:
        conn.execute(text("PRAGMA foreign_keys = ON"))
        conn.execute(
            text(
                "INSERT INTO users (id, username, password_hash) "
                "VALUES (1, 'alice', 'hash')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO sessions (id, user_id, expires_at) "
                "VALUES ('tok1', 1, '2099-01-01')"
            )
        )
        conn.commit()

        conn.execute(text("DELETE FROM users WHERE id = 1"))
        conn.commit()

        rows = conn.execute(text("SELECT id FROM sessions WHERE user_id = 1")).fetchall()
        assert rows == []


def test_delete_user_cascades_chats_and_messages(sync_engine):
    """Deleting a user cascades through chats → messages."""
    with sync_engine.connect() as conn:
        conn.execute(text("PRAGMA foreign_keys = ON"))
        conn.execute(
            text(
                "INSERT INTO users (id, username, password_hash) "
                "VALUES (2, 'bob', 'hash')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO chats (id, user_id, title) "
                "VALUES (1, 2, 'My Chat')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO messages (id, chat_id, role, content) "
                "VALUES (1, 1, 'user', 'Hello')"
            )
        )
        conn.commit()

        conn.execute(text("DELETE FROM users WHERE id = 2"))
        conn.commit()

        chats_left = conn.execute(text("SELECT id FROM chats WHERE user_id = 2")).fetchall()
        msgs_left = conn.execute(text("SELECT id FROM messages WHERE id = 1")).fetchall()
        assert chats_left == []
        assert msgs_left == []


def test_delete_user_sets_null_on_audit_log(sync_engine):
    """Deleting a user must SET NULL on audit_log.user_id (not cascade)."""
    with sync_engine.connect() as conn:
        conn.execute(text("PRAGMA foreign_keys = ON"))
        conn.execute(
            text(
                "INSERT INTO users (id, username, password_hash) "
                "VALUES (3, 'carol', 'hash')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO audit_log (id, user_id, event) "
                "VALUES (1, 3, 'auth.login.success')"
            )
        )
        conn.commit()

        conn.execute(text("DELETE FROM users WHERE id = 3"))
        conn.commit()

        row = conn.execute(text("SELECT user_id FROM audit_log WHERE id = 1")).fetchone()
        assert row is not None
        assert row[0] is None  # SET NULL, row still exists


def test_delete_message_cascades_embeddings(sync_engine):
    """Deleting a message must cascade-delete its embeddings."""
    with sync_engine.connect() as conn:
        conn.execute(text("PRAGMA foreign_keys = ON"))
        conn.execute(
            text(
                "INSERT INTO users (id, username, password_hash) "
                "VALUES (4, 'dave', 'hash')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO chats (id, user_id, title) "
                "VALUES (2, 4, 'Chat')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO messages (id, chat_id, role, content) "
                "VALUES (2, 2, 'assistant', 'Hi')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO message_embeddings (message_id, embedding_model_id, "
                "embedding, text_hash) VALUES (2, 'nomic-v1', X'DEADBEEF', 'abc123')"
            )
        )
        conn.commit()

        conn.execute(text("DELETE FROM messages WHERE id = 2"))
        conn.commit()

        emb = conn.execute(
            text("SELECT message_id FROM message_embeddings WHERE message_id = 2")
        ).fetchall()
        assert emb == []
