# SPDX-License-Identifier: Apache-2.0
"""P12c: composite (chat_id, id) index on messages for cursor pagination.

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-21

Changes
-------
Add index ``ix_messages_chat_id_id`` on ``messages(chat_id, id)``.

The existing ``ix_messages_chat_id`` index covers ``WHERE chat_id = ?``
scans, but keyset pagination also needs ``id < :before_id`` or
``id > :since_id`` filtering within the chat scope.  The composite
index makes both patterns efficient (index range scan on the (chat_id,
id) column pair) without touching the ``ix_messages_chat_id`` index,
which is left in place for compatibility.

Downgrade path
--------------
Drops the composite index.  Reversible with no data loss.
"""
from __future__ import annotations

from alembic import op

revision: str = "0008"
down_revision: str = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add composite (chat_id, id) index on messages."""
    op.create_index(
        "ix_messages_chat_id_id",
        "messages",
        ["chat_id", "id"],
    )


def downgrade() -> None:
    """Drop the composite (chat_id, id) index."""
    op.drop_index("ix_messages_chat_id_id", table_name="messages")
