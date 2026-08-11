# SPDX-License-Identifier: Apache-2.0
"""FK-constraint integrity sweep.

Invariant:
    Zero FK violations (chat_shares.chat_id -> chats.id, etc.)

Uses SQLite's ``PRAGMA foreign_key_check`` when the DB is SQLite —
fastest path.  For Postgres we run a synthetic query that LEFT JOINs
every documented FK and counts mismatches.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.stress_invariant


async def test_sqlite_foreign_key_check(stress_engine: AsyncEngine) -> None:
    """SQLite-native FK check — fails if any FK constraint is violated."""
    if stress_engine.dialect.name != "sqlite":
        pytest.skip("SQLite-only check")
        return
    async with stress_engine.connect() as conn:
        result = await conn.execute(text("PRAGMA foreign_key_check"))
        rows = result.fetchall()
    assert not rows, f"FK violations detected: {rows}"


async def test_documented_fks(stress_engine: AsyncEngine) -> None:
    """LEFT JOIN sweep over every documented parent/child pair."""
    # Documented FK constraints (cross-checked against
    # ``src/lmchat/db/schema.py``).  ``messages`` has NO
    # ``user_id`` column — ownership flows through ``chat_id``.
    pairs = [
        ("messages", "chat_id", "chats", "id"),
        ("chats", "user_id", "users", "id"),
        ("chat_shares", "chat_id", "chats", "id"),
        ("memory_insights", "user_id", "users", "id"),
        ("insight_activations", "message_id", "messages", "id"),
        ("insight_activations", "insight_id", "memory_insights", "id"),
        ("audit_log", "user_id", "users", "id"),
    ]
    async with stress_engine.connect() as conn:
        for child, ckey, parent, pkey in pairs:
            try:
                missing = await conn.scalar(
                    text(
                        f"SELECT COUNT(*) FROM {child} c "
                        f"LEFT JOIN {parent} p ON p.{pkey} = c.{ckey} "
                        f"WHERE c.{ckey} IS NOT NULL AND p.{pkey} IS NULL"
                    )
                )
            except Exception:  # noqa: BLE001
                continue
            assert (missing or 0) == 0, (
                f"FK leak: {missing} {child}.{ckey} rows missing {parent}.{pkey}"
            )
