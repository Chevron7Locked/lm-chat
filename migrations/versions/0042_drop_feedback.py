# SPDX-License-Identifier: Apache-2.0
"""Drop the unused ``feedback`` table (thumbs-up/down write-path removal).

Revision ID: 0042
Revises: 0041
Create Date: 2026-07-19

Problem
-------
The P4 thumbs-up/down message-feedback loop (``feedback`` table, the
``POST``/``GET``/``DELETE /api/messages/{id}/feedback`` routes, and
``MemoryService.record_activations`` / ``handle_feedback``) was built
but never wired to any UI. A defrag sweep (2026-07-19) confirmed the
service methods had zero production callers and the routes were dead
surface area; the route and service-layer removal ships in the same
change as this migration. This migration drops the ``feedback`` table.

The ``memory_insights.ups`` / ``downs`` / ``last_feedback_at`` columns
and the recall bayesian scorer are OUT OF SCOPE and untouched — they
become a harmless, permanently-zero constant. ``insight_activations``
is likewise untouched (retained for schema/privacy-invariant
continuity); it simply has no writer left.

Reversibility
-------------
``downgrade()`` recreates the table mirroring the column definitions
that existed in ``db/schema.py`` immediately before this migration. No
data is restored — the write path had zero production callers, so in
practice the table held no rows worth preserving.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0042"
down_revision: str = "0041"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Drop the unused feedback table."""
    op.drop_table("feedback")


def downgrade() -> None:
    """Recreate feedback (empty — no data preservation, write path was dead)."""
    op.create_table(
        "feedback",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
        ),
        sa.Column(
            "message_id",
            sa.BigInteger(),
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("rating", sa.SmallInteger(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
