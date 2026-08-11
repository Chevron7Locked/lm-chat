# SPDX-License-Identifier: Apache-2.0
"""P15: fix audit_log.created_at monotonicity under concurrent load.

Revision ID: 0018
Revises: 0017
Create Date: 2026-05-23

Problem
-------
P15 stress-baseline (test_audit_timestamps_non_decreasing) surfaced a real
timestamp-inversion bug: rows ordered by id had inverted created_at values
(~198ms skew, exceeding the 100ms tolerance).

Root cause: ``audit_log.created_at`` used ``now()`` / ``func.now()`` as the
server default.  In Postgres, ``now()`` is bound to **transaction start time**
(equivalent to ``CURRENT_TIMESTAMP``).  Under concurrent insert load, two
transactions can acquire their sequence IDs in one order but commit in a
different order, so the transaction that grabbed id=974 may have a later
``now()`` value than the transaction that grabbed id=975 if it started later
but committed first.

Fix
---
Replace the server default with ``clock_timestamp()``.  Unlike ``now()``,
``clock_timestamp()`` returns the actual wall-clock time at the moment the
function is evaluated (i.e. at INSERT execution time), not the transaction
start time.  This eliminates the id/timestamp ordering inversion.

SQLite (dev/test only) does not have ``clock_timestamp()``.  The downgrade
path restores ``CURRENT_TIMESTAMP`` which is adequate for SQLite's
single-writer model.  The upgrade path is dialect-guarded: on SQLite the
default is set to the strftime expression that produces millisecond-precision
ISO-8601 timestamps (SQLite's ``CURRENT_TIMESTAMP`` only has second
granularity, but the stress test is Postgres-targeted anyway).

Reversibility
-------------
``downgrade()`` restores the original ``now()`` / ``CURRENT_TIMESTAMP``
server default, which is safe (reverts to the pre-fix behaviour — timestamps
will again reflect transaction-start time but remain functionally correct for
non-stress workloads).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str = "0017"
branch_labels = None
depends_on = None

# ---------------------------------------------------------------------------
# Dialect-aware default expressions
# ---------------------------------------------------------------------------
_PG_DEFAULT = "clock_timestamp()"
_SQLITE_DEFAULT = "(strftime('%Y-%m-%dT%H:%M:%fZ','now'))"
_FALLBACK_DEFAULT = "CURRENT_TIMESTAMP"


def _dialect() -> str:
    """Return the current Alembic connection dialect name."""
    bind = op.get_bind()
    return bind.dialect.name  # type: ignore[attr-defined]


def upgrade() -> None:
    """Switch audit_log.created_at server default to clock_timestamp()."""
    dialect = _dialect()

    if dialect == "postgresql":
        op.alter_column(
            "audit_log",
            "created_at",
            server_default=sa.text(_PG_DEFAULT),
            existing_type=sa.DateTime(timezone=True),
            existing_nullable=False,
        )
    elif dialect == "sqlite":
        # SQLite ALTER COLUMN is restricted; use batch_alter_table.
        with op.batch_alter_table("audit_log") as batch_op:
            batch_op.alter_column(
                "created_at",
                server_default=sa.text(_SQLITE_DEFAULT),
                existing_type=sa.DateTime(timezone=True),
                existing_nullable=False,
            )
    else:
        # Generic fallback — CURRENT_TIMESTAMP is standard SQL.
        op.alter_column(
            "audit_log",
            "created_at",
            server_default=sa.text(_FALLBACK_DEFAULT),
            existing_type=sa.DateTime(timezone=True),
            existing_nullable=False,
        )


def downgrade() -> None:
    """Restore audit_log.created_at server default to now() / CURRENT_TIMESTAMP."""
    dialect = _dialect()

    if dialect == "postgresql":
        op.alter_column(
            "audit_log",
            "created_at",
            server_default=sa.text("now()"),
            existing_type=sa.DateTime(timezone=True),
            existing_nullable=False,
        )
    elif dialect == "sqlite":
        with op.batch_alter_table("audit_log") as batch_op:
            batch_op.alter_column(
                "created_at",
                server_default=sa.text(_FALLBACK_DEFAULT),
                existing_type=sa.DateTime(timezone=True),
                existing_nullable=False,
            )
    else:
        op.alter_column(
            "audit_log",
            "created_at",
            server_default=sa.text(_FALLBACK_DEFAULT),
            existing_type=sa.DateTime(timezone=True),
            existing_nullable=False,
        )
