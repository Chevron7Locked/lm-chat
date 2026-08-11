# SPDX-License-Identifier: Apache-2.0
"""Project auto-summary — add rolling-summary columns to ``projects``.

Revision ID: 0039
Revises: 0038
Create Date: 2026-07-07

Wave 3 gap (#10): a rolling per-project summary (a ~24h project
digest) that accumulates understanding of a project's
conversations, is injected into that project's chats as ambient context,
and is shown + regenerable on the project page.

Columns
-------
``summary`` — Text, NOT NULL, ``''`` default. The current rolling
summary text. "" (not NULL) = no summary generated yet, matching this
table's other free-text columns (``description`` / ``system_prompt``).

``summary_updated_at`` — nullable ``DateTime(timezone=True)``. Wall-clock
time of the last regeneration; NULL until the first summary is
generated.

``summary_message_watermark`` — Integer, NOT NULL, ``0`` default. The
project's total message count (across all its chats) at the last
regeneration — the throttle the auto-refresh trigger
(``StreamingService._safe_refresh_project_summary``) compares against
so it doesn't call the OOB summarizer on every turn.

See ``services/project_summary_service.py`` for the generator +
throttle logic and ``services/projects_service.py:ProjectsService.set_summary``
for the writer.

Dialect support
---------------
Mirrors ``0038_project_archiving.py``'s handling of the same
``projects`` table: ``op.batch_alter_table`` on SQLite, plain
``op.add_column`` on Postgres.

Reversibility
-------------
``downgrade()`` drops all three columns. Accumulated summary state is
lost on downgrade — no project rows are ever deleted by either
direction.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0039"
down_revision: str = "0038"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add ``projects.summary`` / ``summary_updated_at`` / ``summary_message_watermark``."""
    bind = op.get_bind()
    dialect = bind.dialect.name  # type: ignore[attr-defined]

    if dialect == "sqlite":
        with op.batch_alter_table("projects") as bop:
            bop.add_column(
                sa.Column(
                    "summary",
                    sa.Text(),
                    nullable=False,
                    server_default="",
                )
            )
            bop.add_column(
                sa.Column(
                    "summary_updated_at", sa.DateTime(timezone=True), nullable=True
                )
            )
            bop.add_column(
                sa.Column(
                    "summary_message_watermark",
                    sa.Integer(),
                    nullable=False,
                    server_default="0",
                )
            )
    else:
        op.add_column(
            "projects",
            sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        )
        op.add_column(
            "projects",
            sa.Column(
                "summary_updated_at", sa.DateTime(timezone=True), nullable=True
            ),
        )
        op.add_column(
            "projects",
            sa.Column(
                "summary_message_watermark",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )


def downgrade() -> None:
    """Drop the rolling-summary columns."""
    bind = op.get_bind()
    dialect = bind.dialect.name  # type: ignore[attr-defined]
    if dialect == "sqlite":
        with op.batch_alter_table("projects") as bop:
            bop.drop_column("summary_message_watermark")
            bop.drop_column("summary_updated_at")
            bop.drop_column("summary")
    else:
        op.drop_column("projects", "summary_message_watermark")
        op.drop_column("projects", "summary_updated_at")
        op.drop_column("projects", "summary")
