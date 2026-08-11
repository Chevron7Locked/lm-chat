# SPDX-License-Identifier: Apache-2.0
"""Add ``provider_configs`` table for multi-provider credential persistence.

A4 workstream — Phase 2a config-persistence foundation.

Adds one row per external LLM provider (openai / openrouter / groq /
custom).  API keys are stored encrypted under the ADR-007 ``enc$v1$``
envelope via ``lmchat.services.provider_config_service``.

Table shape (all columns described in db/schema.py):
    id              — BIGINT autoincrement PK
    provider        — String(64) unique slug (e.g. "openrouter")
    base_url        — TEXT NOT NULL
    api_key_enc     — TEXT nullable (enc$v1$ ciphertext)
    default_model   — TEXT nullable
    extra_headers   — JSON nullable (e.g. HTTP-Referer for OpenRouter)
    enabled         — BOOLEAN NOT NULL server_default=true
    created_at      — DATETIME(tz) NOT NULL server_default=NOW()
    updated_at      — DATETIME(tz) NOT NULL server_default=NOW()
    ix_provider_configs_provider — unique index on provider

Reversibility
-------------
``downgrade()`` drops the table.  No data migration required because this
is a brand-new table (locked decision: additive only).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# Revision identifiers, used by Alembic.
revision: str = "0029"
down_revision: str = "0028"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create the ``provider_configs`` table."""
    op.create_table(
        "provider_configs",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
        ),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("api_key_enc", sa.Text(), nullable=True),
        sa.Column("default_model", sa.Text(), nullable=True),
        sa.Column("extra_headers", sa.JSON(), nullable=True),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("provider", name="uq_provider_configs_provider"),
    )
    op.create_index(
        "ix_provider_configs_provider",
        "provider_configs",
        ["provider"],
    )


def downgrade() -> None:
    """Drop the ``provider_configs`` table."""
    op.drop_index("ix_provider_configs_provider", table_name="provider_configs")
    op.drop_table("provider_configs")
