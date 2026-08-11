# SPDX-License-Identifier: Apache-2.0
"""Add ``messages.stop_reason`` for the persisted Continue chip.

Revision ID: 0026
Revises: 0025
Create Date: 2026-06-10

Problem
-------
The Continue chip ("answer was cut off — send another message to continue")
only existed in the SSE state machine: ``chat.end.stop_reason == "length"``
lived in per-tab memory, so on page reload (or the post-stream refetch swap)
the chip vanished. The chain was broken at the persistence layer — the
``messages`` table had no column recording why generation terminated.

Fix (Continue-chip closeout, audit 2026-06-10)
----------------------------------------------
Add a nullable TEXT column ``stop_reason`` to ``messages``. The streaming
service captures ``stop_reason`` from the upstream ``chat.end`` event and
writes it in ``_finalize_message``. ``list_for_chat`` returns it; the FE
renders the chip for persisted rows where ``stop_reason == "length"``.

Backfill (locked-decision-3 style)
----------------------------------
NULL for all existing rows — no re-mining of historical streams. Old rows
simply render without a chip; only streams finalized after this migration
carry a reason.

Reversibility
-------------
``downgrade()`` drops the column (batch_alter_table on SQLite per ADR-011
precedent). No data loss concern beyond the chip itself — the value is
re-derivable only from a re-run, and the chip is informational.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# Revision identifiers, used by Alembic.
revision: str = "0026"
down_revision: str = "0025"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add nullable stop_reason TEXT column to messages.

    NULL for all existing rows (locked decision 3 — no backfill; the column
    is populated only for streams finalized from this point on).
    """
    bind = op.get_bind()
    dialect = bind.dialect.name  # type: ignore[attr-defined]

    if dialect == "sqlite":
        with op.batch_alter_table("messages") as batch_op:
            batch_op.add_column(
                sa.Column("stop_reason", sa.Text, nullable=True)
            )
    else:
        op.add_column(
            "messages",
            sa.Column("stop_reason", sa.Text, nullable=True),
        )


def downgrade() -> None:
    """Drop stop_reason column from messages."""
    bind = op.get_bind()
    dialect = bind.dialect.name  # type: ignore[attr-defined]

    if dialect == "sqlite":
        with op.batch_alter_table("messages") as batch_op:
            batch_op.drop_column("stop_reason")
    else:
        op.drop_column("messages", "stop_reason")
