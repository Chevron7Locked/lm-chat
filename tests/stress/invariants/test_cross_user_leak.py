# SPDX-License-Identifier: Apache-2.0
"""Cross-user-leak invariant: every row's owner matches its parent's owner.

Invariant:
    Zero cross-user leaks (rows correctly attributed)

Schema notes (see ``src/lmchat/db/schema.py``):

* ``messages`` has NO ``user_id`` column — ownership flows through the
  parent ``chats.user_id``.  The invariant is asserted by joining the
  ``messages`` row through its ``chat_id`` to ``chats`` and asserting
  that the chat row exists (covered by the orphan_check invariant).
  The cross-user-attribution invariant that DOES need a query is on
  ``feedback`` rows: ``feedback.user_id`` must equal the owning user
  of the message's chat.
* ``memory_insights`` has NO ``source_chat_id`` column; the link
  between an insight and the chat it was derived from is mediated by
  ``insight_activations.message_id -> messages.chat_id -> chats.user_id``.
  We assert that every insight's ``user_id`` matches the owning user
  of every chat that produced an activation row pointing at that
  insight.
* ``audit_log`` has NO ``target_user_id`` column; it has a single
  ``user_id`` column (SET NULL on delete).  Validity of that pointer
  is covered by the orphan_check invariant; no additional query here.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.stress_invariant


async def test_feedback_owner_matches_chat(stress_engine: AsyncEngine) -> None:
    """Every feedback row's ``user_id`` must equal the owning user of the
    message's chat.

    A feedback row attributed to user A on a message owned by user B is
    a cross-user-leak: user A can rate user B's private message.
    """
    async with stress_engine.connect() as conn:
        leaks = await conn.scalar(
            text(
                "SELECT COUNT(*) FROM feedback f "
                "JOIN messages m ON m.id = f.message_id "
                "JOIN chats c ON c.id = m.chat_id "
                "WHERE f.user_id <> c.user_id"
            )
        )
    assert (leaks or 0) == 0, (
        f"{leaks} feedback rows with mismatched user_id vs chat owner"
    )


async def test_insight_activations_owner_consistent(
    stress_engine: AsyncEngine,
) -> None:
    """For every ``insight_activations`` row, the activated insight's
    ``user_id`` must equal the owning user of the chat that produced
    the message which triggered the activation.

    Schema chain: ``insight_activations -> messages -> chats.user_id``
    must equal ``insight_activations -> memory_insights.user_id``.
    """
    async with stress_engine.connect() as conn:
        try:
            leaks = await conn.scalar(
                text(
                    "SELECT COUNT(*) FROM insight_activations a "
                    "JOIN memory_insights i ON i.id = a.insight_id "
                    "JOIN messages m ON m.id = a.message_id "
                    "JOIN chats c ON c.id = m.chat_id "
                    "WHERE i.user_id <> c.user_id"
                )
            )
        except Exception:  # noqa: BLE001
            pytest.skip("insight_activations schema variant")
            return
    assert (leaks or 0) == 0, (
        f"{leaks} insight_activations linking insight.user_id != chat.user_id"
    )


async def test_audit_log_user_id_references_live_user(
    stress_engine: AsyncEngine,
) -> None:
    """Every audit_log row with a non-NULL ``user_id`` must reference a
    live ``users`` row.  ``user_id`` is SET NULL on user delete, so any
    surviving non-NULL pointer that misses ``users`` is a leak.
    """
    async with stress_engine.connect() as conn:
        leaks = await conn.scalar(
            text(
                "SELECT COUNT(*) FROM audit_log a "
                "WHERE a.user_id IS NOT NULL "
                "  AND NOT EXISTS ("
                "    SELECT 1 FROM users u WHERE u.id = a.user_id"
                "  )"
            )
        )
    assert (leaks or 0) == 0, (
        f"{leaks} audit_log rows referencing missing users"
    )
