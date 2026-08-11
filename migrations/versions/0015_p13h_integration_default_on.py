# SPDX-License-Identifier: Apache-2.0
"""P13h: per-integration ``enabled_by_default`` flag.

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-22

Numbering note
--------------
Originally planned as 0014 per the cluster brief, but P13k landed its own
``0014_p13k_admin_invites.py`` revising 0013 in parallel.  To avoid a
divergent revision DAG, P13h chains after 0014 and lands as 0015.

Changes
-------
Implements the schema half of P13h (audit F-08 — remote MCP server picker):

1. ``mcp_integrations_list.enabled_by_default INTEGER NOT NULL DEFAULT 0``
   — boolean flag set per-integration by the admin via the Admin:
   Integrations page.  When ``1``, the chat composer pre-selects the
   integration on a fresh chat session; when ``0``, the integration is
   available but opt-in for each chat (the user must toggle it on
   before sending the next message).

The flag pairs with the per-request integrations override carried on
``CanonicalChatRequest.integrations`` (introduced in P12e and used
verbatim by ``encode_native``); P13h does NOT change the wire shape of
chat-send.  The wire already accepts per-message ``integrations`` lists;
P13h only adds an admin-set default-seed for the composer chip-row.

Downgrade path
--------------
Drops the column.  The admin's flag values are lost (the admin must
re-flag any default-on entries after a downgrade).

Round-trip rationale
--------------------
``batch_alter_table`` because SQLite's ALTER TABLE has no DROP COLUMN.
The batch dance copies-and-recreates on SQLite while issuing a plain
ALTER on Postgres.  ``server_default=text('0')`` lets the column be
added without rewriting existing rows.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add ``mcp_integrations_list.enabled_by_default``."""
    with op.batch_alter_table("mcp_integrations_list") as batch_op:
        batch_op.add_column(
            sa.Column(
                "enabled_by_default",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )


def downgrade() -> None:
    """Drop ``mcp_integrations_list.enabled_by_default``."""
    with op.batch_alter_table("mcp_integrations_list") as batch_op:
        batch_op.drop_column("enabled_by_default")
