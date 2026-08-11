# SPDX-License-Identifier: Apache-2.0
"""Project-scope filter predicates.

A single helper translates the (``project_id``, ``unscoped``) request
shape into a SQLAlchemy WHERE clause. Used by every list-style route
that has a ``project_id`` query param:

- ``GET /api/chats``
- ``GET /api/documents``
- ``GET /api/folders`` (4 endpoints)
- ``GET /api/search`` (same helper)

Predicate shape (uniform across all callers):

- ``project_id`` non-null → filter to that project
- ``project_id`` None AND ``unscoped=True`` → filter to ``project_id IS NULL``
  (legacy un-projected rows only — the admin explicitly asked for them)
- ``project_id`` None AND ``unscoped=False`` (default) → no filter
  (user-scoped union — the existing behavior, every row the user owns)

The route layer is responsible for combining the returned clause with
the user_id filter via ``.where(...)``; callers that pass ``None``
back from this helper skip adding any predicate.
"""
from __future__ import annotations

from sqlalchemy.sql import ColumnElement


def project_scope_clause(
    project_id_column: ColumnElement,
    *,
    project_id: int | None,
    unscoped: bool,
) -> ColumnElement | None:
    """Return a SQL predicate matching the (project_id, unscoped) request.

    Args:
        project_id_column: The ``project_id`` column on the target table
            (e.g. ``chats.c.project_id``).
        project_id: When non-None, filter to that exact project_id.
        unscoped: When True AND ``project_id`` is None, filter to
            ``project_id IS NULL``. When False AND ``project_id`` is
            None, return None (no filter — user-scoped union).

    Returns:
        A SQLAlchemy ColumnElement to AND into the caller's WHERE, or
        ``None`` meaning "no additional filter."
    """
    if project_id is not None:
        return project_id_column == project_id
    if unscoped:
        return project_id_column.is_(None)
    return None
