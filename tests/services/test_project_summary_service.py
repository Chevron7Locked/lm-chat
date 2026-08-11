# SPDX-License-Identifier: Apache-2.0
"""Tests for the rolling project auto-summary (Wave 3 #10).

Covers:
- ``should_refresh``: the throttle — no summary yet fires immediately;
  below-threshold new-message-count doesn't; at/above threshold does.
- ``count_project_messages``: counts across every chat in the project,
  scoped to the owning user, ignoring other users'/projects' messages.
- ``refresh_project_summary``: gathers content, calls the OOB summarizer
  (mocked — no live model required), persists via
  ``ProjectsService.set_summary``, and is fail-soft throughout (unknown
  project → None; empty project → unchanged; OOB failure → unchanged;
  no loaded model → unchanged).
- ``_resolve_summary_model``: prefers the admin-pinned background model,
  falls back to a hint model id, and — when neither is available — scans
  for any other loaded non-coder/embedding LLM instead of returning "".
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import chats, messages, metadata, users
from lmchat.services import project_summary_service as pss
from lmchat.services.models_service import Capabilities, ModelInfo, ModelsService
from lmchat.services.projects_service import Project, ProjectsService


async def _make_engine(tmp_path: Path) -> AsyncEngine:
    eng = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/project_summary.db", pool_pre_ping=True
    )
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return eng


async def _insert_user(eng: AsyncEngine, username: str = "alice") -> int:
    async with eng.begin() as conn:
        result = await conn.execute(
            insert(users).values(username=username, password_hash="scrypt$dummy")
        )
        return int(result.inserted_primary_key[0])  # type: ignore[index]


async def _insert_chat(
    eng: AsyncEngine, *, user_id: int, project_id: int, title: str = "c"
) -> int:
    async with eng.begin() as conn:
        result = await conn.execute(
            insert(chats).values(user_id=user_id, project_id=project_id, title=title)
        )
        return int(result.inserted_primary_key[0])  # type: ignore[index]


async def _insert_message(
    eng: AsyncEngine, *, chat_id: int, role: str, content: str
) -> None:
    async with eng.begin() as conn:
        await conn.execute(
            insert(messages).values(chat_id=chat_id, role=role, content=content)
        )


def _make_project(**overrides: object) -> Project:
    base: dict[str, object] = {
        "id": 1,
        "user_id": 1,
        "name": "P",
        "description": "",
        "system_prompt": "",
        "embedding_model_id": None,
        "default_model_id": None,
        "rag_threshold": None,
        "created_at": 0.0,
        "updated_at": 0.0,
        "summary": "",
        "summary_updated_at": None,
        "summary_message_watermark": 0,
    }
    base.update(overrides)
    return Project(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# should_refresh
# ---------------------------------------------------------------------------


def test_should_refresh_true_when_no_summary_yet() -> None:
    project = _make_project(summary="", summary_message_watermark=0)
    assert pss.should_refresh(project, current_message_count=1) is True


def test_should_refresh_false_below_threshold() -> None:
    project = _make_project(summary="Existing.", summary_message_watermark=10)
    # Only 3 new messages since the watermark — below the default threshold.
    assert pss.should_refresh(project, current_message_count=13) is False


def test_should_refresh_true_at_threshold() -> None:
    project = _make_project(summary="Existing.", summary_message_watermark=10)
    assert (
        pss.should_refresh(
            project, current_message_count=10 + pss._REFRESH_EVERY
        )
        is True
    )


# ---------------------------------------------------------------------------
# count_project_messages
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_count_project_messages_counts_across_chats(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        proj_svc = ProjectsService(engine=eng)
        proj = await proj_svc.create(user_id=uid, name="Proj")
        chat_a = await _insert_chat(eng, user_id=uid, project_id=proj.id)
        chat_b = await _insert_chat(eng, user_id=uid, project_id=proj.id)
        await _insert_message(eng, chat_id=chat_a, role="user", content="hi")
        await _insert_message(eng, chat_id=chat_a, role="assistant", content="hello")
        await _insert_message(eng, chat_id=chat_b, role="user", content="one more")

        count = await pss.count_project_messages(
            eng, user_id=uid, project_id=proj.id
        )
        assert count == 3
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_count_project_messages_ignores_other_projects(
    tmp_path: Path,
) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        proj_svc = ProjectsService(engine=eng)
        proj_a = await proj_svc.create(user_id=uid, name="ProjA")
        proj_b = await proj_svc.create(user_id=uid, name="ProjB")
        chat_a = await _insert_chat(eng, user_id=uid, project_id=proj_a.id)
        chat_b = await _insert_chat(eng, user_id=uid, project_id=proj_b.id)
        await _insert_message(eng, chat_id=chat_a, role="user", content="a")
        await _insert_message(eng, chat_id=chat_b, role="user", content="b")

        assert (
            await pss.count_project_messages(eng, user_id=uid, project_id=proj_a.id)
            == 1
        )
    finally:
        await eng.dispose()


# ---------------------------------------------------------------------------
# refresh_project_summary
# ---------------------------------------------------------------------------


def _mock_models_service(loaded_key: str = "some-llm") -> ModelsService:
    from lmchat.services.models_service import ResolvedModel

    mock = AsyncMock(spec=ModelsService)
    mock.list_loaded.return_value = [
        ModelInfo(
            key=loaded_key,
            type="llm",
            capabilities=Capabilities(vision=False, trained_for_tool_use=True),
            loaded_instance_ids=[f"{loaded_key}@instance"],
        )
    ]
    mock.resolve_to_loaded_or_fallback.return_value = ResolvedModel(
        wire_id=f"{loaded_key}@instance", requested=loaded_key
    )
    return mock


@pytest.mark.anyio
async def test_refresh_project_summary_returns_none_for_unknown_project(
    tmp_path: Path,
) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        proj_svc = ProjectsService(engine=eng)
        result = await pss.refresh_project_summary(
            engine=eng,
            projects_service=proj_svc,
            lm_client=AsyncMock(),
            models_service=None,
            user_id=uid,
            project_id=99999,
        )
        assert result is None
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_refresh_project_summary_empty_project_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No chats, no existing summary — nothing to gather; the OOB call
    must never even be attempted."""
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        proj_svc = ProjectsService(engine=eng)
        proj = await proj_svc.create(user_id=uid, name="Empty")

        oob_calls = 0

        async def _fake_oob(**_kwargs: object) -> str:
            nonlocal oob_calls
            oob_calls += 1
            return "should not be reached"

        monkeypatch.setattr(pss, "_summarize_oob", _fake_oob)

        result = await pss.refresh_project_summary(
            engine=eng,
            projects_service=proj_svc,
            lm_client=AsyncMock(),
            models_service=None,
            user_id=uid,
            project_id=proj.id,
        )
        assert result is not None
        assert result.summary == ""
        assert oob_calls == 0
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_refresh_project_summary_gathers_and_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED-ON-REVERT: with real chat content, the OOB summarizer is
    called with the gathered lines + existing summary, and its return
    value is persisted (summary + watermark bumped to the live count)."""
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        proj_svc = ProjectsService(engine=eng)
        proj = await proj_svc.create(user_id=uid, name="Research")
        chat_id = await _insert_chat(eng, user_id=uid, project_id=proj.id)
        await _insert_message(eng, chat_id=chat_id, role="user", content="Let's study dark energy.")
        await _insert_message(
            eng, chat_id=chat_id, role="assistant", content="Sure, where should we start?"
        )

        captured: dict[str, object] = {}

        async def _fake_oob(**kwargs: object) -> str:
            captured.update(kwargs)
            return "The project is about dark energy research."

        monkeypatch.setattr(pss, "_summarize_oob", _fake_oob)

        result = await pss.refresh_project_summary(
            engine=eng,
            projects_service=proj_svc,
            lm_client=AsyncMock(),
            models_service=_mock_models_service(),
            user_id=uid,
            project_id=proj.id,
            hint_model_id="chat-model",
        )

        assert result is not None
        assert result.summary == "The project is about dark energy research."
        assert result.summary_updated_at is not None
        assert result.summary_message_watermark == 2

        assert "Let's study dark energy." in "\n".join(
            captured["conversation_lines"]  # type: ignore[arg-type]
        )
        assert captured["existing_summary"] == ""
        assert captured["model"] == "some-llm@instance"

        # Re-fetching the project confirms the write landed, not just the
        # in-memory return value.
        refetched = await proj_svc.get(user_id=uid, project_id=proj.id)
        assert refetched is not None
        assert refetched.summary == "The project is about dark energy research."
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_refresh_project_summary_carries_forward_existing_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second regeneration passes the PRIOR summary to the OOB call —
    this is a rolling digest, not a from-scratch rewrite each time."""
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        proj_svc = ProjectsService(engine=eng)
        proj = await proj_svc.create(user_id=uid, name="Research")
        await proj_svc.set_summary(
            user_id=uid,
            project_id=proj.id,
            summary="Prior digest of the project so far.",
            message_watermark=1,
        )
        chat_id = await _insert_chat(eng, user_id=uid, project_id=proj.id)
        await _insert_message(eng, chat_id=chat_id, role="user", content="New update.")

        captured: dict[str, object] = {}

        async def _fake_oob(**kwargs: object) -> str:
            captured.update(kwargs)
            return "Updated digest."

        monkeypatch.setattr(pss, "_summarize_oob", _fake_oob)

        await pss.refresh_project_summary(
            engine=eng,
            projects_service=proj_svc,
            lm_client=AsyncMock(),
            models_service=_mock_models_service(),
            user_id=uid,
            project_id=proj.id,
        )

        assert captured["existing_summary"] == "Prior digest of the project so far."
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_refresh_project_summary_oob_failure_leaves_summary_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-soft: an OOB call returning "" (its own internal-error
    contract — see ``_summarize_oob``) must never raise into the caller
    and must leave the prior summary untouched."""
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        proj_svc = ProjectsService(engine=eng)
        proj = await proj_svc.create(user_id=uid, name="Proj")
        chat_id = await _insert_chat(eng, user_id=uid, project_id=proj.id)
        await _insert_message(eng, chat_id=chat_id, role="user", content="hi")

        async def _fake_oob(**_kwargs: object) -> str:
            return ""

        monkeypatch.setattr(pss, "_summarize_oob", _fake_oob)

        result = await pss.refresh_project_summary(
            engine=eng,
            projects_service=proj_svc,
            lm_client=AsyncMock(),
            models_service=_mock_models_service(),
            user_id=uid,
            project_id=proj.id,
        )
        assert result is not None
        assert result.summary == ""
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_refresh_project_summary_skips_when_no_model_loaded(
    tmp_path: Path,
) -> None:
    """No admin-pinned background model, no hint, nothing loaded — must
    not raise, and the project comes back unchanged."""
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        proj_svc = ProjectsService(engine=eng)
        proj = await proj_svc.create(user_id=uid, name="Proj")
        chat_id = await _insert_chat(eng, user_id=uid, project_id=proj.id)
        await _insert_message(eng, chat_id=chat_id, role="user", content="hi")

        mock_models = AsyncMock(spec=ModelsService)
        mock_models.list_loaded.return_value = []

        result = await pss.refresh_project_summary(
            engine=eng,
            projects_service=proj_svc,
            lm_client=AsyncMock(),
            models_service=mock_models,
            user_id=uid,
            project_id=proj.id,
        )
        assert result is not None
        assert result.summary == ""
    finally:
        await eng.dispose()


# ---------------------------------------------------------------------------
# _resolve_summary_model
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_resolve_summary_model_falls_back_to_any_loaded_llm(
    tmp_path: Path,
) -> None:
    """No preferred background model configured and no hint model id —
    must scan for ANY other loaded non-coder/embed LLM rather than
    returning the empty-string shortcut."""
    eng = await _make_engine(tmp_path)
    try:
        wire_id = await pss._resolve_summary_model(
            engine=eng,
            models_service=_mock_models_service("qwen-general"),
            hint_model_id=None,
        )
        assert wire_id == "qwen-general@instance"
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_resolve_summary_model_none_when_nothing_loaded(
    tmp_path: Path,
) -> None:
    eng = await _make_engine(tmp_path)
    try:
        mock_models = AsyncMock(spec=ModelsService)
        mock_models.list_loaded.return_value = []
        wire_id = await pss._resolve_summary_model(
            engine=eng, models_service=mock_models, hint_model_id=None
        )
        assert wire_id is None
    finally:
        await eng.dispose()
