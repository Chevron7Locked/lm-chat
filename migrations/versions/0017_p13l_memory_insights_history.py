# SPDX-License-Identifier: Apache-2.0
"""P13l.3: memory_insights_history table (refine backup + undo).

Revision ID: 0017
Revises: 0016
Create Date: 2026-05-22

Changes
-------
Implements the schema half of P13l.3 (audit O-15 — memory edit + refine):

1. ``memory_insights_history`` — append-only audit-and-restore table
   written before destructive memory operations.  Stores a JSON snapshot
   of the user's insights at the moment the destructive op runs so the
   admin can undo it with one click.

The refine endpoint replaces N insights with K ≤ N refined entries; any
information lost in the merge can be recovered by POSTing the returned
``history_id`` back to ``POST /api/memory/restore/{history_id}``.  The
restore handler mints fresh PKs for the restored rows because the old
IDs may have been freed in the interim (LLM-curated refine collapses
near-duplicates by intent).

Columns
-------
* ``id`` — auto-incrementing PK (portable BIGINT/INTEGER variant).
* ``user_id`` — FK users.id, CASCADE delete.
* ``event`` — short tag describing the destructive op.  ``"refine"``
  for now; future-proof for ``"delete"`` / ``"bulk_purge"``.
* ``insights_before`` — JSON list of ``{id, text, created_at}`` tuples.
* ``created_at`` — row creation timestamp.

Index
-----
``ix_memory_insights_history_user_id`` accelerates the per-user
``GET /api/memory/history`` listing (not yet exposed; the route layer
fetches a single history_id at restore time).

Downgrade path
--------------
Drops the table.  Any undo history is lost — acceptable because refine
is itself reversible only through this table, so dropping it forfeits
the undo capability symmetrically with the upgrade direction.

Round-trip rationale
--------------------
Plain ``create_table`` / ``drop_table`` / ``create_index`` /
``drop_index`` — no FK rename, no column rename, no batch dance.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create memory_insights_history table + supporting index."""
    op.create_table(
        "memory_insights_history",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("insights_before", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_memory_insights_history_user_id",
        "memory_insights_history",
        ["user_id"],
    )


def downgrade() -> None:
    """Drop memory_insights_history table."""
    op.drop_index(
        "ix_memory_insights_history_user_id",
        table_name="memory_insights_history",
    )
    op.drop_table("memory_insights_history")
