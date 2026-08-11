# SPDX-License-Identifier: Apache-2.0
"""Abstract base types for lm-chat session storage.

``Session`` is a Pydantic v2 model for one ``sessions`` row; callers never
touch raw DB rows. ``SessionStore`` is an async ABC — ``SQLiteSessionStore``
is the shipped implementation. ``supports_distributed_revoke`` lets
``auth_service`` detect and warn about single-session enforcement in
multi-replica deployments without a distributed store.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from pydantic import BaseModel


class Session(BaseModel):
    """Immutable view of a session row returned from the store.

    All datetime fields are UTC-aware.  ``rotated_at`` is ``None`` for
    sessions that have not been superseded by a ``rotate()`` call.
    """

    id: str
    """Opaque 32-byte url-safe token used as the session cookie value."""

    user_id: int
    """Foreign key into the ``users`` table."""

    expires_at: datetime
    """UTC wall-clock time after which this session is invalid."""

    created_at: datetime
    """UTC wall-clock time this session row was inserted."""

    rotated_at: datetime | None
    """UTC time this session was superseded by ``rotate()``, or ``None``.

    The old row is kept as a short audit trail until ``cleanup()`` removes it.
    """


class SessionStore(ABC):
    """Async pluggable session-storage abstraction.

    ``SQLiteSessionStore`` is the shipped single-replica
    implementation.  Callers depend only on this ABC; the concrete
    implementation is injected via FastAPI ``Depends()``.
    """

    @property
    @abstractmethod
    def supports_distributed_revoke(self) -> bool:
        """Return ``True`` if ``revoke()`` propagates to all replicas.

        SQLite returns ``False`` — revocation is local to the process.
        Redis returns ``True`` — it publishes a ``session:revoke:<user_id>``
        message that every replica subscribes to.

        ``auth_service`` reads this property and emits a structured-log
        warning when ``False`` and ``LM_CHAT_REPLICAS_DETECTED=true``.
        """

    @abstractmethod
    async def create(self, *, user_id: int, ttl_seconds: int) -> Session:
        """Insert a new session row and return the ``Session`` model.

        Args:
            user_id:     Owner of the session (FK into ``users.id``).
            ttl_seconds: Lifetime in seconds.  ``expires_at`` is set to
                         ``now() + ttl_seconds``.

        Returns:
            The freshly created :class:`Session`.
        """

    @abstractmethod
    async def get(self, session_id: str) -> Session | None:
        """Fetch a session by its token ID.

        Returns ``None`` when absent **or** expired — even if the row still
        exists (expired rows are logically deleted until ``cleanup()`` runs).

        Returns:
            The :class:`Session`, or ``None`` if absent or expired.
        """

    @abstractmethod
    async def rotate(self, session_id: str) -> Session:
        """Atomically replace *session_id* with a new session.

        Marks the old row ``rotated_at = now()`` (kept for audit trail) and
        inserts a new row with a fresh token and the same TTL, in one
        transaction — a crash mid-rotate leaves no partial state.

        Returns:
            The new :class:`Session` (with a new ``id``).

        Raises:
            KeyError: If *session_id* does not exist or has already expired.
        """

    @abstractmethod
    async def extend(self, session_id: str, *, ttl_seconds: int) -> Session:
        """Push ``expires_at`` forward by *ttl_seconds* from now.

        Used by ``auth_service`` for sliding-window TTL on active sessions.
        The SQLite impl updates the row in-place; the Redis impl resets
        the key TTL.

        Returns:
            The updated :class:`Session`.

        Raises:
            KeyError: If *session_id* does not exist or has already expired.
        """

    @abstractmethod
    async def revoke(self, user_id: int) -> None:
        """Delete every session row owned by *user_id*.

        Used by single-session enforcement (before creating a new session)
        and by ``change_password`` (invalidates all existing sessions).
        """

    @abstractmethod
    async def cleanup(self) -> int:
        """Delete expired sessions and return the count removed.

        Called periodically from a background task (every 5 minutes per
        the lifespan scheduler).  The SQLite impl also retains recently-
        rotated rows for one TTL cycle so the audit trail is not truncated
        immediately.

        Returns:
            Number of rows deleted.
        """

    @abstractmethod
    async def revoke_session(self, session_id: str) -> None:
        """Delete a single session row by its token ID.

        Used by ``auth_service.logout()`` to revoke exactly the session
        being ended, without touching the user's other sessions.
        Idempotent: deleting an already-absent row is a no-op.
        """

    @abstractmethod
    async def revoke_others(self, user_id: int, except_session_id: str) -> None:
        """Delete all LIVE sessions for *user_id* except *except_session_id*.

        Used by ``change_password()`` (invalidate other active sessions,
        keep the current one) and by ``login()`` (re-assert single-session).

        Implementations must leave already-rotated rows alone: a rotated
        token can't authenticate regardless (see ``rotate()``/``get()``), so
        deleting them here has no security benefit and would truncate the
        audit trail before ``cleanup()`` reaps it.

        Idempotent when there are no other live sessions.
        """
