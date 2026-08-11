# SPDX-License-Identifier: Apache-2.0
"""P12e: admin-supplied MCP integrations list table.

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-21

Changes
-------
Creates ``mcp_integrations_list`` — the DB-backed half of the admin-
supplied MCP integrations workaround.  See
``src/lmchat/services/integrations_service.py`` for details.

WHY this table exists
~~~~~~~~~~~~~~~~~~~~~
LM Studio does not expose MCP server enumeration over HTTP (all candidate
endpoints return 404).  lm-chat cannot auto-
discover MCP servers from LM Studio; the admin must supply the list
manually.  This table holds the DB-backed version of that list (admin-
editable; takes precedence over the ``LM_CHAT_AVAILABLE_INTEGRATIONS`` env
var when any rows are present).

Downgrade path
--------------
Drops the sort-order index, then the table.  No data migration needed
(table is admin-supplied; re-populating after a re-upgrade is the
admin's responsibility).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create mcp_integrations_list table and sort_order index."""
    op.create_table(
        "mcp_integrations_list",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("value", sa.String(255), nullable=False, unique=True),
        sa.Column("sort_order", sa.Integer, nullable=False, default=0),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_mcp_integrations_list_sort_order",
        "mcp_integrations_list",
        ["sort_order"],
    )


def downgrade() -> None:
    """Drop mcp_integrations_list table and its sort_order index."""
    op.drop_index("ix_mcp_integrations_list_sort_order", table_name="mcp_integrations_list")
    op.drop_table("mcp_integrations_list")
