# SPDX-License-Identifier: Apache-2.0
"""P13j: chat_shares — public share tokens for chats.

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-22

Changes
-------
Implements the storage layer for P13j chat-share:

1. ``chat_shares`` table — one row per active share token.  Each row maps
   a randomly generated ``secrets.token_urlsafe(24)`` token to a chat.
   The presence of a row makes the chat publicly readable at
   ``/share/:token`` without authentication.  Revocation = DELETE the row.

2. ``ix_chat_shares_chat_id`` — supports the "is this chat shared?" lookup
   that the chat-list / chat-detail surfaces will run.

3. The unique constraint on ``token`` is declared in the column itself
   (``unique=True``); SQLAlchemy emits the matching unique index under
   the name SQLite picks (``sqlite_autoindex_chat_shares_1``).  No
   explicit ``Index(..., unique=True)`` is needed.

Privacy invariant (P13 PLAN R-15) is enforced at the ROUTE layer via
``ChatService.is_shareable`` (returns False for incognito chats).  This
migration does NOT add a column to ``chats``.

Downgrade path
--------------
Drops the chat_id index then the table.  CASCADE on the FK to ``chats``
means downgrading first (drop table) before any ``chats`` schema change
is the safe order.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the chat_shares table + supporting indices."""
    op.create_table(
        "chat_shares",
        sa.Column(
            "id",
            sa.Integer().with_variant(sa.BigInteger(), "postgresql"),
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        sa.Column(
            "chat_id",
            sa.BigInteger(),
            sa.ForeignKey("chats.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "token",
            sa.String(64),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_chat_shares_chat_id",
        "chat_shares",
        ["chat_id"],
    )


def downgrade() -> None:
    """Drop the supporting index then the table."""
    op.drop_index("ix_chat_shares_chat_id", table_name="chat_shares")
    op.drop_table("chat_shares")
