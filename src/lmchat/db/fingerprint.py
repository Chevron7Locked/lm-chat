# SPDX-License-Identifier: Apache-2.0
"""Schema fingerprinting for Alembic baseline stamp gating.

The fingerprint is a 32-character hex string (BLAKE2b, digest_size=16)
that is stable under column reordering in source but differs under any
of:
- column type change
- column server_default change
- column nullability or primary_key change
- FK target table or cascade action change
- UNIQUE constraint column set change
- CHECK constraint text change

Algorithm (canonical representation)
--------------------------------------
For each table, sorted by table name:
  For each column, sorted by column name:
    canonical_type — try ``column.type.python_type.__name__``; fall back
      to ``column.type.__class__.__name__``.  This is dialect-stable:
      String(192) → "str", BigInteger → "int", JSON → "dict".
    canonical_default — see _canonical_server_default().
    tuple: (name, canonical_type, canonical_default, nullable,
            primary_key, unique_in_table)
  For each constraint, sorted by (kind, repr):
    FK: ("fk", target_table_name, ondelete, onupdate)
    UNIQUE: ("uq", sorted_columns_tuple)
    CHECK: ("ck", sqltext)

All tuples are assembled into a list, serialised via
``json.dumps(sort_keys=True)``, then hashed with BLAKE2b.

The ``unique`` column attribute is set to True if the column carries a
``unique=True`` flag OR if it is the sole column in any UniqueConstraint
on the table.  This is extracted by inspecting both
``column.unique`` and the table's UniqueConstraint objects.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import MetaData, UniqueConstraint
from sqlalchemy.sql.elements import TextClause
from sqlalchemy.sql.schema import DefaultClause, ForeignKeyConstraint


def _canonical_type(col: Any) -> str:  # noqa: ANN401
    """Return the dialect-stable type name for *col*.

    Tries ``col.type.python_type.__name__`` first so that
    ``String(192)`` → ``"str"``, ``BigInteger`` → ``"int"``, and
    ``JSON`` → ``"dict"`` (dialect-stable across SQLite and Postgres).
    Falls back to the class name if ``python_type`` raises
    ``NotImplementedError`` (e.g. ``LargeBinary`` in some builds).

    Args:
        col: A SQLAlchemy :class:`~sqlalchemy.schema.Column` instance.

    Returns:
        A canonical string representing the column's type.
    """
    try:
        return col.type.python_type.__name__
    except NotImplementedError:
        return col.type.__class__.__name__


def _canonical_server_default(col: Any) -> str:  # noqa: ANN401
    """Return a canonical string for *col*'s server_default.

    Canonicalisation rules:
    - ``None`` → ``"None"``
    - ``DefaultClause`` wrapping a ``TextClause`` → text verbatim
      (e.g. ``"false"``, ``"'final'"``)
    - Other ``DefaultClause`` (e.g. wrapping ``func.now()``) →
      ``str(col.server_default.arg).lower().strip()``
    - Anything else → ``repr(col.server_default)``

    This canonicalises across SQLAlchemy versions that may wrap
    ``func.now()`` differently but produce the same lowercased string.

    Args:
        col: A SQLAlchemy :class:`~sqlalchemy.schema.Column` instance.

    Returns:
        A canonical string for the column's server_default.
    """
    sd = col.server_default
    if sd is None:
        return "None"
    if isinstance(sd, DefaultClause):
        if isinstance(sd.arg, TextClause):
            return str(sd.arg)
        return str(sd.arg).lower().strip()
    return repr(sd)


def _column_unique_in_table(col: Any, table: Any) -> bool:  # noqa: ANN401
    """Return True if *col* is declared unique on *table*.

    Checks both the column-level ``unique`` flag and whether *col* is
    the sole column of any ``UniqueConstraint`` on the table.

    Args:
        col: A SQLAlchemy :class:`~sqlalchemy.schema.Column` instance.
        table: The :class:`~sqlalchemy.schema.Table` that owns *col*.

    Returns:
        ``True`` if the column is effectively unique.
    """
    if col.unique:
        return True
    for constraint in table.constraints:
        if isinstance(constraint, UniqueConstraint):
            cols = list(constraint.columns)
            if len(cols) == 1 and cols[0].name == col.name:
                return True
    return False


def _table_repr(table: Any) -> tuple[str, list[Any], list[Any]]:  # noqa: ANN401
    """Build the canonical (name, columns, constraints) tuple for *table*.

    Args:
        table: A SQLAlchemy :class:`~sqlalchemy.schema.Table`.

    Returns:
        A 3-tuple of (table_name, sorted_column_tuples,
        sorted_constraint_tuples).
    """
    col_tuples: list[tuple[str, str, str, bool, bool, bool]] = []
    for col in sorted(table.columns, key=lambda c: c.name):
        col_tuples.append((
            col.name,
            _canonical_type(col),
            _canonical_server_default(col),
            bool(col.nullable),
            bool(col.primary_key),
            _column_unique_in_table(col, table),
        ))

    constraint_tuples: list[Any] = []
    for constraint in sorted(table.constraints, key=lambda c: repr(c)):
        if isinstance(constraint, ForeignKeyConstraint):
            for fk in constraint.elements:
                constraint_tuples.append((
                    "fk",
                    fk.column.table.name,
                    (constraint.ondelete or "").upper(),
                    (constraint.onupdate or "").upper(),
                ))
        elif isinstance(constraint, UniqueConstraint):
            cols = sorted(c.name for c in constraint.columns)
            if cols:  # skip degenerate empty constraints
                constraint_tuples.append(("uq", cols))
        else:
            # CheckConstraint and others — emit text if available.
            sqltext = getattr(constraint, "sqltext", None)
            if sqltext is not None:
                constraint_tuples.append(("ck", str(sqltext)))

    constraint_tuples.sort()

    return (table.name, col_tuples, constraint_tuples)


def schema_fingerprint(metadata: MetaData) -> str:
    """Compute a stable BLAKE2b fingerprint of *metadata*'s schema.

    The fingerprint is stable under column reordering in source
    (columns are sorted by name before hashing) and differs under any
    type, default, nullability, or constraint change.

    Used by the Alembic startup-stamp logic in ``app.py`` to gate the
    ``alembic stamp head`` call: if the live DB's schema matches the
    baseline fingerprint, stamp and proceed; if not, raise so the
    admin knows the schema has drifted.

    Args:
        metadata: A SQLAlchemy :class:`~sqlalchemy.schema.MetaData`
            instance whose :attr:`~sqlalchemy.schema.MetaData.tables`
            have been populated (via
            :meth:`~sqlalchemy.schema.MetaData.reflect` or
            :func:`~sqlalchemy.schema.Table` declarations).

    Returns:
        A 32-character lowercase hex string (BLAKE2b, digest_size=16).
    """
    table_reprs = [
        _table_repr(table)
        for table in sorted(metadata.sorted_tables, key=lambda t: t.name)
    ]
    serialised = json.dumps(table_reprs, sort_keys=True, default=str)
    digest = hashlib.blake2b(serialised.encode(), digest_size=16)
    return digest.hexdigest()
