# SPDX-License-Identifier: Apache-2.0
"""Add ``memory_insights.last_active_epoch`` for SQL-level recency ordering.

Revision ID: 0043
Revises: 0042
Create Date: 2026-08-08

Problem
-------
``memory_insights`` recency (used by the auto-memory cap eviction and the
``recall_insights`` candidate-pool scan) is "last_used if the row was ever
recalled, else created_at". ``last_used`` is a ``Float`` UNIX-epoch column
but ``created_at`` is a ``DateTime`` column — different SQL storage types.
On SQLite, ``COALESCE(last_used, created_at)`` is not merely inelegant, it
is WRONG: SQLite compares mixed storage classes by class first (NULL <
INTEGER/REAL < TEXT), so a REAL ``last_used`` value always sorts before a
TEXT ``created_at`` fallback regardless of which timestamp is actually more
recent. On Postgres, ``COALESCE`` over incompatible types does not execute
at all. Both ``MemoryService._evict_auto_over_cap`` and
``MemoryService.recall_insights`` therefore had to fetch the whole
per-user active-row set and sort it in PYTHON on every call
(``_memory_recency_epoch``, removed by this change).

Fix
---
Add ``last_active_epoch`` — a ``Float`` UNIX-epoch column that mirrors
``last_used`` once a row has been recalled, and otherwise holds
``created_at``'s own epoch. Both sides of a recency comparison are now
Float, so a plain SQL ``COALESCE(last_active_epoch, epoch(created_at))``
is safe on every dialect, and eviction/recall can use a SQL
``ORDER BY ... LIMIT`` instead of a full Python scan. See
``lmchat.services.memory_service._recency_order_expr`` for the query-side
half of this fix and ``MemoryService.save_auto_insight`` /
``MemoryService._touch_insights`` for the write-side half (the app sets
this column on every insert/recall going forward).

Backfill
--------
Existing rows get ``last_active_epoch = COALESCE(last_used, epoch(created_at))``
computed dialect-appropriately:

- SQLite: ``created_at`` (``DateTime(timezone=True)``) round-trips as a
  NAIVE UTC wall-clock string (aiosqlite/SQLAlchemy do not persist the
  ``+00:00`` offset — see ``lmchat.utils.clock.ensure_utc``'s docstring).
  Both ``julianday(created_at)`` and plain ``strftime('%s', created_at)``
  parse that stored text LITERALLY, applying NO timezone conversion, so
  either yields the correct UTC epoch — exactly the same "treat the naive
  value as UTC, not host-local" correction ``ensure_utc`` applies on the
  Python side. ``julianday`` is used here (not ``strftime('%s', ...)``)
  purely for sub-second precision: ``strftime('%s', ...)`` truncates to
  whole seconds, ``julianday`` does not. What WOULD reintroduce the
  host-timezone skew this migration exists to avoid is passing the
  ``'utc'`` (or ``'localtime'``) modifier to ``strftime`` — e.g.
  ``strftime('%s', created_at, 'utc')`` tells SQLite "this text is LOCAL
  time, convert it to UTC" — neither modifier is used here.
- Postgres: ``created_at`` is a real ``TIMESTAMPTZ``, so
  ``EXTRACT(EPOCH FROM created_at)`` is timezone-correct regardless of
  session timezone.

The column stays nullable (no schema-level default): a row's
``last_active_epoch`` legitimately depends on data present at insert time
(``last_used`` may or may not be set), so there is no single constant
default that would be correct. A NULL post-migration only occurs for a row
written by something other than ``MemoryService`` (e.g. a raw-SQL insert);
the query-side ``COALESCE`` fallback in ``_recency_order_expr`` covers that
case identically to how a never-recalled row was handled before this
migration.

Dialect support
---------------
Both SQLite and Postgres. Plain ``op.add_column`` for the nullable
addition (no batch mode needed — SQLite natively supports adding a
nullable column without a default via ``ALTER TABLE ADD COLUMN``,
matching the precedent set by migrations 0034 / 0040).

Reversibility
-------------
``downgrade()`` drops the column (batch mode, for SQLite <3.35
portability). Purely additive/derived data — nothing computed from
``last_active_epoch`` is not already reconstructable from ``last_used`` /
``created_at``, so dropping it loses no information.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0043"
down_revision: str = "0042"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add ``memory_insights.last_active_epoch`` and backfill existing rows."""
    bind = op.get_bind()
    dialect = bind.dialect.name  # type: ignore[attr-defined]

    op.add_column(
        "memory_insights",
        sa.Column("last_active_epoch", sa.Float(), nullable=True),
    )

    if dialect == "sqlite":
        # julianday() parses the stored (naive-but-actually-UTC) text
        # literally — no timezone conversion — so this yields the correct
        # UTC epoch. Plain strftime('%s', created_at) would be equally
        # UTC-correct; julianday is used instead purely for sub-second
        # precision (strftime('%s', ...) truncates to whole seconds).
        # Do NOT pass the 'utc'/'localtime' modifier to strftime here —
        # that tells SQLite the text IS local time and converts it,
        # which is what actually causes an 8h-class skew bug, not a
        # plain no-modifier call. 2440587.5 is the Julian day number at
        # the UNIX epoch (1970-01-01T00:00:00Z); 86400.0 converts days
        # to seconds.
        op.execute(
            "UPDATE memory_insights SET last_active_epoch = "
            "COALESCE(last_used, (julianday(created_at) - 2440587.5) * 86400.0) "
            "WHERE last_active_epoch IS NULL"
        )
    else:
        op.execute(
            "UPDATE memory_insights SET last_active_epoch = "
            "COALESCE(last_used, EXTRACT(EPOCH FROM created_at)) "
            "WHERE last_active_epoch IS NULL"
        )


def downgrade() -> None:
    """Drop ``memory_insights.last_active_epoch``.

    Uses batch mode for SQLite <3.35 portability. No data preserved —
    the column is fully derivable from ``last_used`` / ``created_at``.
    """
    with op.batch_alter_table("memory_insights") as batch_op:
        batch_op.drop_column("last_active_epoch")
