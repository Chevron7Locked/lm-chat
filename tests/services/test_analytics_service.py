# SPDX-License-Identifier: Apache-2.0
"""Unit tests for AnalyticsService.

Pattern: real SQLite in-memory DB via metadata.create_all; no mocks at the
service layer.

Covers:
- user_stats happy path (counts, top_models).
- user_stats with no data (empty user).
- user_stats with audit_log gap (messages but no audit_log rows).
- system_stats happy path.
- system_stats with zero data.
"""
from __future__ import annotations

import time
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import chats, messages, metadata, users
from lmchat.services.analytics_service import AnalyticsService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def db_engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    """Per-test SQLite engine with full schema."""
    db_path = tmp_path / "test_analytics.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}", pool_pre_ping=True)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
        # Ensure display_order column exists (P8c migration — applied via schema).
        # For tests we create_all from the live schema, so no migration needed.
    yield eng
    await eng.dispose()


async def _insert_user(engine: AsyncEngine, username: str) -> int:
    """Insert a user row and return its id."""
    async with engine.begin() as conn:
        result = await conn.execute(
            insert(users).values(username=username, password_hash="x")
        )
        pk = result.inserted_primary_key
        assert pk is not None
        return int(pk[0])


async def _insert_chat(engine: AsyncEngine, user_id: int, title: str = "c") -> int:
    """Insert a chat row and return its id."""
    async with engine.begin() as conn:
        result = await conn.execute(
            insert(chats).values(user_id=user_id, title=title, settings={})
        )
        pk = result.inserted_primary_key
        assert pk is not None
        return int(pk[0])


async def _insert_message(
    engine: AsyncEngine,
    chat_id: int,
    role: str = "assistant",
    model_id: str | None = "m1",
    created_at: datetime | None = None,
) -> None:
    """Insert a message row."""
    ts = created_at or datetime.now(UTC)
    async with engine.begin() as conn:
        await conn.execute(
            insert(messages).values(
                chat_id=chat_id,
                role=role,
                content="hello",
                state="final",
                model_id=model_id,
                created_at=ts,
            )
        )


# ---------------------------------------------------------------------------
# user_stats tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_stats_empty(db_engine: AsyncEngine) -> None:
    """user_stats returns zero counts for a user with no chats/messages."""
    uid = await _insert_user(db_engine, "alice")
    svc = AnalyticsService(engine=db_engine)
    result = await svc.user_stats(uid)

    assert result.total_messages == 0
    assert result.total_chats == 0
    assert result.messages_last_7_days == 0
    assert result.top_models == []


@pytest.mark.asyncio
async def test_user_stats_happy_path(db_engine: AsyncEngine) -> None:
    """user_stats counts messages, chats, and surfaces top models."""
    uid = await _insert_user(db_engine, "bob")
    cid1 = await _insert_chat(db_engine, uid, "chat1")
    cid2 = await _insert_chat(db_engine, uid, "chat2")

    now = datetime.now(UTC)
    # 3 assistant messages with model "m1"
    for _ in range(3):
        await _insert_message(db_engine, cid1, role="assistant", model_id="m1", created_at=now)
    # 2 assistant messages with model "m2"
    for _ in range(2):
        await _insert_message(db_engine, cid2, role="assistant", model_id="m2", created_at=now)
    # 1 user message (should count towards total_messages but not top_models)
    await _insert_message(db_engine, cid1, role="user", model_id=None, created_at=now)

    svc = AnalyticsService(engine=db_engine)
    result = await svc.user_stats(uid)

    assert result.total_chats == 2
    assert result.total_messages == 6
    assert result.messages_last_7_days == 6
    assert len(result.top_models) == 2
    assert result.top_models[0].model_id == "m1"
    assert result.top_models[0].count == 3
    assert result.top_models[1].model_id == "m2"
    assert result.top_models[1].count == 2


@pytest.mark.asyncio
async def test_user_stats_last_7_days_gap(db_engine: AsyncEngine) -> None:
    """messages_last_7_days only counts messages within the 7-day window."""
    uid = await _insert_user(db_engine, "carol")
    cid = await _insert_chat(db_engine, uid)

    now = datetime.now(UTC)
    old = now - timedelta(days=10)

    # 2 recent messages + 1 old message
    await _insert_message(db_engine, cid, created_at=now)
    await _insert_message(db_engine, cid, created_at=now)
    await _insert_message(db_engine, cid, created_at=old)

    svc = AnalyticsService(engine=db_engine)
    result = await svc.user_stats(uid)

    assert result.total_messages == 3
    assert result.messages_last_7_days == 2


@pytest.mark.asyncio
async def test_user_stats_cross_user_isolation(db_engine: AsyncEngine) -> None:
    """user_stats is scoped to the requesting user only."""
    uid_a = await _insert_user(db_engine, "dave")
    uid_b = await _insert_user(db_engine, "eve")
    cid_a = await _insert_chat(db_engine, uid_a)
    cid_b = await _insert_chat(db_engine, uid_b)

    await _insert_message(db_engine, cid_a)
    await _insert_message(db_engine, cid_a)
    await _insert_message(db_engine, cid_b)

    svc = AnalyticsService(engine=db_engine)
    result_a = await svc.user_stats(uid_a)
    result_b = await svc.user_stats(uid_b)

    assert result_a.total_messages == 2
    assert result_b.total_messages == 1
    assert result_a.total_chats == 1
    assert result_b.total_chats == 1


# ---------------------------------------------------------------------------
# system_stats tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_system_stats_empty(db_engine: AsyncEngine) -> None:
    """system_stats returns zeros on a fresh DB."""
    svc = AnalyticsService(engine=db_engine)
    result = await svc.system_stats()

    assert result.total_users == 0
    assert result.total_chats == 0
    assert result.total_messages == 0
    assert result.messages_last_7_days == 0
    assert result.top_models == []


@pytest.mark.asyncio
async def test_system_stats_happy_path(db_engine: AsyncEngine) -> None:
    """system_stats aggregates across all users."""
    uid1 = await _insert_user(db_engine, "frank")
    uid2 = await _insert_user(db_engine, "grace")
    cid1 = await _insert_chat(db_engine, uid1)
    cid2 = await _insert_chat(db_engine, uid2)

    now = datetime.now(UTC)
    await _insert_message(db_engine, cid1, model_id="gpt-1", created_at=now)
    await _insert_message(db_engine, cid2, model_id="gpt-1", created_at=now)
    await _insert_message(db_engine, cid2, model_id="gpt-2", created_at=now)

    svc = AnalyticsService(engine=db_engine)
    result = await svc.system_stats()

    assert result.total_users == 2
    assert result.total_chats == 2
    assert result.total_messages == 3
    assert result.messages_last_7_days == 3
    assert len(result.top_models) >= 1
    assert result.top_models[0].model_id == "gpt-1"
    assert result.top_models[0].count == 2


# ---------------------------------------------------------------------------
# messages_by_day zero-fill anchor (UTC vs host-local "today")
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="time.tzset() is POSIX-only")
@pytest.mark.asyncio
@pytest.mark.parametrize("tz_name", ["Etc/GMT-12", "Etc/GMT+12"])
async def test_messages_by_day_zero_fill_uses_utc_today(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tz_name: str
) -> None:
    """The zero-fill 'today' bucket must be UTC, not host-local.

    messages.created_at buckets are grouped by UTC calendar day
    (func.date() on a UTC-stamped column, per the docstring). If the
    zero-fill series anchor is a host-LOCAL date.today(), a host whose
    local date differs from the UTC date either drops today's real
    activity off the end of the series or shows a phantom empty day.
    ``Etc/GMT-12`` / ``Etc/GMT+12`` are exact opposite offsets whose
    "local date == UTC date" windows are complementary — together they
    cover every hour of the day, so at least one of the two
    parametrized runs always exercises the divergence regardless of
    when this test executes.
    """
    monkeypatch.setenv("TZ", tz_name)
    time.tzset()
    try:
        uid = await _insert_user(db_engine, f"tz_{tz_name.replace('/', '_')}")
        cid = await _insert_chat(db_engine, uid)
        now_utc = datetime.now(UTC)
        await _insert_message(db_engine, cid, created_at=now_utc)

        svc = AnalyticsService(engine=db_engine)
        result = await svc.user_stats(uid)

        assert result.messages_by_day[-1].day == now_utc.date().isoformat()
        assert result.messages_by_day[-1].count == 1
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time.tzset()
