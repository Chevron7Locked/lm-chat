# SPDX-License-Identifier: Apache-2.0
"""Shared UTC clock helpers.

The app is UTC-first end to end: writes stamp ``datetime.now(UTC)``, and
UTC-bucketed columns (quota usage, daily-activity chart buckets) key off
UTC calendar days.

SQLite does not round-trip the ``+00:00`` offset on
``DateTime(timezone=True)`` columns — a value written as
``datetime.now(UTC)`` comes back timezone-naive on read, even though the
stored value is UTC. A naive datetime fed to ``.timestamp()`` or compared
against ``date.today()`` is silently treated as host-local time. Use
these helpers instead of ``date.today()`` / ``datetime.now()``
(host-local) or calling ``.timestamp()`` on a possibly-naive value.
"""
from __future__ import annotations

from datetime import UTC, date, datetime


def utc_now() -> datetime:
    """Return the current UTC-aware datetime."""
    return datetime.now(UTC)


def utc_today() -> date:
    """Return today's calendar date in UTC.

    Use instead of ``date.today()`` (host-local) anywhere "today" is
    compared against or used to bucket UTC-stamped DB rows.
    """
    return datetime.now(UTC).date()


def ensure_utc(dt: datetime | None) -> datetime | None:
    """Attach ``UTC`` to a naive datetime read back from SQLite.

    SQLite doesn't round-trip the ``+00:00`` offset on
    ``DateTime(timezone=True)`` columns — values come back timezone-naive
    even though the stored value is UTC. Call this before any arithmetic
    or ``.timestamp()`` conversion on a DB-read value.

    Args:
        dt: A ``datetime`` (possibly naive) or ``None``.

    Returns:
        The same ``datetime`` with ``tzinfo=UTC`` attached if naive,
        unchanged if already tz-aware, or ``None`` if *dt* is ``None``.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt
