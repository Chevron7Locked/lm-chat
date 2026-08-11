# SPDX-License-Identifier: Apache-2.0
"""P8d: per-user quotas and daily usage tracking.

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-20

Changes:
1. ``quotas`` table — per-user daily limits.
2. ``quota_usage`` table — daily consumption counters.

Schema notes
------------
- ``quotas`` PK is ``user_id`` (FK users CASCADE) — one row per user.
- ``quota_usage`` composite PK ``(user_id, date)`` — no autoincrement needed.
- The atomic check-and-increment pattern (see quota_service.py) relies on
  the ``quota_usage.tokens_consumed + :amount <=
  quotas.tokens_per_day`` subquery in the UPDATE WHERE clause.

Downgrade path
--------------
Drops ``quota_usage`` then ``quotas``.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str = "0004"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Apply P8d quota schema additions."""
    # ------------------------------------------------------------------
    # 1. quotas table
    # ------------------------------------------------------------------
    op.create_table(
        "quotas",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "tokens_per_day",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("100000"),
        ),
        sa.Column(
            "requests_per_day",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1000"),
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
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id"),
    )

    # ------------------------------------------------------------------
    # 2. quota_usage table
    # ------------------------------------------------------------------
    op.create_table(
        "quota_usage",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column(
            "tokens_consumed",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "requests_consumed",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "date"),
    )
    op.create_index("ix_quota_usage_user_id", "quota_usage", ["user_id"])


def downgrade() -> None:
    """Remove P8d quota schema additions."""
    op.drop_index("ix_quota_usage_user_id", table_name="quota_usage")
    op.drop_table("quota_usage")
    op.drop_table("quotas")
