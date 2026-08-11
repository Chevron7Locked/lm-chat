# SPDX-License-Identifier: Apache-2.0
"""Add four app-level admin settings to ``server_lm_studio_default``.

Promotes four env-only flags to runtime admin overrides:

    - ``memory_distillation_enabled``          Boolean  (nullable)
    - ``subsession_memory_distillation_enabled`` Boolean  (nullable)
    - ``web_search_provider``                  String(16) (nullable)
    - ``searxng_url``                          String(512) (nullable)

When a column is NULL (the default), the resolver falls back to the
corresponding ``Settings`` env-var default.  Admins can set/clear each
override via ``PATCH /api/settings/app``.

Only one row ever exists in ``server_lm_studio_default`` (id=1).
The columns are read by ``app_settings_service.py`` and consumed by
``streaming_service.py``, ``routes/chats.py``, and ``app.py``.

Reversibility
-------------
``downgrade()`` drops all four columns using batch mode for SQLite
<3.35 portability (plain ALTER TABLE DROP COLUMN is unsupported on
older SQLite versions).  Postgres uses a plain ALTER TABLE DROP COLUMN.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0036"
down_revision: str = "0035"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add four app-level override columns to ``server_lm_studio_default``."""
    op.add_column(
        "server_lm_studio_default",
        sa.Column("memory_distillation_enabled", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "server_lm_studio_default",
        sa.Column("subsession_memory_distillation_enabled", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "server_lm_studio_default",
        sa.Column("web_search_provider", sa.String(16), nullable=True),
    )
    op.add_column(
        "server_lm_studio_default",
        sa.Column("searxng_url", sa.String(512), nullable=True),
    )


def downgrade() -> None:
    """Drop the four app-level override columns from ``server_lm_studio_default``."""
    with op.batch_alter_table("server_lm_studio_default") as batch_op:
        batch_op.drop_column("searxng_url")
        batch_op.drop_column("web_search_provider")
        batch_op.drop_column("subsession_memory_distillation_enabled")
        batch_op.drop_column("memory_distillation_enabled")
