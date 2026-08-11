# SPDX-License-Identifier: Apache-2.0
"""Write-retry helper with exponential backoff for SQLite locking.

SQLite in WAL mode serialises writers; under high concurrency a write
can observe ``SQLITE_BUSY`` which SQLAlchemy surfaces as an
``OperationalError`` containing the text "database is locked".  This
helper retries such errors with exponential backoff.  Any other
exception type — including other ``OperationalError`` variants — is
re-raised immediately so callers see the real error.

The backoff schedule is fixed: 50 ms → 200 ms → 500 ms (with the
re-raise on the third failure attempt), giving at most ~750 ms of
total wait for a default 3-attempt call.  The ``busy_timeout`` PRAGMA
(5 s) is the first line of defence; ``with_write_retry`` is the
second layer for the rare case where even 5 s is not enough under
sustained concurrent load.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from sqlalchemy.exc import OperationalError

from lmchat.logging import get_logger

log = get_logger(__name__)

_BACKOFF_DELAYS: tuple[float, float, float] = (0.05, 0.20, 0.50)

_LOCKED_SENTINEL: str = "database is locked"

# Generic over the awaitable return type. PEP 695 `def fn[T](...)` syntax
# would be more compact but requires Python ≥3.12; the distroless deploy
# image ships Python 3.11. TypeVar keeps us portable across both.
T = TypeVar("T")


async def with_write_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 0.05,
) -> T:
    """Call *fn* up to *attempts* times, retrying on SQLite lock errors.

    The backoff schedule is ``base_delay * (4 ** attempt_index)``,
    which for the default ``base_delay=0.05`` and ``attempts=3``
    produces 50 ms, 200 ms — the final (third) attempt raises
    immediately if it also fails.

    Args:
        fn: A zero-argument async callable that performs the database
            write.  Called once per attempt.
        attempts: Maximum number of attempts.  Must be ≥ 1.
        base_delay: Delay in seconds before the second attempt.
            Subsequent delays scale as ``base_delay * 4 ** i``.

    Returns:
        The return value of *fn* on success.

    Raises:
        sqlalchemy.exc.OperationalError: If *fn* raises a lock error on
            every attempt, the final error is re-raised.
        Exception: Any non-lock exception is re-raised on the first
            occurrence without any retry.
    """
    if attempts < 1:
        raise ValueError(f"attempts must be >= 1, got {attempts}")

    last_exc: OperationalError | None = None
    for attempt in range(attempts):
        try:
            return await fn()
        except OperationalError as exc:
            if _LOCKED_SENTINEL not in str(exc):
                # Not a lock error — re-raise immediately, no retry.
                raise
            last_exc = exc
            if attempt < attempts - 1:
                delay = base_delay * (4**attempt)
                log.warning(
                    "sqlite write retry",
                    attempt=attempt + 1,
                    attempts=attempts,
                    delay_s=delay,
                    error=str(exc),
                )
                await asyncio.sleep(delay)
            else:
                log.error(
                    "sqlite write retry exhausted",
                    attempts=attempts,
                    error=str(exc),
                )

    # We only reach here if every attempt raised a lock error.
    # last_exc is always set when attempts >= 1 and every iteration raised.
    if last_exc is None:
        # Defensive: unreachable with valid attempts >= 1 and a raising fn.
        raise RuntimeError("with_write_retry: exhausted attempts but no exception captured")
    raise last_exc
