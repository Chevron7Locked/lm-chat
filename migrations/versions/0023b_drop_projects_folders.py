# SPDX-License-Identifier: Apache-2.0
"""Drop ``projects.folders`` column.

Revision ID: 0023b
Revises: 0023a
Create Date: 2026-06-05

Problem
-------
PROJECTS-V1 additions Phase 10 (E1) removed per-project folders
(ADR-028). Migration 0023a archived the data into
``projects.meta.folders`` and stopped reading ``projects.folders``.
This migration drops the now-unread column so the schema reflects
the live shape.

Data preservation: ``projects.meta.folders`` carries the JSON array
that was in ``projects.folders`` at 0023a-apply time. Admins who
need the data after this drop read from there.

Dialect support
---------------
Both SQLite and Postgres run cleanly. SQLite requires
``batch_alter_table`` for column drops on non-temporary tables
(precedent: 0007_p11b_model_admin_ops_quota.py). Postgres uses plain
``op.drop_column``.

Reversibility
-------------
``downgrade()`` re-adds the column as a nullable JSON with
``server_default '[]'``. The data is NOT restored on downgrade — the
admin would need to copy ``meta.folders`` back into
``projects.folders`` via a one-off script. This is documented as a
one-way migration in the admin's runbook; the alternative would be
keeping the dead column around forever which defeats the point of
landing this in v1.0.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# Revision identifiers, used by Alembic.
revision: str = "0023b"
down_revision: str | None = "0023a"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name  # type: ignore[attr-defined]
    if dialect == "sqlite":
        with op.batch_alter_table("projects") as bop:
            bop.drop_column("folders")
    else:
        op.drop_column("projects", "folders")


def downgrade() -> None:
    """Re-add the column. Data NOT restored (admin-action — copy
    ``meta.folders`` back if needed)."""
    bind = op.get_bind()
    dialect = bind.dialect.name  # type: ignore[attr-defined]
    if dialect == "sqlite":
        with op.batch_alter_table("projects") as bop:
            bop.add_column(
                sa.Column(
                    "folders",
                    sa.JSON,
                    nullable=False,
                    server_default=sa.text("'[]'"),
                )
            )
    else:
        op.add_column(
            "projects",
            sa.Column(
                "folders",
                sa.JSON,
                nullable=False,
                server_default=sa.text("'[]'"),
            ),
        )
