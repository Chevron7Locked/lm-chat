# SPDX-License-Identifier: Apache-2.0
"""Tests for lmchat.db.retry — write-retry with exponential backoff.

Tests for DB retry logic.

Covers:
- Happy path: fn succeeds on first attempt.
- Retry path: fn fails once with "database is locked", succeeds on 2nd.
- Exhausted path: fn always raises lock error → reraises after N attempts.
- Non-lock OperationalError: reraises immediately, no retry.
- Non-OperationalError: reraises immediately, no retry.
"""
from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError

from lmchat.db.retry import with_write_retry


def _lock_error() -> OperationalError:
    """Build a realistic SQLite 'database is locked' OperationalError."""
    return OperationalError(
        "statement",
        {},
        Exception("(sqlite3.OperationalError) database is locked"),
    )


def _other_op_error() -> OperationalError:
    """Build an OperationalError that does NOT contain the lock sentinel."""
    return OperationalError(
        "statement",
        {},
        Exception("(sqlite3.OperationalError) no such table: foo"),
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_on_first_attempt():
    """fn that succeeds immediately returns its value without any retry."""
    calls: list[int] = []

    async def fn() -> str:
        calls.append(1)
        return "ok"

    result = await with_write_retry(fn, attempts=3)
    assert result == "ok"
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Retry path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_succeeds_on_second_attempt(monkeypatch):
    """fn that fails once with a lock error should succeed on 2nd attempt."""
    import asyncio
    monkeypatch.setattr(asyncio, "sleep", lambda _: _async_noop())

    calls: list[int] = []

    async def fn() -> str:
        calls.append(1)
        if len(calls) == 1:
            raise _lock_error()
        return "recovered"

    result = await with_write_retry(fn, attempts=3, base_delay=0.0)
    assert result == "recovered"
    assert len(calls) == 2


async def _async_noop() -> None:
    """No-op coroutine for patching asyncio.sleep."""
    return None


# ---------------------------------------------------------------------------
# Exhausted path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exhausted_raises_after_all_attempts(monkeypatch):
    """fn that always raises lock error reraises on the final attempt."""
    import asyncio
    monkeypatch.setattr(asyncio, "sleep", lambda _: _async_noop())

    calls: list[int] = []

    async def fn() -> str:
        calls.append(1)
        raise _lock_error()

    with pytest.raises(OperationalError) as exc_info:
        await with_write_retry(fn, attempts=3, base_delay=0.0)

    assert "database is locked" in str(exc_info.value)
    assert len(calls) == 3


# ---------------------------------------------------------------------------
# Non-lock OperationalError — immediate reraise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_lock_operational_error_reraises_immediately():
    """OperationalError without lock sentinel must reraise without retry."""
    calls: list[int] = []

    async def fn() -> str:
        calls.append(1)
        raise _other_op_error()

    with pytest.raises(OperationalError) as exc_info:
        await with_write_retry(fn, attempts=3, base_delay=0.0)

    assert "no such table" in str(exc_info.value)
    assert len(calls) == 1  # called exactly once — no retry


# ---------------------------------------------------------------------------
# Non-OperationalError — immediate reraise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_operational_error_reraises_immediately():
    """A ValueError (or any non-OperationalError) must reraise without retry."""
    calls: list[int] = []

    async def fn() -> str:
        calls.append(1)
        raise ValueError("oops")

    with pytest.raises(ValueError, match="oops"):
        await with_write_retry(fn, attempts=3, base_delay=0.0)

    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Adversarial: attempts < 1
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_attempts_raises_value_error():
    """attempts=0 must raise ValueError, not silently succeed."""

    async def fn() -> str:
        return "never"

    with pytest.raises(ValueError, match="attempts must be >= 1"):
        await with_write_retry(fn, attempts=0)
