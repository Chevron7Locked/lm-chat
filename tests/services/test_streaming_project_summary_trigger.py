# SPDX-License-Identifier: Apache-2.0
"""``StreamingService._safe_refresh_project_summary`` — the throttled,
fire-and-forget trigger fired after a completed PROJECT-chat turn
(Wave 3 #10).

Covers:
- Skips entirely when there's no ``projects_service`` wired.
- Skips when the memory-distillation enable flag is off (the auto-refresh
  reuses that flag rather than adding a new admin toggle).
- Skips when the project doesn't resolve (deleted/cross-user).
- Throttle: doesn't regenerate when the project already has a summary and
  hasn't accumulated enough new messages; DOES regenerate when it has no
  summary yet, or once the threshold is met.
- Never raises even if an internal step blows up — the chat already
  streamed successfully.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

import lmchat.services.streaming_service as ss
from lmchat.db.schema import chats, messages, metadata, users
from lmchat.services import project_summary_service as pss
from lmchat.services.projects_service import ProjectsService


async def _make_engine(tmp_path: Path) -> AsyncEngine:
    eng = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/streaming_project_summary.db",
        pool_pre_ping=True,
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


async def _insert_chat(eng: AsyncEngine, *, user_id: int, project_id: int) -> int:
    async with eng.begin() as conn:
        result = await conn.execute(
            insert(chats).values(user_id=user_id, project_id=project_id, title="c")
        )
        return int(result.inserted_primary_key[0])  # type: ignore[index]


async def _insert_messages(eng: AsyncEngine, *, chat_id: int, n: int) -> None:
    async with eng.begin() as conn:
        for i in range(n):
            await conn.execute(
                insert(messages).values(
                    chat_id=chat_id, role="user", content=f"msg {i}"
                )
            )


def _make_streamer(engine: AsyncEngine, *, projects_service: object | None) -> ss.StreamingService:
    return ss.StreamingService(
        engine=engine,
        lm_client=AsyncMock(),
        memory_service=AsyncMock(),
        chat_locks={},
        projects_service=projects_service,
    )


@pytest.mark.anyio
async def test_skips_when_no_projects_service(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        streamer = _make_streamer(eng, projects_service=None)
        # Must not raise, and refresh_project_summary must never be called.
        calls = 0

        async def _fake_refresh(**_kwargs: object) -> None:
            nonlocal calls
            calls += 1

        with patch.object(ss, "refresh_project_summary", _fake_refresh):
            await streamer._safe_refresh_project_summary(
                user_id=1, project_id=1, model_id="m"
            )
        assert calls == 0
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_skips_when_distillation_flag_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        proj_svc = ProjectsService(engine=eng)
        proj = await proj_svc.create(user_id=uid, name="Proj")
        streamer = _make_streamer(eng, projects_service=proj_svc)

        async def _disabled(_engine: object) -> bool:
            return False

        monkeypatch.setattr(ss, "_resolve_memory_distillation_enabled", _disabled)

        calls = 0

        async def _fake_refresh(**_kwargs: object) -> None:
            nonlocal calls
            calls += 1

        monkeypatch.setattr(ss, "refresh_project_summary", _fake_refresh)

        await streamer._safe_refresh_project_summary(
            user_id=uid, project_id=proj.id, model_id="m"
        )
        assert calls == 0
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_skips_when_project_not_found(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        proj_svc = ProjectsService(engine=eng)
        streamer = _make_streamer(eng, projects_service=proj_svc)

        calls = 0

        async def _fake_refresh(**_kwargs: object) -> None:
            nonlocal calls
            calls += 1

        with patch.object(ss, "refresh_project_summary", _fake_refresh):
            await streamer._safe_refresh_project_summary(
                user_id=uid, project_id=99999, model_id="m"
            )
        assert calls == 0
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_throttle_skips_below_threshold(tmp_path: Path) -> None:
    """A project that already has a summary and hasn't accumulated
    ``_REFRESH_EVERY`` new messages does NOT regenerate."""
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        proj_svc = ProjectsService(engine=eng)
        proj = await proj_svc.create(user_id=uid, name="Proj")
        await proj_svc.set_summary(
            user_id=uid, project_id=proj.id, summary="Existing.", message_watermark=2
        )
        chat_id = await _insert_chat(eng, user_id=uid, project_id=proj.id)
        # Only 1 new message since the watermark of 2 — below the threshold.
        await _insert_messages(eng, chat_id=chat_id, n=3)

        streamer = _make_streamer(eng, projects_service=proj_svc)

        calls = 0

        async def _fake_refresh(**_kwargs: object) -> None:
            nonlocal calls
            calls += 1

        with patch.object(ss, "refresh_project_summary", _fake_refresh):
            await streamer._safe_refresh_project_summary(
                user_id=uid, project_id=proj.id, model_id="m"
            )
        assert calls == 0
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_fires_when_no_summary_yet(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        proj_svc = ProjectsService(engine=eng)
        proj = await proj_svc.create(user_id=uid, name="Proj")
        chat_id = await _insert_chat(eng, user_id=uid, project_id=proj.id)
        await _insert_messages(eng, chat_id=chat_id, n=1)

        streamer = _make_streamer(eng, projects_service=proj_svc)

        captured: dict[str, object] = {}

        async def _fake_refresh(**kwargs: object) -> None:
            captured.update(kwargs)

        with patch.object(ss, "refresh_project_summary", _fake_refresh):
            await streamer._safe_refresh_project_summary(
                user_id=uid, project_id=proj.id, model_id="chat-model-x"
            )
        assert captured.get("project_id") == proj.id
        assert captured.get("user_id") == uid
        assert captured.get("hint_model_id") == "chat-model-x"
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_fires_once_threshold_met(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        proj_svc = ProjectsService(engine=eng)
        proj = await proj_svc.create(user_id=uid, name="Proj")
        await proj_svc.set_summary(
            user_id=uid, project_id=proj.id, summary="Existing.", message_watermark=0
        )
        chat_id = await _insert_chat(eng, user_id=uid, project_id=proj.id)
        await _insert_messages(eng, chat_id=chat_id, n=pss._REFRESH_EVERY)

        streamer = _make_streamer(eng, projects_service=proj_svc)

        calls = 0

        async def _fake_refresh(**_kwargs: object) -> None:
            nonlocal calls
            calls += 1

        with patch.object(ss, "refresh_project_summary", _fake_refresh):
            await streamer._safe_refresh_project_summary(
                user_id=uid, project_id=proj.id, model_id="m"
            )
        assert calls == 1
    finally:
        await eng.dispose()


@pytest.mark.anyio
async def test_never_raises_on_internal_failure(tmp_path: Path) -> None:
    """Defence-in-depth: even if the underlying refresh blows up, the
    wrapper swallows it (the chat already streamed successfully)."""
    eng = await _make_engine(tmp_path)
    try:
        uid = await _insert_user(eng)
        proj_svc = ProjectsService(engine=eng)
        proj = await proj_svc.create(user_id=uid, name="Proj")
        chat_id = await _insert_chat(eng, user_id=uid, project_id=proj.id)
        await _insert_messages(eng, chat_id=chat_id, n=1)

        streamer = _make_streamer(eng, projects_service=proj_svc)

        async def _boom(**_kwargs: object) -> None:
            raise RuntimeError("boom")

        with patch.object(ss, "refresh_project_summary", _boom):
            # Must not raise.
            await streamer._safe_refresh_project_summary(
                user_id=uid, project_id=proj.id, model_id="m"
            )
    finally:
        await eng.dispose()
