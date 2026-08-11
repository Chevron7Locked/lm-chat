# SPDX-License-Identifier: Apache-2.0
"""Tests for the auto-memory-saved SSE indicator (2026-08-04).

Spec: after a natural chat.end, the post-answer epilogue (right after the
followups block, but independent of the followups toggle) bounded-waits
(``asyncio.wait_for`` + ``asyncio.shield``) on the detached auto-memory
distillation task launched by ``_fire_post_finalize_background``. If it
resolves to >=1 newly-stored fact within
``streaming_service._MEMORY_SAVED_FRAME_WAIT_SEC``, a ``memory.saved`` SSE
frame is yielded.

``asyncio.shield`` is mandatory: without it, ``wait_for``'s timeout would
CANCEL the still-running detached distillation task, and a slow local model
would silently lose the fact instead of just missing the inline indicator.
Mirrors the harness in tests/services/test_followups_oob.py.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import chats, metadata
from lmchat.lmstudio.types import (
    CanonicalChatRequest,
    CanonicalEvent,
    CanonicalInputBlock,
)
from lmchat.services import streaming_service as streaming_service_module
from lmchat.services.streaming_service import (
    ChatStreamRequest,
    StreamingService,
    _format_memory_saved_frame,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers — mirrors tests/services/test_followups_oob.py.
# ---------------------------------------------------------------------------


@pytest.fixture
async def engine() -> AsyncEngine:
    e = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with e.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return e


def _make_events(content: str = "hello") -> list[CanonicalEvent]:
    """Minimal stream — start / message / end."""
    return [
        CanonicalEvent(type="chat.start"),
        CanonicalEvent(type="message.start"),
        CanonicalEvent(type="message.delta", content=content),
        CanonicalEvent(type="message.end"),
        CanonicalEvent(type="chat.end", response_id="r-memsave-test"),
    ]


def _fake_stream(*args: Any, **kwargs: Any) -> AsyncIterator[CanonicalEvent]:
    async def _gen() -> AsyncIterator[CanonicalEvent]:
        for ev in _make_events():
            yield ev

    return _gen()


def _mock_request(disconnected: bool = False) -> MagicMock:
    from tests.services.conftest import make_disconnect_receive

    r = MagicMock()
    r.receive = make_disconnect_receive(disconnected)
    return r


def _mock_user(user_id: int = 1) -> MagicMock:
    u = MagicMock()
    u.id = user_id
    return u


def _make_payload(content: str = "say hello") -> ChatStreamRequest:
    canonical = CanonicalChatRequest(
        model="test-model",
        input=[CanonicalInputBlock(type="text", content=content)],
    )
    return ChatStreamRequest(chat_id=1, payload=canonical)


async def _seed_chat(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(insert(chats).values(id=1, user_id=1, title="t"))


async def _build_service(engine: AsyncEngine, lm_client: Any) -> StreamingService:
    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)
    return StreamingService(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        chat_locks={},
        idle_timeout_sec=60,
    )


async def _drain_events(stream: AsyncIterator[bytes]) -> list[dict]:  # type: ignore[type-arg]
    """Drain a streaming_service byte stream; decode SSE frames to dicts."""
    result: list[dict] = []  # type: ignore[type-arg]
    buf = b""
    async for chunk in stream:
        buf += chunk
    for block in buf.split(b"\n\n"):
        lines = block.strip().split(b"\n")
        data_line = next(
            (ln[5:] for ln in lines if ln.startswith(b"data: ")), None
        )
        if data_line:
            try:
                result.append(json.loads(data_line))
            except json.JSONDecodeError:
                pass
    return result


# ---------------------------------------------------------------------------
# Test 1: memory.saved frame yielded when distill resolves within the wait
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_saved_frame_emitted_when_distill_resolves_in_time(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fast (well-under-budget) distill task's count reaches the wire."""
    lm_client = MagicMock()
    lm_client.stream = _fake_stream
    # Adapter is None → followups OOB returns [] without extra mocking.
    lm_client._adapter = None

    await _seed_chat(engine)
    svc = await _build_service(engine, lm_client)

    async def fake_distill(**kwargs: Any) -> int:
        return 2

    monkeypatch.setattr(svc, "_safe_distill_memory", fake_distill)

    with patch("lmchat.config.get_settings") as mock_settings:
        cfg = MagicMock()
        cfg.lm_chat_followups_enabled = False
        mock_settings.return_value = cfg

        events = await _drain_events(
            svc.stream_chat(
                chat_id=1,
                user=_mock_user(1),
                payload=_make_payload(),
                request=_mock_request(),
            )
        )

    types = [e.get("type") for e in events]
    assert "memory.saved" in types, f"No memory.saved frame in events: {types}"
    ms_event = next(e for e in events if e.get("type") == "memory.saved")
    assert ms_event["count"] == 2
    assert isinstance(ms_event.get("msg_id"), int)


@pytest.mark.asyncio
async def test_memory_saved_frame_absent_when_distill_finds_nothing(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fast distill that stores 0 facts emits no frame (count must be > 0)."""
    lm_client = MagicMock()
    lm_client.stream = _fake_stream
    lm_client._adapter = None

    await _seed_chat(engine)
    svc = await _build_service(engine, lm_client)

    async def fake_distill(**kwargs: Any) -> int:
        return 0

    monkeypatch.setattr(svc, "_safe_distill_memory", fake_distill)

    with patch("lmchat.config.get_settings") as mock_settings:
        cfg = MagicMock()
        cfg.lm_chat_followups_enabled = False
        mock_settings.return_value = cfg

        events = await _drain_events(
            svc.stream_chat(
                chat_id=1,
                user=_mock_user(1),
                payload=_make_payload(),
                request=_mock_request(),
            )
        )

    types = [e.get("type") for e in events]
    assert "memory.saved" not in types, f"memory.saved present with 0 stored: {types}"


# ---------------------------------------------------------------------------
# Test 2: shield guard — a timed-out wait yields NO frame but does NOT
# cancel the underlying distillation task, which still completes + stores.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_saved_frame_absent_on_timeout_but_distill_survives(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED-ON-REVERT for asyncio.shield.

    Dropping the shield would let ``wait_for``'s timeout cancel the detached
    distill task, losing the fact on a slow local model. Shrink the wait
    budget so a "slow" distill is cheap to simulate in a unit test, then
    prove the task survives the timeout, keeps running, and still completes
    with its result — the exact guarantee the shield exists for.
    """
    monkeypatch.setattr(
        streaming_service_module, "_MEMORY_SAVED_FRAME_WAIT_SEC", 0.05
    )

    lm_client = MagicMock()
    lm_client.stream = _fake_stream
    lm_client._adapter = None

    await _seed_chat(engine)
    svc = await _build_service(engine, lm_client)

    task_ref: dict[str, asyncio.Task[int]] = {}

    async def slow_distill(**kwargs: Any) -> int:
        current = asyncio.current_task()
        assert current is not None
        task_ref["task"] = current
        await asyncio.sleep(0.2)
        return 1

    monkeypatch.setattr(svc, "_safe_distill_memory", slow_distill)

    with patch("lmchat.config.get_settings") as mock_settings:
        cfg = MagicMock()
        cfg.lm_chat_followups_enabled = False
        mock_settings.return_value = cfg

        events = await _drain_events(
            svc.stream_chat(
                chat_id=1,
                user=_mock_user(1),
                payload=_make_payload(),
                request=_mock_request(),
            )
        )

    types = [e.get("type") for e in events]
    assert "memory.saved" not in types, (
        f"memory.saved frame present despite the distill exceeding the wait: {types}"
    )

    task = task_ref.get("task")
    assert task is not None, (
        "slow_distill never ran — nothing to assert the shield against"
    )
    assert not task.cancelled(), (
        "the detached distill task was CANCELLED by the wait_for timeout — "
        "asyncio.shield is missing/broken; a slow model would silently lose "
        "the fact it was about to store"
    )

    # Give the still-running task room to actually finish, proving it wasn't
    # just "not yet cancelled" at the moment we checked, but genuinely
    # completes and stores after the stream (and the client) has moved on.
    await asyncio.sleep(0.3)
    assert task.done()
    assert not task.cancelled()
    assert task.result() == 1


# ---------------------------------------------------------------------------
# Test 3: _format_memory_saved_frame wire format
# ---------------------------------------------------------------------------


def test_format_memory_saved_frame_wire_format() -> None:
    """The memory.saved frame has the correct SSE event name and data shape."""
    frame = _format_memory_saved_frame(count=3, msg_id=42)

    assert isinstance(frame, bytes)
    text = frame.decode("utf-8")

    assert text.startswith("event: memory.saved\n")
    data_line = next(
        line for line in text.splitlines() if line.startswith("data: ")
    )
    payload = json.loads(data_line[len("data: "):])

    assert payload["type"] == "memory.saved"
    assert payload["msg_id"] == 42
    assert payload["count"] == 3
