# SPDX-License-Identifier: Apache-2.0
"""P11b: model_admin_ops_per_day column on quotas.

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-21

Changes
-------
1. ``quotas.model_admin_ops_per_day`` column — daily limit for admin model
   lifecycle operations (load/unload/download).  Default 1000.
2. ``quota_usage.model_admin_ops_consumed`` column — daily consumption counter
   for the same budget.  Default 0.

Schema notes
------------
- Non-admin users are blocked at the route layer (403) before any quota check,
  so the effective non-admin budget is 0 by policy, not by DB row.
- Admin users get 1000 ops/day by default.  The admin controls the hardware;
  1000 is generous but prevents unbounded scripted loads.
- The atomic check-and-increment pattern in the route handler mirrors the
  pattern in quota_service.py.

Downgrade path
--------------
Drops both columns.  SQLite's ALTER TABLE does not support DROP COLUMN in all
versions, so we use a compatible approach: recreate the tables without the
new columns.  Alembic's op.drop_column uses ``batch_alter_table`` for SQLite.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str = "0006"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add model_admin_ops columns to quotas and quota_usage."""
    with op.batch_alter_table("quotas") as batch_op:
        batch_op.add_column(
            sa.Column(
                "model_admin_ops_per_day",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("1000"),
            )
        )

    with op.batch_alter_table("quota_usage") as batch_op:
        batch_op.add_column(
            sa.Column(
                "model_admin_ops_consumed",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )


def downgrade() -> None:
    """Remove model_admin_ops columns from quotas and quota_usage."""
    with op.batch_alter_table("quota_usage") as batch_op:
        batch_op.drop_column("model_admin_ops_consumed")

    with op.batch_alter_table("quotas") as batch_op:
        batch_op.drop_column("model_admin_ops_per_day")
