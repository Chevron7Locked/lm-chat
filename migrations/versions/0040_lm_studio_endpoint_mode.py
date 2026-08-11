# SPDX-License-Identifier: Apache-2.0
"""Add ``lm_studio_endpoint_mode`` column to ``server_lm_studio_default``.

Backs the LM Studio endpoint-mode selector (Settings → Models → LM Studio
+ onboarding): a global toggle between LM Studio's native ``/api/v1/chat``
surface (LM Studio runs MCP tools itself, server-side conversation
chaining) and its OpenAI-compatible ``/v1/chat/completions`` surface
(LM Chat drives MCP tools client-side through its own MCP Store, full
history replayed each turn). Which MCP system runs the tool loop follows
this setting automatically — see ``streaming_service.py``'s A3 provider
resolution block.

Column notes
------------
lm_studio_endpoint_mode  TEXT nullable.  NULL (default) means ``"native"``;
    ``"openai_compat"`` is the only other stored value.  Mirrors the
    NULL="default" convention already used by ``preferred_embedding_model_id``
    / ``preferred_background_model_id`` on this same row.

Only one row ever exists in ``server_lm_studio_default`` (id=1).  The
column is written by ``LmStudioOverridesService.set_endpoint_mode`` (PATCH
``/api/settings/lmstudio/endpoint-mode``) and read by
``fetch_endpoint_mode`` / the free-function mirror
``resolve_lm_studio_endpoint_mode`` used in the streaming hot path.  It is
NOT written by ``set_admin_default`` / ``_upsert_admin_row`` (different
lifecycle; no rewire — independent of the LM Studio connection
parameters).

Reversibility
-------------
``downgrade()`` drops the column using batch mode for SQLite <3.35
portability (plain ALTER TABLE DROP COLUMN is unsupported on older
SQLite versions).  Postgres uses a plain ALTER TABLE DROP COLUMN.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0040"
down_revision: str = "0039"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add ``lm_studio_endpoint_mode`` to ``server_lm_studio_default``."""
    op.add_column(
        "server_lm_studio_default",
        sa.Column("lm_studio_endpoint_mode", sa.Text, nullable=True),
    )


def downgrade() -> None:
    """Drop ``lm_studio_endpoint_mode`` from ``server_lm_studio_default``.

    Uses batch mode for SQLite <3.35 portability.
    """
    with op.batch_alter_table("server_lm_studio_default") as batch_op:
        batch_op.drop_column("lm_studio_endpoint_mode")
