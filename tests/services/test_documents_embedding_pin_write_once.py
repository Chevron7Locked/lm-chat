# SPDX-License-Identifier: Apache-2.0
"""Write-once-on-attach invariant for projects.embedding_model_id.

Covers ``_enforce_embedding_pin_or_pin``
across both attach paths (set_document_project_id move + the upload-
through-upload_document flow share the same helper). The 409 body
construction is tested separately in test_form_utils_embedding_pin.py.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import create_async_engine

from lmchat.db.schema import documents, metadata, projects, users
from lmchat.services.documents_service import (
    EmbeddingModelPinConflict,
    _enforce_embedding_pin_or_pin,
    set_document_project_id,
)
from lmchat.services.models_service import ModelsService


async def _make_engine(tmp_path: Path):
    eng = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/pin_write_once.db",
        pool_pre_ping=True,
    )
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return eng


async def _seed(eng) -> tuple[int, int, int]:
    """Create user, project, document; return ids."""
    async with eng.begin() as conn:
        u = await conn.execute(
            insert(users).values(username="alice", password_hash="x")
        )
        uid = int(u.inserted_primary_key[0])
        p = await conn.execute(
            insert(projects).values(
                user_id=uid,
                name="P",
                description="",
                system_prompt="",

                created_at=time.time(),
                updated_at=time.time(),
            )
        )
        pid = int(p.inserted_primary_key[0])
        d = await conn.execute(
            insert(documents).values(
                user_id=uid,
                title="t",
                mime_type="text/plain",
                byte_size=1,
                chunk_count=0,
                embedding_model_id="",
                sha256="d" * 64,
                deleted_at=None,
            )
        )
        did = int(d.inserted_primary_key[0])
    return uid, pid, did


def _models_service_stub(active_embedding_id: str | None = "embed-A") -> ModelsService:
    """Mock ModelsService.list_loaded returning a single embedding model."""
    svc = MagicMock()
    if active_embedding_id is None:
        svc.list_loaded = AsyncMock(return_value=[])
    else:
        embed = MagicMock(key=active_embedding_id, type="embedding")
        svc.list_loaded = AsyncMock(return_value=[embed])
    return cast(ModelsService, svc)


# ─── Helper invariants ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_attach_pins_active_model(tmp_path: Path) -> None:
    """When projects.embedding_model_id is NULL, _enforce pins it to
    the currently active embedding model. Subsequent attaches under
    the SAME model are no-ops."""
    eng = await _make_engine(tmp_path)
    uid, pid, _ = await _seed(eng)
    ms = _models_service_stub("embed-A")

    await _enforce_embedding_pin_or_pin(
        project_id=pid, user_id=uid, engine=eng, models_service=ms
    )

    async with eng.connect() as conn:
        row = (
            await conn.execute(
                select(projects.c.embedding_model_id).where(
                    projects.c.id == pid
                )
            )
        ).fetchone()
    assert row is not None and row[0] == "embed-A", (
        f"first attach failed to pin: {row}"
    )

    # Second attach under same active model — no-op, no raise.
    await _enforce_embedding_pin_or_pin(
        project_id=pid, user_id=uid, engine=eng, models_service=ms
    )
    await eng.dispose()


@pytest.mark.asyncio
async def test_mismatched_active_model_raises_conflict(
    tmp_path: Path,
) -> None:
    """Pin = ``embed-A``; active = ``embed-B`` → raises
    EmbeddingModelPinConflict with the right attrs."""
    eng = await _make_engine(tmp_path)
    uid, pid, _ = await _seed(eng)
    # First, pin under embed-A.
    await _enforce_embedding_pin_or_pin(
        project_id=pid,
        user_id=uid,
        engine=eng,
        models_service=_models_service_stub("embed-A"),
    )

    # Now swap active model and try again.
    with pytest.raises(EmbeddingModelPinConflict) as ei:
        await _enforce_embedding_pin_or_pin(
            project_id=pid,
            user_id=uid,
            engine=eng,
            models_service=_models_service_stub("embed-B"),
        )
    exc = ei.value
    assert exc.project_id == pid
    assert exc.pinned_model_id == "embed-A"
    assert exc.active_model_id == "embed-B"
    await eng.dispose()


@pytest.mark.asyncio
async def test_no_active_embedding_model_is_graceful_no_op(
    tmp_path: Path,
) -> None:
    """When ``models_service.list_loaded`` returns no embedding model
    (admin has none loaded), _enforce is a graceful no-op — does
    NOT raise and does NOT write the pin (NULL fallback contract)."""
    eng = await _make_engine(tmp_path)
    uid, pid, _ = await _seed(eng)
    ms = _models_service_stub(None)

    await _enforce_embedding_pin_or_pin(
        project_id=pid, user_id=uid, engine=eng, models_service=ms
    )

    async with eng.connect() as conn:
        row = (
            await conn.execute(
                select(projects.c.embedding_model_id).where(
                    projects.c.id == pid
                )
            )
        ).fetchone()
    assert row is not None and row[0] is None, (
        f"no-active path should NOT write a pin, got: {row}"
    )
    await eng.dispose()


# ─── set_document_project_id integration ─────────────────────────────────


@pytest.mark.asyncio
async def test_set_document_project_id_attach_enforces_pin(
    tmp_path: Path,
) -> None:
    """When attaching a document into a project (project_id != None)
    AND models_service is supplied, the move call enforces the pin."""
    eng = await _make_engine(tmp_path)
    uid, pid, did = await _seed(eng)

    # First attach — pin to embed-A.
    await set_document_project_id(
        document_id=did,
        user_id=uid,
        project_id=pid,
        engine=eng,
        models_service=_models_service_stub("embed-A"),
    )
    # Detach is exempt — no models_service required.
    await set_document_project_id(
        document_id=did,
        user_id=uid,
        project_id=None,
        engine=eng,
    )

    # Re-attach under embed-B → 409 territory at the service layer.
    with pytest.raises(EmbeddingModelPinConflict):
        await set_document_project_id(
            document_id=did,
            user_id=uid,
            project_id=pid,
            engine=eng,
            models_service=_models_service_stub("embed-B"),
        )
    await eng.dispose()


@pytest.mark.asyncio
async def test_set_document_project_id_backward_compat_when_models_service_none(
    tmp_path: Path,
) -> None:
    """Legacy callers that pass ``models_service=None`` skip the pin
    enforcement (backward compat for existing tests + scripts). The
    move still succeeds; the spec contract is enforced at the route
    layer where models_service IS supplied."""
    eng = await _make_engine(tmp_path)
    uid, pid, did = await _seed(eng)

    await set_document_project_id(
        document_id=did,
        user_id=uid,
        project_id=pid,
        engine=eng,
        # models_service omitted — legacy shape.
    )

    async with eng.connect() as conn:
        row = (
            await conn.execute(
                select(documents.c.project_id).where(documents.c.id == did)
            )
        ).fetchone()
    assert row is not None and row[0] == pid
    await eng.dispose()


# ─── TOCTOU / double-resolve fix ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_active_override_is_used_without_calling_list_loaded(
    tmp_path: Path,
) -> None:
    """When ``active_override`` is supplied, ``_enforce`` does NOT
    call ``models_service.list_loaded()`` — the override IS the
    source of truth. Closes the double-resolve TOCTOU.
    """
    eng = await _make_engine(tmp_path)
    uid, pid, _ = await _seed(eng)

    # models_service stub that would RAISE if called — proves we
    # don't fall through to its list_loaded.
    raising = MagicMock()
    raising.list_loaded = AsyncMock(
        side_effect=AssertionError(
            "list_loaded should not be called when active_override is set"
        )
    )

    await _enforce_embedding_pin_or_pin(
        project_id=pid,
        user_id=uid,
        engine=eng,
        models_service=raising,
        active_override="embed-from-upstream",
    )

    # Pin landed correctly under the override.
    async with eng.connect() as conn:
        row = (
            await conn.execute(
                select(projects.c.embedding_model_id).where(
                    projects.c.id == pid
                )
            )
        ).fetchone()
    assert row is not None and row[0] == "embed-from-upstream"
    await eng.dispose()


@pytest.mark.asyncio
async def test_pin_is_atomic_conditional_update(tmp_path: Path) -> None:
    """The pin write is a conditional UPDATE
    ``WHERE embedding_model_id IS NULL OR = ''`` so a concurrent
    writer who pinned the same project in between does NOT clobber.
    Simulate the race by pre-pinning the row to a DIFFERENT model
    BEFORE the helper runs.
    """
    from sqlalchemy import update

    eng = await _make_engine(tmp_path)
    uid, pid, _ = await _seed(eng)

    # Simulate "concurrent writer pinned to embed-X before us".
    async with eng.begin() as conn:
        await conn.execute(
            update(projects)
            .where(projects.c.id == pid)
            .values(embedding_model_id="embed-X")
        )

    # We try to pin to embed-Y — the conditional UPDATE matches 0
    # rows; the SELECT fall-through reads ``embed-X`` and raises
    # the conflict.
    with pytest.raises(EmbeddingModelPinConflict) as ei:
        await _enforce_embedding_pin_or_pin(
            project_id=pid,
            user_id=uid,
            engine=eng,
            models_service=_models_service_stub(None),
            active_override="embed-Y",
        )
    assert ei.value.pinned_model_id == "embed-X"
    assert ei.value.active_model_id == "embed-Y"

    # The pre-existing pin was NOT clobbered.
    async with eng.connect() as conn:
        row = (
            await conn.execute(
                select(projects.c.embedding_model_id).where(
                    projects.c.id == pid
                )
            )
        ).fetchone()
    assert row is not None and row[0] == "embed-X"
    await eng.dispose()
