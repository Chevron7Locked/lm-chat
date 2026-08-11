# SPDX-License-Identifier: Apache-2.0
"""Background maintenance tasks for lm-chat.

``run_daily_purge`` — asyncio loop that purges soft-deleted document rows
older than ``retention_days`` once per day (spawned from the lifespan).

No apscheduler: a plain ``asyncio.sleep`` loop is sufficient for a single
daily job. Cutoff is computed in Python (``now - retention_days``) and
passed as a bind parameter rather than DB-side date arithmetic, avoiding
the SQLite/Postgres difference and staying testable without mocking DB
functions.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Final

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from lmchat.db.schema import documents
from lmchat.logging import get_logger
from lmchat.services._active_streams import is_active

log = get_logger(__name__)

_DEFAULT_INTERVAL_SEC: Final[int] = 86400  # 24 hours
_DEFAULT_RETENTION_DAYS: Final[int] = 30


async def _purge_soft_deleted_documents(
    engine: AsyncEngine,
    retention_days: int,
) -> int:
    """Delete soft-deleted document rows older than *retention_days*.

    Args:
        engine:         Async SQLAlchemy engine.
        retention_days: Documents soft-deleted more than this many days ago
                        are permanently deleted.

    Returns:
        Number of rows deleted.
    """
    cutoff = datetime.now(tz=UTC) - timedelta(days=retention_days)
    async with engine.begin() as conn:
        result = await conn.execute(
            sa.delete(documents).where(
                documents.c.deleted_at.isnot(None),
                documents.c.deleted_at < cutoff,
            )
        )
        return result.rowcount


async def run_daily_purge(
    engine: AsyncEngine,
    *,
    interval_sec: int = _DEFAULT_INTERVAL_SEC,
    retention_days: int = _DEFAULT_RETENTION_DAYS,
) -> None:
    """Run the soft-deleted document purge loop forever.

    Waits ``interval_sec`` between runs; exits cleanly on
    ``asyncio.CancelledError`` (lifespan shutdown). Non-fatal errors are
    logged at WARNING and the loop continues.
    """
    log.info(
        "background_tasks.daily_purge_started",
        interval_sec=interval_sec,
        retention_days=retention_days,
    )
    while True:
        try:
            await asyncio.sleep(interval_sec)
            deleted = await _purge_soft_deleted_documents(engine, retention_days)
            if deleted > 0:
                log.info(
                    "background_tasks.documents_purged",
                    deleted_count=deleted,
                    retention_days=retention_days,
                )
            else:
                log.debug("background_tasks.daily_purge_nothing_to_purge")
        except asyncio.CancelledError:
            log.info("background_tasks.daily_purge_cancelled")
            return
        except Exception as exc:  # noqa: BLE001
            # Non-fatal — log and continue.
            log.warning(
                "background_tasks.daily_purge_error",
                error=str(exc),
                retention_days=retention_days,
            )



async def run_incognito_ttl_purge(
    engine: AsyncEngine,
    *,
    interval_sec: int,
) -> None:
    """Periodic incognito-chat TTL sweep.

    Wakes every ``interval_sec`` seconds and DELETEs every chat row where
    ``incognito=1 AND incognito_expires_at < now()`` (CASCADE wipes
    messages + embeddings). Writes one ``chat.deleted`` audit event per
    purged chat so the audit log reflects auto TTL purge the same as a
    manual delete.

    Mirrors ``run_daily_purge``: plain ``asyncio.sleep`` loop, no
    apscheduler; errors logged at WARNING, loop continues.
    """
    # Local import avoids a circular import (audit_service ↔ db.engine ↔
    # background_tasks) at module load time.
    from lmchat.db.schema import chats as chats_tbl
    from lmchat.services.audit_service import write_audit_event

    log.info(
        "background_tasks.incognito_ttl_purge_started",
        interval_sec=interval_sec,
    )
    while True:
        try:
            await asyncio.sleep(interval_sec)
            # Inline DELETE (not via ChatService) — this task only has
            # `engine` in scope and must keep working even before
            # ChatService is wired up.
            from time import time as _time

            now_ts = _time()
            async with engine.begin() as conn:
                # Select first for per-chat audit events; FK cascade
                # handles messages + embeddings.
                result = await conn.execute(
                    sa.select(chats_tbl.c.id, chats_tbl.c.user_id).where(
                        chats_tbl.c.incognito == 1,
                        chats_tbl.c.incognito_expires_at.isnot(None),
                        chats_tbl.c.incognito_expires_at < now_ts,
                    )
                )
                rows = [(int(r[0]), int(r[1])) for r in result.fetchall()]
                # Skip chats with an in-progress stream — deleting mid-turn
                # would kill a live conversation. Accepted TOCTOU (same as
                # the reaper's is_active() guard): a stream could call
                # mark_active() during the DELETE below after we've decided
                # it's safe. Not closed — worst case is an already-expired
                # chat losing its row mid-turn, never an in-TTL chat purged
                # early; next tick catches anything skipped.
                live_rows = [r for r in rows if not is_active(r[0])]
                skipped = len(rows) - len(live_rows)
                if skipped:
                    log.debug(
                        "background_tasks.incognito_ttl_skipped_active_streams",
                        skipped_count=skipped,
                    )
                rows = live_rows
                if rows:
                    await conn.execute(
                        sa.delete(chats_tbl).where(
                            chats_tbl.c.id.in_([r[0] for r in rows])
                        )
                    )

            if rows:
                # Emit one chat.deleted audit row per purge, AFTER the
                # DELETE commit (never reference a still-extant chat).
                # reason='incognito_ttl' lets these be filtered from
                # manual-delete analytics. Audit failures are non-fatal.
                for chat_id, user_id in rows:
                    try:
                        await write_audit_event(
                            user_id=user_id,
                            event="chat.deleted",
                            ip=None,
                            user_agent=None,
                            detail={
                                "chat_id": chat_id,
                                "reason": "incognito_ttl",
                            },
                            engine=engine,
                        )
                    except Exception as audit_exc:  # noqa: BLE001
                        log.warning(
                            "background_tasks.incognito_ttl_audit_failed",
                            chat_id=chat_id,
                            user_id=user_id,
                            error=str(audit_exc),
                        )
                log.info(
                    "background_tasks.incognito_chats_purged",
                    deleted_count=len(rows),
                )
            else:
                log.debug("background_tasks.incognito_ttl_nothing_to_purge")
        except asyncio.CancelledError:
            log.info("background_tasks.incognito_ttl_purge_cancelled")
            return
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "background_tasks.incognito_ttl_purge_error",
                error=str(exc),
            )
