# SPDX-License-Identifier: Apache-2.0
"""Wave 3 (#10) — the project's rolling auto-summary reaches the wire.

Mirrors the real end-to-end harness in ``test_streaming_a1_composition.py``
(drives ``StreamingService.stream_chat`` with a stubbed ``lm_client`` and
captures the outbound ``system_prompt``) rather than re-implementing the
composition logic inline, so a regression that drops or reorders the
injection is caught at the same layer the A1 project_prompt regression was.

Composition order asserted: ``[project_summary][project_prompt][chat_prompt]``
— the summary is ambient accumulated context, prepended ahead of the
project's explicit ``system_prompt`` so instructions stay closest to the
chat's own prompt (see ``streaming_service.py`` around the A1 hoist).
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import metadata
from lmchat.lmstudio.types import (
    CanonicalChatRequest,
    CanonicalEvent,
    CanonicalInputBlock,
)
from lmchat.services.streaming_service import ChatStreamRequest, StreamingService

PROJECT_MARKER = "PROJECT_PROMPT_MARKER_42"
CHAT_MARKER = "CHAT_PROMPT_MARKER_99"
SUMMARY_TEXT = "The team has been researching dark energy for three weeks."


@pytest.fixture
async def engine() -> AsyncEngine:
    e = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with e.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return e


def _make_events(content: str = "ack") -> list[CanonicalEvent]:
    return [
        CanonicalEvent(type="chat.start"),
        CanonicalEvent(type="message.start"),
        CanonicalEvent(type="message.delta", content=content),
        CanonicalEvent(type="message.end"),
        CanonicalEvent(type="chat.end", response_id="r-summary"),
    ]


def _mock_request() -> MagicMock:
    from tests.services.conftest import make_disconnect_receive

    r = MagicMock()
    r.receive = make_disconnect_receive(False)
    return r


def _mock_user(user_id: int = 1) -> MagicMock:
    u = MagicMock()
    u.id = user_id
    return u


def _make_payload() -> ChatStreamRequest:
    canonical = CanonicalChatRequest(
        model="qwen3.6-35b-a3b",
        input=[CanonicalInputBlock(type="text", content="ping")],
        system_prompt=CHAT_MARKER,
    )
    return ChatStreamRequest(chat_id=1, payload=canonical)


def _build_service(
    engine: AsyncEngine, lm_client: Any, *, summary: str
) -> StreamingService:
    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)
    proj_svc = MagicMock()
    proj_svc.get = AsyncMock(
        return_value=SimpleNamespace(
            id=42,
            user_id=1,
            name="P",
            description="",
            system_prompt=PROJECT_MARKER,
            summary=summary,
            created_at=0.0,
            updated_at=0.0,
        )
    )
    return StreamingService(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        chat_locks={},
        idle_timeout_sec=60,
        projects_service=proj_svc,
        embedding_client=None,
        models_service=None,
    )


async def _drain(stream: AsyncIterator[Any]) -> list[Any]:
    return [ev async for ev in stream]


async def _capture_outbound_sys_prompt(engine: AsyncEngine, *, summary: str) -> str:
    from sqlalchemy import insert

    from lmchat.db.schema import chats, projects

    async with engine.begin() as conn:
        await conn.execute(
            insert(projects).values(
                id=42,
                user_id=1,
                name="P",
                description="",
                system_prompt=PROJECT_MARKER,
                summary=summary,
                created_at=0.0,
                updated_at=0.0,
            )
        )
        await conn.execute(
            insert(chats).values(id=1, user_id=1, title="t", project_id=42)
        )

    captured: dict[str, Any] = {}

    async def _fake_stream(*args: Any, **kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        captured["payload"] = kwargs.get("request") or (args[0] if args else None)
        for ev in _make_events():
            yield ev

    lm_client = MagicMock()
    lm_client.stream = _fake_stream

    svc = _build_service(engine, lm_client, summary=summary)

    with patch("lmchat.config.get_settings") as mock_settings:
        cfg = MagicMock()
        cfg.lm_chat_followups_enabled = False
        mock_settings.return_value = cfg
        await _drain(
            svc.stream_chat(
                chat_id=1,
                user=_mock_user(1),
                payload=_make_payload(),
                request=_mock_request(),
            )
        )

    sent = captured.get("payload")
    assert sent is not None, "lm_client.stream was not called"
    return getattr(sent, "system_prompt", "") or ""


@pytest.mark.asyncio
async def test_project_summary_reaches_the_wire(engine: AsyncEngine) -> None:
    sent = await _capture_outbound_sys_prompt(engine, summary=SUMMARY_TEXT)
    assert "Project summary:" in sent, f"summary label missing: {sent!r}"
    assert SUMMARY_TEXT in sent, f"summary text missing: {sent!r}"
    assert PROJECT_MARKER in sent
    assert CHAT_MARKER in sent


@pytest.mark.asyncio
async def test_project_summary_precedes_project_prompt_and_chat_prompt(
    engine: AsyncEngine,
) -> None:
    sent = await _capture_outbound_sys_prompt(engine, summary=SUMMARY_TEXT)
    summary_idx = sent.find(SUMMARY_TEXT)
    proj_idx = sent.find(PROJECT_MARKER)
    chat_idx = sent.find(CHAT_MARKER)
    assert summary_idx >= 0 and proj_idx >= 0 and chat_idx >= 0, (
        f"summary={summary_idx} proj={proj_idx} chat={chat_idx} sent={sent!r}"
    )
    assert summary_idx < proj_idx < chat_idx, (
        f"expected [summary][project_prompt][chat_prompt] order; "
        f"got summary={summary_idx} proj={proj_idx} chat={chat_idx} sent={sent!r}"
    )


@pytest.mark.asyncio
async def test_empty_summary_injects_nothing(engine: AsyncEngine) -> None:
    """A project with no summary yet (the default "" column value)
    must not leak a "Project summary:" label into the prompt."""
    sent = await _capture_outbound_sys_prompt(engine, summary="")
    assert "Project summary:" not in sent, f"unexpected summary block: {sent!r}"
    assert PROJECT_MARKER in sent
    assert CHAT_MARKER in sent
