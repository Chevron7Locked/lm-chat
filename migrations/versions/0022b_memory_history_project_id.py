# SPDX-License-Identifier: Apache-2.0
"""Extend memory_insights_history.insights_before JSON tuple shape.

Revision ID: 0022b
Revises: 0022
Create Date: 2026-06-04

Problem
-------
PROJECTS-V1 additions Phase 9 (C2 substrate fix). Spec v3.1 makes
per-project refine/restore preserve ``project_id`` end-to-end. The
``insights_before`` column on ``memory_insights_history`` is a
freeform JSON list of tuples; the pre-Phase-9 shape captured
``{id, text, created_at}`` (per schema.py:914 docstring). Phase 9
extends that to ``{id, text, created_at, project_id}`` so a snapshot
written under refine carries enough information for restore to
re-insert each insight under its original project scope.

Fix
---
No DDL — JSON columns accept any list-of-dicts shape. The migration
file exists so the Alembic head advances numerically (admin's
``alembic heads`` reflects 0022b after Phase 9 ships); the docstring
records the contract change.

Backward-compat read
--------------------
``MemoryService.restore_from_history`` reads each entry with
``entry.get("project_id")``. Legacy snapshots written before Phase 9
don't carry the key and restore as un-projected (``project_id IS
NULL``) — explicit graceful degradation; no backfill is
required because legacy snapshots have NO source for what their
``project_id`` would have been (pre-Phase-9 refine was user-wide).

Reversibility
-------------
``downgrade()`` is also a no-op. Snapshots written under 0022b that
carry a ``project_id`` key are simply ignored on the downgraded
restore path (the ``project_id`` key is read with ``.get()`` which
returns ``None`` after downgrade). No data loss on downgrade.
"""

from __future__ import annotations

# Revision identifiers, used by Alembic.
revision: str = "0022b"
down_revision: str | None = "0022"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """No DDL — JSON tuple shape extension is documented in schema.py."""
    # PROJECTS-V1 additions Phase 9 (C2 substrate fix). The pre-Phase-9
    # tuple shape ``{id, text, created_at}`` is extended to add
    # ``project_id``. Legacy snapshots without the key read as
    # un-projected via ``MemoryService.restore_from_history``'s
    # ``entry.get("project_id")``.


def downgrade() -> None:
    """No DDL — snapshots that carry ``project_id`` continue to read
    cleanly on the downgraded path (the new field is ignored)."""
