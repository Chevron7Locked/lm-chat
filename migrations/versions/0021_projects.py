# SPDX-License-Identifier: Apache-2.0
"""Add `projects` table + nullable `project_id` FKs on chats / documents / memory_insights.

Revision ID: 0021
Revises: 0020
Create Date: 2026-06-04

Problem
-------
LMChat needs project workspace containers: a Project owns chats,
documents, and memory_insights via FK, plus carries its own
``system_prompt`` (injected at stream-time) and a folder list. Without
a ``projects`` table and nullable ``project_id`` FKs on the three
child tables, retrieval cannot be scoped and the admin cannot
"open a project and start a chat with the right docs + instructions
already attached."

Fix
---
* Create ``projects`` (id, user_id CASCADE, name, description,
  system_prompt, folders JSON, created_at, updated_at).
* Add nullable ``project_id`` FK columns on ``chats``, ``documents``,
  and ``memory_insights``. ``ON DELETE SET NULL`` so deleting a
  project never destroys chats/docs/insights — they just become
  un-projected. Indexed for lookup performance.

All FK columns are nullable; existing rows hydrate with
``project_id IS NULL`` and behave identically to the pre-Projects
substrate. No data backfill required.

Dialect notes
-------------
SQLite requires ``batch_alter_table`` for column adds that carry a
FOREIGN KEY clause — only via the table-recreation pattern does
SQLite reliably enforce the FK constraint with PRAGMA foreign_keys=ON
(set by the lifespan). Pattern mirrored from ``0019_per_chat_model_id``
and ``0020_user_profile``. Postgres accepts plain ``op.add_column``
with inline FK.

Reversibility
-------------
``downgrade()`` drops the three FK columns then drops the projects
table. Same dialect-aware pattern.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create projects table + add nullable project_id FKs on three child tables."""
    bind = op.get_bind()
    dialect = bind.dialect.name  # type: ignore[attr-defined]

    # 1. Create the projects table.
    op.create_table(
        "projects",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            sa.BigInteger,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column(
            "description",
            sa.Text,
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column(
            "system_prompt",
            sa.Text,
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column(
            "folders",
            sa.JSON,
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
    )
    op.create_index("ix_projects_user_id", "projects", ["user_id"])

    # 2. Add nullable project_id FKs on the three child tables.
    #    SQLite needs batch_alter_table so the FK is part of the
    #    recreated table (PRAGMA foreign_keys=ON in lifespan then
    #    enforces ON DELETE SET NULL). Postgres accepts inline.
    # FK constraints must carry explicit names so batch_alter_table
    # can manipulate them (anonymous constraints raise ValueError
    # under SQLite's table-recreation path).
    def _project_fk(parent_table: str) -> sa.ForeignKey:
        return sa.ForeignKey(
            "projects.id",
            ondelete="SET NULL",
            name=f"fk_{parent_table}_project_id",
        )

    if dialect == "sqlite":
        with op.batch_alter_table("chats") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "project_id",
                    sa.Integer,
                    _project_fk("chats"),
                    nullable=True,
                )
            )
        with op.batch_alter_table("documents") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "project_id",
                    sa.Integer,
                    _project_fk("documents"),
                    nullable=True,
                )
            )
        with op.batch_alter_table("memory_insights") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "project_id",
                    sa.Integer,
                    _project_fk("memory_insights"),
                    nullable=True,
                )
            )
    else:
        op.add_column(
            "chats",
            sa.Column(
                "project_id",
                sa.Integer,
                _project_fk("chats"),
                nullable=True,
            ),
        )
        op.add_column(
            "documents",
            sa.Column(
                "project_id",
                sa.Integer,
                _project_fk("documents"),
                nullable=True,
            ),
        )
        op.add_column(
            "memory_insights",
            sa.Column(
                "project_id",
                sa.Integer,
                _project_fk("memory_insights"),
                nullable=True,
            ),
        )

    # 3. Indexes on the new FK columns (created outside batch_alter so
    #    the names match the schema.py mirror exactly).
    op.create_index("ix_chats_project_id", "chats", ["project_id"])
    op.create_index(
        "ix_documents_project_id", "documents", ["project_id"]
    )
    op.create_index(
        "ix_memory_insights_project_id",
        "memory_insights",
        ["project_id"],
    )


def downgrade() -> None:
    """Drop project_id FKs then drop the projects table."""
    bind = op.get_bind()
    dialect = bind.dialect.name  # type: ignore[attr-defined]

    op.drop_index("ix_memory_insights_project_id", "memory_insights")
    op.drop_index("ix_documents_project_id", "documents")
    op.drop_index("ix_chats_project_id", "chats")

    if dialect == "sqlite":
        with op.batch_alter_table("memory_insights") as batch_op:
            batch_op.drop_column("project_id")
        with op.batch_alter_table("documents") as batch_op:
            batch_op.drop_column("project_id")
        with op.batch_alter_table("chats") as batch_op:
            batch_op.drop_column("project_id")
    else:
        op.drop_column("memory_insights", "project_id")
        op.drop_column("documents", "project_id")
        op.drop_column("chats", "project_id")

    op.drop_index("ix_projects_user_id", "projects")
    op.drop_table("projects")
