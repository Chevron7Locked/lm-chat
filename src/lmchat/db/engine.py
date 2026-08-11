# SPDX-License-Identifier: Apache-2.0
"""AsyncEngine factory for lm-chat.

Public surface:
    ``get_engine() -> AsyncEngine``  — returns the cached engine; creates
        it on first call.  Thread-safe in CPython via module-level lock.
    ``dispose_engine() -> None``  — disposes the engine and clears the
        cache.  Called from the FastAPI lifespan shutdown hook and from
        tests that need isolation.

Engine configuration:
- ``pool_size=20, max_overflow=10``: sized for the 50-concurrent-stream
  target plus headroom.
- ``pool_pre_ping=True``: detects dropped Postgres connections after long
  idle periods.  No-op overhead for SQLite.
- ``echo``: disabled by default; enabled by ``LM_CHAT_SQL_ECHO=true``.

Dialect gates:
- SQLite (``sqlite+aiosqlite://``): attaches a ``connect`` event hook
  that calls ``apply_sqlite_pragmas``.  The hook fires on the sync DBAPI
  layer, which is where ``PRAGMA`` execution must happen.
- Postgres (``postgresql+asyncpg://``): no PRAGMA hook;
  ``statement_timeout`` and other Postgres-side tuning are applied elsewhere.
"""
from __future__ import annotations

import os
import threading
from typing import Final

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.config import get_settings
from lmchat.db.pragmas import apply_sqlite_pragmas
from lmchat.logging import get_logger

log = get_logger(__name__)

_POOL_SIZE: Final[int] = 20
_MAX_OVERFLOW: Final[int] = 10

_engine: AsyncEngine | None = None
_engine_lock: threading.Lock = threading.Lock()


def _build_engine(database_url: str) -> AsyncEngine:
    """Construct and configure a new ``AsyncEngine`` for *database_url*.

    Not part of the public API — call :func:`get_engine` instead.

    SQLite uses a ``StaticPool`` (single-connection in-process) and does
    not accept ``pool_size`` or ``max_overflow``.  Those kwargs are only
    passed for Postgres, where the QueuePool is used and the 20-connection
    pool targets the 50-concurrent-stream load.

    Args:
        database_url: SQLAlchemy async URL string.

    Returns:
        A configured :class:`~sqlalchemy.ext.asyncio.AsyncEngine`.
    """
    echo_raw = os.environ.get("LM_CHAT_SQL_ECHO", "false").lower()
    echo = echo_raw in ("1", "true", "yes")

    is_sqlite = database_url.startswith("sqlite")

    if is_sqlite:
        # SQLite uses StaticPool; pool_size / max_overflow are not accepted.
        engine = create_async_engine(
            database_url,
            pool_pre_ping=True,
            echo=echo,
        )
    else:
        engine = create_async_engine(
            database_url,
            pool_size=_POOL_SIZE,
            max_overflow=_MAX_OVERFLOW,
            pool_pre_ping=True,
            echo=echo,
        )

    if is_sqlite:
        # Attach PRAGMA hook on the underlying sync engine.
        @event.listens_for(engine.sync_engine, "connect")
        def _on_connect(dbapi_conn: object, _connection_record: object) -> None:
            """Apply PRAGMAs immediately after each new DBAPI connection."""
            apply_sqlite_pragmas(dbapi_conn)

        log.info(
            "sqlite engine created",
            database_url=database_url,
            echo=echo,
        )
    else:
        # Postgres: statement_timeout and connection parameters are wired elsewhere.
        log.info(
            "postgres engine created",
            database_url=database_url,
            echo=echo,
            pool_size=_POOL_SIZE,
            max_overflow=_MAX_OVERFLOW,
        )

    return engine


def get_engine() -> AsyncEngine:
    """Return the module-level cached ``AsyncEngine``.

    Creates the engine on first call using ``settings.database_url``.
    Subsequent calls return the same instance — the engine's connection
    pool is long-lived across the FastAPI application lifespan.

    Returns:
        The singleton :class:`~sqlalchemy.ext.asyncio.AsyncEngine`.
    """
    global _engine
    if _engine is not None:
        return _engine
    with _engine_lock:
        # Double-check inside the lock to handle the race between two
        # threads that both observed _engine is None.
        if _engine is not None:
            return _engine
        settings = get_settings()
        _engine = _build_engine(settings.database_url)
    return _engine


def dispose_engine() -> None:
    """Dispose the cached engine and release all pooled connections.

    Called from test fixtures that require engine isolation between test
    cases (synchronous context).  For the FastAPI lifespan shutdown,
    prefer :func:`async_dispose_engine` to avoid ``MissingGreenlet``
    errors when SQLAlchemy's async pool teardown runs inside an event loop.

    If no engine has been created yet, this is a no-op.
    """
    global _engine
    with _engine_lock:
        if _engine is not None:
            # sync_engine.dispose() is safe from synchronous (non-async)
            # contexts — test teardown, signal handlers, etc.
            _engine.sync_engine.dispose()
            log.info("engine disposed (sync)")
            _engine = None


async def async_dispose_engine() -> None:
    """Async variant of :func:`dispose_engine` for use inside coroutines.

    Calling ``sync_engine.dispose()``
    from within an async lifespan context triggers SQLAlchemy's
    ``MissingGreenlet`` error because the pool's async teardown expects to
    run inside the event loop.  Using ``await engine.dispose()`` is the
    SQLAlchemy-recommended way to release an async engine from within an
    async context.

    If no engine has been created yet, this is a no-op.
    """
    global _engine
    with _engine_lock:
        engine_snapshot = _engine
        _engine = None  # Clear before await so re-entrant calls don't double-dispose.

    if engine_snapshot is not None:
        await engine_snapshot.dispose()
        log.info("engine disposed (async)")
