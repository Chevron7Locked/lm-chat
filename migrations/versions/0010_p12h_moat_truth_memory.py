# SPDX-License-Identifier: Apache-2.0
"""P12h: moat-truth memory — insight_activations + memory_insights score columns.

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-21

Changes
-------
Implements the schema half of ADR-008 §6 (cognitive-decay + Bayesian
Laplace scoring + injection-tracking for feedback attribution).

1. ``insight_activations`` table — NEW.  Tracks the (assistant_message,
   insight) tuples that ``memory_service.recall_insights`` emits, so
   ``handle_feedback`` can attribute thumbs-up/down to the specific
   insights that were injected for THAT message rather than to the
   global pool. Per ADR-008 §6.7.

2. ``memory_insights`` table — ALTER.  Adds the score-bearing columns
   referenced by ADR §6.1–§6.5:
   - ``category``           — Tier label for τ lookup (CATEGORY_HALF_LIVES).
   - ``use_count``          — Bumped on every retrieval; feeds boost.
   - ``ups`` / ``downs``    — Laplace numerator/denominator (vote unit 0.5).
   - ``last_used``          — REAL UNIX-epoch seconds; hyperbolic-decay anchor.
   - ``last_feedback_at``   — REAL seconds; written on every feedback event.
   - ``state``              — ``'active'`` | ``'faded'``; fade transition
                              when compound score < 0.1 (no permanent silencing).

ADR §6.7 originally claimed "the existing schema already carries the
required columns" — verification against migrations 0001-0009 disproved
that claim (the columns were never added).  P12h closes this gap.

The ``insight_activations.insight_id`` column is declared ``BigInteger``
to match the actual ``memory_insights.id`` primary-key type (sqlite ``INTEGER``,
postgres ``BIGINT``); ADR §6.7's SQL snippet uses ``TEXT`` but that was
inherited from the v0.5.x source which had a TEXT PK on user_insights.
v1 keeps the PK type consistent — documented divergence.

Downgrade path
--------------
Drops insight_activations + its indexes, then drops each new
memory_insights column.  No data-preservation step (the columns are
post-fact-additions to a column-less-before table for these fields).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add memory_insights score columns + create insight_activations."""
    # -----------------------------------------------------------------------
    # 1. ALTER memory_insights — add score columns.
    # -----------------------------------------------------------------------
    # SQLite ALTER TABLE only supports ADD COLUMN one at a time; batch_alter_table
    # gives us a cross-dialect path that uses temp-table-copy on SQLite and
    # plain ALTER on Postgres.
    with op.batch_alter_table("memory_insights") as batch_op:
        batch_op.add_column(
            sa.Column(
                "category",
                sa.String(32),
                nullable=False,
                server_default=sa.text("'context'"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "use_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "ups",
                sa.Float(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "downs",
                sa.Float(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "last_used",
                sa.Float(),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "last_feedback_at",
                sa.Float(),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "state",
                sa.String(16),
                nullable=False,
                server_default=sa.text("'active'"),
            )
        )

    # -----------------------------------------------------------------------
    # 2. CREATE insight_activations.
    # -----------------------------------------------------------------------
    op.create_table(
        "insight_activations",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
            autoincrement=True,
        ),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        # insight_id is BigInteger to match memory_insights.id (see module
        # docstring for the ADR §6.7 TEXT-vs-BigInteger note).
        sa.Column("insight_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["insight_id"],
            ["memory_insights.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_activations_message",
        "insight_activations",
        ["message_id"],
    )
    op.create_index(
        "idx_activations_insight",
        "insight_activations",
        ["insight_id"],
    )


def downgrade() -> None:
    """Drop insight_activations + remove memory_insights score columns."""
    op.drop_index("idx_activations_insight", table_name="insight_activations")
    op.drop_index("idx_activations_message", table_name="insight_activations")
    op.drop_table("insight_activations")

    with op.batch_alter_table("memory_insights") as batch_op:
        batch_op.drop_column("state")
        batch_op.drop_column("last_feedback_at")
        batch_op.drop_column("last_used")
        batch_op.drop_column("downs")
        batch_op.drop_column("ups")
        batch_op.drop_column("use_count")
        batch_op.drop_column("category")
