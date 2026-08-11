# SPDX-License-Identifier: Apache-2.0
"""Add ``preset_models`` column to ``user_prefs``.

Per-preset model/provider defaults for the admin.  The mapping is stored
as a nullable JSON object on the existing ``user_prefs`` row alongside
``folders``.

Column shape:
    preset_models — JSON nullable, inserted after ``folders``.
                    Stores a dict keyed by preset id:
                    ``{"general": {"provider": "openrouter", "model_id": "..."}, ...}``.
                    NULL / {} means no per-preset defaults (fall back to
                    the caller-supplied model — today's behavior).

Reversibility
-------------
``downgrade()`` drops the column.  Alembic's batch_alter_table handles
the portable column-recreation approach (SQLite compatibility).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: str = "0030"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add ``preset_models`` nullable JSON column to ``user_prefs``."""
    with op.batch_alter_table("user_prefs") as batch_op:
        batch_op.add_column(
            sa.Column("preset_models", sa.JSON(), nullable=True),
        )


def downgrade() -> None:
    """Drop the ``preset_models`` column from ``user_prefs``."""
    with op.batch_alter_table("user_prefs") as batch_op:
        batch_op.drop_column("preset_models")
