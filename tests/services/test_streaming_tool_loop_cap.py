# SPDX-License-Identifier: Apache-2.0
"""Integration tests for the per-turn tool-loop cap (2026-06-24).

The bug: a LOCAL model (LM Studio runs the MCP tool loop natively) gets stuck
re-deciding to call tools with slightly-varied queries and ends the turn with
NO answer — "dies instead of finishing." The cloud agentic path already caps
rounds + force-synthesizes; the local path had no such net.

The fix (``_MAX_TOOL_ROUNDS_PER_TURN`` in ``streaming_service``): once a single
turn's ``tool_call.success`` / ``.failure`` count EXCEEDS the cap, abort the
upstream and FINALIZE with the partial answer (or a clear message), plus a
``tool_loop_cap`` warning frame and a synthetic ``chat.end``.

Tests:
- A runaway loop (rounds > cap) with NO natural chat.end → stream STOPS,
  finalizes with a NON-EMPTY message, emits the warning + a chat.end frame,
  and bumps the salvage counter. (Red-on-revert: fails if the cap is removed —
  without it the upstream exhausts without chat.end and finalize is never
  reached with the loop-cap message.)
- A normal turn that uses a few tools then answers naturally (rounds <= cap)
  behaves EXACTLY as before — no warning, content is the model's answer, and
  the salvage counter does NOT move.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from prometheus_client import REGISTRY  # type: ignore[import-untyped]
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import chats, messages, metadata
from lmchat.lmstudio.types import (
    CanonicalChatRequest,
    CanonicalEvent,
    CanonicalInputBlock,
    CanonicalToolCall,
)
from lmchat.services import streaming_service as ss
from lmchat.services.streaming_service import ChatStreamRequest, StreamingService

_CAP = ss._MAX_TOOL_ROUNDS_PER_TURN


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_payload() -> ChatStreamRequest:
    return ChatStreamRequest(
        chat_id=1,
        payload=CanonicalChatRequest(
            model="test-model",
            input=[CanonicalInputBlock(type="text", content="search the web for X")],
        ),
    )


def _round(idx: int, *, content: str | None = None) -> list[CanonicalEvent]:
    """One tool-call round: start → name → arguments → success.

    Each round carries a slightly different query so exact-repeat detection
    would NOT fire (mirrors the varied-query loop). An
    optional ``content`` delta simulates the model narrating between rounds,
    so the partial answer is non-empty at finalize.
    """
    tc_id = f"tc-{idx}"
    evs = [
        CanonicalEvent(
            type="tool_call.start", tool_call=CanonicalToolCall(id=tc_id, name="", arguments={})
        ),
        CanonicalEvent(
            type="tool_call.name",
            tool_call=CanonicalToolCall(id=tc_id, name="search_web", arguments={}),
        ),
        CanonicalEvent(
            type="tool_call.arguments",
            tool_call=CanonicalToolCall(
                id=tc_id, name="search_web", arguments={"query": f"targeted search {idx}"}
            ),
        ),
        CanonicalEvent(
            type="tool_call.success",
            tool_call=CanonicalToolCall(
                id=tc_id,
                name="search_web",
                arguments={"query": f"targeted search {idx}"},
                result=f"(no useful result for query {idx})",
            ),
        ),
    ]
    if content is not None:
        evs.insert(0, CanonicalEvent(type="message.delta", content=content))
    return evs


def _identical_round(idx: int) -> list[CanonicalEvent]:
    """One tool-call round with a FIXED name+args — IDENTICAL across rounds.

    Mirrors a model genuinely stuck re-issuing the same call. The tc_id varies
    (it is not part of the signature) but name+arguments are constant, so the
    consecutive-identical degenerate-loop signal fires.
    """
    tc_id = f"id-{idx}"
    args = {"query": "the exact same query"}
    return [
        CanonicalEvent(
            type="tool_call.start", tool_call=CanonicalToolCall(id=tc_id, name="", arguments={})
        ),
        CanonicalEvent(
            type="tool_call.name",
            tool_call=CanonicalToolCall(id=tc_id, name="search_web", arguments={}),
        ),
        CanonicalEvent(
            type="tool_call.arguments",
            tool_call=CanonicalToolCall(id=tc_id, name="search_web", arguments=args),
        ),
        CanonicalEvent(
            type="tool_call.success",
            tool_call=CanonicalToolCall(
                id=tc_id, name="search_web", arguments=args, result="(same result)"
            ),
        ),
    ]


async def _make_service(
    engine: AsyncEngine,
    events: list[CanonicalEvent],
    *,
    aclose_delay: float = 0.0,
    idle_timeout_sec: int = 60,
) -> StreamingService:
    async def _fake_stream(*_args: Any, **_kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        # An async generator whose aclose() optionally blocks, to reproduce a
        # slow upstream teardown (network/MCP-server shutdown) during which the
        # disconnect/stall watcher is still polling. The cap block must have
        # silenced the watcher BEFORE awaiting aclose() — else a near-zero idle
        # timeout would let a spurious upstream_stall frame race the clean cap.
        try:
            for ev in events:
                yield ev
        finally:
            if aclose_delay:
                import asyncio as _asyncio

                await _asyncio.sleep(aclose_delay)

    lm_client = MagicMock()
    lm_client.stream = _fake_stream

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    return StreamingService(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        chat_locks={},
        idle_timeout_sec=idle_timeout_sec,
    )


async def _run_stream(svc: StreamingService) -> list[bytes]:
    """Drive the stream to completion and return every yielded SSE frame."""
    from tests.services.conftest import make_disconnect_receive

    request = AsyncMock()
    request.receive = make_disconnect_receive(False)
    user = MagicMock()
    user.id = 1
    frames: list[bytes] = []
    async for frame in svc.stream_chat(
        chat_id=1,
        user=user,
        payload=_make_payload(),
        request=request,
    ):
        frames.append(frame)
    return frames


def _salvaged_counter(reason: str) -> float:
    for metric in REGISTRY.collect():
        if metric.name == "lmchat_streams_salvaged":
            for sample in metric.samples:
                if (
                    sample.name == "lmchat_streams_salvaged_total"
                    and sample.labels.get("reason") == reason
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
async def test_tool_loop_cap_stops_and_finalizes_with_partial(
    engine_with_chat: AsyncEngine,
) -> None:
    """Runaway loop (rounds > cap, no natural chat.end) is cut and finalized.

    Red-on-revert: with the cap removed, this upstream NEVER emits chat.end, so
    the generator exhausts into the "generator_exhausted_without_terminal"
    error path — _finalize_message is never called with the loop-cap message and
    no ``tool_loop_cap`` warning frame is ever yielded.
    """
    # Emit (cap + 2) tool rounds and NO natural chat.end, each with a narrating
    # delta so partial content exists. The cap fires on the (cap + 1)th success.
    events: list[CanonicalEvent] = [
        CanonicalEvent(type="chat.start", response_id="rid-loop"),
        CanonicalEvent(type="message.start"),
    ]
    for i in range(_CAP + 2):
        events.extend(_round(i, content=f"Let me try a more targeted search ({i}). "))

    svc = await _make_service(engine_with_chat, events)

    captured: dict[str, Any] = {}
    original = svc._finalize_message

    async def _spy(**kwargs: Any) -> bool:
        captured.update(kwargs)
        return await original(**kwargs)

    svc._finalize_message = _spy  # type: ignore[method-assign]

    before = _salvaged_counter("tool_loop_cap")
    frames = await _run_stream(svc)
    after = _salvaged_counter("tool_loop_cap")

    # Salvage counter for the loop cap incremented exactly once.
    assert after == before + 1.0, f"tool_loop_cap counter: before={before} after={after}"

    # _finalize_message was called with a NON-EMPTY message + the cap stop_reason.
    assert "final_content" in captured, "finalize was never reached — loop was not cut"
    assert captured["final_content"].strip(), "finalize content must be non-empty"
    assert captured.get("stop_reason") == "tool_loop_cap"
    # Partial narration is preserved in the finalized content.
    assert "targeted search" in captured["final_content"]

    # A FE-visible warning frame announced the cut.
    joined = b"".join(frames)
    assert b"event: warning" in joined
    assert b"tool_loop_cap" in joined
    # And a terminal chat.end frame closed the stream cleanly.
    assert b"event: chat.end" in joined

    # The upstream was cut well before all (cap + 2) rounds streamed through —
    # the persisted tool_calls list must not exceed cap + 1 entries.
    assert captured.get("tool_calls") is not None
    assert len(captured["tool_calls"]) <= _CAP + 1

    # DB row finalized with the loop-cut content.
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
    assert row.content and row.content.strip()
    assert row.stop_reason == "tool_loop_cap"


@pytest.mark.asyncio
async def test_tool_loop_cap_empty_content_uses_clear_message(
    engine_with_chat: AsyncEngine,
) -> None:
    """Runaway loop with NO narration → finalize with a clear user-facing message."""
    events: list[CanonicalEvent] = [
        CanonicalEvent(type="chat.start", response_id="rid-loop2"),
        CanonicalEvent(type="message.start"),
    ]
    for i in range(_CAP + 2):
        events.extend(_round(i))  # no content delta

    svc = await _make_service(engine_with_chat, events)

    captured: dict[str, Any] = {}
    original = svc._finalize_message

    async def _spy(**kwargs: Any) -> bool:
        captured.update(kwargs)
        return await original(**kwargs)

    svc._finalize_message = _spy  # type: ignore[method-assign]

    await _run_stream(svc)

    assert "final_content" in captured
    msg = captured["final_content"]
    assert msg.strip(), "empty-content loop cut must still finalize a clear message"
    assert "stopped it" in msg.lower() or "kept calling tools" in msg.lower()


@pytest.mark.asyncio
async def test_normal_tool_turn_under_cap_is_unaffected(
    engine_with_chat: AsyncEngine,
) -> None:
    """A turn that uses a few tools (rounds <= cap) then answers behaves as before.

    No warning frame, no salvage-counter bump, content is the model's answer.
    """
    events: list[CanonicalEvent] = [
        CanonicalEvent(type="chat.start", response_id="rid-normal"),
        CanonicalEvent(type="message.start"),
    ]
    # Use exactly cap rounds (the cap fires only when count EXCEEDS the cap).
    for i in range(_CAP):
        events.extend(_round(i))
    events.extend(
        [
            CanonicalEvent(type="message.delta", content="Here is your answer."),
            CanonicalEvent(type="message.end"),
            CanonicalEvent(type="chat.end", response_id="rid-normal", stop_reason="stop"),
        ]
    )

    svc = await _make_service(engine_with_chat, events)

    captured: dict[str, Any] = {}
    original = svc._finalize_message

    async def _spy(**kwargs: Any) -> bool:
        captured.update(kwargs)
        return await original(**kwargs)

    svc._finalize_message = _spy  # type: ignore[method-assign]

    before = _salvaged_counter("tool_loop_cap")
    frames = await _run_stream(svc)
    after = _salvaged_counter("tool_loop_cap")

    # Cap did NOT fire.
    assert after == before, "cap fired on an under-cap turn"
    joined = b"".join(frames)
    assert b"tool_loop_cap" not in joined, "loop-cap warning emitted on a normal turn"

    # The natural answer was finalized with the natural stop_reason.
    assert captured.get("final_content") == "Here is your answer."
    assert captured.get("stop_reason") == "stop"


@pytest.mark.asyncio
async def test_tool_loop_cap_does_not_race_a_spurious_stall(
    engine_with_chat: AsyncEngine,
) -> None:
    """Cap-fire with a SLOW upstream teardown + active watcher → no stall race.

    The cap block returned without silencing the
    disconnect/stall watcher before the (potentially slow) ``aiter_iter.aclose()``
    teardown. With a near-zero idle timeout the watcher could fire a spurious
    ``upstream_stall`` (double draft-release + a stall error frame) on a turn
    that is cleanly finishing. The fix sets ``_state["done"]`` /
    ``["stall_handled"]`` BEFORE the teardown.

    Here ``idle_timeout_sec=0`` arms the watcher to fire on its first poll, and
    ``aclose_delay`` holds the teardown open across that window. The turn must
    still complete via the loop-cap path — NO ``upstream_stall`` frame, and the
    terminal frame is the synthetic ``chat.end``.
    """
    events: list[CanonicalEvent] = [
        CanonicalEvent(type="chat.start", response_id="rid-race"),
        CanonicalEvent(type="message.start"),
    ]
    for i in range(_CAP + 2):
        events.extend(_round(i, content=f"search {i}. "))

    # Idle timeout 0 → the watcher's first poll (after _DISCONNECT_POLL_SEC)
    # sees idle_s > 0 and would fire a stall unless the cap silenced it. The
    # teardown delay keeps the window open past that first poll.
    svc = await _make_service(
        engine_with_chat, events, aclose_delay=3.0, idle_timeout_sec=0
    )

    frames = await _run_stream(svc)
    joined = b"".join(frames)

    # The clean loop-cap path ran; NO spurious stall.
    assert b"tool_loop_cap" in joined, "loop-cap warning missing — cap did not fire"
    assert b"upstream_stall" not in joined, (
        "spurious upstream_stall raced the clean cap completion"
    )
    assert b"event: chat.end" in joined

    # DB row finalized exactly once, with the loop-cap stop_reason (not aborted).
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
    assert row.stop_reason == "tool_loop_cap"
    assert row.content and row.content.strip()


@pytest.mark.asyncio
async def test_consecutive_identical_calls_cut_fast(
    engine_with_chat: AsyncEngine,
) -> None:
    """A model re-issuing the SAME call is cut by the identical-loop signal far
    below the high count backstop, and the message names the real reason."""
    from lmchat.services import streaming_service as ss

    assert ss._MAX_IDENTICAL_TOOL_ROUNDS > 0
    n = ss._MAX_IDENTICAL_TOOL_ROUNDS + 2  # enough identical calls to trip it
    assert n < ss._MAX_TOOL_ROUNDS_PER_TURN, "must trip identical signal, not the backstop"

    events: list[CanonicalEvent] = [
        CanonicalEvent(type="chat.start", response_id="rid-id"),
        CanonicalEvent(type="message.start"),
    ]
    for i in range(n):
        events.extend(_identical_round(i))

    svc = await _make_service(engine_with_chat, events)
    captured: dict[str, Any] = {}
    original = svc._finalize_message

    async def _spy(**kwargs: Any) -> bool:
        captured.update(kwargs)
        return await original(**kwargs)

    svc._finalize_message = _spy  # type: ignore[method-assign]

    before = _salvaged_counter("tool_loop_cap")
    frames = await _run_stream(svc)
    after = _salvaged_counter("tool_loop_cap")

    assert after == before + 1.0, "identical-loop cut should bump the salvage counter"
    assert captured.get("stop_reason") == "tool_loop_cap"
    # Cut FAST — far fewer rounds than the high count backstop.
    assert captured.get("tool_calls") is not None
    assert len(captured["tool_calls"]) <= ss._MAX_IDENTICAL_TOOL_ROUNDS + 1
    assert "same tool call" in captured["final_content"].lower()
    assert b"tool_loop_cap" in b"".join(frames)


@pytest.mark.asyncio
async def test_varied_research_above_old_cap_completes_uncut(
    engine_with_chat: AsyncEngine,
) -> None:
    """Varied iterative research — 20 DIFFERENT successful searches, well above
    the OLD cap of 8 — completes naturally and is NOT cut. Regression for the
    'loop detection stopping actual research' (2026-06-24):
    different queries are research, not a degenerate loop."""
    events: list[CanonicalEvent] = [
        CanonicalEvent(type="chat.start", response_id="rid-research"),
        CanonicalEvent(type="message.start"),
    ]
    for i in range(20):  # 20 DIFFERENT queries
        events.extend(_round(i))
    events.extend(
        [
            CanonicalEvent(
                type="message.delta", content="Based on my research, here are three books."
            ),
            CanonicalEvent(type="message.end"),
            CanonicalEvent(type="chat.end", response_id="rid-research", stop_reason="stop"),
        ]
    )

    svc = await _make_service(engine_with_chat, events)
    captured: dict[str, Any] = {}
    original = svc._finalize_message

    async def _spy(**kwargs: Any) -> bool:
        captured.update(kwargs)
        return await original(**kwargs)

    svc._finalize_message = _spy  # type: ignore[method-assign]

    before = _salvaged_counter("tool_loop_cap")
    frames = await _run_stream(svc)
    after = _salvaged_counter("tool_loop_cap")

    assert after == before, "varied research must NOT trip the loop cap"
    assert b"tool_loop_cap" not in b"".join(frames)
    assert captured.get("final_content") == "Based on my research, here are three books."
    assert captured.get("stop_reason") == "stop"


# ---------------------------------------------------------------------------
# New tests: client-advisory early-cut paths
# ---------------------------------------------------------------------------


def _repeat_warning_sequence(
    idx: int, *, tool_name: str = "firecrawl_scrape"
) -> list[CanonicalEvent]:
    """Simulate what the streaming CLIENT emits for a repeated successful call.

    The client yields: start → name → arguments → repeat_warning → success.
    (The repeat_warning is yielded BEFORE the success event, then both flow
    through to streaming_service; the warning advisory fires the early-cut path,
    and the subsequent success event is never processed because the upstream is
    already aborted.)
    """
    tc_id = f"rep-{idx}"
    args = {"url": "https://example.com/page"}  # identical across all calls
    tc_full = CanonicalToolCall(id=tc_id, name=tool_name, arguments=args, result="(same)")
    return [
        CanonicalEvent(
            type="tool_call.start",
            tool_call=CanonicalToolCall(id=tc_id, name="", arguments={}),
        ),
        CanonicalEvent(
            type="tool_call.name",
            tool_call=CanonicalToolCall(id=tc_id, name=tool_name, arguments={}),
        ),
        CanonicalEvent(
            type="tool_call.arguments",
            tool_call=CanonicalToolCall(id=tc_id, name=tool_name, arguments=args),
        ),
        # The streaming client emits the repeat_warning BEFORE the success event
        # when it detects a prior identical successful call in the lookback window.
        CanonicalEvent(type="tool_call.repeat_warning", tool_call=tc_full),
        CanonicalEvent(type="tool_call.success", tool_call=tc_full),
    ]


def _failure_streak_sequence(
    idx: int,
    *,
    tool_name: str = "broken_tool",
    include_streak_warning: bool = False,
) -> list[CanonicalEvent]:
    """Simulate a failed tool call, optionally including the failure_streak_warning.

    The streaming client emits failure_streak_warning on the Nth consecutive
    failure (FAILURE_STREAK_THRESHOLD).  For the test we inject it explicitly
    after the last failure that tips the threshold.
    """
    tc_id = f"fail-{idx}"
    args: dict = {}
    tc_fail = CanonicalToolCall(id=tc_id, name=tool_name, arguments=args)
    evs = [
        CanonicalEvent(
            type="tool_call.start",
            tool_call=CanonicalToolCall(id=tc_id, name="", arguments={}),
        ),
        CanonicalEvent(
            type="tool_call.name",
            tool_call=CanonicalToolCall(id=tc_id, name=tool_name, arguments={}),
        ),
        CanonicalEvent(
            type="tool_call.arguments",
            tool_call=CanonicalToolCall(id=tc_id, name=tool_name, arguments=args),
        ),
        CanonicalEvent(type="tool_call.failure", tool_call=tc_fail),
    ]
    if include_streak_warning:
        evs.append(
            CanonicalEvent(
                type="tool_call.failure_streak_warning",
                tool_call=tc_fail,
                error={
                    "code": "tool_failure_streak",
                    "tool": tool_name,
                    "streak": idx + 1,
                },
            )
        )
    return evs


def _salvaged_counter_for(reason: str) -> float:
    """Read the STREAMS_SALVAGED prometheus counter for a given reason label."""
    for metric in REGISTRY.collect():
        if metric.name == "lmchat_streams_salvaged":
            for sample in metric.samples:
                if (
                    sample.name == "lmchat_streams_salvaged_total"
                    and sample.labels.get("reason") == reason
                ):
                    return sample.value
    return 0.0


@pytest.mark.asyncio
async def test_repeat_warning_early_cut_after_k_repeats(
    engine_with_chat: AsyncEngine,
) -> None:
    """K=2 repeat_warnings for the same tool → early loop-cut well before cap.

    Simulates the live bug: firecrawl_scrape called 9+ times with identical
    args; the streaming client fires tool_call.repeat_warning each time but
    streaming_service never acted on it.  With the fix, the 2nd repeat_warning
    (K=2, i.e. the 3rd identical call in the lookback window) triggers the cut.

    Red-on-revert: if _REPEAT_WARNING_CUT_K handling is removed, the stream
    runs past the K threshold without cutting and this test fails because the
    salvage counter for 'repeat_loop' never increments.
    """
    from lmchat.services import streaming_service as ss

    k = ss._REPEAT_WARNING_CUT_K
    assert k > 0, "_REPEAT_WARNING_CUT_K must be positive for this test"

    # Emit enough rounds to trip K repeat_warnings (k+1 rounds, where round 0
    # has no warning and rounds 1..k have a warning; cut fires on warning k).
    # We send k+2 rounds total to be clear we'd keep going without the fix.
    n_rounds = k + 2

    events: list[CanonicalEvent] = [
        CanonicalEvent(type="chat.start", response_id="rid-repeat"),
        CanonicalEvent(type="message.start"),
        # First round: no repeat_warning (this call hasn't been seen before).
        CanonicalEvent(
            type="tool_call.start",
            tool_call=CanonicalToolCall(id="rep-0", name="", arguments={}),
        ),
        CanonicalEvent(
            type="tool_call.name",
            tool_call=CanonicalToolCall(id="rep-0", name="firecrawl_scrape", arguments={}),
        ),
        CanonicalEvent(
            type="tool_call.arguments",
            tool_call=CanonicalToolCall(
                id="rep-0",
                name="firecrawl_scrape",
                arguments={"url": "https://example.com/page"},
            ),
        ),
        CanonicalEvent(
            type="tool_call.success",
            tool_call=CanonicalToolCall(
                id="rep-0",
                name="firecrawl_scrape",
                arguments={"url": "https://example.com/page"},
                result="(first result)",
            ),
        ),
    ]
    # Rounds 1..n_rounds-1: each includes a repeat_warning (prior success found).
    for i in range(1, n_rounds):
        events.extend(_repeat_warning_sequence(i))

    svc = await _make_service(engine_with_chat, events)
    captured: dict[str, Any] = {}
    original = svc._finalize_message

    async def _spy(**kwargs: Any) -> bool:
        captured.update(kwargs)
        return await original(**kwargs)

    svc._finalize_message = _spy  # type: ignore[method-assign]

    before_repeat = _salvaged_counter_for("repeat_loop")
    before_cap = _salvaged_counter("tool_loop_cap")
    frames = await _run_stream(svc)
    after_repeat = _salvaged_counter_for("repeat_loop")
    after_cap = _salvaged_counter("tool_loop_cap")

    # Exactly one repeat_loop cut fired.
    assert after_repeat == before_repeat + 1.0, (
        f"repeat_loop salvage counter: before={before_repeat} after={after_repeat}"
    )
    # The tool_loop_cap counter must NOT have moved (different reason).
    assert after_cap == before_cap, "tool_loop_cap counter must not move for repeat_loop cut"

    # finalize was called with the loop-cap stop_reason (DB compat) and a
    # message mentioning the repeat.
    assert "final_content" in captured, "finalize was never called"
    assert captured.get("stop_reason") == "tool_loop_cap", (
        "stop_reason must remain 'tool_loop_cap' for FE compat"
    )
    assert "same tool call" in captured["final_content"].lower() or (
        "kept" in captured["final_content"].lower()
    ), f"expected repeat description in message, got: {captured['final_content']!r}"

    # The stream terminated cleanly: warning frame + synthetic chat.end.
    joined = b"".join(frames)
    assert b"event: warning" in joined
    assert b"tool_loop_cap" in joined
    assert b"event: chat.end" in joined

    # Cut FAST — far fewer tool rounds than the per-turn backstop.
    tool_calls_saved = captured.get("tool_calls") or []
    assert len(tool_calls_saved) <= k + 2, (
        f"cut should fire fast; got {len(tool_calls_saved)} tool_calls saved"
    )

    # DB row finalized.
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
    assert row.stop_reason == "tool_loop_cap"


@pytest.mark.asyncio
async def test_failure_streak_warning_triggers_loop_cut(
    engine_with_chat: AsyncEngine,
) -> None:
    """A tool_call.failure_streak_warning immediately triggers the loop-cut.

    Simulates the streaming client detecting FAILURE_STREAK_THRESHOLD consecutive
    failures for the same tool and emitting failure_streak_warning.  The service
    must cut the loop on that event.

    Red-on-revert: if failure_streak_warning handling is removed, the stream
    runs on to the high-count backstop and this test fails because the
    'failure_streak' salvage counter never increments.
    """
    # Build a stream: 2 plain failures + 1 failure WITH the streak_warning.
    STREAK_THRESHOLD = 3  # mirrors FAILURE_STREAK_THRESHOLD in the client
    events: list[CanonicalEvent] = [
        CanonicalEvent(type="chat.start", response_id="rid-streak"),
        CanonicalEvent(type="message.start"),
    ]
    for i in range(STREAK_THRESHOLD - 1):
        # Plain failures — no warning yet.
        events.extend(_failure_streak_sequence(i, tool_name="broken_tool"))
    # Final failure that crosses the threshold → includes the streak_warning.
    events.extend(
        _failure_streak_sequence(
            STREAK_THRESHOLD - 1,
            tool_name="broken_tool",
            include_streak_warning=True,
        )
    )
    # Extra events that should NEVER be reached if the cut fires correctly.
    events.extend(_failure_streak_sequence(STREAK_THRESHOLD, tool_name="broken_tool"))
    events.append(CanonicalEvent(type="chat.end", stop_reason="stop"))

    svc = await _make_service(engine_with_chat, events)
    captured: dict[str, Any] = {}
    original = svc._finalize_message

    async def _spy(**kwargs: Any) -> bool:
        captured.update(kwargs)
        return await original(**kwargs)

    svc._finalize_message = _spy  # type: ignore[method-assign]

    before_streak = _salvaged_counter_for("failure_streak")
    before_cap = _salvaged_counter("tool_loop_cap")
    frames = await _run_stream(svc)
    after_streak = _salvaged_counter_for("failure_streak")
    after_cap = _salvaged_counter("tool_loop_cap")

    # Exactly one failure_streak cut fired.
    assert after_streak == before_streak + 1.0, (
        f"failure_streak salvage counter: before={before_streak} after={after_streak}"
    )
    assert after_cap == before_cap, "tool_loop_cap counter must not move for failure_streak cut"

    # finalize was called with the correct stop_reason and a useful message.
    assert "final_content" in captured, "finalize was never called"
    assert captured.get("stop_reason") == "tool_loop_cap"
    assert "kept" in captured["final_content"].lower() or (
        "fail" in captured["final_content"].lower()
    ), f"expected failure description in message, got: {captured['final_content']!r}"

    # Stream terminated cleanly.
    joined = b"".join(frames)
    assert b"event: warning" in joined
    assert b"tool_loop_cap" in joined
    assert b"event: chat.end" in joined

    # Only STREAK_THRESHOLD rounds of tool_calls in DB (the extra round was aborted).
    tool_calls_saved = captured.get("tool_calls") or []
    assert len(tool_calls_saved) <= STREAK_THRESHOLD, (
        f"extra rounds leaked past cut; got {len(tool_calls_saved)} tool_calls"
    )

    # DB row finalized.
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
    assert row.stop_reason == "tool_loop_cap"


@pytest.mark.asyncio
async def test_single_repeat_warning_does_not_cut(
    engine_with_chat: AsyncEngine,
) -> None:
    """Only ONE repeat_warning (K=2 threshold) → cut does NOT fire.

    Validates that K=2 means we tolerate the first repeat_warning and only cut
    on the second (3rd identical call in the window).  Legitimate retry-on-fail
    or near-identical-but-distinct calls must not be penalised.
    """
    from lmchat.services import streaming_service as ss

    k = ss._REPEAT_WARNING_CUT_K
    assert k >= 2, "this test assumes K >= 2"

    events: list[CanonicalEvent] = [
        CanonicalEvent(type="chat.start", response_id="rid-single-rep"),
        CanonicalEvent(type="message.start"),
        # First call: no warning.
        CanonicalEvent(
            type="tool_call.start",
            tool_call=CanonicalToolCall(id="r0", name="", arguments={}),
        ),
        CanonicalEvent(
            type="tool_call.name",
            tool_call=CanonicalToolCall(id="r0", name="search", arguments={}),
        ),
        CanonicalEvent(
            type="tool_call.arguments",
            tool_call=CanonicalToolCall(id="r0", name="search", arguments={"q": "x"}),
        ),
        CanonicalEvent(
            type="tool_call.success",
            tool_call=CanonicalToolCall(id="r0", name="search", arguments={"q": "x"}, result="r"),
        ),
        # Second call: ONE repeat_warning — below the K=2 threshold.
        CanonicalEvent(
            type="tool_call.repeat_warning",
            tool_call=CanonicalToolCall(id="r1", name="search", arguments={"q": "x"}),
        ),
        CanonicalEvent(
            type="tool_call.success",
            tool_call=CanonicalToolCall(id="r1", name="search", arguments={"q": "x"}, result="r2"),
        ),
        # Model answers naturally after two calls.
        CanonicalEvent(type="message.delta", content="Here is the answer."),
        CanonicalEvent(type="message.end"),
        CanonicalEvent(type="chat.end", stop_reason="stop"),
    ]

    svc = await _make_service(engine_with_chat, events)
    captured: dict[str, Any] = {}
    original = svc._finalize_message

    async def _spy(**kwargs: Any) -> bool:
        captured.update(kwargs)
        return await original(**kwargs)

    svc._finalize_message = _spy  # type: ignore[method-assign]

    before_repeat = _salvaged_counter_for("repeat_loop")
    before_cap = _salvaged_counter("tool_loop_cap")
    frames = await _run_stream(svc)
    after_repeat = _salvaged_counter_for("repeat_loop")
    after_cap = _salvaged_counter("tool_loop_cap")

    # Neither cut fired.
    assert after_repeat == before_repeat, "single repeat_warning must NOT fire the cut"
    assert after_cap == before_cap, "tool_loop_cap must not move"

    # Natural completion with the model's answer.
    assert captured.get("final_content") == "Here is the answer."
    assert captured.get("stop_reason") == "stop"
    joined = b"".join(frames)
    assert b"tool_loop_cap" not in joined


# ---------------------------------------------------------------------------
# streaming-5: _fire_post_finalize_background terminal divergence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_finalize_background_fires_distill_and_summary_only_on_chat_end(
    engine: AsyncEngine,
) -> None:
    """The two success terminals share one launcher but diverge on purpose.

    Both the tool_loop_cap terminal and a natural chat.end ALWAYS schedule
    memory-indexing + quota-token consumption. Auto-memory distillation and
    the project-summary refresh must fire ONLY on the natural chat.end path
    (``with_distill_and_summary=True``) and NEVER on the tool_loop_cap cut.

    Red-on-revert: flipping ``with_distill_and_summary`` on either call site
    in ``streaming_service.py`` (e.g. accidentally setting it True for the
    tool_loop_cap terminal, or False for chat.end) flips one of the
    assertions below.
    """
    # A single PROJECT chat (chat_id=1, matching _make_payload()/_run_stream's
    # hardcoded chat_id) so the natural chat.end half can also prove the
    # project-summary refresh fires with the right project_id.
    async with engine.begin() as conn:
        await conn.execute(
            chats.insert().values(user_id=1, project_id=42, title="project-chat")
        )

    # -- Part A: tool_loop_cap terminal — distill/summary must NOT fire.
    events_cap: list[CanonicalEvent] = [
        CanonicalEvent(type="chat.start", response_id="rid-cap-bg"),
        CanonicalEvent(type="message.start"),
    ]
    for i in range(_CAP + 2):
        events_cap.extend(_round(i, content=f"narrating {i} "))

    svc_cap = await _make_service(engine, events_cap)
    svc_cap._safe_index_message = AsyncMock(return_value=None)  # type: ignore[method-assign]
    svc_cap._safe_consume_tokens = AsyncMock(return_value=None)  # type: ignore[method-assign]
    svc_cap._safe_distill_memory = AsyncMock(return_value=None)  # type: ignore[method-assign]
    svc_cap._safe_refresh_project_summary = AsyncMock(return_value=None)  # type: ignore[method-assign]

    await _run_stream(svc_cap)
    await asyncio.sleep(0.05)

    assert svc_cap._safe_index_message.await_count == 1, "index not scheduled on tool_loop_cap"
    assert svc_cap._safe_consume_tokens.await_count == 1, "quota not scheduled on tool_loop_cap"
    assert svc_cap._safe_distill_memory.await_count == 0, (
        "distill scheduled on the tool_loop_cap terminal"
    )
    assert svc_cap._safe_refresh_project_summary.await_count == 0, (
        "project-summary scheduled on the tool_loop_cap terminal"
    )

    # -- Part B: natural chat.end on the SAME project chat — distill AND
    # project-summary must fire, alongside index + quota.
    events_end: list[CanonicalEvent] = [
        CanonicalEvent(type="chat.start", response_id="rid-end-bg"),
        CanonicalEvent(type="message.start"),
        CanonicalEvent(type="message.delta", content="Here is your answer."),
        CanonicalEvent(type="message.end"),
        CanonicalEvent(type="chat.end", response_id="rid-end-bg", stop_reason="stop"),
    ]
    svc_end = await _make_service(engine, events_end)
    svc_end._safe_index_message = AsyncMock(return_value=None)  # type: ignore[method-assign]
    svc_end._safe_consume_tokens = AsyncMock(return_value=None)  # type: ignore[method-assign]
    # _safe_distill_memory now returns the newly-stored fact count (int),
    # not None — see the memory.saved SSE indicator (streaming_service.py
    # _fire_post_finalize_background / the chat.end epilogue's bounded wait
    # on this task). 0 preserves this test's original intent (asserting
    # scheduling, not the frame) without tripping the epilogue's
    # `_saved_count > 0` comparison on a None.
    svc_end._safe_distill_memory = AsyncMock(return_value=0)  # type: ignore[method-assign]
    svc_end._safe_refresh_project_summary = AsyncMock(return_value=None)  # type: ignore[method-assign]

    await _run_stream(svc_end)
    await asyncio.sleep(0.05)

    assert svc_end._safe_index_message.await_count == 1, "index not scheduled on chat.end"
    assert svc_end._safe_consume_tokens.await_count == 1, "quota not scheduled on chat.end"
    assert svc_end._safe_distill_memory.await_count == 1, (
        "distill NOT scheduled on a natural chat.end"
    )
    assert svc_end._safe_refresh_project_summary.await_count == 1, (
        "project-summary NOT scheduled for a project-chat chat.end"
    )
    refresh_await_args = svc_end._safe_refresh_project_summary.await_args
    assert refresh_await_args is not None
    assert refresh_await_args.kwargs.get("project_id") == 42
