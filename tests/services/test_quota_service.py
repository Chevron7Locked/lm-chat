# SPDX-License-Identifier: Apache-2.0
"""Unit tests for quota_service.

Tests:
- happy-path token consumption
- happy-path request consumption
- over-quota token error
- over-quota request error
- day-boundary row creation (idempotent INSERT OR IGNORE)
- get_usage returns zeros when no row exists
- get_quota returns defaults when no row exists
- set_quota upsert
- get_all_quotas
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from lmchat.db.schema import metadata
from lmchat.services.quota_service import (
    OverQuotaError,
    consume_request,
    consume_tokens,
    get_all_quotas,
    get_quota,
    get_usage,
    set_quota,
)


@pytest.fixture()
async def engine():
    """Per-test in-memory SQLite engine with schema."""
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture()
async def user_id(engine) -> int:
    """Insert a minimal user row and return the id."""
    from sqlalchemy import text

    uid = 42
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, username, password_hash, is_admin) "
                "VALUES (:uid, 'testuser', 'x', 0)"
            ),
            {"uid": uid},
        )
    return uid


# ---------------------------------------------------------------------------
# get_usage — zero default
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_usage_no_row_returns_zeros(engine, user_id):
    """When no usage row exists, get_usage returns zeros."""
    usage = await get_usage(user_id, datetime.now(tz=UTC).date(), engine)
    assert usage.user_id == user_id
    assert usage.tokens_consumed == 0
    assert usage.requests_consumed == 0


# ---------------------------------------------------------------------------
# get_quota — defaults when no row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_quota_defaults_when_no_row(engine, user_id):
    """When no quotas row, get_quota returns system defaults."""
    q = await get_quota(user_id, engine)
    assert q.tokens_per_day == 100_000
    assert q.requests_per_day == 1_000


# ---------------------------------------------------------------------------
# set_quota upsert
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_quota_creates_row(engine, user_id):
    """set_quota inserts a new row."""
    q = await set_quota(user_id, 5_000, 100, engine)
    assert q.tokens_per_day == 5_000
    assert q.requests_per_day == 100
    # read back
    q2 = await get_quota(user_id, engine)
    assert q2.tokens_per_day == 5_000


@pytest.mark.asyncio
async def test_set_quota_updates_existing(engine, user_id):
    """set_quota updates an existing row."""
    await set_quota(user_id, 5_000, 100, engine)
    await set_quota(user_id, 10_000, 200, engine)
    q = await get_quota(user_id, engine)
    assert q.tokens_per_day == 10_000
    assert q.requests_per_day == 200


# ---------------------------------------------------------------------------
# consume_tokens — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consume_tokens_happy(engine, user_id):
    """Consuming tokens within budget succeeds."""
    await set_quota(user_id, 1_000, 50, engine)
    await consume_tokens(user_id, 100, engine)
    usage = await get_usage(user_id, datetime.now(tz=UTC).date(), engine)
    assert usage.tokens_consumed == 100


@pytest.mark.asyncio
async def test_consume_tokens_multiple_calls(engine, user_id):
    """Multiple consume_tokens calls accumulate correctly."""
    await set_quota(user_id, 1_000, 50, engine)
    await consume_tokens(user_id, 100, engine)
    await consume_tokens(user_id, 200, engine)
    usage = await get_usage(user_id, datetime.now(tz=UTC).date(), engine)
    assert usage.tokens_consumed == 300


@pytest.mark.asyncio
async def test_consume_tokens_zero_is_noop(engine, user_id):
    """consume_tokens(0) is a no-op."""
    await set_quota(user_id, 1_000, 50, engine)
    await consume_tokens(user_id, 0, engine)
    usage = await get_usage(user_id, datetime.now(tz=UTC).date(), engine)
    assert usage.tokens_consumed == 0


# ---------------------------------------------------------------------------
# consume_tokens — over-quota
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consume_tokens_over_quota_raises(engine, user_id):
    """Consuming more than the budget raises OverQuotaError."""
    await set_quota(user_id, 100, 50, engine)
    await consume_tokens(user_id, 99, engine)
    with pytest.raises(OverQuotaError) as exc_info:
        await consume_tokens(user_id, 10, engine)
    assert exc_info.value.kind == "tokens"
    assert exc_info.value.user_id == user_id


@pytest.mark.asyncio
async def test_consume_tokens_exactly_at_limit(engine, user_id):
    """Consuming exactly the budget amount succeeds."""
    await set_quota(user_id, 100, 50, engine)
    await consume_tokens(user_id, 100, engine)
    usage = await get_usage(user_id, datetime.now(tz=UTC).date(), engine)
    assert usage.tokens_consumed == 100


# ---------------------------------------------------------------------------
# consume_request — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consume_request_happy(engine, user_id):
    """Consuming a request within budget increments the counter."""
    await set_quota(user_id, 1_000, 10, engine)
    await consume_request(user_id, engine)
    usage = await get_usage(user_id, datetime.now(tz=UTC).date(), engine)
    assert usage.requests_consumed == 1


# ---------------------------------------------------------------------------
# consume_request — over-quota
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consume_request_over_quota_raises(engine, user_id):
    """Exceeding the request quota raises OverQuotaError."""
    await set_quota(user_id, 1_000, 2, engine)
    await consume_request(user_id, engine)
    await consume_request(user_id, engine)
    with pytest.raises(OverQuotaError) as exc_info:
        await consume_request(user_id, engine)
    assert exc_info.value.kind == "requests"


# ---------------------------------------------------------------------------
# Day-boundary row creation (idempotent)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_usage_row_is_idempotent(engine, user_id):
    """Calling consume_request twice on same day does not duplicate the row."""
    from lmchat.services.quota_service import _ensure_usage_row

    await set_quota(user_id, 1_000, 100, engine)
    await _ensure_usage_row(user_id, engine)
    await _ensure_usage_row(user_id, engine)  # should not raise
    usage = await get_usage(user_id, datetime.now(tz=UTC).date(), engine)
    # Still just one row — tokens and requests untouched.
    assert usage.tokens_consumed == 0


# ---------------------------------------------------------------------------
# get_all_quotas
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_all_quotas_empty(engine):
    """get_all_quotas returns empty list when no quota rows exist."""
    rows = await get_all_quotas(engine)
    assert rows == []


@pytest.mark.asyncio
async def test_get_all_quotas_multiple_users(engine):
    """get_all_quotas returns one entry per user with a quotas row."""
    from sqlalchemy import text

    # Insert two users
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, username, password_hash, is_admin) "
                "VALUES (1, 'alice', 'x', 0), (2, 'bob', 'x', 0)"
            )
        )

    await set_quota(1, 5_000, 100, engine)
    await set_quota(2, 10_000, 200, engine)

    rows = await get_all_quotas(engine)
    assert len(rows) == 2
    ids = {r.user_id for r in rows}
    assert ids == {1, 2}


# ---------------------------------------------------------------------------
# OverQuotaError defaults (no quotas row → uses defaults)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consume_tokens_default_limit(engine, user_id):
    """When no quotas row, default limit (100 000) is applied."""
    # Should succeed since 1 < 100 000
    await consume_tokens(user_id, 1, engine)
    usage = await get_usage(user_id, datetime.now(tz=UTC).date(), engine)
    assert usage.tokens_consumed == 1
