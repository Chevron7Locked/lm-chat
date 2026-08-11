# SPDX-License-Identifier: Apache-2.0
"""Tests for the out-of-band followups feature (2026-06-23 decoupling).

Spec: the main generation's system_prompt must NOT contain the followups
directive; a separate lightweight call generates chips after chat.end;
the ``followups`` SSE frame is emitted when enabled and omitted when
disabled.
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
from lmchat.services.streaming_service import (
    ChatStreamRequest,
    StreamingService,
    _format_followups_frame,
    _parse_followups_json,
)

# ---------------------------------------------------------------------------
# Fixtures
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
        CanonicalEvent(type="chat.end", response_id="r-oob-test"),
    ]


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
        await conn.execute(
            insert(chats).values(id=1, user_id=1, title="t")
        )


async def _build_service(
    engine: AsyncEngine,
    lm_client: Any,
) -> StreamingService:
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
    # Parse SSE frames from buf.
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
# Test 1: main system_prompt must NOT contain the followups directive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_main_system_prompt_never_contains_followups_directive(
    engine: AsyncEngine,
) -> None:
    """The followups directive must never be injected into the main request.

    Regardless of lm_chat_followups_enabled, the system_prompt sent to the
    model must not contain the '<!--followups' marker.
    """
    captured: dict[str, Any] = {}

    async def _fake_stream(
        *args: Any, **kwargs: Any
    ) -> AsyncIterator[CanonicalEvent]:
        req = kwargs.get("request") or (args[0] if args else None)
        captured["system_prompt"] = getattr(req, "system_prompt", None) or ""
        for ev in _make_events():
            yield ev

    lm_client = MagicMock()
    lm_client.stream = _fake_stream
    # Patch the OOB call to a no-op so the test doesn't need a live LM Studio.
    lm_client._adapter = None  # makes _generate_followups_oob return []

    await _seed_chat(engine)
    svc = await _build_service(engine, lm_client)

    for followups_enabled in (True, False):
        captured.clear()
        with patch("lmchat.config.get_settings") as mock_settings:
            cfg = MagicMock()
            cfg.lm_chat_followups_enabled = followups_enabled
            mock_settings.return_value = cfg

            async for _ in svc.stream_chat(
                chat_id=1,
                user=_mock_user(1),
                payload=_make_payload(),
                request=_mock_request(),
            ):
                pass

        sys_prompt = captured.get("system_prompt", "")
        assert "<!--followups" not in sys_prompt, (
            f"followups directive found in main system_prompt "
            f"(followups_enabled={followups_enabled}): {sys_prompt!r}"
        )


# ---------------------------------------------------------------------------
# Test 2: `followups` SSE frame emitted when enabled, absent when disabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_followups_sse_frame_emitted_when_enabled(
    engine: AsyncEngine,
) -> None:
    """A `followups` SSE frame is yielded after chat.end when enabled."""
    async def _fake_stream(*args: Any, **kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        for ev in _make_events():
            yield ev

    lm_client = MagicMock()
    lm_client.stream = _fake_stream
    # Adapter is None → _generate_followups_oob returns [].
    lm_client._adapter = None

    await _seed_chat(engine)
    svc = await _build_service(engine, lm_client)

    with patch("lmchat.config.get_settings") as mock_settings:
        cfg = MagicMock()
        cfg.lm_chat_followups_enabled = True
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
    assert "followups" in types, (
        f"No followups frame in events when enabled: {types}"
    )
    # The followups frame should contain a list (possibly empty when OOB
    # call is a no-op because adapter is None).
    fu_event = next(e for e in events if e.get("type") == "followups")
    assert isinstance(fu_event.get("followups"), list)


@pytest.mark.asyncio
async def test_followups_sse_frame_absent_when_disabled(
    engine: AsyncEngine,
) -> None:
    """No `followups` SSE frame when lm_chat_followups_enabled=False."""
    async def _fake_stream(*args: Any, **kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        for ev in _make_events():
            yield ev

    lm_client = MagicMock()
    lm_client.stream = _fake_stream
    lm_client._adapter = None

    await _seed_chat(engine)
    svc = await _build_service(engine, lm_client)

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
    assert "followups" not in types, (
        f"followups frame present when disabled: {types}"
    )


# ---------------------------------------------------------------------------
# Test 3: _parse_followups_json — defensive parsing
# ---------------------------------------------------------------------------


def test_parse_followups_json_valid_array() -> None:
    """Valid JSON array → list of strings."""
    raw = '["How does X work?", "Why does Y matter?", "Can you explain Z?"]'
    result = _parse_followups_json(raw)
    assert result == [
        "How does X work?",
        "Why does Y matter?",
        "Can you explain Z?",
    ]


def test_parse_followups_json_fenced() -> None:
    """```json fence stripped before parsing."""
    raw = '```json\n["How does X work?","What is Y?"]\n```'
    result = _parse_followups_json(raw)
    assert result == ["How does X work?", "What is Y?"]


def test_parse_followups_json_with_prose_prefix() -> None:
    """Stray prose before the array is ignored."""
    raw = 'Here are some questions:\n["How?", "Why?"]'
    result = _parse_followups_json(raw)
    assert result == ["How?", "Why?"]


def test_parse_followups_json_garbage_returns_empty() -> None:
    """Unparseable input returns [] — never raises."""
    assert _parse_followups_json("not json at all") == []
    assert _parse_followups_json("") == []
    assert _parse_followups_json("{no array}") == []


def test_parse_followups_json_caps_at_three() -> None:
    """More than 3 items in the array are capped at 3."""
    raw = '["Q1?", "Q2?", "Q3?", "Q4?", "Q5?"]'
    result = _parse_followups_json(raw)
    assert len(result) == 3
    assert result[0] == "Q1?"


def test_parse_followups_json_filters_non_strings() -> None:
    """Non-string items in the array are silently dropped."""
    raw = '["Valid?", 42, null, "Also valid?"]'
    result = _parse_followups_json(raw)
    assert result == ["Valid?", "Also valid?"]


def test_parse_followups_json_not_a_list() -> None:
    """A valid JSON object (not array) returns []."""
    raw = '{"type": "followups", "items": ["Q1?"]}'
    result = _parse_followups_json(raw)
    assert result == []



def test_oob_reasoning_salvage_reads_reasoning_when_content_empty() -> None:
    """Regression (live dogfood 2026-07-18): a reasoning background model emits
    the JSON array in ``reasoning_content`` and leaves ``content`` empty when the
    reasoning runs long. Reading content alone silently dropped the result — the
    reason auto-memory distilled NOTHING on a reasoning bg model even when it
    answered correctly. The OOB extractor must salvage from reasoning_content,
    taking the LAST array (the final answer, after any drafts).

    RED-ON-REVERT: read only content and both salvage assertions fail.
    """
    from lmchat.services.streaming_service import (
        _last_json_array_of_strings,
        _oob_json_array_with_reasoning_salvage,
    )

    # content present → use it (reasoning ignored).
    assert _oob_json_array_with_reasoning_salvage(
        {"content": '["a", "b"]', "reasoning_content": "irrelevant"}
    ) == ["a", "b"]

    # content EMPTY → salvage the LAST array from reasoning (skip the draft).
    reasoning = (
        "Let me draft: [\"maybe Dana\"]\n"
        "Refine. Final answer:\n"
        '["Name: Dana", "Location: Portland", "Profession: Marine biologist"]\n'
    )
    assert _oob_json_array_with_reasoning_salvage(
        {"content": "", "reasoning_content": reasoning}
    ) == ["Name: Dana", "Location: Portland", "Profession: Marine biologist"]

    # last-array helper directly.
    assert _last_json_array_of_strings('["draft"] then ["final", "answer"]') == [
        "final",
        "answer",
    ]
    # no array anywhere → [].
    assert (
        _oob_json_array_with_reasoning_salvage(
            {"content": "", "reasoning_content": "no array here at all"}
        )
        == []
    )


# ---------------------------------------------------------------------------
# Test 4: _format_followups_frame wire format
# ---------------------------------------------------------------------------


def test_format_followups_frame_wire_format() -> None:
    """The followups frame has the correct SSE event name and data shape."""
    followups = ["How does this work?", "Why is this important?"]
    frame = _format_followups_frame(followups=followups, msg_id=42)

    assert isinstance(frame, bytes)
    text = frame.decode("utf-8")

    # SSE format: event line + data line + blank line
    assert text.startswith("event: followups\n")
    data_line = next(
        line for line in text.splitlines() if line.startswith("data: ")
    )
    payload = json.loads(data_line[len("data: "):])

    assert payload["type"] == "followups"
    assert payload["msg_id"] == 42
    assert payload["followups"] == followups


def test_format_followups_frame_empty_list() -> None:
    """Empty followups list is valid (OOB call failure → empty list → empty frame)."""
    frame = _format_followups_frame(followups=[], msg_id=1)
    text = frame.decode("utf-8")
    data_line = next(
        line for line in text.splitlines() if line.startswith("data: ")
    )
    payload = json.loads(data_line[len("data: "):])
    assert payload["followups"] == []



def test_oob_aux_timeouts_generous_for_slow_reasoning_models() -> None:
    """Regression (live dogfood 2026-07-18): a slow reasoning background-model
    (e.g. a 122B-MoE) spends ~40s emitting reasoning+JSON before the array
    lands in ``content``. The old 30s OOB / 18s title ceilings timed out EVERY
    such call, silently killing auto-memory, follow-up chips, and auto-titles.

    Guard the bumped budgets so nobody quietly restores the too-short values.
    RED-ON-REVERT: drop distill back to 30 / title back to 18 and this fails.

    Auto-title and compaction-summary now source their budget from
    ``ChatService._aux_model_timeout_sec`` (wired from
    ``settings.lm_chat_aux_model_timeout_sec``, default 900.0) rather than a
    module constant — local models are naturally slow and nothing waits on
    these background calls, so the default is generous by design.
    """
    import inspect

    from lmchat.services.chat_service import ChatService
    from lmchat.services.streaming_service import (
        _distill_memory_oob,
        _generate_followups_oob,
    )

    followups_to = inspect.signature(_generate_followups_oob).parameters[
        "timeout_sec"
    ].default
    distill_to = inspect.signature(_distill_memory_oob).parameters[
        "timeout_sec"
    ].default
    chat_service_aux_to = inspect.signature(ChatService.__init__).parameters[
        "aux_model_timeout_sec"
    ].default

    # All three aux calls share the bg_aux serialization gate and a slow
    # background model, so each needs the same generous budget once it starts.
    # A live-dogfood slow-model run timed out follow-ups at 90s; align at 120s.
    assert followups_to >= 120, f"followups OOB timeout too short: {followups_to}"
    assert distill_to >= 120, f"distill OOB timeout too short: {distill_to}"
    # >= 90: a live dogfood showed 60s still timed out on a 122B-MoE title call.
    assert chat_service_aux_to >= 90, (
        "auto-title/compaction aux timeout too short for reasoning models: "
        f"{chat_service_aux_to}"
    )


# ---------------------------------------------------------------------------
# Test: slow OOB followups must not race a spurious upstream_stall
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_followups_slow_call_does_not_race_a_spurious_stall(
    engine: AsyncEngine,
) -> None:
    """A slow OOB followups call must not trip a spurious ``upstream_stall``.

    Mirrors ``test_tool_loop_cap_does_not_race_a_spurious_stall`` in
    ``test_streaming_tool_loop_cap.py`` — same race, different terminal
    site. The tool-loop-cap exit sets ``_state["done"]`` /
    ``_state["stall_handled"]`` BEFORE its own (potentially slow) teardown
    so the disconnect/stall watcher can't fire during it. The
    normal-completion ``chat.end`` epilogue (finalize + OOB followups +
    memory-distill wait) used to leave ``_state["done"]`` False until the
    WHOLE epilogue finished, so a slow followups call could outlast the
    watcher's idle-timeout + dead-man-hedge grace window and fire an
    ``upstream_stall`` error frame on a turn that already has its final
    answer.

    ``idle_timeout_sec=0`` arms the watcher to fire on its very first poll
    (after ``_DISCONNECT_POLL_SEC``); the followups stub sleeps past that
    poll AND the dead-man hedge's grace window (``_STALL_GRACE_SEC``). The
    turn must still complete with the real answer and NO ``upstream_stall``
    frame.

    RED-ON-REVERT: moving the ``_state["done"] = True`` /
    ``_state["stall_handled"] = True`` lines back to AFTER the followups
    call (or removing them) makes the watcher's dead-man hedge fire, and an
    ``error`` frame with ``code == "upstream_stall"`` shows up in the
    drained events.
    """

    async def _fake_stream(*_args: Any, **_kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        for ev in _make_events(content="the real answer"):
            yield ev

    lm_client = MagicMock()
    lm_client.stream = _fake_stream

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    from lmchat.services.streaming_service import StreamingService as _SS

    svc = _SS(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        chat_locks={},
        idle_timeout_sec=0,
    )

    async def _slow_followups(*_args: Any, **_kwargs: Any) -> list[str]:
        # Longer than _DISCONNECT_POLL_SEC (0.5s) + _STALL_GRACE_SEC (2.0s)
        # combined, so the watcher's first poll AND its dead-man hedge both
        # get a chance to fire if the epilogue hasn't silenced them yet.
        await asyncio.sleep(3.0)
        return []

    await _seed_chat(engine)

    with (
        patch(
            "lmchat.services.streaming_service._generate_followups_oob",
            side_effect=_slow_followups,
        ),
        patch("lmchat.config.get_settings") as mock_settings,
    ):
        cfg = MagicMock()
        cfg.lm_chat_followups_enabled = True
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
    assert "chat.end" in types, f"stream never reached chat.end: {types}"
    assert "followups" in types, f"followups frame missing: {types}"
    error_events = [e for e in events if e.get("type") == "error"]
    assert not any(e.get("error", {}).get("code") == "upstream_stall" for e in error_events), (
        f"spurious upstream_stall raced the followups epilogue: {events}"
    )
