# SPDX-License-Identifier: Apache-2.0
"""Add ``projects.embedding_model_id`` + ``projects.default_model_id`` (nullable Text).

Revision ID: 0022
Revises: 0021
Create Date: 2026-06-04

Problem
-------
Two distinct local-first concerns surfaced by the additions
conceptual panel (5 seats, 2026-06-04):

1. Admins routinely swap embedding models in LMChat (cloud users
   effectively never do). When they do, chunks previously embedded
   under model X live in a different vector space than fresh queries
   embedded under model Y — cosine similarity becomes noise. A
   per-project embedding-model pin (`embedding_model_id`) lets the
   write-side refuse a mismatched attach and the UI surface a
   re-embed flow.

2. LMChat's heterogeneous local models (varying context windows and
   capabilities) mean a project is partly a context-budget + capability
   commitment. A per-project model pin (`default_model_id`) seeds
   `chats.model_id` on new in-project chat creation; chats keep
   their own override.

Fix
---
Add two nullable Text columns to ``projects``. Both default to NULL
(no pin); existing projects continue to behave identically.

* ``embedding_model_id`` — pinned by the FIRST document attach
  (write-once invariant lives in ``documents_service``). Subsequent
  attaches under a different active model raise an
  ``EmbeddingModelPinConflict`` → 409.
* ``default_model_id`` — seeded into ``chats.model_id`` on
  ``POST /api/projects/{id}/chats``. NULL falls through to the
  user's global default.

Both columns are advisory at the data-model layer; enforcement is
service-side. The read-time wiring of ``embedding_model_id`` into
``retrieval_service.retrieve`` lands in Phase 9 (plan v1.1 patch #1).

Dialect notes
-------------
Both columns are plain nullable Text — no FK constraint. The dialect
branch is therefore lighter than ``0021``'s; SQLite still uses
``batch_alter_table`` per the project's standing convention for
column adds (precedent: 0019, 0020, 0021), even though a plain
``op.add_column`` works for non-FK columns on modern SQLite. The
batch pattern keeps the diff symmetric for downgrade and matches
admin expectations.

Reversibility
-------------
``downgrade()`` drops both columns via ``batch_alter_table``. Any
projects with pinned models lose the pin on downgrade (acceptable —
the column carries advisory data; downgrade is a one-way admin
choice).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# Revision identifiers, used by Alembic.
revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name  # type: ignore[attr-defined]

    if dialect == "sqlite":
        with op.batch_alter_table("projects") as bop:
            bop.add_column(
                sa.Column("embedding_model_id", sa.Text, nullable=True)
            )
            bop.add_column(
                sa.Column("default_model_id", sa.Text, nullable=True)
            )
    else:
        op.add_column(
            "projects",
            sa.Column("embedding_model_id", sa.Text, nullable=True),
        )
        op.add_column(
            "projects",
            sa.Column("default_model_id", sa.Text, nullable=True),
        )


def downgrade() -> None:
    with op.batch_alter_table("projects") as bop:
        bop.drop_column("default_model_id")
        bop.drop_column("embedding_model_id")
