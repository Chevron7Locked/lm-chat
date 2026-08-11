# SPDX-License-Identifier: Apache-2.0
"""P13k: admin_invites table (one-shot registration tokens).

Revision ID: 0014
Revises: 0013
Create Date: 2026-05-22

Changes
-------
Implements the schema half of P13k (Admin Users page + admin invite):

1. ``admin_invites`` — one row per admin-issued invite.  Holds the
   ``secrets.token_urlsafe(24)`` token, the issuing admin (``created_by``,
   SET NULL on issuer deletion), the expiry timestamp, and on consumption
   the ``used_at`` + ``used_by`` columns.

The invite is consumed by the existing ``POST /api/auth/register`` endpoint
via the same ``?token=`` query parameter / ``X-Setup-Token`` header surface
already used by the bootstrap setup token (P13f).  Choosing OPTION A from
the PLAN keeps the wire format consistent across both gating mechanisms.

Downgrade path
--------------
Drops the table.  Any unused invites are lost (acceptable — they were
single-use and the admin can re-issue).

Round-trip rationale
--------------------
Plain ``create_table`` / ``drop_table`` — no FK rename, no column rename, no
batch dance required.  SET NULL on both ``created_by`` and ``used_by`` FKs
follows the same pattern as ``audit_log.user_id`` so deleting a user does
not also erase the invite-history audit trail.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create admin_invites table + supporting index."""
    op.create_table(
        "admin_invites",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
        ),
        sa.Column("token", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "created_by",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "used_by",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_admin_invites_expires_at",
        "admin_invites",
        ["expires_at"],
    )


def downgrade() -> None:
    """Drop admin_invites table."""
    op.drop_index("ix_admin_invites_expires_at", table_name="admin_invites")
    op.drop_table("admin_invites")
