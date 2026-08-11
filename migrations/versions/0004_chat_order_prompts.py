# SPDX-License-Identifier: Apache-2.0
"""P8c: chats.display_order + prompts table.

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-20

Changes:
1. ``chats.display_order`` INTEGER column (default 0) — DnD sort order
   within each folder / pinned section.
2. ``prompts`` table — user-managed prompt presets.

Downgrade path
--------------
Drops ``prompts`` table; drops ``chats.display_order`` column.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str = "0003"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Apply P8c schema additions."""
    # 1. chats.display_order
    # Guard: SQLite doesn't support IF NOT EXISTS on ADD COLUMN, so we check
    # via reflection first. This handles the edge case where a previous
    # migration run partially applied the schema (e.g. via metadata.create_all
    # in test fixtures).
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_cols = {c["name"] for c in inspector.get_columns("chats")}
    if "display_order" not in existing_cols:
        op.add_column(
            "chats",
            sa.Column(
                "display_order",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )

    # 2. prompts table (guard: only create if not already present)
    existing_tables = sa.inspect(bind).get_table_names()
    if "prompts" in existing_tables:
        return
    op.create_table(
        "prompts",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
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
        # Inline unique constraint — SQLite does not support
        # ALTER TABLE ADD CONSTRAINT, so this must be inline at CREATE TABLE.
        sa.UniqueConstraint("user_id", "name", name="uq_prompt_user_name"),
    )
    op.create_index("ix_prompts_user_id", "prompts", ["user_id"])


def downgrade() -> None:
    """Reverse P8c schema additions."""
    op.drop_table("prompts")
    op.drop_column("chats", "display_order")
