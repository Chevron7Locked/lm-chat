# SPDX-License-Identifier: Apache-2.0
"""P13l.2: user_prefs table (user-named folder buckets).

Revision ID: 0016
Revises: 0015
Create Date: 2026-05-22

Changes
-------
Implements the schema half of P13l.2 (audit O-14 — folder CRUD UI):

1. ``user_prefs`` — one row per user.  Holds the user-named folder
   buckets so the sidebar can show empty folders the user has just
   created before any chat lives in them.  ``folders`` is a JSON
   array of strings (default ``'[]'``).

Sidebar folder list rendering combines this table with the chats
table at the route layer:

    visible_folders = prefs.folders ∪ DISTINCT chats.folder WHERE user_id=?

This way folder creation is independent of chat creation; rename /
delete continue to flow through ``chats.folder`` mutations.

Forward compatibility
---------------------
Per PLAN-r1 F2 the table is a dedicated row (not a JSON blob on the
``users`` row) so future preferences — theme, language, notification
settings, panel layout — can ride the same record without re-shaping
the schema.

Downgrade path
--------------
Drops the table.  Any user-named empty folders are lost (folders
with at least one chat continue to appear via ``chats.folder``).

Round-trip rationale
--------------------
Plain ``create_table`` / ``drop_table`` — no FK rename, no column rename,
no batch dance required.  The CASCADE FK to ``users.id`` matches the
pattern used for every other per-user table.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create user_prefs table."""
    op.create_table(
        "user_prefs",
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "folders",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    """Drop user_prefs table."""
    op.drop_table("user_prefs")
