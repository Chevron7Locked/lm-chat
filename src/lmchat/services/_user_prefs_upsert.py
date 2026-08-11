# SPDX-License-Identifier: Apache-2.0
"""Shared ``user_prefs`` row upsert primitive.

``user_prefs`` is a one-row-per-user table (``folders`` + ``preset_models``
+ ``updated_at``) written from two independent call sites —
``folder_service`` (owns the ``folders`` column) and
``preset_models_service`` (owns the ``preset_models`` column). Both used
to hand-roll their own "SELECT existence check → INSERT, falling back to
an UPDATE on ``IntegrityError``" sequence. That duplicated the logic
across two files and left the first-row-creation path race-prone: two
concurrent callers could both see "no row" from the SELECT and both
race into the INSERT, relying on catching ``IntegrityError`` afterwards
to paper over it.

This module provides one atomic primitive, :func:`user_prefs_upsert`,
built on a dialect-native ``INSERT ... ON CONFLICT (user_id) DO UPDATE`` —
the same pattern already used for the ``server_lm_studio_default``
singleton row in ``lm_studio_overrides_service`` / ``memory_service``.
The row is created-or-updated in a single statement per attempt, so the
race is closed by the database itself rather than by application-level
exception handling.

Each caller passes its own ``insert_extra`` (columns to set only when
creating a brand-new row) and ``update_values`` (columns to set when the
row already exists / on conflict). This preserves each service's exact
column set and default values — nothing is homogenized between the two
callers.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from lmchat.db.schema import user_prefs as user_prefs_table


async def user_prefs_upsert(
    conn: AsyncConnection,
    user_id: int,
    *,
    insert_extra: Mapping[str, Any],
    update_values: Mapping[str, Any],
) -> None:
    """Atomically create-or-update the ``user_prefs`` row for *user_id*.

    Uses a dialect-native ``INSERT ... ON CONFLICT DO UPDATE`` on
    Postgres and SQLite (the two dialects this app actually runs on —
    see ``lmchat.db.engine``), so the first-row-creation race is closed
    by a single statement rather than a SELECT-then-branch round trip.

    Args:
        conn: An open connection/transaction. The caller owns the
            surrounding ``engine.begin()`` and retry policy (see
            ``lmchat.db.retry.with_write_retry``) — this function does
            not open its own transaction or retry on its own.
        user_id: PK of the ``user_prefs`` row. Must not appear as a key
            in *insert_extra* or *update_values*.
        insert_extra: Columns (besides ``user_id`` / ``updated_at``) to
            set when the row does not yet exist. Passed through
            verbatim so each caller keeps its own INSERT defaults —
            e.g. ``folder_service`` sets only ``folders``;
            ``preset_models_service`` also sets ``folders=[]`` on
            insert. Must not contain ``"user_id"`` or ``"updated_at"``.
        update_values: Columns to set when the row already exists (the
            ON CONFLICT branch). Only these columns are touched — any
            column not listed here is left untouched, matching the
            original hand-rolled UPDATE statements. Must not contain
            ``"user_id"`` or ``"updated_at"``.
    """
    now = datetime.now(UTC)
    dialect_name = conn.dialect.name

    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as _upsert_insert
    elif dialect_name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as _upsert_insert
    else:  # pragma: no cover — generic fallback; no dialect-native upsert
        existing = (
            await conn.execute(
                select(user_prefs_table.c.user_id).where(
                    user_prefs_table.c.user_id == user_id
                )
            )
        ).first()
        if existing is None:
            await conn.execute(
                insert(user_prefs_table).values(
                    user_id=user_id, updated_at=now, **insert_extra
                )
            )
        else:
            await conn.execute(
                update(user_prefs_table)
                .where(user_prefs_table.c.user_id == user_id)
                .values(updated_at=now, **update_values)
            )
        return

    stmt = _upsert_insert(user_prefs_table).values(
        user_id=user_id, updated_at=now, **insert_extra
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["user_id"],
        set_={**update_values, "updated_at": now},
    )
    await conn.execute(stmt)
