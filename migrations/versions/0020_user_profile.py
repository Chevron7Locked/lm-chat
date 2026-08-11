# SPDX-License-Identifier: Apache-2.0
"""Add email, display_name, avatar_url to users for the Profile tab.

Revision ID: 0020
Revises: 0019
Create Date: 2026-06-03

Problem
-------
The 2026-06-03 UI audit confirmed Settings has no Profile tab and
``/api/auth/me`` exposes no user-presentable identity beyond
``username``. The chat shell, sidebar, and any future "share with
collaborator" surface need a richer identity (display name, contact
hint, optional avatar). Without these columns the frontend has nothing
to render in a Profile section.

Fix
---
Add three nullable TEXT columns to ``users``:

* ``email`` — non-unique on purpose (no email-based recovery yet, so a
  unique constraint would block legitimate multi-tenancy with shared
  inboxes without any functional benefit). Format-validated at the
  route layer.
* ``display_name`` — free-text presentation name; falls back to
  ``username`` when null.
* ``avatar_url`` — URL string, NOT a blob. Storing image bytes in
  ``users`` would bloat every auth round-trip; the frontend can host
  its own asset pipeline or use external avatars.

All three are nullable so the migration is safe on a live DB without
backfill. Existing accounts hydrate with all three at ``null`` and a
Profile tab visit becomes the first opportunity to populate them.

Reversibility
-------------
``downgrade()`` drops all three columns via batch_alter_table (SQLite
compatibility).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add nullable email/display_name/avatar_url TEXT columns to users."""
    bind = op.get_bind()
    dialect = bind.dialect.name  # type: ignore[attr-defined]

    if dialect == "sqlite":
        with op.batch_alter_table("users") as batch_op:
            batch_op.add_column(sa.Column("email", sa.Text, nullable=True))
            batch_op.add_column(sa.Column("display_name", sa.Text, nullable=True))
            batch_op.add_column(sa.Column("avatar_url", sa.Text, nullable=True))
    else:
        op.add_column("users", sa.Column("email", sa.Text, nullable=True))
        op.add_column("users", sa.Column("display_name", sa.Text, nullable=True))
        op.add_column("users", sa.Column("avatar_url", sa.Text, nullable=True))


def downgrade() -> None:
    """Drop profile columns from users."""
    bind = op.get_bind()
    dialect = bind.dialect.name  # type: ignore[attr-defined]

    if dialect == "sqlite":
        with op.batch_alter_table("users") as batch_op:
            batch_op.drop_column("avatar_url")
            batch_op.drop_column("display_name")
            batch_op.drop_column("email")
    else:
        op.drop_column("users", "avatar_url")
        op.drop_column("users", "display_name")
        op.drop_column("users", "email")
