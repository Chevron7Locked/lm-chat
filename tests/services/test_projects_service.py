# SPDX-License-Identifier: Apache-2.0
"""Unit tests for ProjectsService."""
from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import create_async_engine

from lmchat.db.schema import metadata, users
from lmchat.services.projects_service import (
    InvalidProjectFieldError,
    ProjectsService,
    _row_to_project,
)


async def _make_engine(tmp_path: Path):
    eng = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/projects.db", pool_pre_ping=True
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


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_create_minimal(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        svc = ProjectsService(engine=eng)
        p = await svc.create(user_id=uid, name="Research")
        assert p.id > 0
        assert p.user_id == uid
        assert p.name == "Research"
        assert p.description == ""
        assert p.system_prompt == ""
        assert p.created_at == p.updated_at
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_create_with_all_fields(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        svc = ProjectsService(engine=eng)
        # The per-project folders feature was removed; ``create`` no
        # longer accepts a ``folders`` kwarg at all.
        p = await svc.create(
            user_id=uid,
            name="Book",
            description="Q3 outline",
            system_prompt="Write tightly.",
        )
        assert p.name == "Book"
        assert p.description == "Q3 outline"
        assert p.system_prompt == "Write tightly."
    finally:
        await eng.dispose()


# The folder de-dup + corrupt-shape tests + folders=None tests were
# DELETED alongside the removed folders feature. ``create``/``update``
# no longer accept a ``folders`` kwarg at all.


@pytest.mark.anyio
async def test_create_rejects_empty_name(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        svc = ProjectsService(engine=eng)
        with pytest.raises(InvalidProjectFieldError):
            await svc.create(user_id=uid, name="")
        with pytest.raises(InvalidProjectFieldError):
            await svc.create(user_id=uid, name="   ")
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_create_rejects_nul_bytes(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        svc = ProjectsService(engine=eng)
        with pytest.raises(InvalidProjectFieldError):
            await svc.create(user_id=uid, name="okay", description="bad\x00")
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_create_rejects_oversized_name(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        svc = ProjectsService(engine=eng)
        with pytest.raises(InvalidProjectFieldError):
            await svc.create(user_id=uid, name="x" * 257)
    finally:
        await eng.dispose()


# ---------------------------------------------------------------------------
# list_for_user
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_list_empty_for_new_user(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        svc = ProjectsService(engine=eng)
        assert await svc.list_for_user(uid) == []
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_list_user_isolation(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid_a = await _insert_user(eng, "alice")
        uid_b = await _insert_user(eng, "bob")
        svc = ProjectsService(engine=eng)
        await svc.create(user_id=uid_a, name="A-one")
        await svc.create(user_id=uid_a, name="A-two")
        await svc.create(user_id=uid_b, name="B-one")
        a_list = await svc.list_for_user(uid_a)
        b_list = await svc.list_for_user(uid_b)
        assert {p.name for p in a_list} == {"A-one", "A-two"}
        assert {p.name for p in b_list} == {"B-one"}
    finally:
        await eng.dispose()


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_returns_project(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        svc = ProjectsService(engine=eng)
        created = await svc.create(user_id=uid, name="Proj")
        fetched = await svc.get(user_id=uid, project_id=created.id)
        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.name == "Proj"
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_get_returns_none_for_other_user(tmp_path: Path) -> None:
    """Cross-user access returns None — never leak project existence."""
    eng = await _make_engine(tmp_path)
    try:
        uid_a = await _insert_user(eng, "alice")
        uid_b = await _insert_user(eng, "bob")
        svc = ProjectsService(engine=eng)
        a_proj = await svc.create(user_id=uid_a, name="alice's")
        assert (
            await svc.get(user_id=uid_b, project_id=a_proj.id) is None
        )
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_get_returns_none_for_missing(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        svc = ProjectsService(engine=eng)
        assert await svc.get(user_id=uid, project_id=99999) is None
    finally:
        await eng.dispose()


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_update_name_only(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        svc = ProjectsService(engine=eng)
        p = await svc.create(
            user_id=uid,
            name="Old",
            description="kept",
            system_prompt="kept",
        )
        # Sleep ~2ms so time.time() advances strictly — without this, a
        # fast SQLite path returns the same float and a regression that
        # dropped the updated_at bump would pass the test.
        import asyncio

        await asyncio.sleep(0.002)
        updated = await svc.update(
            user_id=uid, project_id=p.id, name="New"
        )
        assert updated is not None
        assert updated.name == "New"
        # Untouched fields preserve their values.
        assert updated.description == "kept"
        assert updated.system_prompt == "kept"
        # STRICT > so a regression that drops the updated_at bump fails.
        assert updated.updated_at > p.updated_at
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_update_clears_description(tmp_path: Path) -> None:
    """Empty string is a real value (clears)."""
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        svc = ProjectsService(engine=eng)
        p = await svc.create(
            user_id=uid, name="Xyz", description="had text"
        )
        updated = await svc.update(
            user_id=uid, project_id=p.id, description=""
        )
        assert updated is not None
        assert updated.description == ""
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_update_no_op_returns_current(tmp_path: Path) -> None:
    """update() with all-None args returns the current row, untouched."""
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        svc = ProjectsService(engine=eng)
        p = await svc.create(user_id=uid, name="Xyz")
        out = await svc.update(user_id=uid, project_id=p.id)
        assert out is not None
        assert out.id == p.id
        assert out.updated_at == p.updated_at
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_update_other_users_project_returns_none(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid_a = await _insert_user(eng, "alice")
        uid_b = await _insert_user(eng, "bob")
        svc = ProjectsService(engine=eng)
        a_proj = await svc.create(user_id=uid_a, name="Alice")
        assert (
            await svc.update(
                user_id=uid_b, project_id=a_proj.id, name="hijack"
            )
            is None
        )
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_update_validates_new_name(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        svc = ProjectsService(engine=eng)
        p = await svc.create(user_id=uid, name="okay")
        with pytest.raises(InvalidProjectFieldError):
            await svc.update(user_id=uid, project_id=p.id, name="")
    finally:
        await eng.dispose()


# ---------------------------------------------------------------------------
# update — default_model_id / rag_threshold writer
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_update_sets_default_model_id(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        svc = ProjectsService(engine=eng)
        p = await svc.create(user_id=uid, name="Proj")
        assert p.default_model_id is None
        updated = await svc.update(
            user_id=uid, project_id=p.id, default_model_id="qwen3.6-35b-a3b"
        )
        assert updated is not None
        assert updated.default_model_id == "qwen3.6-35b-a3b"
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_update_clears_default_model_id(tmp_path: Path) -> None:
    """None means 'don't touch' for this kwarg — clearing to NULL goes
    through the `clear` set instead."""
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        svc = ProjectsService(engine=eng)
        p = await svc.create(user_id=uid, name="Proj")
        await svc.update(
            user_id=uid, project_id=p.id, default_model_id="pinned-model"
        )
        cleared = await svc.update(
            user_id=uid,
            project_id=p.id,
            clear=frozenset({"default_model_id"}),
        )
        assert cleared is not None
        assert cleared.default_model_id is None
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_update_rejects_invalid_model_id(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        svc = ProjectsService(engine=eng)
        p = await svc.create(user_id=uid, name="Proj")
        with pytest.raises(InvalidProjectFieldError):
            await svc.update(
                user_id=uid, project_id=p.id, default_model_id="bad\x00id"
            )
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_update_sets_rag_threshold(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        svc = ProjectsService(engine=eng)
        p = await svc.create(user_id=uid, name="Proj")
        assert p.rag_threshold is None
        updated = await svc.update(
            user_id=uid, project_id=p.id, rag_threshold=4096
        )
        assert updated is not None
        assert updated.rag_threshold == 4096
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_update_clears_rag_threshold(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        svc = ProjectsService(engine=eng)
        p = await svc.create(user_id=uid, name="Proj")
        await svc.update(user_id=uid, project_id=p.id, rag_threshold=4096)
        cleared = await svc.update(
            user_id=uid,
            project_id=p.id,
            clear=frozenset({"rag_threshold"}),
        )
        assert cleared is not None
        assert cleared.rag_threshold is None
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_update_rejects_negative_rag_threshold(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        svc = ProjectsService(engine=eng)
        p = await svc.create(user_id=uid, name="Proj")
        with pytest.raises(InvalidProjectFieldError):
            await svc.update(user_id=uid, project_id=p.id, rag_threshold=-1)
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_update_rag_threshold_zero_is_valid(tmp_path: Path) -> None:
    """0 is a real value (forces HYBRID at read time), not a clear signal."""
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        svc = ProjectsService(engine=eng)
        p = await svc.create(user_id=uid, name="Proj")
        updated = await svc.update(user_id=uid, project_id=p.id, rag_threshold=0)
        assert updated is not None
        assert updated.rag_threshold == 0
    finally:
        await eng.dispose()


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_delete_succeeds(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        svc = ProjectsService(engine=eng)
        p = await svc.create(user_id=uid, name="goner")
        assert await svc.delete(user_id=uid, project_id=p.id) is True
        assert await svc.get(user_id=uid, project_id=p.id) is None
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_delete_missing_returns_false(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        svc = ProjectsService(engine=eng)
        assert (
            await svc.delete(user_id=uid, project_id=99999) is False
        )
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_delete_other_users_project_returns_false(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid_a = await _insert_user(eng, "alice")
        uid_b = await _insert_user(eng, "bob")
        svc = ProjectsService(engine=eng)
        a_proj = await svc.create(user_id=uid_a, name="alice's")
        assert (
            await svc.delete(user_id=uid_b, project_id=a_proj.id) is False
        )
        # Still there for alice.
        assert (
            await svc.get(user_id=uid_a, project_id=a_proj.id) is not None
        )
    finally:
        await eng.dispose()


# ---------------------------------------------------------------------------
# _row_to_project defensive shape handling
# ---------------------------------------------------------------------------


# The ``_row_to_project`` defensive shape handlers for the dropped
# ``folders`` column were REMOVED alongside the column itself
# (migration 0023b). The corrupt-shape + None tests that used to
# exercise the fallback are gone; ``_row_to_project`` no longer reads
# ``folders`` at all.


def _row_stub(*, archived_at: datetime | None, summary_updated_at: datetime | None):
    """A minimal row-like object covering every field _row_to_project reads."""
    return SimpleNamespace(
        id=1,
        user_id=1,
        name="Proj",
        description="",
        system_prompt="",
        embedding_model_id=None,
        default_model_id=None,
        rag_threshold=None,
        created_at=0.0,
        updated_at=0.0,
        archived_at=archived_at,
        summary="",
        summary_updated_at=summary_updated_at,
        summary_message_watermark=0,
    )


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="time.tzset() is POSIX-only")
def test_row_to_project_archived_at_epoch_is_utc_not_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A naive archived_at (SQLite's round-trip of a tz-aware write) must
    surface the same epoch regardless of the host's local timezone.

    aiosqlite/SQLAlchemy's SQLite dialect does not round-trip the
    ``+00:00`` offset on a ``DateTime(timezone=True)`` column: the
    value written by set_archived() as ``datetime.now(UTC)`` comes
    back timezone-naive, with UTC wall-clock components. Calling
    ``.timestamp()`` directly on that naive value makes Python treat
    it as host-LOCAL time, skewing the surfaced epoch by the host's
    UTC offset.
    """
    monkeypatch.setenv("TZ", "America/New_York")  # fixed non-zero UTC offset
    time.tzset()
    try:
        naive_utc_wallclock = datetime(2026, 1, 1, 12, 0, 0)  # no tzinfo
        expected_epoch = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC).timestamp()
        row = _row_stub(
            archived_at=naive_utc_wallclock, summary_updated_at=naive_utc_wallclock
        )
        project = _row_to_project(row)
        assert project.archived_at == pytest.approx(expected_epoch)
        assert project.summary_updated_at == pytest.approx(expected_epoch)
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time.tzset()


def test_row_to_project_archived_at_none_stays_none() -> None:
    row = _row_stub(archived_at=None, summary_updated_at=None)
    project = _row_to_project(row)
    assert project.archived_at is None
    assert project.summary_updated_at is None


# ---------------------------------------------------------------------------
# set_archived / list_for_user(include_archived=)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_set_archived_true_sets_archived_at(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        svc = ProjectsService(engine=eng)
        p = await svc.create(user_id=uid, name="Proj")
        assert p.archived_at is None
        archived = await svc.set_archived(
            user_id=uid, project_id=p.id, archived=True
        )
        assert archived is not None
        assert archived.archived_at is not None
        assert archived.archived_at > 0
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_set_archived_false_clears_archived_at(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        svc = ProjectsService(engine=eng)
        p = await svc.create(user_id=uid, name="Proj")
        await svc.set_archived(user_id=uid, project_id=p.id, archived=True)
        unarchived = await svc.set_archived(
            user_id=uid, project_id=p.id, archived=False
        )
        assert unarchived is not None
        assert unarchived.archived_at is None
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_set_archived_other_users_project_returns_none(
    tmp_path: Path,
) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid_a = await _insert_user(eng, "alice")
        uid_b = await _insert_user(eng, "bob")
        svc = ProjectsService(engine=eng)
        a_proj = await svc.create(user_id=uid_a, name="alice's")
        assert (
            await svc.set_archived(
                user_id=uid_b, project_id=a_proj.id, archived=True
            )
            is None
        )
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_list_for_user_excludes_archived_by_default(
    tmp_path: Path,
) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        svc = ProjectsService(engine=eng)
        active = await svc.create(user_id=uid, name="active")
        archived = await svc.create(user_id=uid, name="archived")
        await svc.set_archived(
            user_id=uid, project_id=archived.id, archived=True
        )
        listed = await svc.list_for_user(uid)
        assert {p.name for p in listed} == {"active"}
        assert active.id in {p.id for p in listed}
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_list_for_user_include_archived_returns_both(
    tmp_path: Path,
) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        svc = ProjectsService(engine=eng)
        await svc.create(user_id=uid, name="active")
        archived = await svc.create(user_id=uid, name="archived")
        await svc.set_archived(
            user_id=uid, project_id=archived.id, archived=True
        )
        listed = await svc.list_for_user(uid, include_archived=True)
        assert {p.name for p in listed} == {"active", "archived"}
    finally:
        await eng.dispose()


# ---------------------------------------------------------------------------
# set_summary — rolling auto-summary
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_new_project_has_empty_summary_defaults(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        svc = ProjectsService(engine=eng)
        p = await svc.create(user_id=uid, name="Proj")
        assert p.summary == ""
        assert p.summary_updated_at is None
        assert p.summary_message_watermark == 0
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_set_summary_persists_all_three_fields(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        svc = ProjectsService(engine=eng)
        p = await svc.create(user_id=uid, name="Proj")
        updated = await svc.set_summary(
            user_id=uid,
            project_id=p.id,
            summary="The team is building a rolling summarizer.",
            message_watermark=12,
        )
        assert updated is not None
        assert updated.summary == "The team is building a rolling summarizer."
        assert updated.summary_updated_at is not None
        assert updated.summary_updated_at > 0
        assert updated.summary_message_watermark == 12
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_set_summary_does_not_bump_updated_at(tmp_path: Path) -> None:
    """A background summary refresh must not reorder the project list —
    ``updated_at`` stays reserved for explicit user edits/archive actions."""
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        svc = ProjectsService(engine=eng)
        p = await svc.create(user_id=uid, name="Proj")
        updated = await svc.set_summary(
            user_id=uid, project_id=p.id, summary="S", message_watermark=1
        )
        assert updated is not None
        assert updated.updated_at == p.updated_at
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_set_summary_other_users_project_returns_none(
    tmp_path: Path,
) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid_a = await _insert_user(eng, "alice")
        uid_b = await _insert_user(eng, "bob")
        svc = ProjectsService(engine=eng)
        a_proj = await svc.create(user_id=uid_a, name="alice's")
        assert (
            await svc.set_summary(
                user_id=uid_b,
                project_id=a_proj.id,
                summary="hijacked",
                message_watermark=1,
            )
            is None
        )
    finally:
        await eng.dispose()
