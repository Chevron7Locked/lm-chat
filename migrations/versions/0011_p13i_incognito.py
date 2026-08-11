# SPDX-License-Identifier: Apache-2.0
"""P13i: incognito mode — chats.incognito + chats.incognito_expires_at.

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-22

Changes
-------
Implements the schema half of incognito mode:

1. ``chats.incognito INTEGER NOT NULL DEFAULT 0`` — boolean flag set at
   chat creation. When 1, three memory-write paths short-circuit:
   - ``MemoryService.index_message`` (no embeddings persisted),
   - ``MemoryService.record_activations`` (no insight_activations rows),
   - ``MemoryService.pin_insight`` (no memory_insights rows).

2. ``chats.incognito_expires_at REAL`` (NULLABLE) — UNIX epoch seconds.
   When non-NULL, the periodic incognito-TTL purge sweeps any row where
   incognito_expires_at < now().  Default TTL of 1 hour is set by
   ChatService.create() when the request flips incognito=1.

3. ``ix_chats_incognito_expires`` — partial-ish index on
   ``incognito_expires_at`` so the purge SELECT
   (``WHERE incognito=1 AND incognito_expires_at < ?``) is O(log n).

Downgrade path
--------------
Drops the index, then drops both columns.  No data-preservation step.

Round-trip rationale
--------------------
batch_alter_table is used because SQLite ALTER TABLE only supports
ADD COLUMN one column at a time; batch_alter_table copies the table
under the hood on SQLite while issuing plain ALTER on Postgres.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add chats.incognito + chats.incognito_expires_at + supporting index."""
    with op.batch_alter_table("chats") as batch_op:
        batch_op.add_column(
            sa.Column(
                "incognito",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "incognito_expires_at",
                sa.Float(),
                nullable=True,
            )
        )

    op.create_index(
        "ix_chats_incognito_expires",
        "chats",
        ["incognito_expires_at"],
    )


def downgrade() -> None:
    """Drop the index, then both columns."""
    op.drop_index("ix_chats_incognito_expires", table_name="chats")
    with op.batch_alter_table("chats") as batch_op:
        batch_op.drop_column("incognito_expires_at")
        batch_op.drop_column("incognito")
