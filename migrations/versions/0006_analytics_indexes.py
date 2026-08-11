# SPDX-License-Identifier: Apache-2.0
"""P8/P9: PG-friendly composite indexes for analytics queries.

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-20

Changes
-------
1. ``ix_chats_user_id_created_at`` — composite index on (user_id, created_at)
   speeds up the 7-day user-stats cutoff query in the analytics service.
2. ``ix_messages_chat_id_created_at`` — composite index on (chat_id, created_at)
   speeds up per-chat analytics range scans.

Both indexes use ``if_not_exists=True`` for SQLite compat (SQLite silently
ignores duplicate index creation when the flag is honoured via the SA/Alembic
dialect). PostgreSQL benefits more from the covering composite form.

Downgrade path
--------------
Drops both indexes in reverse order.
"""
from __future__ import annotations

from alembic import op

revision: str = "0006"
down_revision: str = "0005"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add composite analytics indexes."""
    op.create_index(
        "ix_chats_user_id_created_at",
        "chats",
        ["user_id", "created_at"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_messages_chat_id_created_at",
        "messages",
        ["chat_id", "created_at"],
        if_not_exists=True,
    )


def downgrade() -> None:
    """Remove composite analytics indexes."""
    op.drop_index("ix_messages_chat_id_created_at", table_name="messages")
    op.drop_index("ix_chats_user_id_created_at", table_name="chats")
