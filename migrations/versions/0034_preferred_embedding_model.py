# SPDX-License-Identifier: Apache-2.0
"""Add ``preferred_embedding_model_id`` column to ``server_lm_studio_default``.

Fixes non-deterministic embedding-model selection when more than one
embedding model is loaded simultaneously.  The new column stores the
admin's persisted preference so that ``MemoryService`` (and all
other new-indexing paths) always embed under a single, stable model
key rather than whichever happens to sort first in cache order.

Column notes
------------
preferred_embedding_model_id  TEXT nullable.  When NULL (default),
    the first lexicographically-sorted embedding model key is used on
    first indexing call and then written back as the preference
    (``persist_default=True`` path).  When set, the resolver fails
    LOUD if the preferred model is not currently loaded rather than
    silently picking a dimensionally-incompatible fallback.

Only one row ever exists in ``server_lm_studio_default`` (id=1).
The column is written by the new ``LmStudioOverridesService.set_preferred_embedding_model``
setter (Fix A — POST ``/api/settings/lmstudio/embedding-model``) and read by
``MemoryService.resolve_active_embedding_model_key``.  It is NOT written by
``set_admin_default`` / ``_upsert_admin_row`` (different lifecycle; no rewire).

Reversibility
-------------
``downgrade()`` drops the column using batch mode for SQLite <3.35
portability (plain ALTER TABLE DROP COLUMN is unsupported on older
SQLite versions).  Postgres uses a plain ALTER TABLE DROP COLUMN.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: str = "0033"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add ``preferred_embedding_model_id`` to ``server_lm_studio_default``."""
    op.add_column(
        "server_lm_studio_default",
        sa.Column("preferred_embedding_model_id", sa.Text, nullable=True),
    )


def downgrade() -> None:
    """Drop ``preferred_embedding_model_id`` from ``server_lm_studio_default``.

    Uses batch mode for SQLite <3.35 portability.
    """
    with op.batch_alter_table("server_lm_studio_default") as batch_op:
        batch_op.drop_column("preferred_embedding_model_id")
