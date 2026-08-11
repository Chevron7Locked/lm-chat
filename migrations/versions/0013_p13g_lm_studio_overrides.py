# SPDX-License-Identifier: Apache-2.0
"""P13g: LM Studio config overrides (per-user + server-admin default).

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-22

Changes
-------
Implements the schema half of P13g + ADR-023:

1. ``user_lm_studio_overrides`` — one row per user that holds optional
   override values for the LM Studio base URL, API key (encrypted at
   rest via the ADR-007 ``enc$v1$`` envelope; kind ``lm_studio_api_key``
   with record_id=user_id), and default model.

2. ``server_lm_studio_default`` — singleton table for the admin-set
   server-wide default.  Only ``id=1`` is ever inserted; the route layer
   UPSERTs on that PK.  API key is encrypted with envelope kind
   ``lm_studio_admin_default``, record_id=1.

The fallback chain (user override → admin default → env var) is
implemented in ``LmStudioOverridesService.resolve``; this migration
only provides the storage shape.

Downgrade path
--------------
Drops both tables; data loss is intentional (the env-var default
remains the live source after downgrade).

Round-trip rationale
--------------------
Plain ``create_table`` / ``drop_table`` for both — no ALTER on existing
rows, no FK rename, no batch dance required.  CASCADE on the user_id
FK matches the per-user CASCADE pattern used everywhere else in the
schema (sessions, chats, etc.).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create user_lm_studio_overrides + server_lm_studio_default tables."""
    op.create_table(
        "user_lm_studio_overrides",
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("base_url", sa.Text(), nullable=True),
        sa.Column("api_key_enc", sa.Text(), nullable=True),
        sa.Column("default_model", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "server_lm_studio_default",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("base_url", sa.Text(), nullable=True),
        sa.Column("api_key_enc", sa.Text(), nullable=True),
        sa.Column("default_model", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    """Drop both tables."""
    op.drop_table("server_lm_studio_default")
    op.drop_table("user_lm_studio_overrides")
