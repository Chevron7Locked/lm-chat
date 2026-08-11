# SPDX-License-Identifier: Apache-2.0
"""Analytics aggregation service for lm-chat.

Aggregates usage statistics from the ``audit_log`` and ``messages`` tables.
Two views: **user_stats** (scoped to a single user_id) and **system_stats**
(admin-only, across all users).

All queries use SQLAlchemy Core; stats are computed at query time with no
materialised counters. Adequate for the quantities involved (tens of
thousands of rows per user); a caching layer can be added later if needed.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from lmchat.db.schema import chats, messages, users
from lmchat.logging import get_logger
from lmchat.utils.clock import utc_today

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Public models
# ---------------------------------------------------------------------------


class TopModel(BaseModel):
    """One row in the top-models breakdown.

    Attributes:
        model_id:   LM Studio model identifier string.
        count:      Number of messages produced by this model.
    """

    model_config = ConfigDict(from_attributes=True)

    model_id: str
    count: int


class DayCount(BaseModel):
    """A single day's message count for the activity series."""

    model_config = ConfigDict(from_attributes=True)

    day: str   # ISO date, e.g. "2026-05-21"
    count: int


class UserAnalytics(BaseModel):
    """Per-user usage statistics.

    Attributes:
        total_messages:       Total messages authored by this user (all time).
        total_chats:          Total chats owned by this user.
        messages_last_7_days: Messages in the last 7 calendar days.
        top_models:           Top 5 models by message count (assistant only).
        messages_by_day:      Message counts for the last 14 days (zero-filled),
                              oldest first — drives the activity chart.
    """

    model_config = ConfigDict(from_attributes=True)

    total_messages: int
    total_chats: int
    messages_last_7_days: int
    top_models: list[TopModel]
    messages_by_day: list[DayCount] = []


class SystemAnalytics(BaseModel):
    """System-wide usage statistics (admin only).

    Attributes:
        total_users:    Total registered users.
        total_chats:    Total chats across all users.
        total_messages: Total messages across all users.
        messages_last_7_days: Messages in the last 7 calendar days.
        top_models:     Top 5 models by usage across all users.
    """

    model_config = ConfigDict(from_attributes=True)

    total_users: int
    total_chats: int
    total_messages: int
    messages_last_7_days: int
    top_models: list[TopModel]


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class AnalyticsService:
    """Aggregates usage statistics from the DB.

    Args:
        engine: Async SQLAlchemy engine.
    """

    def __init__(self, *, engine: AsyncEngine) -> None:
        self._engine = engine

    async def user_stats(self, user_id: int) -> UserAnalytics:
        """Return per-user analytics for *user_id*.

        Args:
            user_id: PK of the requesting user.

        Returns:
            :class:`UserAnalytics` with four metric fields.
        """
        cutoff = datetime.now(UTC) - timedelta(days=7)

        async with self._engine.connect() as conn:
            # User owns messages indirectly via chat.
            total_msg_row = (
                await conn.execute(
                    select(func.count())
                    .select_from(messages)
                    .join(chats, messages.c.chat_id == chats.c.id)
                    .where(chats.c.user_id == user_id)
                )
            ).scalar_one()
            total_messages: int = int(total_msg_row or 0)

            total_chats_row = (
                await conn.execute(
                    select(func.count())
                    .select_from(chats)
                    .where(chats.c.user_id == user_id)
                )
            ).scalar_one()
            total_chats: int = int(total_chats_row or 0)

            recent_row = (
                await conn.execute(
                    select(func.count())
                    .select_from(messages)
                    .join(chats, messages.c.chat_id == chats.c.id)
                    .where(
                        chats.c.user_id == user_id,
                        messages.c.created_at >= cutoff,
                    )
                )
            ).scalar_one()
            messages_last_7: int = int(recent_row or 0)

            top_rows = (
                await conn.execute(
                    select(messages.c.model_id, func.count().label("cnt"))
                    .select_from(messages)
                    .join(chats, messages.c.chat_id == chats.c.id)
                    .where(
                        chats.c.user_id == user_id,
                        messages.c.role == "assistant",
                        messages.c.model_id.is_not(None),
                        messages.c.model_id != "",
                    )
                    .group_by(messages.c.model_id)
                    .order_by(text("cnt DESC"))
                    .limit(5)
                )
            ).fetchall()

        top_models = [
            TopModel(model_id=row[0], count=int(row[1])) for row in top_rows
        ]

        messages_by_day = await self._messages_by_day(user_id=user_id, days=14)

        return UserAnalytics(
            total_messages=total_messages,
            total_chats=total_chats,
            messages_last_7_days=messages_last_7,
            top_models=top_models,
            messages_by_day=messages_by_day,
        )

    async def _messages_by_day(self, *, user_id: int, days: int) -> list[DayCount]:
        """Return a zero-filled daily message-count series (oldest first).

        Groups messages by UTC calendar day over the trailing *days*
        window and back-fills any day with no activity as 0, so the
        frontend chart has a continuous x-axis.
        """
        window_start = (datetime.now(UTC) - timedelta(days=days - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(
                        func.date(messages.c.created_at).label("day"),
                        func.count().label("cnt"),
                    )
                    .select_from(messages)
                    .join(chats, messages.c.chat_id == chats.c.id)
                    .where(
                        chats.c.user_id == user_id,
                        messages.c.created_at >= window_start,
                    )
                    .group_by(text("day"))
                )
            ).fetchall()

        counts: dict[str, int] = {str(r[0]): int(r[1]) for r in rows}
        # DB buckets are UTC calendar days; the zero-fill anchor must
        # match (utc_today(), not host-local) or a host whose local date
        # differs from UTC drops today's activity or back-fills a
        # phantom empty day.
        today = utc_today()
        series: list[DayCount] = []
        for offset in range(days - 1, -1, -1):
            d = (today - timedelta(days=offset)).isoformat()
            series.append(DayCount(day=d, count=counts.get(d, 0)))
        return series

    async def system_stats(self) -> SystemAnalytics:
        """Return system-wide analytics (admin only).

        Returns:
            :class:`SystemAnalytics` with five metric fields.
        """
        cutoff = datetime.now(UTC) - timedelta(days=7)

        async with self._engine.connect() as conn:
            total_users_row = (
                await conn.execute(select(func.count()).select_from(users))
            ).scalar_one()
            total_users: int = int(total_users_row or 0)

            total_chats_row = (
                await conn.execute(select(func.count()).select_from(chats))
            ).scalar_one()
            total_chats: int = int(total_chats_row or 0)

            total_msgs_row = (
                await conn.execute(select(func.count()).select_from(messages))
            ).scalar_one()
            total_messages: int = int(total_msgs_row or 0)

            recent_row = (
                await conn.execute(
                    select(func.count())
                    .select_from(messages)
                    .where(messages.c.created_at >= cutoff)
                )
            ).scalar_one()
            messages_last_7: int = int(recent_row or 0)

            top_rows = (
                await conn.execute(
                    select(messages.c.model_id, func.count().label("cnt"))
                    .select_from(messages)
                    .where(
                        messages.c.role == "assistant",
                        messages.c.model_id.is_not(None),
                        messages.c.model_id != "",
                    )
                    .group_by(messages.c.model_id)
                    .order_by(text("cnt DESC"))
                    .limit(5)
                )
            ).fetchall()

        top_models = [
            TopModel(model_id=row[0], count=int(row[1])) for row in top_rows
        ]

        return SystemAnalytics(
            total_users=total_users,
            total_chats=total_chats,
            total_messages=total_messages,
            messages_last_7_days=messages_last_7,
            top_models=top_models,
        )
