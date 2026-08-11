# SPDX-License-Identifier: Apache-2.0
"""One-time folder archival helper for migration 0023a.

Per-project folders were dropped. Migration 0023a is the
two-step migration's PART A: add ``projects.meta`` JSON, archive the
folders array, stop reading ``projects.folders``. Migration 0023b is
PART B and drops the ``projects.folders`` column in the same v1.0
release window.

**Dual-dialect archival**:

* SQLite — ``json_set(COALESCE(meta, '{}'), '$.folders', json(:folders))``
  writes the ``meta.folders`` key in place.
* Postgres — ``jsonb_set(COALESCE(meta::jsonb, '{}'::jsonb),
  '{folders}', to_jsonb(:folders::json))`` writes the equivalent
  key on the ``jsonb`` column.

Both paths are idempotent: re-runs OVERWRITE ``meta.folders`` with
the CURRENT folders value. Batched by ``batch_size`` (default 100)
as a mitigation for large project sets.
"""

from __future__ import annotations

import json as _json
from typing import Any

import sqlalchemy as sa


def _archive_folders_sync_op(
    bind: Any, *, batch_size: int = 100
) -> None:
    """Batch-archive ``projects.folders`` → ``projects.meta.folders``.

    Called from migration 0023a within its sync bind context.
    Dispatches on ``bind.dialect.name`` so SQLite and Postgres both
    work — see module docstring for the per-dialect SQL.

    Args:
        bind:       Alembic sync bind.
        batch_size: Rows per UPDATE batch (default 100).

    Raises:
        NotImplementedError: Dialect other than sqlite / postgresql.
    """
    dialect = bind.dialect.name
    if dialect == "sqlite":
        _archive_sqlite(bind, batch_size=batch_size)
    elif dialect in ("postgresql", "postgres"):
        _archive_postgres(bind, batch_size=batch_size)
    else:
        raise NotImplementedError(
            f"0023a folders archival has no implementation for "
            f"dialect={dialect!r}. Supported: sqlite, postgresql."
        )


def _archive_sqlite(bind: Any, *, batch_size: int) -> None:
    """SQLite branch — ``json_set`` writes ``meta.folders`` in place."""
    result = bind.execute(
        sa.text("SELECT id, folders FROM projects ORDER BY id")
    )
    rows = result.fetchall()
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        for row in batch:
            # SQLAlchemy 2.x decodes JSON columns on read — ``row.folders``
            # is a Python list (or None), NOT raw text. Re-encode before
            # binding (bind-a-list-to-:folders failure fix).
            folders_json = _json.dumps(row.folders or [])
            bind.execute(
                sa.text(
                    "UPDATE projects "
                    "SET meta = json_set("
                    "    COALESCE(meta, '{}'), '$.folders', json(:folders)"
                    ") WHERE id = :id"
                ),
                {"folders": folders_json, "id": row.id},
            )


def _archive_postgres(bind: Any, *, batch_size: int) -> None:
    """Postgres branch — ``jsonb_set`` writes ``meta.folders`` in place.

    Differences from SQLite:

    * ``jsonb_set`` takes a path array ``'{folders}'`` rather than a
      JSONPath string.
    * ``meta`` is cast to ``jsonb`` defensively — the column itself
      may be ``json`` or ``jsonb`` depending on how it was added; the
      cast normalizes.
    * The bound folders value is a JSON-encoded string fed through
      ``:folders::json`` then ``to_jsonb`` so the admin's nested
      list shape round-trips losslessly.
    """
    result = bind.execute(
        sa.text("SELECT id, folders FROM projects ORDER BY id")
    )
    rows = result.fetchall()
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        for row in batch:
            folders_json = _json.dumps(row.folders or [])
            bind.execute(
                sa.text(
                    "UPDATE projects "
                    "SET meta = jsonb_set("
                    "    COALESCE(meta::jsonb, '{}'::jsonb), "
                    "    '{folders}', "
                    "    to_jsonb(:folders::json)"
                    ") WHERE id = :id"
                ),
                {"folders": folders_json, "id": row.id},
            )
