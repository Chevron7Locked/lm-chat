# SPDX-License-Identifier: Apache-2.0
"""Background draft-reaper task for lm-chat streaming.

The reaper runs as a FastAPI lifespan background task (single process,
single replica).  On each tick it performs two
cleanup operations:

1. **Recover ``pending_finalization`` rows older than N seconds.**
   These rows exist when the upstream ``chat.end`` event arrived but the
   final DB commit either failed or the process was restarted before it
   could complete.  The reaper re-runs the atomic ``finalize_pending``
   helper, which is idempotent under concurrent execution: the
   ``WHERE state='pending_finalization'`` predicate means only one
   caller ever wins ``rowcount=1``.

2. **Delete ``draft`` rows older than 24 hours.**
   Draft rows whose stream never progressed (client disconnected before
   LM Studio emitted any ``message.end``, or the upstream timed out and
   was never reconnected) are garbage-collected.  An ``audit_log`` row
   with ``event="stream.draft_reaped"`` is written for each deleted row
   so admins can track lost streams.  Memory-service notification is
   NOT needed: draft rows were never indexed (``index_message`` is
   gated on FINAL state only).

Configurable via:
- ``LM_CHAT_REAPER_FINALIZATION_TIMEOUT_SEC`` (default 5) — age in
  seconds before a ``pending_finalization`` row is treated as stuck.
- ``LM_CHAT_REAPER_DRAFT_MAX_AGE_HOURS`` (default 24) — age in hours
  before a ``draft`` row is deleted.
- ``LM_CHAT_REAPER_INTERVAL_SEC`` (default 60) — seconds between ticks
  (also runtime-overridable via the ``interval_sec`` parameter).

Cancellation contract
---------------------
The outer ``asyncio.CancelledError`` is caught and causes ``run_reaper``
to return cleanly, flushing the current per-tick work before exiting.
Within a tick, individual ``finalize_pending`` failures are caught and
logged; a single row failure does not abort the remainder of the tick.
"""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.sql.functions import coalesce

from lmchat.db.retry import with_write_retry
from lmchat.db.schema import audit_log, messages
from lmchat.logging import get_logger
from lmchat.services._active_streams import is_active
from lmchat.services._stream_state import finalize_pending

log = get_logger(__name__)


async def _write_draft_reaped_audit(
    *,
    engine: AsyncEngine,
    message_id: int,
    draft_age_hours: float,
) -> None:
    """Insert one ``stream.draft_reaped`` audit-log row.

    Written outside ``write_audit_event`` to avoid a circular import
    (audit_service imports from engine; reaper imports audit_service).
    The call is wrapped in ``with_write_retry`` for SQLite WAL contention.

    Args:
        engine:          The async SQLAlchemy engine to use.
        message_id:      Primary key of the deleted message row.
        draft_age_hours: Age of the draft row in hours, for admin
                         diagnostics.
    """
    detail = {"message_id": message_id, "draft_age_hours": round(draft_age_hours, 2)}

    async def _insert() -> None:
        async with engine.begin() as conn:
            await conn.execute(
                audit_log.insert().values(
                    user_id=None,
                    event="stream.draft_reaped",
                    ip=None,
                    user_agent=None,
                    detail=json.dumps(detail),
                )
            )

    await with_write_retry(_insert)

    log.info(
        "stream.draft_reaped",
        message_id=message_id,
        draft_age_hours=round(draft_age_hours, 2),
    )


async def _recover_pending(
    *,
    engine: AsyncEngine,
    finalization_timeout_sec: int,
) -> None:
    """Find and finalize all stuck ``pending_finalization`` rows.

    A row is considered stuck if its ``created_at`` is older than
    *finalization_timeout_sec* seconds.  Each call to ``finalize_pending``
    is atomic; concurrent reaper instances (or a stream committing
    concurrently) race safely: only one wins ``rowcount=1``.

    Args:
        engine:                   The async SQLAlchemy engine to use.
        finalization_timeout_sec: Age threshold in seconds.
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=finalization_timeout_sec)

    async with engine.connect() as conn:
        result = await conn.execute(
            select(messages.c.id).where(
                messages.c.state == "pending_finalization",
                messages.c.created_at < cutoff,
            )
        )
        pending_ids: list[int] = [row[0] for row in result.fetchall()]

    for msg_id in pending_ids:
        try:
            won = await finalize_pending(engine=engine, message_id=msg_id)
            if won:
                log.info(
                    "reaper.finalize_recovered",
                    message_id=msg_id,
                )
            else:
                log.info(
                    "reaper.finalize_already_done",
                    message_id=msg_id,
                )
        except Exception as exc:
            log.error(
                "reaper.finalize_failed",
                message_id=msg_id,
                error=str(exc),
            )


async def _finalize_stuck_drafts(
    *,
    engine: AsyncEngine,
    stuck_after_minutes: int,
) -> None:
    """Transition ``draft`` rows stuck >``stuck_after_minutes`` to FINAL.

    The streaming service's normal error paths release drafts immediately
    (``_release_stuck_draft``), so this is the safety net for *abandoned*
    drafts (process crashed, client navigated away mid-stream, etc.) —
    without it the next chat-stream attempt on the same chat_id sees the
    stuck draft and is 409'd by :func:`_assert_no_in_progress_stream`.
    Confirmed 2026-06-07: stuck drafts blocked the next chat-stream attempt
    with a 409 despite no real stream in progress; the reaper's 24h budget
    was far too long for a live chat session.

    Transition (vs. the 24h delete in :func:`_reap_old_drafts`) is the
    chat-shaped move: the failed turn stays visible in history as an
    empty assistant message rather than vanishing. The 24h delete pass
    still GCs ancient drafts.

    Default 5 minutes is chosen so a slow streaming model (which can spend
    2+ min on one tool round) isn't reaped mid-turn, but a genuinely
    abandoned draft unblocks the chat within minutes, not 24h.

    Selects on ``COALESCE(last_activity_at, created_at)``
    instead of ``created_at`` alone.  Actively-streaming drafts bump
    ``last_activity_at`` on every ``_CoalesceTimer.flush()`` so they never
    age past the threshold; only truly abandoned drafts (no DB touch in
    >5 min) are reaped.  This eliminates the race where a long tool-chain
    (2+ min/round) was reaped mid-stream.
    """
    cutoff = datetime.now(UTC) - timedelta(minutes=stuck_after_minutes)
    # Use COALESCE(last_activity_at, created_at) so rows
    # without last_activity_at (pre-migration historical rows) fall back to
    # created_at, preserving the old behaviour for those rows.
    activity_col = coalesce(messages.c.last_activity_at, messages.c.created_at)
    async with engine.connect() as conn:
        result = await conn.execute(
            select(messages.c.id, messages.c.chat_id).where(
                messages.c.state == "draft",
                activity_col < cutoff,
            )
        )
        rows = result.fetchall()
    if not rows:
        return

    # Skip drafts whose chat_id has an in-process stream.
    # STREAMS_ACTIVE is a gauge (no membership query); _active_streams.py
    # provides the set-based companion registry.  A restart resets it to
    # empty, which is safe: after restart there are no active streams.
    live_rows = [r for r in rows if not is_active(r[1])]
    if live_rows:
        log.debug(
            "reaper.skipped_active_streams",
            skipped_count=len(rows) - len(live_rows),
            stuck_count=len(live_rows),
        )
    rows = live_rows
    if not rows:
        return

    stuck_ids = [r[0] for r in rows]

    async def _flip_batch() -> None:
        async with engine.begin() as conn:
            await conn.execute(
                sa_update(messages)
                .where(messages.c.id.in_(stuck_ids), messages.c.state == "draft")
                .values(state="final")
            )

    await with_write_retry(_flip_batch)
    for row in rows:
        log.warning(
            "reaper.stuck_draft_finalized",
            message_id=row[0],
            chat_id=row[1],
            stuck_after_minutes=stuck_after_minutes,
        )


async def _reap_old_drafts(
    *,
    engine: AsyncEngine,
    draft_max_age_hours: int,
) -> None:
    """Delete ``draft`` rows older than *draft_max_age_hours* and audit each.

    Uses ``DELETE ... RETURNING id`` so we can write one audit-log row per
    deleted message without a separate SELECT.  SQLite ≥ 3.35 and Postgres
    both support ``RETURNING``; the SQLAlchemy Core ``.returning()`` clause
    is backend-portable.

    Args:
        engine:              The async SQLAlchemy engine to use.
        draft_max_age_hours: Age threshold in hours.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=draft_max_age_hours)

    # We need both id and created_at to compute the age for the audit row.
    async with engine.connect() as conn:
        # First: collect the rows we want to delete (with created_at for age).
        select_result = await conn.execute(
            select(messages.c.id, messages.c.created_at).where(
                messages.c.state == "draft",
                messages.c.created_at < cutoff,
            )
        )
        stale_rows = select_result.fetchall()

    if not stale_rows:
        return

    stale_ids = [row[0] for row in stale_rows]

    async def _delete_batch() -> None:
        async with engine.begin() as conn:
            await conn.execute(
                delete(messages).where(messages.c.id.in_(stale_ids))
            )

    await with_write_retry(_delete_batch)

    now = datetime.now(UTC)
    for row in stale_rows:
        msg_id: int = row[0]
        created_at: datetime = row[1]
        # created_at may be naive (SQLite) or aware (Postgres); normalise.
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        age_hours = (now - created_at).total_seconds() / 3600.0

        try:
            await _write_draft_reaped_audit(
                engine=engine,
                message_id=msg_id,
                draft_age_hours=age_hours,
            )
        except Exception as exc:
            # Audit failure must not prevent continued reaping of other rows.
            log.error(
                "reaper.audit_write_failed",
                message_id=msg_id,
                error=str(exc),
            )


async def run_reaper(
    *,
    engine: AsyncEngine,
    interval_sec: int = 60,
    finalization_timeout_sec: int = 5,
    draft_stuck_after_minutes: int = 5,
    draft_max_age_hours: int = 24,
) -> None:
    """Run the draft-reaper loop until cancelled.

    Designed to be started as a FastAPI lifespan background task:

    .. code-block:: python

        async def lifespan(app):
            task = asyncio.create_task(
                run_reaper(engine=engine, settings=settings)
            )
            yield
            task.cancel()
            await task

    On each tick the reaper:

    1. Recovers ``pending_finalization`` rows older than
       *finalization_timeout_sec* seconds by calling ``finalize_pending``
       atomically for each.
    2. Deletes ``draft`` rows older than *draft_max_age_hours* hours and
       writes one ``stream.draft_reaped`` audit-log row per deleted row.

    ``asyncio.CancelledError`` is caught after the current tick completes
    (not mid-tick), so in-progress cleanup is not interrupted.

    Args:
        engine:                   The async SQLAlchemy engine to use for
                                  all DB operations.
        interval_sec:             Seconds to sleep between reaper ticks.
                                  Defaults to 60; override for tests.
        finalization_timeout_sec: Age in seconds before a
                                  ``pending_finalization`` row is treated
                                  as stuck and finalized by the reaper.
        draft_max_age_hours:      Age in hours before a ``draft`` row is
                                  deleted and audit-logged.
    """
    log.info(
        "reaper.started",
        interval_sec=interval_sec,
        finalization_timeout_sec=finalization_timeout_sec,
        draft_max_age_hours=draft_max_age_hours,
    )

    while True:
        try:
            await asyncio.sleep(interval_sec)
        except asyncio.CancelledError:
            log.info("reaper.cancelled_during_sleep")
            return

        try:
            await _recover_pending(
                engine=engine,
                finalization_timeout_sec=finalization_timeout_sec,
            )
            await _finalize_stuck_drafts(
                engine=engine,
                stuck_after_minutes=draft_stuck_after_minutes,
            )
            await _reap_old_drafts(
                engine=engine,
                draft_max_age_hours=draft_max_age_hours,
            )
        except asyncio.CancelledError:
            # CancelledError during tick work: log and exit cleanly.
            log.info("reaper.cancelled_during_tick")
            return
        except Exception as exc:
            # Per-tick failures are non-fatal; the reaper continues.
            log.error(
                "reaper.tick_failed",
                error=str(exc),
            )
