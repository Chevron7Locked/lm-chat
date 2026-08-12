# SPDX-License-Identifier: Apache-2.0
"""Streaming persist state machine for lm-chat.

The state machine is the atomic core of the at-least-once persistence
guarantee.  Every state transition is a single SQL UPDATE with a
``WHERE state = :from_state`` predicate.  The rowcount of that update
is the authoritative answer to "did I win the race?"  There is NO
SELECT-then-UPDATE pattern here — that would introduce a TOCTOU window
under concurrent access.

Public surface
--------------
- ``PersistState`` — str-enum whose ``.value`` matches the DB string
  (``"draft"``, ``"pending_finalization"``, ``"final"``,
  ``"aborted_by_client"``).
- ``transition()`` — atomic single-row state move; returns ``True`` if
  the caller won the race, ``False`` otherwise.
- ``safe_abort_draft()`` — convenience wrapper: draft → aborted_by_client.
- ``finalize_pending()`` — convenience wrapper: pending_finalization → final.

All three accept an optional ``table`` (default ``messages``) so the SAME
state machine drives ``sub_session_messages`` durability (durable
sub-sessions, migration 0045) — both tables share the identical ``id``/
``state`` column shape, so this is a genuine table swap, not a fork.
"""
from __future__ import annotations

from enum import StrEnum

from sqlalchemy import Table, update
from sqlalchemy.ext.asyncio import AsyncEngine

from lmchat.db.retry import with_write_retry
from lmchat.db.schema import messages
from lmchat.logging import get_logger

log = get_logger(__name__)


class PersistState(StrEnum):
    """Valid values for the ``messages.state`` column.

    Inheriting from ``str`` guarantees that ``PersistState.DRAFT == "draft"``
    so the enum value is directly substitutable in SQLAlchemy bind-params
    without an extra ``.value`` dereference.
    """

    DRAFT = "draft"
    PENDING_FINALIZATION = "pending_finalization"
    FINAL = "final"
    ABORTED_BY_CLIENT = "aborted_by_client"


async def transition(
    *,
    engine: AsyncEngine,
    message_id: int,
    from_state: PersistState,
    to_state: PersistState,
    table: Table = messages,
) -> bool:
    """Atomically move ``message_id`` from *from_state* to *to_state*.

    Issues a single ``UPDATE <table> SET state=:to WHERE id=:mid AND
    state=:from`` inside a write-retried transaction.  The caller wins
    the race if and only if ``rowcount == 1``; a ``rowcount`` of 0 means
    the row was already in a different state (another coroutine got there
    first, or the row does not exist).

    Args:
        engine:       The async SQLAlchemy engine to use.
        message_id:   Primary key of the row in *table*.
        from_state:   Expected current state; the WHERE predicate.
        to_state:     Desired new state; the SET value.
        table:        Target table; ``messages`` (default) for the main
            chat, ``sub_session_messages`` for durable sub-sessions. Both
            share the same ``id``/``state`` column shape.

    Returns:
        ``True`` if exactly one row was updated (caller won).
        ``False`` if zero rows were updated (race lost or row absent).

    Raises:
        sqlalchemy.exc.OperationalError: On unrecoverable DB errors after
            the write-retry budget is exhausted.
    """
    result_holder: list[int] = []

    async def _do_update() -> None:
        async with engine.begin() as conn:
            result = await conn.execute(
                update(table)
                .where(
                    table.c.id == message_id,
                    table.c.state == from_state.value,
                )
                .values(state=to_state.value)
            )
            result_holder.append(result.rowcount)

    await with_write_retry(_do_update)

    rowcount = result_holder[0] if result_holder else 0
    won = rowcount == 1

    log.info(
        "stream.state_transition",
        message_id=message_id,
        from_state=from_state.value,
        to_state=to_state.value,
        table=table.name,
        won=won,
    )
    return won


async def safe_abort_draft(
    *,
    engine: AsyncEngine,
    message_id: int,
    table: Table = messages,
) -> bool:
    """Attempt to move *message_id* from ``draft`` to ``aborted_by_client``.

    This is the canonical disconnect handler: if the upstream finished
    first and already moved the row to ``pending_finalization`` (or
    beyond), the function returns ``False`` and is a clean no-op — the
    caller must not treat this as an error.

    Args:
        engine:      The async SQLAlchemy engine to use.
        message_id:  Primary key of the row in *table*.
        table:       Target table; ``messages`` (default) or
            ``sub_session_messages``.

    Returns:
        ``True`` if the abort succeeded (row was in ``draft``).
        ``False`` if the row was already in a later state.
    """
    return await transition(
        engine=engine,
        message_id=message_id,
        from_state=PersistState.DRAFT,
        to_state=PersistState.ABORTED_BY_CLIENT,
        table=table,
    )


async def finalize_pending(
    *,
    engine: AsyncEngine,
    message_id: int,
    table: Table = messages,
) -> bool:
    """Attempt to move *message_id* from ``pending_finalization`` to ``final``.

    Idempotent by design: if the row is already ``final`` (another reaper
    cycle or the stream itself committed successfully), returns ``False``
    and is a clean no-op.

    Args:
        engine:      The async SQLAlchemy engine to use.
        message_id:  Primary key of the row in *table*.
        table:       Target table; ``messages`` (default) or
            ``sub_session_messages``.

    Returns:
        ``True`` if the finalization succeeded.
        ``False`` if the row was already ``final`` or absent.
    """
    return await transition(
        engine=engine,
        message_id=message_id,
        from_state=PersistState.PENDING_FINALIZATION,
        to_state=PersistState.FINAL,
        table=table,
    )
