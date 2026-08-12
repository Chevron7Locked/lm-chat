# SPDX-License-Identifier: Apache-2.0
"""Add ``repeat_warning_cut_k`` column to ``server_lm_studio_default``.

Backs the tool-call repeat-loop cut threshold (K) admin override — the
global counterpart to the per-chat ``chats.settings.repeat_warning_cut_k``
override. ``streaming_service.py``'s ``_track_loop_cut_signals`` aborts a
turn once the streaming client's lookback deque has fired a
``tool_call.repeat_warning`` for the same (tool name, args) signature K
times.

Column notes
------------
repeat_warning_cut_k  Integer nullable.  NULL (default) means "use the
    Settings env-var default" (``Settings.lm_chat_repeat_warning_cut_k``,
    itself defaulting to 16).  Range 0..100 enforced at the route/service
    layer (not a DB constraint, matching the sibling override columns on
    this table); 0 disables the cut.

Only one row ever exists in ``server_lm_studio_default`` (id=1).  The
column is read by ``app_settings_service.resolve_repeat_warning_cut_k``
and written by ``app_settings_service.set_repeat_warning_cut_k`` (PATCH
``/api/settings/app``).  Effective-K resolution order: per-chat override
-> this global admin override -> config default.

Reversibility
-------------
``downgrade()`` drops the column using batch mode for SQLite <3.35
portability (plain ALTER TABLE DROP COLUMN is unsupported on older SQLite
versions).  Postgres uses a plain ALTER TABLE DROP COLUMN.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0044"
down_revision: str = "0043"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add ``repeat_warning_cut_k`` to ``server_lm_studio_default``."""
    op.add_column(
        "server_lm_studio_default",
        sa.Column("repeat_warning_cut_k", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    """Drop ``repeat_warning_cut_k`` from ``server_lm_studio_default``.

    Uses batch mode for SQLite <3.35 portability.
    """
    with op.batch_alter_table("server_lm_studio_default") as batch_op:
        batch_op.drop_column("repeat_warning_cut_k")
