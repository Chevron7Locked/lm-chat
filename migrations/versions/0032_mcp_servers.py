# SPDX-License-Identifier: Apache-2.0
"""Create ``mcp_servers`` table — MCP Store (Workstream C).

One row per installed MCP server.  Stores transport config, encrypted
secrets (ADR-007 enc$v1$ envelope), and provenance/consent metadata.

Column notes
------------
slug         Stable identifier; unique per installation (mirrors catalog id
             for curated entries).
transport    "stdio" | "http" | "sse"
command      Executable for stdio transport (e.g. "npx").
args         JSON list[str] of CLI arguments.
url          Endpoint for http/sse transport; NULL for stdio.
secrets_enc  JSON list[{"key": str, "enc_value": str}].  Each value
             encrypted independently: enc$v1$, kind="mcp_server_secret",
             record_id=row.id.
enabled      UI visibility gate.  Defaults True.
source       "official" | "byo".  Defaults "byo".
trust        "curated" | "byo".  Defaults "byo".
consented    Explicit admin consent before the backend spawns the child
             process (B4 security gate).  Defaults False.

Reversibility
-------------
``downgrade()`` drops the table unconditionally.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: str = "0031"
branch_labels: str | None = None
depends_on: str | None = None

# Portable autoincrement PK — mirrors schema.py's _AUTO_PK_TYPE.
_PK_TYPE = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    """Create the ``mcp_servers`` table."""
    op.create_table(
        "mcp_servers",
        sa.Column("id", _PK_TYPE, primary_key=True),
        sa.Column("slug", sa.String(128), nullable=False, unique=True),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("transport", sa.String(16), nullable=False),
        sa.Column("command", sa.Text, nullable=True),
        sa.Column("args", sa.JSON, nullable=True),
        sa.Column("url", sa.Text, nullable=True),
        sa.Column("secrets_enc", sa.JSON, nullable=True),
        sa.Column(
            "enabled",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "source",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'byo'"),
        ),
        sa.Column(
            "trust",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'byo'"),
        ),
        sa.Column(
            "consented",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_mcp_servers_slug", "mcp_servers", ["slug"])


def downgrade() -> None:
    """Drop the ``mcp_servers`` table."""
    op.drop_index("ix_mcp_servers_slug", table_name="mcp_servers")
    op.drop_table("mcp_servers")
