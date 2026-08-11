# SPDX-License-Identifier: Apache-2.0
"""Add ``allowed_models`` column to ``provider_configs``.

Per-provider explicit model allowlist for the model picker.  NULL or an
empty JSON array means all models from this provider are visible (current
behavior preserved).  A non-empty list restricts the picker to only those
model ids.

The allowlist governs the picker / ``/api/models`` response only; dispatch
is not blocked at request time (the model id is still valid upstream).

Column shape:
    allowed_models — JSON nullable, inserted after ``default_model``.
                     Stores a list[str] of model ids.

Reversibility
-------------
``downgrade()`` drops the column.  SQLite does not support ``DROP COLUMN``
natively prior to version 3.35.0; Alembic's batch_alter_table handles the
portable column-recreation approach.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: str = "0029"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add ``allowed_models`` nullable JSON column to ``provider_configs``."""
    with op.batch_alter_table("provider_configs") as batch_op:
        batch_op.add_column(
            sa.Column("allowed_models", sa.JSON(), nullable=True),
        )


def downgrade() -> None:
    """Drop the ``allowed_models`` column from ``provider_configs``."""
    with op.batch_alter_table("provider_configs") as batch_op:
        batch_op.drop_column("allowed_models")
