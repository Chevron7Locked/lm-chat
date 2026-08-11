# SPDX-License-Identifier: Apache-2.0
"""Add ``messages.last_activity_at`` for reaper inactivity threshold.

Revision ID: 0024
Revises: 0023b
Create Date: 2026-06-10

Problem
-------
The stream reaper (``_stream_reaper.py``) selected on ``messages.created_at``
to decide which draft rows to finalize or delete.  This meant a draft row for
an actively-streaming model that takes >5 minutes on a single tool round would
be reaped mid-stream and the chat would be 409'd on the next send.

Fix (Cluster 5 Task 2)
-----------------------
Add a nullable ``last_activity_at`` column to ``messages``.  The streaming
service's ``_CoalesceTimer.flush()`` updates this column on every
content-bearing write (folded into the existing UPDATE — NOT a second
statement).  The reaper now selects on
``last_activity_at < now - 5 min`` (falling back to ``created_at`` for rows
with a NULL ``last_activity_at`` via ``COALESCE``).

Backfill (locked decision 4)
-----------------------------
Existing rows get ``last_activity_at = created_at`` so the reaper behaviour
is identical to the old path for historical rows.  NULL rows (written after
the migration but before the code deploy) fall back to ``created_at`` via
``COALESCE`` in the reaper query.

Dialect support
---------------
Both SQLite and Postgres.  SQLite requires ``batch_alter_table`` for column
additions per ADR-011 precedent.

Reversibility
-------------
``downgrade()`` drops the column and its index.  Behaviour reverts to
``created_at``-based reaper selection; no data loss because the column is
additive.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name  # type: ignore[attr-defined]

    if dialect == "sqlite":
        with op.batch_alter_table("messages") as bop:
            bop.add_column(
                sa.Column(
                    "last_activity_at",
                    sa.DateTime(timezone=True),
                    nullable=True,
                )
            )
        # Backfill: set last_activity_at = created_at for existing rows.
        op.execute(
            "UPDATE messages SET last_activity_at = created_at WHERE last_activity_at IS NULL"
        )
        # Add index after data is populated so SQLite doesn't choke on a
        # partial-index scan during the backfill itself.
        op.create_index(
            "ix_messages_last_activity_at",
            "messages",
            ["last_activity_at"],
        )
    else:
        op.add_column(
            "messages",
            sa.Column(
                "last_activity_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )
        op.execute(
            "UPDATE messages SET last_activity_at = created_at WHERE last_activity_at IS NULL"
        )
        op.create_index(
            "ix_messages_last_activity_at",
            "messages",
            ["last_activity_at"],
        )


def downgrade() -> None:
    op.drop_index("ix_messages_last_activity_at", table_name="messages")
    bind = op.get_bind()
    dialect = bind.dialect.name  # type: ignore[attr-defined]
    if dialect == "sqlite":
        with op.batch_alter_table("messages") as bop:
            bop.drop_column("last_activity_at")
    else:
        op.drop_column("messages", "last_activity_at")
