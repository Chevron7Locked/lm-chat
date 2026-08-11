# SPDX-License-Identifier: Apache-2.0
"""Add model_id column to chats table for per-chat model persistence.

Revision ID: 0019
Revises: 0018
Create Date: 2026-05-28

Problem
-------
The per-chat model-selection feature was half-wired: the frontend sends
``model_id`` via PATCH /api/chats/{id}, and ``ChatService.set_model_id``
runs ``UPDATE chats SET model_id=...``, but the ``chats`` table had no
``model_id`` column — causing ``sqlite3.OperationalError`` at runtime and
making the model selection never persist across page reloads.

Fix
---
Add a nullable ``TEXT`` column ``model_id`` to ``chats``.  NULL means the
chat has no model selected yet (new-chat default: "Select a model…"
placeholder in the frontend).  The column is nullable with no NOT NULL
constraint so existing rows are unaffected and the migration is safe to
apply on a live database without data loss.

Reversibility
-------------
``downgrade()`` drops the column via batch_alter_table (required for SQLite,
which does not support ``ALTER TABLE ... DROP COLUMN`` on all versions).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add nullable model_id TEXT column to chats."""
    bind = op.get_bind()
    dialect = bind.dialect.name  # type: ignore[attr-defined]

    if dialect == "sqlite":
        # SQLite requires batch mode for structural ALTER TABLE changes.
        with op.batch_alter_table("chats") as batch_op:
            batch_op.add_column(sa.Column("model_id", sa.Text, nullable=True))
    else:
        op.add_column("chats", sa.Column("model_id", sa.Text, nullable=True))


def downgrade() -> None:
    """Drop model_id column from chats."""
    bind = op.get_bind()
    dialect = bind.dialect.name  # type: ignore[attr-defined]

    if dialect == "sqlite":
        with op.batch_alter_table("chats") as batch_op:
            batch_op.drop_column("model_id")
    else:
        op.drop_column("chats", "model_id")
