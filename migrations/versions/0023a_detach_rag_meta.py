# SPDX-License-Identifier: Apache-2.0
"""Detach snapshot + RAG-mode threshold + projects.meta archival.

Revision ID: 0023a
Revises: 0022b
Create Date: 2026-06-05

Problem
-------
PROJECTS-V1 additions Phase 10 lands three additive nullable columns
plus a one-time data migration that archives the soon-to-be-removed
``projects.folders`` JSON into a new generic ``projects.meta`` JSON
column. The "0023a" suffix matches the spec's phasing: 0023b drops
the ``projects.folders`` column itself in the same v1.0 window;
0023a only stops reading it.

Fix
---
1. ``chats.detached_from_project_meta`` JSON nullable — snapshot of
   ``{project_id, name, detached_at, system_prompt_hash}`` written at
   move-out time so the chat history can render a "Detached from X on
   Y" separator turn even if the project is later deleted. Spec
   v3.1 §D1 + convergence Q2=A.
2. ``projects.rag_threshold`` Integer nullable — per-project override
   for the RAG-mode inline/hybrid threshold. NULL falls back to the
   ADR-locked ``ctx_window × inline_fraction`` formula at runtime.
   Spec v3.1 §D2 + convergence Q3=C.
3. ``projects.meta`` JSON nullable + ``server_default '{}'`` — generic
   per-project metadata home. v1.0 use: archive the soon-to-be-dropped
   ``projects.folders`` JSON into ``meta.folders``.

Dialect support
---------------
Both SQLite (default) and Postgres run cleanly. DDL uses
``op.batch_alter_table`` on SQLite (column adds need the recreate
pattern) and direct ``op.add_column`` on Postgres. The archival step
dispatches to the dialect-specific helper in
``_folder_archival._archive_folders_sync_op`` which uses ``json_set``
on SQLite and ``jsonb_set`` on Postgres. Any other dialect raises
``NotImplementedError`` at the archival step.

Reversibility
-------------
``downgrade()`` drops the three new columns. ``projects.meta`` data
is lost on downgrade — archival is one-way.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# Revision identifiers, used by Alembic.
revision: str = "0023a"
down_revision: str | None = "0022b"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name  # type: ignore[attr-defined]

    if dialect == "sqlite":
        with op.batch_alter_table("chats") as bop:
            bop.add_column(
                sa.Column(
                    "detached_from_project_meta",
                    sa.JSON,
                    nullable=True,
                )
            )
        with op.batch_alter_table("projects") as bop:
            bop.add_column(
                sa.Column(
                    "rag_threshold", sa.Integer, nullable=True
                )
            )
            bop.add_column(
                sa.Column(
                    "meta",
                    sa.JSON,
                    nullable=False,
                    server_default=sa.text("'{}'"),
                )
            )
    else:
        op.add_column(
            "chats",
            sa.Column(
                "detached_from_project_meta", sa.JSON, nullable=True
            ),
        )
        op.add_column(
            "projects",
            sa.Column("rag_threshold", sa.Integer, nullable=True),
        )
        op.add_column(
            "projects",
            sa.Column(
                "meta",
                sa.JSON,
                nullable=False,
                server_default=sa.text("'{}'"),
            ),
        )

    # Data migration — archive projects.folders into projects.meta.folders.
    # Per-dialect SQL lives in ``_folder_archival`` (sqlite=json_set,
    # postgres=jsonb_set). Batched by 100 rows to avoid lock contention
    # on large project sets.
    from lmchat.services._folder_archival import (
        _archive_folders_sync_op,
    )

    _archive_folders_sync_op(bind, batch_size=100)


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name  # type: ignore[attr-defined]
    if dialect == "sqlite":
        with op.batch_alter_table("projects") as bop:
            bop.drop_column("meta")
            bop.drop_column("rag_threshold")
        with op.batch_alter_table("chats") as bop:
            bop.drop_column("detached_from_project_meta")
    else:
        op.drop_column("projects", "meta")
        op.drop_column("projects", "rag_threshold")
        op.drop_column("chats", "detached_from_project_meta")
