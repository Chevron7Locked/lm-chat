# SPDX-License-Identifier: Apache-2.0
"""recall_insights cross-project leakage regression test.

The wiring in ``rag_service.py`` already calls ``recall_insights`` with
``project_id=chat_project_id``. This test pins the contract so a
future regression that drops project_id from that call doesn't slip
through unnoticed.

Coverage:
* Insights pinned in project P1 do NOT appear in recall_insights
  output when scoped to project P2.
* Insights pinned with project_id=None (un-projected) do NOT appear
  when scoped to a project.
* When project_id=None, the legacy user-scoped union returns ALL
  the user's insights regardless of project_id.
* Cross-user isolation still holds regardless of project scope
  (defense-in-depth).
"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, update
from sqlalchemy.ext.asyncio import create_async_engine

from lmchat.db.schema import (
    memory_insights,
    metadata,
    projects,
    users,
)
from lmchat.embedding.client import EmbeddingClient
from lmchat.services.memory_service import MemoryService
from lmchat.services.models_service import (
    Capabilities,
    ModelInfo,
    ModelsService,
)


async def _make_engine(tmp_path: Path):
    eng = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/recall_insights_scope.db",
        pool_pre_ping=True,
    )
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return eng


def _make_memory_service(engine) -> MemoryService:
    """Mirror the stub from test_message_search.py."""
    embed_client = AsyncMock(spec=EmbeddingClient)
    models_svc = AsyncMock(spec=ModelsService)
    model = ModelInfo(
        key="embed-A",
        type="embedding",
        capabilities=Capabilities(
            vision=False, trained_for_tool_use=False
        ),
    )
    models_svc.list_loaded.return_value = [model]
    svc = MemoryService(
        engine=engine,
        embedding_client=embed_client,
        models_service=models_svc,
    )
    svc.index_message = AsyncMock(return_value=None)  # type: ignore[method-assign]
    return svc


async def _seed_users_projects(engine) -> dict[str, int]:
    """Two users; user-1 owns projects P1 + P2; user-2 owns P3."""
    out: dict[str, int] = {}
    async with engine.begin() as conn:
        r = await conn.execute(
            insert(users).values(username="alice", password_hash="x")
        )
        out["u1"] = int(r.inserted_primary_key[0])
        r = await conn.execute(
            insert(users).values(username="bob", password_hash="x")
        )
        out["u2"] = int(r.inserted_primary_key[0])
        now = time.time()
        for label, uid in (
            ("P1", out["u1"]),
            ("P2", out["u1"]),
            ("P3", out["u2"]),
        ):
            r = await conn.execute(
                insert(projects).values(
                    user_id=uid,
                    name=label,
                    description="",
                    system_prompt="",
    
                    created_at=now,
                    updated_at=now,
                )
            )
            out[label] = int(r.inserted_primary_key[0])
    return out


async def _pin(svc: MemoryService, user_id: int, text: str) -> int:
    """Pin via the service so the dedup + cap paths exercise normally,
    then return the row id."""
    insight = await svc.pin_insight(user_id=user_id, text=text)
    return insight.id


async def _retag_project(engine, *, insight_id: int, project_id: int | None) -> None:
    """Direct DB update — set project_id on the pinned row to simulate
    legacy data shapes (where pin_insight didn't accept the
    kwarg). Mirrors the helper used in test_search_scope.py."""
    async with engine.begin() as conn:
        await conn.execute(
            update(memory_insights)
            .where(memory_insights.c.id == insight_id)
            .values(project_id=project_id)
        )


# ─── Tests ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recall_insights_scoped_excludes_other_projects(
    tmp_path: Path,
) -> None:
    """project_id=P1 returns only P1's pinned insights — never P2's
    and never un-projected ones."""
    eng = await _make_engine(tmp_path)
    ids = await _seed_users_projects(eng)
    svc = _make_memory_service(eng)

    in_p1 = await _pin(svc, ids["u1"], "alpha in P1")
    in_p2 = await _pin(svc, ids["u1"], "alpha in P2")
    in_unp = await _pin(svc, ids["u1"], "alpha un-projected")
    await _retag_project(eng, insight_id=in_p1, project_id=ids["P1"])
    await _retag_project(eng, insight_id=in_p2, project_id=ids["P2"])
    await _retag_project(eng, insight_id=in_unp, project_id=None)

    rows = await svc.recall_insights(
        user_id=ids["u1"], top_k=10, project_id=ids["P1"]
    )
    surfaced_texts = {r.text for r in rows}

    assert "alpha in P1" in surfaced_texts, (
        f"P1 insight missing from project_id=P1 recall: {surfaced_texts}"
    )
    assert "alpha in P2" not in surfaced_texts, (
        f"P2 insight leaked into P1 recall: {surfaced_texts}"
    )
    assert "alpha un-projected" not in surfaced_texts, (
        f"Un-projected insight leaked into P1 recall: {surfaced_texts}"
    )
    await eng.dispose()


@pytest.mark.asyncio
async def test_recall_insights_none_returns_user_scoped_union(
    tmp_path: Path,
) -> None:
    """project_id=None preserves the legacy user-scoped union — every
    insight the user owns regardless of project_id."""
    eng = await _make_engine(tmp_path)
    ids = await _seed_users_projects(eng)
    svc = _make_memory_service(eng)

    in_p1 = await _pin(svc, ids["u1"], "beta P1")
    in_p2 = await _pin(svc, ids["u1"], "beta P2")
    in_unp = await _pin(svc, ids["u1"], "beta unprojected")
    await _retag_project(eng, insight_id=in_p1, project_id=ids["P1"])
    await _retag_project(eng, insight_id=in_p2, project_id=ids["P2"])
    await _retag_project(eng, insight_id=in_unp, project_id=None)

    rows = await svc.recall_insights(
        user_id=ids["u1"], top_k=10, project_id=None
    )
    surfaced = {r.text for r in rows}

    # All three present — project_id=None preserves the union shape.
    assert "beta P1" in surfaced
    assert "beta P2" in surfaced
    assert "beta unprojected" in surfaced
    await eng.dispose()


@pytest.mark.asyncio
async def test_recall_insights_cross_user_isolation_holds_with_scope(
    tmp_path: Path,
) -> None:
    """User-1's recall_insights(project_id=P1) never returns user-2's
    insights even if user-2 had a project with the same id (defense-
    in-depth — user_id filter is independent of project_id filter)."""
    eng = await _make_engine(tmp_path)
    ids = await _seed_users_projects(eng)
    svc = _make_memory_service(eng)

    u2_pin = await _pin(svc, ids["u2"], "gamma user2 P3")
    await _retag_project(eng, insight_id=u2_pin, project_id=ids["P3"])

    u1_pin = await _pin(svc, ids["u1"], "gamma user1 P1")
    await _retag_project(eng, insight_id=u1_pin, project_id=ids["P1"])

    # User-1 scoped to P1.
    rows = await svc.recall_insights(
        user_id=ids["u1"], top_k=10, project_id=ids["P1"]
    )
    texts = {r.text for r in rows}
    assert "gamma user1 P1" in texts
    assert "gamma user2 P3" not in texts, (
        f"cross-user leak under project scope: {texts}"
    )
    await eng.dispose()
