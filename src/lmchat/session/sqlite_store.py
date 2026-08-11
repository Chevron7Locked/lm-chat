# SPDX-License-Identifier: Apache-2.0
"""SQLite-backed session store for lm-chat.

Default store for single-process / single-replica deployments; uses the
``sessions`` table via SQLAlchemy Core and the shared ``AsyncEngine``.

Key implementation notes:

- **Expiry semantics.** ``get()`` treats a row as absent when
  ``expires_at < now()`` OR ``rotated_at`` is set — a rotated token is
  dead the instant rotation runs, even though the row lingers for
  ``cleanup()``'s audit trail.
- **Rotate atomicity.** ``rotate()`` inserts the new row and marks the old
  one ``rotated_at`` in a single transaction — no partial state on failure.
- **Write retry.** All writes go through ``with_write_retry`` to handle
  SQLite's ``SQLITE_BUSY`` under WAL contention.
- **No plaintext tokens in logs.** Always log via ``_token_prefix()``.
- **Timezone.** aiosqlite always returns naive datetimes regardless of the
  ``DateTime(timezone=True)`` column type; every value read passes through
  ``_ensure_utc()`` to re-attach UTC.
"""
from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from lmchat.db.engine import get_engine
from lmchat.db.retry import with_write_retry
from lmchat.db.schema import sessions
from lmchat.logging import get_logger
from lmchat.session.base import Session, SessionStore

log = get_logger(__name__)

# Character count for debug-safe token prefix shown in log messages.
_LOG_TOKEN_PREFIX_LEN: int = 8


def _now() -> datetime:
    """Return the current UTC-aware datetime."""
    return datetime.now(UTC)


def _ensure_utc(dt: datetime | None) -> datetime | None:
    """Re-attach UTC to a naive datetime returned by aiosqlite (see module
    docstring's Timezone note). No-op for ``None`` or already-aware values.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _token_prefix(token: str) -> str:
    """Return a truncated token prefix safe for structured-log output."""
    return token[:_LOG_TOKEN_PREFIX_LEN] + "..."


def _row_to_session(row: object) -> Session:
    """Convert a SQLAlchemy ``Row`` to a :class:`Session`, normalising
    naive datetimes to UTC-aware via ``_ensure_utc()``.
    """
    expires_at = _ensure_utc(row.expires_at)  # type: ignore[union-attr]
    created_at = _ensure_utc(row.created_at)  # type: ignore[union-attr]
    rotated_at = _ensure_utc(row.rotated_at)  # type: ignore[union-attr]
    if expires_at is None:
        raise RuntimeError("sessions.expires_at is NOT NULL but returned None from DB")
    if created_at is None:
        raise RuntimeError("sessions.created_at is NOT NULL but returned None from DB")
    return Session(
        id=row.id,  # type: ignore[union-attr]
        user_id=row.user_id,  # type: ignore[union-attr]
        expires_at=expires_at,
        created_at=created_at,
        rotated_at=rotated_at,
    )


class SQLiteSessionStore(SessionStore):
    """``SessionStore`` backed by the ``sessions`` SQLAlchemy Core table.

    All writes go through ``with_write_retry`` to handle SQLite WAL
    contention. Engine is injected at construction (``None`` → application
    singleton; tests pass a dedicated test engine).
    """

    def __init__(self, engine: AsyncEngine | None = None) -> None:
        self._engine: AsyncEngine = engine if engine is not None else get_engine()

    @property
    def supports_distributed_revoke(self) -> bool:
        """Return ``False`` — SQLite revocation is local to the process."""
        return False

    # ------------------------------------------------------------------
    # create
    # ------------------------------------------------------------------

    async def create(self, *, user_id: int, ttl_seconds: int) -> Session:
        """Insert a new session row and return the :class:`Session`.

        The token is 32 bytes url-safe (``secrets.token_urlsafe(32)``, 43
        chars); ``expires_at`` is ``now() + ttl_seconds``.
        """
        token = secrets.token_urlsafe(32)
        now = _now()
        expires_at = now + timedelta(seconds=ttl_seconds)

        async def _insert() -> None:
            async with self._engine.begin() as conn:
                await conn.execute(
                    insert(sessions).values(
                        id=token,
                        user_id=user_id,
                        created_at=now,
                        expires_at=expires_at,
                        rotated_at=None,
                    )
                )

        await with_write_retry(_insert)

        log.debug(
            "session created",
            token_prefix=_token_prefix(token),
            user_id=user_id,
            expires_at=expires_at.isoformat(),
        )

        return Session(
            id=token,
            user_id=user_id,
            expires_at=expires_at,
            created_at=now,
            rotated_at=None,
        )

    # ------------------------------------------------------------------
    # get
    # ------------------------------------------------------------------

    async def get(self, session_id: str) -> Session | None:
        """Return the session if it exists, has not expired, and has not
        been rotated (see inline comment below). Expired/rotated rows are
        treated as absent, not returned as a stale session.
        """
        now = _now()
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(sessions).where(sessions.c.id == session_id)
            )
            row = result.fetchone()

        if row is None:
            return None

        # A rotated row's token is dead the instant rotation ran, even
        # though it lingers as an audit trail until cleanup() reaps it —
        # treat it as absent so the old token cannot authenticate.
        if row.rotated_at is not None:
            return None

        # aiosqlite returns naive datetimes; normalise before comparing.
        row_expires_at = _ensure_utc(row.expires_at)
        if row_expires_at is None:
            raise RuntimeError("sessions.expires_at is NOT NULL but returned None from DB")
        if row_expires_at <= now:
            # Row exists but is logically expired — treat as absent.
            return None

        return _row_to_session(row)

    # ------------------------------------------------------------------
    # rotate
    # ------------------------------------------------------------------

    async def rotate(self, session_id: str) -> Session:
        """Atomically replace *session_id* with a new session.

        Reads the current (non-expired) session, then in one transaction:
        inserts a new row with a fresh token (same remaining TTL) and marks
        the old row ``rotated_at = now()``.

        Raises:
            KeyError: If *session_id* does not exist or has already expired.
        """
        now = _now()

        # Read outside the transaction so we can raise before writing.
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(sessions).where(sessions.c.id == session_id)
            )
            old_row = result.fetchone()

        old_expires_at = _ensure_utc(old_row.expires_at) if old_row is not None else None
        if old_row is None or old_expires_at is None or old_expires_at <= now:
            raise KeyError(f"session not found or expired: {_token_prefix(session_id)}")

        new_token = secrets.token_urlsafe(32)
        # Preserve the remaining TTL on the new session.
        remaining_ttl = old_expires_at - now
        new_expires_at = now + remaining_ttl

        async def _rotate() -> None:
            async with self._engine.begin() as conn:
                await conn.execute(
                    insert(sessions).values(
                        id=new_token,
                        user_id=old_row.user_id,
                        created_at=now,
                        expires_at=new_expires_at,
                        rotated_at=None,
                    )
                )
                await conn.execute(
                    update(sessions)
                    .where(sessions.c.id == session_id)
                    .values(rotated_at=now)
                )

        await with_write_retry(_rotate)

        log.debug(
            "session rotated",
            old_prefix=_token_prefix(session_id),
            new_prefix=_token_prefix(new_token),
            user_id=old_row.user_id,
        )

        return Session(
            id=new_token,
            user_id=old_row.user_id,
            expires_at=new_expires_at,
            created_at=now,
            rotated_at=None,
        )

    # ------------------------------------------------------------------
    # extend
    # ------------------------------------------------------------------

    async def extend(self, session_id: str, *, ttl_seconds: int) -> Session:
        """Push ``expires_at`` forward by *ttl_seconds* from now.

        Raises:
            KeyError: If *session_id* does not exist or has already expired.
        """
        existing = await self.get(session_id)
        if existing is None:
            raise KeyError(f"session not found or expired: {_token_prefix(session_id)}")

        now = _now()
        new_expires_at = now + timedelta(seconds=ttl_seconds)

        async def _extend() -> None:
            async with self._engine.begin() as conn:
                await conn.execute(
                    update(sessions)
                    .where(sessions.c.id == session_id)
                    .values(expires_at=new_expires_at)
                )

        await with_write_retry(_extend)

        return Session(
            id=existing.id,
            user_id=existing.user_id,
            expires_at=new_expires_at,
            created_at=existing.created_at,
            rotated_at=existing.rotated_at,
        )

    # ------------------------------------------------------------------
    # revoke
    # ------------------------------------------------------------------

    async def revoke(self, user_id: int) -> None:
        """Delete every session row owned by *user_id*."""
        async def _revoke() -> None:
            async with self._engine.begin() as conn:
                await conn.execute(
                    delete(sessions).where(sessions.c.user_id == user_id)
                )

        await with_write_retry(_revoke)

        log.debug("sessions revoked", user_id=user_id)

    # ------------------------------------------------------------------
    # revoke_session
    # ------------------------------------------------------------------

    async def revoke_session(self, session_id: str) -> None:
        """Delete a single session row by its token ID. Idempotent."""

        async def _revoke_one() -> None:
            async with self._engine.begin() as conn:
                await conn.execute(
                    delete(sessions).where(sessions.c.id == session_id)
                )

        await with_write_retry(_revoke_one)

        log.debug(
            "session revoked",
            token_prefix=_token_prefix(session_id),
        )

    # ------------------------------------------------------------------
    # revoke_others
    # ------------------------------------------------------------------

    async def revoke_others(self, user_id: int, except_session_id: str) -> None:
        """Delete all LIVE sessions for *user_id* except *except_session_id*.

        Used by ``change_password()`` (keep current session alive) and by
        ``login()`` (re-assert single-session).

        Only ``rotated_at IS NULL`` rows are deleted — already-rotated rows
        are dead tokens already (:meth:`get` rejects them) but linger as a
        short audit trail until :meth:`cleanup` reaps them; deleting them
        here would destroy that trail for no security benefit.
        """

        async def _revoke_others() -> None:
            async with self._engine.begin() as conn:
                await conn.execute(
                    delete(sessions).where(
                        sessions.c.user_id == user_id,
                        sessions.c.id != except_session_id,
                        sessions.c.rotated_at.is_(None),
                    )
                )

        await with_write_retry(_revoke_others)

        log.debug(
            "other sessions revoked",
            user_id=user_id,
            kept_prefix=_token_prefix(except_session_id),
        )

    # ------------------------------------------------------------------
    # cleanup
    # ------------------------------------------------------------------

    async def cleanup(self) -> int:
        """Delete every expired session row and return the count removed.

        Reaps once ``expires_at < now()`` regardless of ``rotated_at`` — a
        rotated row survives only as an audit trail until its own TTL
        expires. No separate reaper exists, so this is what stops
        ``sessions`` from leaking one dead row per ``rotate()``.
        """
        now = _now()
        deleted_count: int = 0

        async def _cleanup() -> None:
            nonlocal deleted_count
            async with self._engine.begin() as conn:
                result = await conn.execute(
                    delete(sessions).where(
                        sessions.c.expires_at < now,
                    )
                )
                deleted_count = result.rowcount

        await with_write_retry(_cleanup)

        log.info(
            "session cleanup completed",
            deleted_count=deleted_count,
        )

        return deleted_count
