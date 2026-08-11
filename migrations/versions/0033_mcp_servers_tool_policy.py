# SPDX-License-Identifier: Apache-2.0
"""Add ``tool_policy`` JSON column to ``mcp_servers`` — B4 per-tool allow/deny.

``tool_policy`` stores a list of namespaced tool-name strings that are
DENIED when the server is active in the agentic loop.  Null / empty list
means all tools are advertised (allow-all default).

Column notes
------------
tool_policy  JSON list[str] of denied tool names, e.g. ["firecrawl_scrape"].
             Nullable; null treated as [] (allow all).  B4 enforcement gate.

Reversibility
-------------
``downgrade()`` drops the column.  SQLite does not support DROP COLUMN via
ALTER TABLE in older SQLite versions, so a table-recreation strategy is used
for SQLite; for Postgres a plain ALTER TABLE DROP COLUMN is used.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision: str = "0032"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add ``tool_policy`` column to ``mcp_servers``."""
    op.add_column(
        "mcp_servers",
        sa.Column("tool_policy", sa.JSON, nullable=True),
    )


def downgrade() -> None:
    """Drop ``tool_policy`` column from ``mcp_servers``.

    Uses batch mode for SQLite <3.35 portability (plain ALTER TABLE
    DROP COLUMN is unsupported on older SQLite versions).
    """
    with op.batch_alter_table("mcp_servers") as batch_op:
        batch_op.drop_column("tool_policy")
