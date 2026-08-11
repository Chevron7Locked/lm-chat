# SPDX-License-Identifier: Apache-2.0
"""Drop the unused ``api_tokens`` table.

Revision ID: 0041
Revises: 0040
Create Date: 2026-07-19

Problem
-------
``api_tokens`` was scaffolded in the baseline migration (0001) as
API-token infrastructure but was never wired to any route, service, or
auth path — LMChat authenticates exclusively via session cookies
(``lmchat_session``). A defrag sweep (2026-07-19) confirmed zero
references to the table outside its own definition in ``db/schema.py``.
This migration drops the dead table.

Reversibility
-------------
``downgrade()`` recreates the table mirroring the column definitions
that existed in ``db/schema.py`` immediately before this migration. No
data is restored — the table had no writer, so in practice it was
always empty.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0041"
down_revision: str = "0040"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Drop the unused api_tokens table."""
    op.drop_table("api_tokens")


def downgrade() -> None:
    """Recreate api_tokens (empty — no data preservation, table was unused)."""
    op.create_table(
        "api_tokens",
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
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
