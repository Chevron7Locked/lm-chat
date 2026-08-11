# SPDX-License-Identifier: Apache-2.0
"""Per-user quota service for lm-chat.

All writes use SQLAlchemy Core (no ORM).

Each user has a row in ``quotas`` (default 100 000 tokens/day, 1 000
requests/day); daily consumption is tracked in ``quota_usage`` keyed by
``(user_id, date)``.

Atomic check-and-increment: a single UPDATE embeds the budget check in the
WHERE clause so no read-modify-write race is possible:

    UPDATE quota_usage
    SET tokens_consumed = tokens_consumed + :amount
    WHERE user_id = :uid AND date = CURRENT_DATE
      AND tokens_consumed + :amount <= (
            SELECT tokens_per_day FROM quotas WHERE user_id = :uid
          )

``rowcount == 0`` means either no row yet (handled by the idempotent
INSERT-OR-IGNORE run before the UPDATE) or the budget is exhausted →
``OverQuotaError``.

Admin users are NOT quota-checked (see ``QuotaMiddleware``).

``CURRENT_DATE`` is evaluated by the database engine, not Python, so the
day boundary tracks the database server's clock — consistent within the
query, no Python/DB timezone skew.
"""
from __future__ import annotations

from datetime import UTC, date, datetime

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncEngine

from lmchat.db.schema import quota_usage, quotas
from lmchat.logging import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------


class OverQuotaError(Exception):
    """Raised when a quota bucket is exhausted.

    Args:
        kind: ``"tokens"`` or ``"requests"``.
        user_id: The user whose quota was exceeded.
    """

    def __init__(self, kind: str, user_id: int) -> None:
        """Initialise with quota kind and user id.

        Args:
            kind:    ``"tokens"`` or ``"requests"``.
            user_id: User PK.
        """
        self.kind = kind
        self.user_id = user_id
        super().__init__(f"user {user_id}: daily {kind} quota exceeded")


# ---------------------------------------------------------------------------
# Pydantic projections
# ---------------------------------------------------------------------------


class Quota(BaseModel):
    """Per-user quota limits row."""

    model_config = ConfigDict(from_attributes=False)

    user_id: int
    tokens_per_day: int
    requests_per_day: int


class QuotaUsage(BaseModel):
    """Per-user daily usage row."""

    model_config = ConfigDict(from_attributes=False)

    user_id: int
    date: date
    tokens_consumed: int
    requests_consumed: int


# ---------------------------------------------------------------------------
# Default quota (used when no quotas row exists for the user)
# ---------------------------------------------------------------------------

_DEFAULT_TOKENS_PER_DAY: int = 100_000
_DEFAULT_REQUESTS_PER_DAY: int = 1_000


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _ensure_usage_row(user_id: int, engine: AsyncEngine) -> None:
    """Idempotently create today's quota_usage row if absent.

    Dialect-appropriate INSERT-OR-IGNORE (SQLite) / ON CONFLICT DO NOTHING
    (Postgres). Safe to call concurrently — the composite PK constraint
    prevents duplicate rows.
    """
    async with engine.begin() as conn:
        dialect = conn.dialect.name  # type: ignore[union-attr]
        if dialect == "sqlite":
            await conn.execute(
                sa.text(
                    "INSERT OR IGNORE INTO quota_usage (user_id, date, "
                    "tokens_consumed, requests_consumed) "
                    "VALUES (:uid, DATE('now'), 0, 0)"
                ),
                {"uid": user_id},
            )
        else:
            await conn.execute(
                sa.text(
                    "INSERT INTO quota_usage (user_id, date, "
                    "tokens_consumed, requests_consumed) "
                    "VALUES (:uid, CURRENT_DATE, 0, 0) "
                    "ON CONFLICT (user_id, date) DO NOTHING"
                ),
                {"uid": user_id},
            )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def consume_tokens(user_id: int, amount: int, engine: AsyncEngine) -> None:
    """Atomically consume *amount* tokens from today's budget.

    Raises:
        OverQuotaError: If ``tokens_consumed + amount > tokens_per_day``.
    """
    if amount <= 0:
        return

    await _ensure_usage_row(user_id, engine)

    async with engine.begin() as conn:
        dialect = conn.dialect.name  # type: ignore[union-attr]
        if dialect == "sqlite":
            result = await conn.execute(
                sa.text(
                    "UPDATE quota_usage "
                    "SET tokens_consumed = tokens_consumed + :amount "
                    "WHERE user_id = :uid "
                    "  AND date = DATE('now') "
                    "  AND tokens_consumed + :amount <= COALESCE("
                    "    (SELECT tokens_per_day FROM quotas WHERE user_id = :uid), "
                    "    :default_tokens"
                    "  )"
                ),
                {
                    "uid": user_id,
                    "amount": amount,
                    "default_tokens": _DEFAULT_TOKENS_PER_DAY,
                },
            )
        else:
            result = await conn.execute(
                sa.text(
                    "UPDATE quota_usage "
                    "SET tokens_consumed = tokens_consumed + :amount "
                    "WHERE user_id = :uid "
                    "  AND date = CURRENT_DATE "
                    "  AND tokens_consumed + :amount <= COALESCE("
                    "    (SELECT tokens_per_day FROM quotas WHERE user_id = :uid), "
                    "    :default_tokens"
                    "  )"
                ),
                {
                    "uid": user_id,
                    "amount": amount,
                    "default_tokens": _DEFAULT_TOKENS_PER_DAY,
                },
            )

        if result.rowcount == 0:
            raise OverQuotaError("tokens", user_id)


async def safe_consume_tokens(
    engine: AsyncEngine | None,
    user_id: int,
    content: str,
    *,
    log_prefix: str = "stream",
) -> None:
    """Fire-and-forget wrapper around :func:`consume_tokens`.

    Approximates the token count via :func:`approx_token_count` (UTF-8
    byte-based, CJK-correct). Catches every exception and logs at WARNING
    — a quota write failure must never propagate; the response/stream
    this is attached to has already completed. No-ops if ``engine`` is
    ``None`` (tests, or callers like ``AbCompareService`` with no DB).

    Shared by ``StreamingService`` and ``AbCompareService`` instead of
    duplicating the consume + error-swallow logic.

    Args:
        log_prefix: Namespaces the emitted log event keys, e.g.
            ``"stream"`` → ``stream.quota_tokens_exceeded_post_completion``.
    """
    if engine is None:
        return

    from lmchat.services._token_budget import approx_token_count

    amount = approx_token_count(content)
    try:
        await consume_tokens(user_id, amount, engine)
    except OverQuotaError:
        # Log only — the response already completed.
        log.warning(
            f"{log_prefix}.quota_tokens_exceeded_post_completion",
            user_id=user_id,
            amount=amount,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            f"{log_prefix}.quota_tokens_consume_failed",
            user_id=user_id,
            amount=amount,
            error=str(exc),
        )


async def consume_request(user_id: int, engine: AsyncEngine) -> None:
    """Atomically consume one request from today's budget.

    Raises:
        OverQuotaError: If ``requests_consumed + 1 > requests_per_day``.
    """
    await _ensure_usage_row(user_id, engine)

    async with engine.begin() as conn:
        dialect = conn.dialect.name  # type: ignore[union-attr]
        if dialect == "sqlite":
            result = await conn.execute(
                sa.text(
                    "UPDATE quota_usage "
                    "SET requests_consumed = requests_consumed + 1 "
                    "WHERE user_id = :uid "
                    "  AND date = DATE('now') "
                    "  AND requests_consumed + 1 <= COALESCE("
                    "    (SELECT requests_per_day FROM quotas WHERE user_id = :uid), "
                    "    :default_requests"
                    "  )"
                ),
                {
                    "uid": user_id,
                    "default_requests": _DEFAULT_REQUESTS_PER_DAY,
                },
            )
        else:
            result = await conn.execute(
                sa.text(
                    "UPDATE quota_usage "
                    "SET requests_consumed = requests_consumed + 1 "
                    "WHERE user_id = :uid "
                    "  AND date = CURRENT_DATE "
                    "  AND requests_consumed + 1 <= COALESCE("
                    "    (SELECT requests_per_day FROM quotas WHERE user_id = :uid), "
                    "    :default_requests"
                    "  )"
                ),
                {
                    "uid": user_id,
                    "default_requests": _DEFAULT_REQUESTS_PER_DAY,
                },
            )

        if result.rowcount == 0:
            raise OverQuotaError("requests", user_id)


async def get_usage(user_id: int, day: date, engine: AsyncEngine) -> QuotaUsage:
    """Read usage for *user_id* on *day*; zero-filled if no row exists."""
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                sa.select(
                    quota_usage.c.user_id,
                    quota_usage.c.date,
                    quota_usage.c.tokens_consumed,
                    quota_usage.c.requests_consumed,
                ).where(
                    quota_usage.c.user_id == user_id,
                    quota_usage.c.date == day,
                )
            )
        ).fetchone()

    if row is None:
        return QuotaUsage(
            user_id=user_id,
            date=day,
            tokens_consumed=0,
            requests_consumed=0,
        )
    return QuotaUsage(
        user_id=row[0],
        date=row[1],
        tokens_consumed=row[2],
        requests_consumed=row[3],
    )


async def get_quota(user_id: int, engine: AsyncEngine) -> Quota:
    """Read the quota limits for *user_id*; defaults if no row exists
    (100 000 tokens/day, 1 000 req/day).
    """
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                sa.select(
                    quotas.c.user_id,
                    quotas.c.tokens_per_day,
                    quotas.c.requests_per_day,
                ).where(quotas.c.user_id == user_id)
            )
        ).fetchone()

    if row is None:
        return Quota(
            user_id=user_id,
            tokens_per_day=_DEFAULT_TOKENS_PER_DAY,
            requests_per_day=_DEFAULT_REQUESTS_PER_DAY,
        )
    return Quota(user_id=row[0], tokens_per_day=row[1], requests_per_day=row[2])


async def set_quota(
    user_id: int,
    tokens_per_day: int,
    requests_per_day: int,
    engine: AsyncEngine,
) -> Quota:
    """Admin upsert for a user's quota limits (insert or update)."""
    now = datetime.now(tz=UTC)
    async with engine.begin() as conn:
        dialect = conn.dialect.name  # type: ignore[union-attr]
        if dialect == "sqlite":
            await conn.execute(
                sa.text(
                    "INSERT INTO quotas (user_id, tokens_per_day, requests_per_day, "
                    "created_at, updated_at) "
                    "VALUES (:uid, :tokens, :requests, :now, :now) "
                    "ON CONFLICT (user_id) DO UPDATE "
                    "SET tokens_per_day = :tokens, "
                    "    requests_per_day = :requests, "
                    "    updated_at = :now"
                ),
                {
                    "uid": user_id,
                    "tokens": tokens_per_day,
                    "requests": requests_per_day,
                    "now": now,
                },
            )
        else:
            await conn.execute(
                sa.text(
                    "INSERT INTO quotas (user_id, tokens_per_day, requests_per_day, "
                    "created_at, updated_at) "
                    "VALUES (:uid, :tokens, :requests, :now, :now) "
                    "ON CONFLICT (user_id) DO UPDATE "
                    "SET tokens_per_day = EXCLUDED.tokens_per_day, "
                    "    requests_per_day = EXCLUDED.requests_per_day, "
                    "    updated_at = EXCLUDED.updated_at"
                ),
                {
                    "uid": user_id,
                    "tokens": tokens_per_day,
                    "requests": requests_per_day,
                    "now": now,
                },
            )

    return Quota(
        user_id=user_id,
        tokens_per_day=tokens_per_day,
        requests_per_day=requests_per_day,
    )


async def get_all_quotas(engine: AsyncEngine) -> list[Quota]:
    """Return quota rows for all users that have one; used by GET /api/admin/quotas."""
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                sa.select(
                    quotas.c.user_id,
                    quotas.c.tokens_per_day,
                    quotas.c.requests_per_day,
                ).order_by(quotas.c.user_id)
            )
        ).fetchall()

    return [
        Quota(user_id=row[0], tokens_per_day=row[1], requests_per_day=row[2])
        for row in rows
    ]
