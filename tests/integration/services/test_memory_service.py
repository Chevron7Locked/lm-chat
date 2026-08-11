# SPDX-License-Identifier: Apache-2.0
"""Service-level IDOR isolation tests for MemoryService.

These tests exercise ``MemoryService.unpin_insight`` directly against a
real SQLite DB (no live server, no HTTP stack) to assert that the
ownership enforcement added by LLM06 fix #168 prevents cross-user deletion.

The ``unpin_insight(insight_id, user_id=…)`` contract under test:
- Owner deletes successfully (no error, row gone).
- Non-owner raises ``InsightNotFoundError`` and the row is untouched.

A second invariant: ``list_pinned(user_id)`` must never return another
user's insights.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import metadata
from lmchat.services.memory_service import InsightNotFoundError, MemoryService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def engine() -> AsyncGenerator[AsyncEngine]:
    """In-memory SQLite engine with the full lm-chat schema."""
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", pool_pre_ping=True)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture()
async def svc(engine: AsyncEngine) -> AsyncGenerator[MemoryService]:
    """MemoryService wired to the in-memory DB; embedding deps stubbed out."""
    embedding_client = AsyncMock()
    models_service = MagicMock()
    yield MemoryService(
        engine=engine,
        embedding_client=embedding_client,
        models_service=models_service,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _pin(svc: MemoryService, user_id: int, text: str = "test insight") -> int:
    """Pin *text* for *user_id*; return the new insight PK."""
    insight = await svc.pin_insight(user_id=user_id, text=text)
    return insight.id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unpin_owner_succeeds(svc: MemoryService) -> None:
    """The owning user can delete their own insight."""
    iid = await _pin(svc, user_id=1)
    # Should not raise.
    await svc.unpin_insight(iid, user_id=1)
    # Confirm the row is gone.
    remaining = await svc.list_pinned(user_id=1)
    assert all(r.id != iid for r in remaining), "insight should be deleted"


@pytest.mark.asyncio
async def test_unpin_cross_user_raises(svc: MemoryService) -> None:
    """User B cannot delete User A's insight — must raise InsightNotFoundError.

    LLM06 #168 regression guard: ``unpin_insight`` previously issued
    ``DELETE … WHERE id = :id`` with NO user_id filter, allowing IDOR.
    The fix adds ``.where(user_id == :user_id)``; this test confirms that
    the row is NOT deleted and the correct exception is raised.
    """
    iid = await _pin(svc, user_id=1, text="user A private insight")

    with pytest.raises(InsightNotFoundError):
        await svc.unpin_insight(iid, user_id=2)  # user B tries to delete user A's row

    # Row must still exist for user A.
    a_pins = await svc.list_pinned(user_id=1)
    assert any(r.id == iid for r in a_pins), (
        "insight must still exist for user A after rejected cross-user delete"
    )


@pytest.mark.asyncio
async def test_unpin_nonexistent_raises(svc: MemoryService) -> None:
    """Deleting a row that doesn't exist also raises InsightNotFoundError."""
    with pytest.raises(InsightNotFoundError):
        await svc.unpin_insight(99999, user_id=1)


@pytest.mark.asyncio
async def test_list_pinned_isolation(svc: MemoryService) -> None:
    """list_pinned(user_id) must not return another user's insights."""
    await _pin(svc, user_id=1, text="user one insight")
    await _pin(svc, user_id=2, text="user two insight")

    a_pins = await svc.list_pinned(user_id=1)
    b_pins = await svc.list_pinned(user_id=2)

    a_ids = {r.id for r in a_pins}
    b_ids = {r.id for r in b_pins}

    assert not a_ids & b_ids, (
        "list_pinned must not leak insights across users; "
        f"overlap: {a_ids & b_ids}"
    )
    assert len(a_pins) == 1
    assert len(b_pins) == 1
