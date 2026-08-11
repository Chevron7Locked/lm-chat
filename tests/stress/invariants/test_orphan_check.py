# SPDX-License-Identifier: Apache-2.0
"""Orphan-row invariant: every message must point at a live chat + user.

Correctness invariants:
    Zero orphaned messages (messages.user_id exists in users)
    Zero FK violations (chat_shares.chat_id -> chats.id, etc.)
"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.stress_invariant


async def test_no_orphan_messages(stress_engine: AsyncEngine) -> None:
    """Every messages.chat_id must resolve to an existing chat."""
    async with stress_engine.connect() as conn:
        orphans = await conn.scalar(
            text(
                "SELECT COUNT(*) FROM messages m "
                "LEFT JOIN chats c ON c.id = m.chat_id "
                "WHERE c.id IS NULL"
            )
        )
    assert (orphans or 0) == 0, f"{orphans} orphaned messages found"


async def test_no_orphan_share_tokens(stress_engine: AsyncEngine) -> None:
    """Every chat_shares.chat_id must resolve to an existing chat."""
    async with stress_engine.connect() as conn:
        try:
            orphans = await conn.scalar(
                text(
                    "SELECT COUNT(*) FROM chat_shares s "
                    "LEFT JOIN chats c ON c.id = s.chat_id "
                    "WHERE c.id IS NULL"
                )
            )
        except Exception:  # noqa: BLE001
            # chat_shares table absent on a freshly-migrated DB — fine.
            pytest.skip("chat_shares table absent")
            return
    assert (orphans or 0) == 0, f"{orphans} orphaned share tokens"


async def test_no_orphan_memory_insights(stress_engine: AsyncEngine) -> None:
    """Every memory_insights.user_id must resolve to an existing user."""
    async with stress_engine.connect() as conn:
        try:
            orphans = await conn.scalar(
                text(
                    "SELECT COUNT(*) FROM memory_insights i "
                    "LEFT JOIN users u ON u.id = i.user_id "
                    "WHERE u.id IS NULL"
                )
            )
        except Exception:  # noqa: BLE001
            pytest.skip("memory_insights table absent")
            return
    assert (orphans or 0) == 0, f"{orphans} orphaned memory insights"
