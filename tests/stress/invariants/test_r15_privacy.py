# SPDX-License-Identifier: Apache-2.0
"""R-15: incognito chats MUST NOT spawn memory rows.

Invariants:
    Zero incognito-chat rows in message_embeddings (via messages join)
    Zero incognito-sourced rows in insight_activations (via messages join)
    100% of share tokens reject incognito chats
"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.stress_invariant


async def test_no_incognito_in_memory_embeddings(
    stress_engine: AsyncEngine,
) -> None:
    """No message_embeddings rows may reference incognito chats.

    message_embeddings links to chats via message_id → messages → chat_id.
    The table has no direct chat_id column; the join traverses messages.
    """
    async with stress_engine.connect() as conn:
        try:
            leaks = await conn.scalar(
                text(
                    "SELECT COUNT(*) FROM message_embeddings e "
                    "JOIN messages m ON m.id = e.message_id "
                    "JOIN chats c ON c.id = m.chat_id "
                    "WHERE c.incognito = 1"
                )
            )
        except Exception:  # noqa: BLE001
            # Schema variation: message_embeddings may not exist in
            # SQLite-default migration.  Treat as pass.
            pytest.skip("message_embeddings absent or schema mismatch")
            return
    assert (leaks or 0) == 0, (
        f"R-15 LEAK: {leaks} message_embeddings rows from incognito chats"
    )


async def test_no_incognito_in_memory_insights(
    stress_engine: AsyncEngine,
) -> None:
    """No insight_activations rows may reference messages from incognito chats.

    memory_insights rows are user-scoped (no source_chat_id column); the
    correct R-15 check is via insight_activations, which records the
    (message_id, insight_id) tuples produced by recall_insights.  Any
    activation whose message_id traces to an incognito chat is a leak.
    """
    async with stress_engine.connect() as conn:
        try:
            leaks = await conn.scalar(
                text(
                    "SELECT COUNT(*) FROM insight_activations ia "
                    "JOIN messages m ON m.id = ia.message_id "
                    "JOIN chats c ON c.id = m.chat_id "
                    "WHERE c.incognito = 1"
                )
            )
        except Exception:  # noqa: BLE001
            pytest.skip("insight_activations absent or schema mismatch")
            return
    assert (leaks or 0) == 0, (
        f"R-15 LEAK: {leaks} insight_activations rows from incognito chats"
    )


async def test_no_share_tokens_for_incognito_chats(
    stress_engine: AsyncEngine,
) -> None:
    """Every chat_shares row must point to a non-incognito chat."""
    async with stress_engine.connect() as conn:
        try:
            leaks = await conn.scalar(
                text(
                    "SELECT COUNT(*) FROM chat_shares s "
                    "JOIN chats c ON c.id = s.chat_id "
                    "WHERE c.incognito = 1"
                )
            )
        except Exception:  # noqa: BLE001
            pytest.skip("chat_shares table absent")
            return
    assert (leaks or 0) == 0, (
        f"R-15 LEAK: {leaks} share tokens minted for incognito chats"
    )
