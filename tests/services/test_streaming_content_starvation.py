# SPDX-License-Identifier: Apache-2.0
"""Unit tests for content-starvation salvage.

Tests:
- Stream with reasoning.delta ONLY (no message.delta): salvage triggers;
  final_content starts with marker; final_reasoning="" (so DB writes NULL).
- Stream with both content + reasoning: salvage does NOT trigger.
- Stream with NEITHER content nor reasoning: salvage does NOT trigger.
- Counter increment assertion.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from prometheus_client import REGISTRY  # type: ignore[import-untyped]
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import chats, messages, metadata
from lmchat.lmstudio.types import CanonicalChatRequest, CanonicalEvent, CanonicalInputBlock
from lmchat.services.streaming_service import ChatStreamRequest, StreamingService

_SALVAGE_MARKER = "_(reasoning surfaced because the model produced no final answer)_"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_payload() -> ChatStreamRequest:
    return ChatStreamRequest(
        chat_id=1,
        payload=CanonicalChatRequest(
            model="test-model",
            input=[CanonicalInputBlock(type="text", content="hi")],
        ),
    )


async def _make_service(engine: AsyncEngine, events: list[CanonicalEvent]) -> StreamingService:
    async def _fake_stream(*_args: Any, **_kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        for ev in events:
            yield ev

    lm_client = MagicMock()
    lm_client.stream = _fake_stream

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    return StreamingService(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        chat_locks={},
    )


async def _run_stream(svc: StreamingService) -> None:
    from tests.services.conftest import make_disconnect_receive

    request = AsyncMock()
    request.receive = make_disconnect_receive(False)
    user = MagicMock()
    user.id = 1
    async for _ in svc.stream_chat(
        chat_id=1,
        user=user,
        payload=_make_payload(),
        request=request,
    ):
        pass


def _get_salvaged_counter() -> float:
    """Read current value of STREAMS_SALVAGED{reason=substance_fold_applied}."""
    for metric in REGISTRY.collect():
        if metric.name == "lmchat_streams_salvaged":
            for sample in metric.samples:
                if (
                    sample.name == "lmchat_streams_salvaged_total"
                    and sample.labels.get("reason") == "substance_fold_applied"
                ):
                    return sample.value
    return 0.0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture()
async def engine_with_chat(engine: AsyncEngine) -> AsyncEngine:
    async with engine.begin() as conn:
        await conn.execute(chats.insert().values(user_id=1, title="test"))
    return engine


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_salvage_triggers_on_reasoning_only_stream(engine_with_chat: AsyncEngine) -> None:
    """Salvage fires when only reasoning.delta events arrive (no message.delta).

    - final_content contains the marker prefix.
    - reasoning_content in the DB row is NULL (accumulated_reasoning was zeroed).
    """
    # Reasoning must exceed substance_fold.STUB_CHARS (240): the salvage
    # gate requires reasoning to be substantively longer than the (empty)
    # base, not any non-empty reasoning. A 28-char reasoning is below the
    # floor and would correctly NOT salvage under this predicate.
    reasoning_text = (
        "I concluded the answer is 42 after a long chain of thought "
        "covering several alternative routes. " * 5
    )

    events = [
        CanonicalEvent(type="chat.start", response_id="rid-salvage"),
        CanonicalEvent(type="message.start"),
        CanonicalEvent(type="reasoning.start"),
        CanonicalEvent(type="reasoning.delta", content=reasoning_text),
        CanonicalEvent(type="reasoning.end"),
        # No message.delta — small-model misfire shape
        CanonicalEvent(type="message.end"),
        CanonicalEvent(type="chat.end", response_id="rid-salvage"),
    ]

    svc = await _make_service(engine_with_chat, events)

    # Spy to capture final_reasoning passed to _finalize_message.
    captured: dict[str, Any] = {}
    original = svc._finalize_message

    async def _spy(**kwargs: Any) -> bool:
        captured.update(kwargs)
        return await original(**kwargs)

    svc._finalize_message = _spy  # type: ignore[method-assign]

    before = _get_salvaged_counter()
    await _run_stream(svc)
    after = _get_salvaged_counter()

    # Counter incremented by 1.
    assert after == before + 1.0, f"Counter did not increment: before={before} after={after}"

    # final_content starts with the marker.
    assert "final_content" in captured
    assert captured["final_content"].startswith(_SALVAGE_MARKER), (
        f"Salvage marker not found in final_content: {captured['final_content'][:80]!r}"
    )

    # final_reasoning is empty so DB writes NULL.
    assert captured.get("final_reasoning") == "", (
        f"Expected empty final_reasoning but got: {captured.get('final_reasoning')!r}"
    )

    # Confirm DB: reasoning_content IS NULL, content is non-empty.
    # Filter to role='assistant' — the user row is also persisted and has
    # content='hi', which must not confuse this check.
    async with engine_with_chat.connect() as conn:
        row = (
            await conn.execute(
                select(messages).where(
                    messages.c.chat_id == 1,
                    messages.c.role == "assistant",
                )
            )
        ).fetchone()
    assert row is not None
    assert row.reasoning_content is None
    assert row.content and _SALVAGE_MARKER in row.content


@pytest.mark.asyncio
async def test_salvage_does_not_trigger_with_both_content_and_reasoning(
    engine_with_chat: AsyncEngine,
) -> None:
    """Salvage must NOT fire when both message.delta and reasoning.delta are present."""
    events = [
        CanonicalEvent(type="chat.start", response_id="rid-both"),
        CanonicalEvent(type="message.start"),
        CanonicalEvent(type="reasoning.start"),
        CanonicalEvent(type="reasoning.delta", content="thinking..."),
        CanonicalEvent(type="reasoning.end"),
        CanonicalEvent(type="message.delta", content="The answer is 42."),
        CanonicalEvent(type="message.end"),
        CanonicalEvent(type="chat.end", response_id="rid-both"),
    ]

    svc = await _make_service(engine_with_chat, events)

    captured: dict[str, Any] = {}
    original = svc._finalize_message

    async def _spy(**kwargs: Any) -> bool:
        captured.update(kwargs)
        return await original(**kwargs)

    svc._finalize_message = _spy  # type: ignore[method-assign]

    await _run_stream(svc)

    # final_content must NOT contain the salvage marker.
    assert _SALVAGE_MARKER not in captured.get("final_content", ""), (
        "Salvage marker found when both content and reasoning were present"
    )
    assert captured["final_content"] == "The answer is 42."
    assert captured["final_reasoning"] == "thinking..."


@pytest.mark.asyncio
async def test_salvage_does_not_trigger_with_neither_content_nor_reasoning(
    engine_with_chat: AsyncEngine,
) -> None:
    """Salvage must NOT fire when both accumulated_content and accumulated_reasoning are empty."""
    events = [
        CanonicalEvent(type="chat.start", response_id="rid-empty"),
        CanonicalEvent(type="message.start"),
        CanonicalEvent(type="message.end"),
        CanonicalEvent(type="chat.end", response_id="rid-empty"),
    ]

    svc = await _make_service(engine_with_chat, events)

    captured: dict[str, Any] = {}
    original = svc._finalize_message

    async def _spy(**kwargs: Any) -> bool:
        captured.update(kwargs)
        return await original(**kwargs)

    svc._finalize_message = _spy  # type: ignore[method-assign]

    await _run_stream(svc)

    # final_content is empty (salvage never fired).
    assert captured.get("final_content") == ""
    assert _SALVAGE_MARKER not in captured.get("final_content", "")


# ---------------------------------------------------------------------------
# substance-fold extensions
# ---------------------------------------------------------------------------


def _long(n: int) -> str:
    base = "the reasoning continues at length and explains the path; "
    return (base * (n // len(base) + 1))[:n]


@pytest.mark.asyncio
async def test_substance_fold_terse_real_content_plus_long_reasoning_no_fold(
    engine_with_chat: AsyncEngine,
) -> None:
    """A terse but REAL answer ("Done.") + long reasoning → NO fold.

    The earlier gate folded any ``len(content) < 240`` body, dumping the
    thinking block over short-but-complete replies ("say
    hello" rendered a clean greeting with ~7 KB of reasoning pasted beneath).
    "Done." carries real answer text, so it is preserved verbatim and the
    reasoning stays in its own channel. The salvage is reserved for genuinely
    empty content — covered by ``test_salvage_triggers_on_reasoning_only_stream``.
    """
    from lmchat.services.substance_fold import STUB_CHARS

    reasoning_text = _long(STUB_CHARS + 100)

    events = [
        CanonicalEvent(type="chat.start", response_id="rid-stub"),
        CanonicalEvent(type="message.start"),
        CanonicalEvent(type="message.delta", content="Done."),
        CanonicalEvent(type="message.end"),
        CanonicalEvent(type="reasoning.start"),
        CanonicalEvent(type="reasoning.delta", content=reasoning_text),
        CanonicalEvent(type="reasoning.end"),
        CanonicalEvent(type="chat.end", response_id="rid-stub"),
    ]

    svc = await _make_service(engine_with_chat, events)

    captured: dict[str, Any] = {}
    original = svc._finalize_message

    async def _spy(**kwargs: Any) -> bool:
        captured.update(kwargs)
        return await original(**kwargs)

    svc._finalize_message = _spy  # type: ignore[method-assign]

    before = _get_salvaged_counter()
    await _run_stream(svc)
    after = _get_salvaged_counter()

    assert after == before, "terse real answer must NOT trigger the salvage"
    assert captured["final_content"] == "Done."
    assert _SALVAGE_MARKER not in captured["final_content"]
    assert reasoning_text not in captured["final_content"]
    assert captured.get("final_reasoning") == reasoning_text


@pytest.mark.asyncio
async def test_substance_fold_stub_content_plus_short_reasoning_no_fold(
    engine_with_chat: AsyncEngine,
) -> None:
    """Short content + short reasoning → no fold.

    Predicate guards against folding on small reasoning; the bubble shows
    the original content + the reasoning event stream stays.
    """
    events = [
        CanonicalEvent(type="chat.start", response_id="rid-short"),
        CanonicalEvent(type="message.start"),
        CanonicalEvent(type="message.delta", content="Hi"),
        CanonicalEvent(type="message.end"),
        CanonicalEvent(type="reasoning.start"),
        CanonicalEvent(type="reasoning.delta", content="brief thought"),
        CanonicalEvent(type="reasoning.end"),
        CanonicalEvent(type="chat.end", response_id="rid-short"),
    ]

    svc = await _make_service(engine_with_chat, events)

    captured: dict[str, Any] = {}
    original = svc._finalize_message

    async def _spy(**kwargs: Any) -> bool:
        captured.update(kwargs)
        return await original(**kwargs)

    svc._finalize_message = _spy  # type: ignore[method-assign]

    before = _get_salvaged_counter()
    await _run_stream(svc)
    after = _get_salvaged_counter()

    assert after == before
    assert captured.get("final_content") == "Hi"
    assert captured.get("final_reasoning") == "brief thought"
    assert _SALVAGE_MARKER not in captured.get("final_content", "")

