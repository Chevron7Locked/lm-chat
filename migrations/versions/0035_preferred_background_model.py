# SPDX-License-Identifier: Apache-2.0
"""Add ``preferred_background_model_id`` column to ``server_lm_studio_default``.

Lets the admin pin a small/always-resident model for the out-of-band
auxiliary LLM calls (auto-memory distillation, chat-title generation and
follow-up chips) so those background tasks stop competing with the user's
next chat turn on a single local model.

Column notes
------------
preferred_background_model_id  TEXT nullable.  When NULL (default), the
    background tasks reuse the CHAT's model (today's behaviour — "Same as
    chat model").  When set, ``resolve_background_model_id`` returns it
    *only if* that model is currently loaded in LM Studio; otherwise it
    FAILS SOFT back to the chat model (background work is best-effort,
    never worth raising for).

Only one row ever exists in ``server_lm_studio_default`` (id=1).
The column is written by the new
``LmStudioOverridesService.set_preferred_background_model`` setter
(PATCH ``/api/settings/lmstudio/background-model``) and read by
``resolve_background_model_id``.  It is NOT written by
``set_admin_default`` / ``_upsert_admin_row`` (different lifecycle).

Reversibility
-------------
``downgrade()`` drops the column using batch mode for SQLite <3.35
portability (plain ALTER TABLE DROP COLUMN is unsupported on older
SQLite versions).  Postgres uses a plain ALTER TABLE DROP COLUMN.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0035"
down_revision: str = "0034"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add ``preferred_background_model_id`` to ``server_lm_studio_default``."""
    op.add_column(
        "server_lm_studio_default",
        sa.Column("preferred_background_model_id", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Drop ``preferred_background_model_id`` from ``server_lm_studio_default``.

    Uses batch mode for SQLite <3.35 portability.
    """
    with op.batch_alter_table("server_lm_studio_default") as batch_op:
        batch_op.drop_column("preferred_background_model_id")
