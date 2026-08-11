# SPDX-License-Identifier: Apache-2.0
"""Unit tests for MemoryService.edit_insight / refine / restore_from_history (P13l.3)."""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import create_async_engine

from lmchat.db.schema import (
    memory_insights_history,
    metadata,
    users,
)
from lmchat.services.memory_service import (
    HistoryNotFoundError,
    InsightNotFoundError,
    MemoryService,
    RefineUpstreamError,
)


async def _make_engine(tmp_path: Path):
    eng = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/mem.db", pool_pre_ping=True
    )
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return eng


async def _insert_user(eng, username: str = "alice") -> int:
    async with eng.begin() as conn:
        result = await conn.execute(
            insert(users).values(username=username, password_hash="scrypt$dummy")
        )
        return int(result.inserted_primary_key[0])


def _make_service(eng) -> MemoryService:
    from unittest.mock import AsyncMock

    embedding_client = AsyncMock()
    models_service = AsyncMock()
    return MemoryService(
        engine=eng,
        embedding_client=embedding_client,
        models_service=models_service,
    )


async def _pin(svc: MemoryService, user_id: int, text: str) -> int:
    row = await svc.pin_insight(user_id=user_id, text=text)
    return row.id


# ---------------------------------------------------------------------------
# edit_insight
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_edit_insight_happy(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        svc = _make_service(eng)
        pin_id = await _pin(svc, uid, "loves coffee")
        updated = await svc.edit_insight(
            insight_id=pin_id, user_id=uid, text="loves espresso"
        )
        assert updated.text == "loves espresso"
        # Hash also updated — re-pinning the original text should now be allowed.
        new = await svc.pin_insight(user_id=uid, text="loves coffee")
        assert new.id != pin_id
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_edit_insight_cross_user_404(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        alice = await _insert_user(eng, "alice")
        bob = await _insert_user(eng, "bob")
        svc = _make_service(eng)
        pin_id = await _pin(svc, alice, "secret")
        with pytest.raises(InsightNotFoundError):
            await svc.edit_insight(
                insight_id=pin_id, user_id=bob, text="bob's edit"
            )
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_edit_insight_empty_raises(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        svc = _make_service(eng)
        pin_id = await _pin(svc, uid, "hello")
        with pytest.raises(ValueError):
            await svc.edit_insight(insight_id=pin_id, user_id=uid, text="   ")
    finally:
        await eng.dispose()


# ---------------------------------------------------------------------------
# refine
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_refine_replaces_pins_and_writes_history(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        svc = _make_service(eng)
        # Three distinct pins (dedup is text-hash based on normalised
        # text; "Likes Python" / "likes python" share a hash, so use
        # three distinct insights here).
        await _pin(svc, uid, "Likes Python")
        await _pin(svc, uid, "prefers tabs over spaces")
        await _pin(svc, uid, "is a coffee enthusiast")

        async def fake_curator(items: list[str]) -> list[str]:
            # Merge near-duplicates; preserve the unique item.
            return ["Likes Python", "prefers tabs over spaces"]

        result, history_id = await svc.refine(
            user_id=uid, refine_callable=fake_curator
        )
        assert len(result) == 2
        texts = {r.text for r in result}
        assert texts == {"Likes Python", "prefers tabs over spaces"}
        assert history_id > 0

        # History row records the before-state.
        async with eng.connect() as conn:
            h = (
                await conn.execute(
                    select(memory_insights_history).where(
                        memory_insights_history.c.id == history_id
                    )
                )
            ).fetchone()
        assert h is not None
        assert h.event == "refine"
        snap = h.insights_before
        # SQLite returns the JSON column as a Python list (SQLAlchemy JSON type).
        if isinstance(snap, str):
            import json

            snap = json.loads(snap)
        assert len(snap) == 3
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_refine_empty_output_raises(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        svc = _make_service(eng)
        await _pin(svc, uid, "Likes Python")

        async def empty_curator(items: list[str]) -> list[str]:
            return []

        with pytest.raises(RefineUpstreamError):
            await svc.refine(user_id=uid, refine_callable=empty_curator)
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_refine_upstream_failure_raises(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        svc = _make_service(eng)
        await _pin(svc, uid, "Likes Python")

        async def boom(items: list[str]) -> list[str]:
            raise RuntimeError("network down")

        with pytest.raises(RefineUpstreamError):
            await svc.refine(user_id=uid, refine_callable=boom)
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_refine_with_no_pins_returns_empty(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        svc = _make_service(eng)

        async def curator(items: list[str]) -> list[str]:
            return ["should-not-be-called"]

        result, history_id = await svc.refine(
            user_id=uid, refine_callable=curator
        )
        assert result == []
        assert history_id == 0
    finally:
        await eng.dispose()


# ---------------------------------------------------------------------------
# restore_from_history
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_restore_undoes_refine(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        svc = _make_service(eng)
        await _pin(svc, uid, "A")
        await _pin(svc, uid, "B")
        await _pin(svc, uid, "C")

        async def curator(items: list[str]) -> list[str]:
            return ["X"]  # collapses everything to a single insight

        _, history_id = await svc.refine(
            user_id=uid, refine_callable=curator
        )
        assert len(await svc.list_pinned(user_id=uid)) == 1

        restored = await svc.restore_from_history(
            user_id=uid, history_id=history_id
        )
        assert len(restored) == 3
        assert {r.text for r in restored} == {"A", "B", "C"}
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_restore_cross_user_404(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        alice = await _insert_user(eng, "alice")
        bob = await _insert_user(eng, "bob")
        svc = _make_service(eng)
        await _pin(svc, alice, "A")

        async def curator(items: list[str]) -> list[str]:
            return ["X"]

        _, history_id = await svc.refine(
            user_id=alice, refine_callable=curator
        )
        with pytest.raises(HistoryNotFoundError):
            await svc.restore_from_history(
                user_id=bob, history_id=history_id
            )
    finally:
        await eng.dispose()
