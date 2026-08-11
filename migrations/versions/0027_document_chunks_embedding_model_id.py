# SPDX-License-Identifier: Apache-2.0
"""Add ``embedding_model_id`` to ``document_chunks``; backfill from ``documents``.

F1b fix — see docs/audit/2026-06-17-live-test-remediation-plan.md §F1.

Problem
-------
``document_chunks`` has no ``embedding_model_id`` column. The retrieval
read path embeds the query with whatever model is currently loaded, but
chunks were written under whatever model was active at write time. If
the admin swaps embedding models (e.g. nomic → bge-m3), cosine
similarity compares vectors from different vector spaces, producing
meaningless scores.

Fix
---
1. Add a nullable TEXT column ``embedding_model_id`` to
   ``document_chunks``.
2. Backfill existing rows from the parent ``documents.embedding_model_id``
   (which IS stored per document).
3. Add ``ix_chunks_embedding_model_id`` index so the read path can
   efficiently group chunks by model.

Reversibility
-------------
``downgrade()`` drops the column (batch_alter_table on SQLite). Backfilled
data is lost on downgrade, but re-upgrading re-backfills from documents
so the state is reproducible.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# Revision identifiers, used by Alembic.
revision: str = "0027"
down_revision: str = "0026"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add ``embedding_model_id`` to ``document_chunks`` + backfill."""
    bind = op.get_bind()
    dialect = bind.dialect.name  # type: ignore[attr-defined]

    if dialect == "sqlite":
        with op.batch_alter_table("document_chunks") as batch_op:
            batch_op.add_column(
                sa.Column("embedding_model_id", sa.Text, nullable=True),
            )
            batch_op.create_index(
                "ix_chunks_embedding_model_id",
                ["embedding_model_id"],
            )
    else:
        op.add_column(
            "document_chunks",
            sa.Column("embedding_model_id", sa.Text, nullable=True),
        )
        op.create_index(
            "ix_chunks_embedding_model_id",
            "document_chunks",
            ["embedding_model_id"],
        )

    # Backfill: set embedding_model_id from the parent document row.
    # Uses a correlated subquery or UPDATE FROM; SQLite supports
    # UPDATE … FROM syntax (3.33+), Postgres has UPDATE … FROM.
    op.execute(
        sa.text("""
            UPDATE document_chunks
            SET embedding_model_id = (
                SELECT d.embedding_model_id
                FROM documents d
                WHERE d.id = document_chunks.document_id
            )
            WHERE embedding_model_id IS NULL
        """)
    )


def downgrade() -> None:
    """Drop ``embedding_model_id`` from ``document_chunks``."""
    bind = op.get_bind()
    dialect = bind.dialect.name  # type: ignore[attr-defined]

    if dialect == "sqlite":
        with op.batch_alter_table("document_chunks") as batch_op:
            batch_op.drop_index("ix_chunks_embedding_model_id")
            batch_op.drop_column("embedding_model_id")
    else:
        op.drop_index("ix_chunks_embedding_model_id", table_name="document_chunks")
        op.drop_column("document_chunks", "embedding_model_id")