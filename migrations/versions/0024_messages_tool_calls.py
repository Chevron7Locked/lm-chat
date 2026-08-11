# SPDX-License-Identifier: Apache-2.0
"""Add tool_calls JSON column to messages table.

Revision ID: 0024
Revises: 0023b
Create Date: 2026-06-10

Problem
-------
Tool calls emitted during streaming are only available in the SSE state
machine (sseState.toolCalls) which is in-memory per tab.  On page reload the
tool call cards vanish because the persisted message row has no tool_calls
column.  This migration adds the column so stream finalize can persist the
tool call list and ChatMessage can render it from the server record.

Fix
---
Add a nullable JSON column ``tool_calls`` to ``messages``.  NULL for all
existing rows per locked decision 3 (no audit-log re-mining; existing
chats render without ToolCallCards).  New chats persist tool_calls on stream
finalize and re-render on reload.

Reversibility
-------------
``downgrade()`` drops the column via batch_alter_table (required for SQLite).
No data loss on downgrade — tool_calls are re-derived from the streaming
SSE state when they are needed live; the persisted copy is purely for reload.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# Revision identifiers, used by Alembic.
revision: str = "0024"
down_revision: str = "0023b"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add nullable tool_calls JSON column to messages.

    NULL for all existing rows (locked decision 3 — no backfill of historical
    tool calls; the column is populated only for new streams from this point on).
    """
    bind = op.get_bind()
    dialect = bind.dialect.name  # type: ignore[attr-defined]

    if dialect == "sqlite":
        with op.batch_alter_table("messages") as batch_op:
            batch_op.add_column(
                sa.Column("tool_calls", sa.JSON, nullable=True)
            )
    else:
        op.add_column(
            "messages",
            sa.Column("tool_calls", sa.JSON, nullable=True),
        )


def downgrade() -> None:
    """Drop tool_calls column from messages."""
    bind = op.get_bind()
    dialect = bind.dialect.name  # type: ignore[attr-defined]

    if dialect == "sqlite":
        with op.batch_alter_table("messages") as batch_op:
            batch_op.drop_column("tool_calls")
    else:
        op.drop_column("messages", "tool_calls")
