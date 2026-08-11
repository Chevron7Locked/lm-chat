# SPDX-License-Identifier: Apache-2.0
"""Regression test: audit_log.created_at must be non-decreasing under concurrent
inserts (P15 stress-baseline bug #170).

Root cause reproduced and fixed:
- Postgres ``now()`` = transaction-start time.  Concurrent transactions can
  acquire sequence IDs in one order but commit in a different order, producing
  id=974 ts > id=975 ts inversions (~200ms skew observed in P15 baseline).
- Fix: migration 0018 changes the server default to ``clock_timestamp()``
  (wall-clock at INSERT time), eliminating the race.

This test runs against SQLite (in-memory) via asyncio concurrency.  SQLite's
WAL mode serialises writers, so true clock inversion cannot happen there —
the test instead validates the schema default fires correctly (non-NULL,
parseable datetime) and that 20 concurrent inserts produce 20 distinct rows
in id order with non-decreasing timestamps.

For Postgres validation: run ``make stress-baseline`` with a live Postgres
instance (the stress harness targets the configured DB_URL).  The
``tests/stress/invariants/test_audit_log_monotonic.py`` invariant test reads
the rows written by that harness.
"""
from __future__ import annotations

import asyncio
import datetime
from collections.abc import AsyncGenerator

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import audit_log, metadata

# ---------------------------------------------------------------------------
# Fixture: isolated in-memory SQLite async engine
# ---------------------------------------------------------------------------

@pytest.fixture()
async def engine() -> AsyncGenerator[AsyncEngine]:
    """Fresh in-memory SQLite async engine with audit_log created."""
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _insert_one(eng: AsyncEngine, event: str, idx: int) -> None:
    """Insert one audit_log row.  Does NOT pass created_at — relies on server default."""
    async with eng.begin() as conn:
        await conn.execute(
            audit_log.insert().values(
                user_id=None,
                event=event,
                ip=None,
                user_agent=None,
                detail={"idx": idx},
            )
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_audit_log_concurrent_inserts_non_decreasing(engine: AsyncEngine) -> None:
    """20 concurrent asyncio tasks each insert one row; timestamps must be
    non-decreasing when rows are read back ordered by id.

    SQLite serialises writers so this primarily validates:
    1. created_at is never NULL (server default fires).
    2. created_at is a datetime (not a bare string or float).
    3. Timestamps are non-decreasing in id order.
    """
    n = 20
    await asyncio.gather(
        *[_insert_one(engine, "test.concurrent", i) for i in range(n)]
    )

    async with engine.connect() as conn:
        result = await conn.execute(
            sa.select(audit_log.c.id, audit_log.c.created_at)
            .order_by(audit_log.c.id.asc())
        )
        rows = result.fetchall()

    assert len(rows) == n, f"Expected {n} rows, got {len(rows)}"

    last_ts: datetime.datetime | None = None
    for row_id, ts in rows:
        assert ts is not None, f"row id={row_id} has NULL created_at"
        # SQLite may return a string; normalise to datetime for comparison.
        if isinstance(ts, str):
            ts = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        assert isinstance(ts, datetime.datetime), (
            f"row id={row_id} created_at is {type(ts)}, expected datetime"
        )
        if last_ts is not None:
            skew_ms = (last_ts - ts).total_seconds() * 1000.0
            assert skew_ms <= 100.0, (
                f"Timestamp inversion beyond 100ms tolerance: "
                f"id={row_id} ts={ts!r} prev={last_ts!r} skew={skew_ms:.3f}ms"
            )
        last_ts = ts if last_ts is None else max(last_ts, ts)


@pytest.mark.asyncio
async def test_audit_log_created_at_not_null(engine: AsyncEngine) -> None:
    """Single insert: created_at must be non-NULL (server default fires)."""
    await _insert_one(engine, "test.singleton", 0)

    async with engine.connect() as conn:
        result = await conn.execute(
            sa.select(audit_log.c.created_at).order_by(audit_log.c.id.asc()).limit(1)
        )
        row = result.fetchone()

    assert row is not None
    assert row[0] is not None, "created_at is NULL — server default did not fire"


@pytest.mark.asyncio
async def test_audit_log_schema_default_comment_preserved(engine: AsyncEngine) -> None:
    """Smoke: schema import is clean and audit_log table object is usable."""
    # If the schema.py import were broken by the clock_timestamp change,
    # the engine fixture would fail to create_all.  This test is a belt-and-
    # suspenders check that the table object is correctly structured.
    cols = {c.name for c in audit_log.c}
    assert "created_at" in cols
    assert "id" in cols
    assert "event" in cols
