# SPDX-License-Identifier: Apache-2.0
"""P8b RAG pipeline: documents, document_chunks, document_chunks_fts,
and chats.settings JSON column.

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-20

Three changes in one revision (per P8b.1 brief §Item 1):

1. Tables ``documents`` and ``document_chunks``.
2. FTS5 virtual table ``document_chunks_fts`` + sync triggers
   (SQLite only; same external-content trigger pattern as 0002).
3. ``chats.settings`` JSON column (default '{}').
   Closes DEFERRED §1 (P8a): per-chat settings for RAG and
   reasoning_effort persistence.

Schema notes
------------
- ``documents`` and ``document_chunks`` PKs use
  ``BigInteger().with_variant(Integer(), "sqlite")`` — the portable
  auto-increment idiom established in 0001_baseline.
- ``document_chunks_fts`` is an external-content FTS5 table over
  ``document_chunks.text`` with ``content_rowid='id'``.  The same
  three-trigger pattern (AI / AU / AD) from 0002 applies.
- FTS5 virtual tables are invisible to SQLAlchemy MetaData inspection,
  so ``schema_fingerprint(metadata)`` is unchanged by the FTS5 DDL.

Downgrade path
--------------
Drops FTS5 triggers + table, drops ``document_chunks``, drops
``documents``, drops ``chats.settings``.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str = "0002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Apply P8b schema additions."""
    bind = op.get_bind()

    # ------------------------------------------------------------------
    # 1. chats.settings JSON column
    # ------------------------------------------------------------------
    # ADD COLUMN with DEFAULT '{}' so every existing row gets {} immediately.
    # SQLite allows ADD COLUMN with a default value.
    op.add_column(
        "chats",
        sa.Column(
            "settings",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )

    # Backfill: set existing rows to {} (belt-and-suspenders; the
    # server_default handles new rows but existing SQLite rows may
    # not carry the default until an explicit update).
    op.execute(sa.text("UPDATE chats SET settings = '{}' WHERE settings IS NULL"))

    # ------------------------------------------------------------------
    # 2. documents table
    # ------------------------------------------------------------------
    op.create_table(
        "documents",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.String(64), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column(
            "chunk_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "embedding_model_id",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "uploaded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_documents_user_id", "documents", ["user_id"])
    op.create_index("ix_documents_sha256", "documents", ["sha256"])

    # ------------------------------------------------------------------
    # 3. document_chunks table
    # ------------------------------------------------------------------
    op.create_table(
        "document_chunks",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column("document_id", sa.BigInteger(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_hash", sa.String(64), nullable=False),
        sa.Column("embedding", sa.LargeBinary(), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "ordinal", name="uq_chunk_doc_ord"),
    )
    op.create_index("ix_chunks_document_id", "document_chunks", ["document_id"])

    # ------------------------------------------------------------------
    # 4. FTS5 virtual table + sync triggers (SQLite only)
    # ------------------------------------------------------------------
    if bind.dialect.name != "sqlite":
        return

    op.execute("""
        CREATE VIRTUAL TABLE document_chunks_fts USING fts5(
            text,
            content='document_chunks',
            content_rowid='id',
            tokenize='porter unicode61'
        )
    """)

    # AFTER INSERT — populate FTS index.
    op.execute("""
        CREATE TRIGGER document_chunks_ai AFTER INSERT ON document_chunks BEGIN
            INSERT INTO document_chunks_fts(rowid, text) VALUES (new.id, new.text);
        END
    """)

    # AFTER UPDATE OF text — sync the FTS index using the explicit-delete form.
    # See 0002_fts5_messages.py for the reasoning behind this pattern.
    op.execute("""
        CREATE TRIGGER document_chunks_au AFTER UPDATE OF text ON document_chunks BEGIN
            INSERT INTO document_chunks_fts(document_chunks_fts, rowid, text)
                VALUES('delete', old.id, old.text);
            INSERT INTO document_chunks_fts(rowid, text) VALUES (new.id, new.text);
        END
    """)

    # AFTER DELETE — remove the FTS index entry.
    op.execute("""
        CREATE TRIGGER document_chunks_ad AFTER DELETE ON document_chunks BEGIN
            INSERT INTO document_chunks_fts(document_chunks_fts, rowid, text)
                VALUES('delete', old.id, old.text);
        END
    """)


def downgrade() -> None:
    """Remove P8b schema additions."""
    bind = op.get_bind()

    # Drop FTS5 triggers + table first (SQLite only).
    if bind.dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS document_chunks_ad")
        op.execute("DROP TRIGGER IF EXISTS document_chunks_au")
        op.execute("DROP TRIGGER IF EXISTS document_chunks_ai")
        op.execute("DROP TABLE IF EXISTS document_chunks_fts")

    op.drop_index("ix_chunks_document_id", table_name="document_chunks")
    op.drop_table("document_chunks")

    op.drop_index("ix_documents_sha256", table_name="documents")
    op.drop_index("ix_documents_user_id", table_name="documents")
    op.drop_table("documents")

    op.drop_column("chats", "settings")
