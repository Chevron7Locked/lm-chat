# SPDX-License-Identifier: Apache-2.0
"""Per-project pin / refine / restore semantics.

Pins the per-project scoping contract:

* ``pin_insight(project_id=X)`` writes the project_id on insert.
* ``refine(project_id=X)`` scopes the snapshot, the DELETE, AND the
  re-insert to that project only — does NOT touch other projects' or
  un-projected pins.
* ``refine(project_id=None)`` (the default semantic) scopes to
  un-projected pins only — NOT a user-wide wipe (a deliberate
  behavioral change from the earlier user-wide refine).
* ``restore_from_history`` preserves each entry's project_id from
  the (extended) snapshot tuple.
* ``restore_from_history(project_id=X)`` is a partial restore — only
  entries with that project_id are re-inserted; other scopes
  untouched.
"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, select
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
        f"sqlite+aiosqlite:///{tmp_path}/per_project_scope.db",
        pool_pre_ping=True,
    )
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return eng


def _make_memory_service(engine) -> MemoryService:
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


async def _seed_user_two_projects(engine) -> tuple[int, int, int]:
    async with engine.begin() as conn:
        u = await conn.execute(
            insert(users).values(username="alice", password_hash="x")
        )
        uid = int(u.inserted_primary_key[0])
        now = time.time()
        p1 = await conn.execute(
            insert(projects).values(
                user_id=uid,
                name="P1",
                description="",
                system_prompt="",

                created_at=now,
                updated_at=now,
            )
        )
        p2 = await conn.execute(
            insert(projects).values(
                user_id=uid,
                name="P2",
                description="",
                system_prompt="",

                created_at=now,
                updated_at=now,
            )
        )
    return uid, int(p1.inserted_primary_key[0]), int(p2.inserted_primary_key[0])


async def _pinned_texts_by_scope(
    engine, *, user_id: int, project_id: int | None
) -> set[str]:
    scope_clause = (
        memory_insights.c.project_id == project_id
        if project_id is not None
        else memory_insights.c.project_id.is_(None)
    )
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(memory_insights.c.text).where(
                    memory_insights.c.user_id == user_id,
                    memory_insights.c.pinned.is_(True),
                    scope_clause,
                )
            )
        ).fetchall()
    return {r[0] for r in rows}


# ─── pin_insight ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pin_insight_writes_project_id(tmp_path: Path) -> None:
    """pin_insight(project_id=P1) lands the row with project_id=P1."""
    eng = await _make_engine(tmp_path)
    uid, p1, _ = await _seed_user_two_projects(eng)
    svc = _make_memory_service(eng)

    await svc.pin_insight(user_id=uid, text="alpha P1", project_id=p1)

    p1_texts = await _pinned_texts_by_scope(eng, user_id=uid, project_id=p1)
    assert p1_texts == {"alpha P1"}
    unp_texts = await _pinned_texts_by_scope(
        eng, user_id=uid, project_id=None
    )
    assert unp_texts == set()
    await eng.dispose()


@pytest.mark.asyncio
async def test_pin_insight_no_project_id_stays_unprojected(
    tmp_path: Path,
) -> None:
    """Legacy behavior: pin_insight without project_id leaves the row
    un-projected (project_id IS NULL)."""
    eng = await _make_engine(tmp_path)
    uid, _, _ = await _seed_user_two_projects(eng)
    svc = _make_memory_service(eng)

    await svc.pin_insight(user_id=uid, text="alpha unp")

    unp_texts = await _pinned_texts_by_scope(
        eng, user_id=uid, project_id=None
    )
    assert unp_texts == {"alpha unp"}
    await eng.dispose()


# ─── refine ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refine_project_scoped_does_not_touch_other_scopes(
    tmp_path: Path,
) -> None:
    """refine(project_id=P1) replaces P1's pins only. P2's pins +
    un-projected pins are NOT deleted."""
    eng = await _make_engine(tmp_path)
    uid, p1, p2 = await _seed_user_two_projects(eng)
    svc = _make_memory_service(eng)

    await svc.pin_insight(user_id=uid, text="P1-old", project_id=p1)
    await svc.pin_insight(user_id=uid, text="P2-keep", project_id=p2)
    await svc.pin_insight(user_id=uid, text="unp-keep")

    async def _stub_refine(items: list[str]) -> list[str]:
        return ["P1-new"]

    await svc.refine(
        user_id=uid, refine_callable=_stub_refine, project_id=p1
    )

    p1_texts = await _pinned_texts_by_scope(eng, user_id=uid, project_id=p1)
    p2_texts = await _pinned_texts_by_scope(eng, user_id=uid, project_id=p2)
    unp_texts = await _pinned_texts_by_scope(
        eng, user_id=uid, project_id=None
    )
    assert p1_texts == {"P1-new"}, f"P1 refine result wrong: {p1_texts}"
    assert p2_texts == {"P2-keep"}, (
        f"refine(P1) wiped P2: {p2_texts}"
    )
    assert unp_texts == {"unp-keep"}, (
        f"refine(P1) wiped un-projected: {unp_texts}"
    )
    await eng.dispose()


@pytest.mark.asyncio
async def test_refine_unprojected_scopes_to_null_only(
    tmp_path: Path,
) -> None:
    """refine(project_id=None) scopes to un-projected pins only — a
    deliberate change from the earlier user-wide behavior."""
    eng = await _make_engine(tmp_path)
    uid, p1, _ = await _seed_user_two_projects(eng)
    svc = _make_memory_service(eng)

    await svc.pin_insight(user_id=uid, text="P1-keep", project_id=p1)
    await svc.pin_insight(user_id=uid, text="unp-old")

    async def _stub(items: list[str]) -> list[str]:
        return ["unp-new"]

    await svc.refine(
        user_id=uid, refine_callable=_stub, project_id=None
    )

    p1_texts = await _pinned_texts_by_scope(eng, user_id=uid, project_id=p1)
    unp_texts = await _pinned_texts_by_scope(
        eng, user_id=uid, project_id=None
    )
    assert p1_texts == {"P1-keep"}, (
        f"refine(None) wiped P1 — should scope to NULL only: {p1_texts}"
    )
    assert unp_texts == {"unp-new"}, (
        f"un-projected refine result wrong: {unp_texts}"
    )
    await eng.dispose()


# ─── restore_from_history ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_restore_preserves_project_id_from_snapshot(
    tmp_path: Path,
) -> None:
    """After a project-scoped refine, restore from the resulting
    history_id re-inserts the original entries WITH their original
    project_id (the snapshot's project_id field)."""
    eng = await _make_engine(tmp_path)
    uid, p1, _ = await _seed_user_two_projects(eng)
    svc = _make_memory_service(eng)

    await svc.pin_insight(user_id=uid, text="P1-original-a", project_id=p1)
    await svc.pin_insight(user_id=uid, text="P1-original-b", project_id=p1)

    async def _stub(items: list[str]) -> list[str]:
        return ["P1-refined"]

    _, history_id = await svc.refine(
        user_id=uid, refine_callable=_stub, project_id=p1
    )

    # Now restore. Snapshot carries project_id=p1 per entry.
    await svc.restore_from_history(user_id=uid, history_id=history_id)

    p1_texts = await _pinned_texts_by_scope(eng, user_id=uid, project_id=p1)
    assert p1_texts == {"P1-original-a", "P1-original-b"}, (
        f"restore lost P1 scope on re-insert: {p1_texts}"
    )
    await eng.dispose()


@pytest.mark.asyncio
async def test_restore_with_project_id_does_partial_restore(
    tmp_path: Path,
) -> None:
    """When the snapshot mixes scopes (e.g. user-wide pre-refine of
    multiple projects via a single legacy refine), a
    restore_from_history(project_id=P1) only restores P1 entries."""
    eng = await _make_engine(tmp_path)
    uid, p1, p2 = await _seed_user_two_projects(eng)
    svc = _make_memory_service(eng)

    # Build a synthetic mixed-scope snapshot via direct DB write —
    # mirrors the legacy refine output shape.
    from lmchat.db.schema import memory_insights_history

    snapshot = [
        {"id": 1, "text": "P1-a", "created_at": None, "project_id": p1},
        {"id": 2, "text": "P1-b", "created_at": None, "project_id": p1},
        {"id": 3, "text": "P2-a", "created_at": None, "project_id": p2},
        {"id": 4, "text": "unp-a", "created_at": None, "project_id": None},
    ]
    async with eng.begin() as conn:
        r = await conn.execute(
            insert(memory_insights_history).values(
                user_id=uid,
                event="refine",
                insights_before=snapshot,
            )
        )
        pk_r = r.inserted_primary_key
        assert pk_r is not None
        hid = int(pk_r[0])

    await svc.restore_from_history(
        user_id=uid, history_id=hid, project_id=p1
    )

    p1_texts = await _pinned_texts_by_scope(eng, user_id=uid, project_id=p1)
    p2_texts = await _pinned_texts_by_scope(eng, user_id=uid, project_id=p2)
    unp_texts = await _pinned_texts_by_scope(
        eng, user_id=uid, project_id=None
    )
    assert p1_texts == {"P1-a", "P1-b"}, (
        f"P1 partial-restore wrong: {p1_texts}"
    )
    assert p2_texts == set(), (
        f"partial-restore should NOT touch P2: {p2_texts}"
    )
    assert unp_texts == set(), (
        f"partial-restore should NOT touch un-projected: {unp_texts}"
    )
    await eng.dispose()


@pytest.mark.asyncio
async def test_legacy_snapshot_without_project_id_restores_as_unprojected(
    tmp_path: Path,
) -> None:
    """Older snapshots don't carry the project_id key.
    restore_from_history must treat the missing key as un-projected
    (entry.get('project_id') returns None) — backward-compat read."""
    eng = await _make_engine(tmp_path)
    uid, _, _ = await _seed_user_two_projects(eng)
    svc = _make_memory_service(eng)

    from lmchat.db.schema import memory_insights_history

    # Legacy shape: no project_id key.
    snapshot = [
        {"id": 1, "text": "legacy-a", "created_at": None},
        {"id": 2, "text": "legacy-b", "created_at": None},
    ]
    async with eng.begin() as conn:
        r = await conn.execute(
            insert(memory_insights_history).values(
                user_id=uid,
                event="refine",
                insights_before=snapshot,
            )
        )
        pk_r2 = r.inserted_primary_key
        assert pk_r2 is not None
        hid = int(pk_r2[0])

    await svc.restore_from_history(user_id=uid, history_id=hid)

    unp_texts = await _pinned_texts_by_scope(
        eng, user_id=uid, project_id=None
    )
    assert unp_texts == {"legacy-a", "legacy-b"}, (
        f"legacy snapshot did not restore as un-projected: {unp_texts}"
    )
    await eng.dispose()
