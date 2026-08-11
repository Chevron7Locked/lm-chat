# SPDX-License-Identifier: Apache-2.0
"""Project archiving — add ``projects.archived_at``.

Revision ID: 0038
Revises: 0037
Create Date: 2026-07-07

Wave 2 parity gap (#17): projects could only be deleted, never archived.
Deleting is destructive (children fall back to un-projected via the
``ON DELETE SET NULL`` cascades on chats / documents / memory_insights);
archiving lets the admin retire a stale project from the default
sidebar/list without losing the "this chat belongs to X" grouping.

Column
------
``archived_at`` — nullable ``DateTime(timezone=True)``. NULL = active
(the default listing filter); a timestamp = archived at that instant.
Same "NULL = active" convention as ``documents.deleted_at`` (see
``ProjectsService.set_archived`` for the writer).

Dialect support
---------------
Mirrors ``0023a_detach_rag_meta.py``'s handling of the same
``projects`` table: ``op.batch_alter_table`` on SQLite, plain
``op.add_column`` on Postgres. No FTS triggers on ``projects`` (unlike
``messages``), so batch mode's copy-and-recreate is safe here.

Reversibility
-------------
``downgrade()`` drops the column. Archived-vs-active state is lost on
downgrade — no project rows are ever deleted by either direction.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0038"
down_revision: str = "0037"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add ``projects.archived_at`` (nullable, NULL = active)."""
    bind = op.get_bind()
    dialect = bind.dialect.name  # type: ignore[attr-defined]

    if dialect == "sqlite":
        with op.batch_alter_table("projects") as bop:
            bop.add_column(
                sa.Column(
                    "archived_at", sa.DateTime(timezone=True), nullable=True
                )
            )
    else:
        op.add_column(
            "projects",
            sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    """Drop ``projects.archived_at``."""
    bind = op.get_bind()
    dialect = bind.dialect.name  # type: ignore[attr-defined]
    if dialect == "sqlite":
        with op.batch_alter_table("projects") as bop:
            bop.drop_column("archived_at")
    else:
        op.drop_column("projects", "archived_at")
