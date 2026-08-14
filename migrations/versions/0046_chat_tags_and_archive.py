# SPDX-License-Identifier: Apache-2.0
"""Chat tags + archive — add ``chats.tags`` and ``chats.archived_at``.

Revision ID: 0046
Revises: 0045
Create Date: 2026-08-13

Parity gap: chats could be organized by folder/pin but had no free-form
tagging and, unlike projects (migration 0038), no way to retire a chat
from the default list without deleting it.

Columns
-------
``tags`` — non-nullable ``JSON`` (``list[str]``), ``server_default '[]'``.
Every chat gets an empty-list baseline so NULL checks are unnecessary —
same convention as the existing ``chats.settings`` blob.

``archived_at`` — nullable ``DateTime(timezone=True)``. NULL = active
(the default listing filter); a timestamp = archived at that instant.
Same "NULL = active" convention as ``projects.archived_at`` (see
``ChatService.set_archived`` for the writer).

Dialect support
---------------
Mirrors ``0038_project_archiving.py``: ``op.batch_alter_table`` on
SQLite, plain ``op.add_column`` on Postgres. No FTS triggers on
``chats`` (unlike ``messages``), so batch mode's copy-and-recreate is
safe here.

Reversibility
-------------
``downgrade()`` drops both columns. Tag/archived state is lost on
downgrade — no chat rows are ever deleted by either direction.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0046"
down_revision: str = "0045"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add ``chats.tags`` (JSON, '[]' default) and ``chats.archived_at`` (nullable)."""
    bind = op.get_bind()
    dialect = bind.dialect.name  # type: ignore[attr-defined]

    if dialect == "sqlite":
        with op.batch_alter_table("chats") as bop:
            bop.add_column(
                sa.Column(
                    "tags",
                    sa.JSON(),
                    nullable=False,
                    server_default=sa.text("'[]'"),
                )
            )
            bop.add_column(
                sa.Column(
                    "archived_at", sa.DateTime(timezone=True), nullable=True
                )
            )
    else:
        op.add_column(
            "chats",
            sa.Column(
                "tags",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            ),
        )
        op.add_column(
            "chats",
            sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    """Drop ``chats.archived_at`` and ``chats.tags``."""
    bind = op.get_bind()
    dialect = bind.dialect.name  # type: ignore[attr-defined]
    if dialect == "sqlite":
        with op.batch_alter_table("chats") as bop:
            bop.drop_column("archived_at")
            bop.drop_column("tags")
    else:
        op.drop_column("chats", "archived_at")
        op.drop_column("chats", "tags")
