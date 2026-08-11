# SPDX-License-Identifier: Apache-2.0
"""Add ``tool_call_id`` to ``messages`` for Responses-API replay history.

Responses-API round-trip: when a tool-result turn (role="tool") arrives from
the stateless-provider (Responses API) encoder, it carries a ``call_id`` that
must be echoed back on the next-turn input array so the provider can correlate
the result with the original function_call output item.

Problem
-------
``CanonicalMessage.tool_call_id`` exists in ``lmchat.lmstudio.types`` and the
responses encoder reads ``msg.tool_call_id → call_id`` in
``_assemble_responses_input``, but the ``messages`` DB table has no such
column — so the value was never persisted and could never be reloaded.

Fix
---
Add a nullable TEXT ``tool_call_id`` column to ``messages``. NULL for all
non-tool-result roles and for rows written before this migration (no
backfill; locked decision 3 pattern).

Reversibility
-------------
``downgrade()`` drops the column (batch_alter_table on SQLite, direct DROP on
Postgres). Lost data is not recoverable from the DB alone, but is recoverable
from the upstream provider's stored response chain.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# Revision identifiers, used by Alembic.
revision: str = "0028"
down_revision: str = "0027"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add ``tool_call_id`` to ``messages``."""
    bind = op.get_bind()
    dialect = bind.dialect.name  # type: ignore[attr-defined]

    if dialect == "sqlite":
        with op.batch_alter_table("messages") as batch_op:
            batch_op.add_column(
                sa.Column("tool_call_id", sa.String(), nullable=True),
            )
    else:
        op.add_column(
            "messages",
            sa.Column("tool_call_id", sa.String(), nullable=True),
        )


def downgrade() -> None:
    """Drop ``tool_call_id`` from ``messages``."""
    bind = op.get_bind()
    dialect = bind.dialect.name  # type: ignore[attr-defined]

    if dialect == "sqlite":
        with op.batch_alter_table("messages") as batch_op:
            batch_op.drop_column("tool_call_id")
    else:
        op.drop_column("messages", "tool_call_id")
