# SPDX-License-Identifier: Apache-2.0
"""fts5 virtual table on messages.content + sync triggers

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-19

SQLite-only migration.  On Postgres the search path uses ``pg_trgm``
(detected at startup) or ``ILIKE``; no DDL change is needed at the DB
level for Postgres.

Virtual table definition
------------------------
The ``messages_fts`` FTS5 content table mirrors ``messages.content``
and is keyed by ``messages.id`` (``content_rowid='id'``).  The
``porter unicode61`` tokeniser applies Porter stemming on top of Unicode
tokenisation, giving reasonable English-language recall without external
plugins.

Trigger sync strategy
---------------------
Three AFTER triggers keep ``messages_fts`` consistent with ``messages``:

- ``messages_ai`` (AFTER INSERT): insert the new FTS row.
- ``messages_au`` (AFTER UPDATE OF content): uses the FTS5
  external-content delete form ``INSERT INTO messages_fts(messages_fts,
  rowid, content) VALUES('delete', old.id, old.content)`` followed by a
  fresh ``INSERT INTO messages_fts(rowid, content)``. The plain
  ``DELETE FROM messages_fts WHERE rowid=old.id`` form would cause
  SQLite to re-read the (mid-UPDATE) external row to reconstruct the
  inverse-index entry — that read fails with "disk image is malformed"
  on UPDATE triggers. The 'delete' command form passes the OLD content
  explicitly so no re-read is required. (SQLite FTS5 §4.4.2.)
  ``INSERT OR REPLACE`` is NOT used either; the REPLACE conflict
  algorithm is not honoured by FTS5 virtual tables.
- ``messages_ad`` (AFTER DELETE): delete the FTS row.

Fingerprint interaction
-----------------------
``messages_fts`` is a virtual table created only by this migration.  It
does NOT appear in ``src/lmchat/db/schema.py``'s ``MetaData`` object
(virtual tables are invisible to SQLAlchemy's inspection).  Therefore
``schema_fingerprint(metadata)`` is unchanged by this migration and
``startup.py``'s fingerprint check remains satisfied after 0002 runs.
The ``alembic_version`` table advances to ``"0002"`` but the fingerprint
guard still passes because it compares the live ``MetaData`` fingerprint
(unchanged) against ``0001_baseline.py``'s ``SCHEMA_FINGERPRINT``
(also unchanged).
"""
from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str = "0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create the FTS5 virtual table and three sync triggers on SQLite.

    On non-SQLite dialects (Postgres) this is a no-op — search is handled
    at query time via ``pg_trgm`` or ``ILIKE``.
    """
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return

    op.execute("""
        CREATE VIRTUAL TABLE messages_fts USING fts5(
            content,
            content='messages',
            content_rowid='id',
            tokenize='porter unicode61'
        )
    """)

    # AFTER INSERT — populate FTS index from the newly inserted messages row.
    op.execute("""
        CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
        END
    """)

    # AFTER UPDATE OF content — sync the FTS index.
    # For external-content FTS5 tables the correct pattern is to delete
    # the OLD entry using the special ('delete', rowid, old_content) form,
    # then insert the NEW entry.  The special delete form removes the index
    # entries using the content we supply — it does NOT re-read the external
    # table, avoiding the malformed-state error that arises when the external
    # row is mid-update and FTS5 tries to re-read it.
    # Reference: https://www.sqlite.org/fts5.html §4.4.2 "The delete command".
    op.execute("""
        CREATE TRIGGER messages_au AFTER UPDATE OF content ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, content)
                VALUES('delete', old.id, old.content);
            INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
        END
    """)

    # AFTER DELETE — remove the FTS index entry using the special delete form.
    op.execute("""
        CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, content)
                VALUES('delete', old.id, old.content);
        END
    """)


def downgrade() -> None:
    """Drop the FTS5 virtual table and sync triggers on SQLite.

    On non-SQLite dialects this is a no-op (matching ``upgrade``).
    """
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return

    op.execute("DROP TRIGGER IF EXISTS messages_ad")
    op.execute("DROP TRIGGER IF EXISTS messages_au")
    op.execute("DROP TRIGGER IF EXISTS messages_ai")
    op.execute("DROP TABLE IF EXISTS messages_fts")
