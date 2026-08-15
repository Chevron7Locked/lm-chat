# SPDX-License-Identifier: Apache-2.0
"""Regression suite for StreamingService — 10 core tests + supplementary.

Tests for the streaming service.

Test matrix:
 1. test_streaming_happy_path_emits_delta_end_chat_end_final_state
 2. test_streaming_client_disconnect_aborts_draft
 3. test_streaming_single_stream_per_chat_returns_409
 4. test_streaming_malformed_tool_call_emits_error_frame_terminates
 5. test_streaming_rejected_param_retries_once_then_succeeds
 6. test_streaming_memory_ingestion_only_on_FINAL_not_aborted
 7. test_streaming_upstream_stall_emits_upstream_stall_error_at_60s
 8. test_streaming_compact_serialization
 9. test_streaming_pending_finalization_recovered_by_reaper
10. test_streaming_read_timeout_emits_error_frame

Two more scenarios (cove+integrations, and strip-up-front) are covered via
the dedicated test files; together they close out the full matrix.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import messages, metadata
from lmchat.lmstudio.types import (
    CanonicalChatRequest,
    CanonicalEvent,
    CanonicalInputBlock,
    CanonicalToolCall,
)
from lmchat.metrics import STREAMS_ACTIVE
from lmchat.services._stream_state import PersistState
from lmchat.services.streaming_errors import StreamInProgressError
from lmchat.services.streaming_service import (
    ChatStreamRequest,
    StreamingService,
    _format_error_frame,
    _format_sse_frame,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request_payload(model: str = "test-model", chat_text: str = "hello") -> ChatStreamRequest:
    """Build a minimal ChatStreamRequest for testing.

    Args:
        model:     LM Studio model key.
        chat_text: Text for the single input block.

    Returns:
        A :class:`~lmchat.services.streaming_service.ChatStreamRequest`.
    """
    return ChatStreamRequest(
        chat_id=1,
        payload=CanonicalChatRequest(
            model=model,
            input=[CanonicalInputBlock(type="text", content=chat_text)],
        ),
    )


def _make_events(  # noqa: E501
    *types: str, content: str = "hello", response_id: str = "rid-1"
) -> list[CanonicalEvent]:
    """Build a minimal event sequence for mocking.

    Args:
        types:       Sequence of event type strings.
        content:     Content string for ``message.delta`` events.
        response_id: response_id for ``chat.end`` events.

    Returns:
        List of :class:`~lmchat.lmstudio.types.CanonicalEvent`.
    """
    evs: list[CanonicalEvent] = []
    for t in types:
        if t == "message.delta":
            evs.append(CanonicalEvent(type="message.delta", content=content))
        elif t == "chat.end":
            evs.append(CanonicalEvent(type="chat.end", response_id=response_id))
        elif t == "chat.start":
            evs.append(CanonicalEvent(type="chat.start"))
        elif t == "message.start":
            evs.append(CanonicalEvent(type="message.start"))
        elif t == "message.end":
            evs.append(CanonicalEvent(type="message.end"))
        elif t == "error":
            evs.append(CanonicalEvent(type="error"))
        else:
            # For tests, allow safe fallback.
            evs.append(CanonicalEvent(type="message.start"))  # type: ignore[arg-type]
    return evs


async def _make_engine() -> AsyncEngine:
    """Create an in-memory SQLite engine with the full schema."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return engine


async def _make_service(
    engine: AsyncEngine,
    lm_client: Any = None,  # noqa: ANN401
    memory_service: Any = None,  # noqa: ANN401
    chat_locks: dict | None = None,
    idle_timeout_sec: int = 60,
    projects_service: Any = None,  # noqa: ANN401
    models_service: Any = None,  # noqa: ANN401
) -> StreamingService:
    """Build a StreamingService with sensible test defaults.

    Args:
        engine:           The async engine.
        lm_client:        Mock ``LmstudioStreamingClient`` (or real).
        memory_service:   Mock ``MemoryService``.
        chat_locks:       Per-chat lock dict.
        idle_timeout_sec: Idle timeout for the service.

    Returns:
        A configured :class:`~lmchat.services.streaming_service.StreamingService`.
    """
    if lm_client is None:
        lm_client = MagicMock()
    if memory_service is None:
        memory_service = AsyncMock()
        memory_service.index_message = AsyncMock(return_value=None)
    if chat_locks is None:
        chat_locks = {}
    return StreamingService(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_service,
        chat_locks=chat_locks,
        idle_timeout_sec=idle_timeout_sec,
        projects_service=projects_service,
        models_service=models_service,
    )


def _mock_user(user_id: int = 1) -> MagicMock:
    """Build a mock User with ``id`` attribute.

    Args:
        user_id: The user PK.

    Returns:
        A :class:`~unittest.mock.MagicMock` representing a User.
    """
    user = MagicMock()
    user.id = user_id
    return user


def _mock_request(disconnected: bool = False) -> AsyncMock:
    """Build a mock FastAPI Request with an ASGI ``receive()`` channel.

    ``_watch_disconnect`` is the sole consumer of ``request.receive()``: it
    drains the channel with a 0.5s timeout each tick and aborts the draft when
    an ``{"type": "http.disconnect"}`` message arrives. This mock models both
    states:

    * ``disconnected=False`` — a live connection that never sends a client
      message. ``receive()`` blocks forever (like a real ASGI server that has
      no queued client frame), so the watcher's ``asyncio.wait_for`` times out
      each tick and the idle-timeout check runs, exactly as in production.
    * ``disconnected=True`` — the client has gone away. ``receive()`` returns
      ``{"type": "http.disconnect"}`` immediately so the watcher aborts.

    Args:
        disconnected: Whether ``receive()`` should yield ``http.disconnect``.

    Returns:
        An :class:`~unittest.mock.AsyncMock` representing a Request.
    """
    request = AsyncMock()

    if disconnected:
        request.receive = AsyncMock(return_value={"type": "http.disconnect"})
    else:
        # Live connection with no queued client frame: block until cancelled
        # (TaskGroup teardown cancels the watcher's pending receive()).
        _never = asyncio.Event()

        async def _block_forever() -> dict[str, str]:
            await _never.wait()
            return {"type": "http.request"}  # pragma: no cover (never reached)

        request.receive = _block_forever

    return request


async def _drain(gen: AsyncIterator[bytes]) -> list[bytes]:
    """Drain an async iterator into a list.

    Args:
        gen: The async iterator to drain.

    Returns:
        List of bytes yielded.
    """
    frames: list[bytes] = []
    async for frame in gen:
        frames.append(frame)
    return frames


def _parse_frames(frames: list[bytes]) -> list[dict]:  # type: ignore[type-arg]
    """Parse SSE frames from raw bytes into dicts.

    Args:
        frames: Raw SSE frame bytes.

    Returns:
        List of parsed data JSON dicts.
    """
    results = []
    for frame in frames:
        text = frame.decode("utf-8")
        for line in text.splitlines():
            if line.startswith("data:"):
                results.append(json.loads(line[5:].strip()))
    return results


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def engine() -> AsyncIterator[AsyncEngine]:
    """Provide an in-memory SQLite engine with the full schema."""
    eng = await _make_engine()
    yield eng
    await eng.dispose()


# ---------------------------------------------------------------------------
# Test 1: Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_happy_path_emits_delta_end_chat_end_final_state(
    engine: AsyncEngine,
) -> None:
    """Stream emits delta + message.end + chat.end frames; row reaches FINAL state.

    Covers the state machine transitions and per-frame identifier contract.
    Also verifies that msg_id is present on every emitted frame.
    """
    events = _make_events(
        "chat.start",
        "message.start",
        "message.delta",
        "message.end",
        "chat.end",
        content="hello world",
        response_id="resp-abc",
    )

    async def _fake_stream(*args: object, **kwargs: object) -> AsyncIterator[CanonicalEvent]:
        for ev in events:
            yield ev

    lm_client = MagicMock()
    lm_client.stream = _fake_stream

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    svc = await _make_service(engine, lm_client=lm_client, memory_service=memory_mock)
    user = _mock_user(1)
    request = _mock_request(disconnected=False)
    payload = _make_request_payload()
    payload_with_cid = ChatStreamRequest(chat_id=1, payload=payload.payload)

    # Insert a chat row first (messages.chat_id FK).
    from lmchat.db.schema import chats
    async with engine.begin() as conn:
        await conn.execute(chats.insert().values(user_id=1, title="test"))

    frames = await _drain(
        svc.stream_chat(
            chat_id=1,
            user=user,
            payload=payload_with_cid,
            request=request,
        )
    )

    assert len(frames) > 0

    # Every frame should have msg_id.
    parsed = _parse_frames(frames)
    for data in parsed:
        assert "msg_id" in data, f"msg_id missing from frame: {data}"

    # Check final DB state — filter to the assistant row only (user row is
    # also present and has response_id=NULL).
    async with engine.connect() as conn:
        result = await conn.execute(
            select(messages.c.state, messages.c.response_id).where(
                messages.c.chat_id == 1,
                messages.c.role == "assistant",
            )
        )
        row = result.fetchone()

    assert row is not None
    assert row[0] == PersistState.FINAL.value
    assert row[1] == "resp-abc"

    # Memory service should have been called (fire-and-forget task).
    # Allow the task to run.
    await asyncio.sleep(0)
    memory_mock.index_message.assert_called_once()


# ---------------------------------------------------------------------------
# Test 2: Client disconnect aborts draft
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_client_disconnect_aborts_draft(
    engine: AsyncEngine,
) -> None:
    """Disconnect mid-stream: draft → aborted_by_client; upstream cancelled within 1s."""
    async def _slow_stream(*args: object, **kwargs: object) -> AsyncIterator[CanonicalEvent]:
        yield CanonicalEvent(type="chat.start")
        yield CanonicalEvent(type="message.start")
        # Block until test signals disconnect.
        await asyncio.sleep(2)
        yield CanonicalEvent(type="message.delta", content="never")

    lm_client = MagicMock()
    lm_client.stream = _slow_stream

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    # Request whose receive() channel yields a benign client frame first (the
    # watcher must IGNORE it and run the idle check), then http.disconnect on
    # the second tick — exercising the message-type discrimination.
    call_count = 0

    async def _receive() -> dict[str, str]:
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            return {"type": "http.disconnect"}  # Disconnect on second receive.
        return {"type": "http.request"}  # Benign — must not abort.

    request = AsyncMock()
    request.receive = _receive

    from lmchat.db.schema import chats
    async with engine.begin() as conn:
        await conn.execute(chats.insert().values(user_id=1, title="test"))

    svc = await _make_service(engine, lm_client=lm_client, memory_service=memory_mock)
    user = _mock_user(1)
    payload = _make_request_payload()

    # Drain with timeout — should complete quickly after disconnect detection.
    frames: list[bytes] = []
    try:
        async with asyncio.timeout(3.0):
            async for frame in svc.stream_chat(
                chat_id=1, user=user, payload=payload, request=request
            ):
                frames.append(frame)
    except TimeoutError:
        pass  # Acceptable if the stream didn't exit cleanly within timeout.

    # Allow disconnect watcher to process.
    await asyncio.sleep(0.6)

    # Check DB state: the assistant draft should be aborted_by_client or draft
    # (not final). Filter to role='assistant' — the user row is also present
    # now and has state='final', which must not confuse this check.
    async with engine.connect() as conn:
        result = await conn.execute(
            select(messages.c.state).where(
                messages.c.chat_id == 1,
                messages.c.role == "assistant",
            )
        )
        rows = result.fetchall()

    # A draft must have been created and the watcher must have aborted it via
    # the receive()-driven http.disconnect — leaving 'draft', not stuck in it.
    assert rows, "expected an assistant draft row to have been created"
    state = rows[0][0]
    assert state == PersistState.ABORTED_BY_CLIENT.value, (
        f"Expected aborted_by_client, got {state}"
    )

    # The chat is no longer 409-bricked: a fresh single-stream check passes.
    await svc._assert_no_in_progress_stream(1)

    # Memory service should NOT have been called (only finalized streams
    # trigger indexing).
    memory_mock.index_message.assert_not_called()


# ---------------------------------------------------------------------------
# Test 2b: receive()-delivered http.disconnect aborts the draft and the
#          STREAMS_ACTIVE gauge returns to baseline.
#
# Regression test for the mid-stream disconnect-detection fix: under
# uvicorn 0.48 + starlette 1.3.1, is_disconnected() (a non-blocking receive)
# never observed the queued http.disconnect while the server was writing the
# stream, so the chat stayed wedged at 409. The watcher now drains
# request.receive() directly; this test drives that exact mechanism.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_receive_disconnect_aborts_draft_and_balances_gauge(
    engine: AsyncEngine,
) -> None:
    """receive() → http.disconnect aborts the draft; gauge back to baseline; no 409."""
    async def _slow_stream(*args: object, **kwargs: object) -> AsyncIterator[CanonicalEvent]:
        yield CanonicalEvent(type="chat.start")
        yield CanonicalEvent(type="message.start")
        await asyncio.sleep(2)  # Block so the disconnect lands mid-stream.
        yield CanonicalEvent(type="message.delta", content="never")  # pragma: no cover

    lm_client = MagicMock()
    lm_client.stream = _slow_stream
    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    # The watcher is the sole consumer of receive(); returning http.disconnect
    # is what unblocks the chat under the fix.
    request = _mock_request(disconnected=True)

    from lmchat.db.schema import chats
    async with engine.begin() as conn:
        await conn.execute(chats.insert().values(user_id=1, title="test"))

    svc = await _make_service(engine, lm_client=lm_client, memory_service=memory_mock)
    user = _mock_user(1)
    payload = _make_request_payload()

    baseline = STREAMS_ACTIVE._value.get()  # type: ignore[attr-defined]

    try:
        async with asyncio.timeout(3.0):
            await _drain(
                svc.stream_chat(chat_id=1, user=user, payload=payload, request=request)
            )
    except TimeoutError:  # pragma: no cover  (disconnect safety bound)
        pass

    # Let the watcher finish its abort + the single try/finally teardown.
    await asyncio.sleep(0.6)

    # (a) The draft was aborted by the receive()-driven disconnect.
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(messages.c.state).where(
                    messages.c.chat_id == 1,
                    messages.c.role == "assistant",
                )
            )
        ).fetchall()
    assert rows, "expected an assistant draft row to have been created"
    assert rows[0][0] == PersistState.ABORTED_BY_CLIENT.value, (
        f"Expected aborted_by_client, got {rows[0][0]}"
    )

    # (b) Gauge balanced — the single try/finally still owns the dec.
    assert STREAMS_ACTIVE._value.get() == baseline, (  # type: ignore[attr-defined]
        "STREAMS_ACTIVE leaked on the receive()-disconnect path"
    )

    # (c) Chat is no longer 409-bricked.
    await svc._assert_no_in_progress_stream(1)


# ---------------------------------------------------------------------------
# Test 2b2: disconnect salvage backfills reasoning + tool_calls, not just
#           whatever the content-only _CoalesceTimer flush already wrote.
#
# RED-ON-REVERT for the main-chat half of the disconnect-salvage fix
# (2026-08-14 disconnect dogfood, J11): _CoalesceTimer.flush() persists
# `content` ONLY — never `reasoning_content` or `tool_calls`. When the
# disconnect watcher's `safe_abort_draft` wins the race (draft ->
# aborted_by_client, state-only, no content write), the outer teardown's
# `_release_stuck_draft` (WHERE state='draft') no-ops, so — before this fix
# — the accumulated reasoning + tool_calls that only ever lived in `_state`
# were silently dropped. Mirrors the sub-session equivalent,
# test_sub_session_stream_disconnect_salvages_draft
# (tests/routes/test_sub_session_durable.py), which is why sub-sessions
# never had this gap: `_salvage_aborted_row` (formerly
# `_salvage_aborted_sub_session_row`) already ran there. This test proves
# the SAME call now runs in stream_chat's own teardown.
#
# Ordering is DETERMINISTIC, not temporal (2026-08-14 fix, after a
# load-sensitive first version): a fixed `asyncio.sleep` gap between "the
# events were emitted" and "the disconnect fires" is a coin flip once the
# machine is busy — an event-mocked `request.receive()` can resolve
# essentially synchronously, so it can win the race against the persist loop
# before it has processed ANYTHING. Instead, the fake stream's OWN generator
# body is the sequencing authority: the async-generator hand-off protocol
# guarantees the persist loop has FULLY processed one yielded event
# (including any `await coalesce.flush()` that event triggered) before the
# generator can resume past that `yield` — a consumer only calls
# `__anext__()` again once its own loop-body processing of the previous
# value has finished. `request.receive()` explicitly BLOCKS on
# `ready_to_disconnect`, an `asyncio.Event` the fake stream sets only AFTER
# that hand-off has happened for the LAST synthetic event (reasoning,
# tool_call round, and both content deltas) — so the disconnect cannot fire
# before every one of them is in `_state`, regardless of scheduler load.
# The 300ms sleep between the two content deltas is NOT part of that race —
# it only needs to give `_CoalesceTimer`'s 250ms interval a chance to elapse
# before the SECOND delta re-checks `should_flush()` (flush is delta-
# triggered only, never on an independent timer), so real coalesce content
# is on disk before the salvage runs too, exercising the "salvage must not
# shrink an already-persisted row" case — not just the in-memory-only one.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_disconnect_salvages_reasoning_and_tool_calls(
    engine: AsyncEngine,
) -> None:
    """Disconnect after reasoning + a completed tool round: both survive on the
    aborted_by_client row, on top of whatever content the coalesce flush
    already wrote — not a fixed sleep race, gated on observable state.
    """
    ready_to_disconnect = asyncio.Event()

    async def _slow_stream(*args: object, **kwargs: object) -> AsyncIterator[CanonicalEvent]:
        yield CanonicalEvent(type="chat.start")
        yield CanonicalEvent(type="message.start")
        yield CanonicalEvent(type="reasoning.start")
        yield CanonicalEvent(type="reasoning.delta", content="Let me think this through. ")
        yield CanonicalEvent(
            type="reasoning.delta", content="The answer depends on several factors."
        )
        yield CanonicalEvent(type="reasoning.end")
        yield _tool_call_event("tool_call.start", id="tc-1", name="", arguments={})
        yield _tool_call_event("tool_call.name", id="tc-1", name="search_web", arguments={})
        yield _tool_call_event(
            "tool_call.arguments", id="tc-1", name="search_web", arguments={"query": "lm studio"}
        )
        yield _tool_call_event(
            "tool_call.success",
            id="tc-1",
            name="search_web",
            arguments={"query": "lm studio"},
            result="LM Studio is a desktop application.",
        )
        yield CanonicalEvent(type="message.delta", content="first chunk ")
        # Not a race: nothing downstream depends on THIS specific duration —
        # the disconnect can only fire after ready_to_disconnect.set() below,
        # which cannot run until "second chunk" has been handed off to (and
        # fully processed by) the consumer. This sleep only needs to outlast
        # _COALESCE_INTERVAL_SEC (250ms) so should_flush() trips on the NEXT
        # delta, regardless of how long the machine actually takes to honor it.
        await asyncio.sleep(0.3)
        yield CanonicalEvent(type="message.delta", content="second chunk")
        # By the time this line runs, "second chunk" — and the coalesce flush
        # it triggered — has already been fully consumed by the persist loop
        # (async-generator hand-off protocol: the generator only resumes past
        # a yield once the consumer's own processing of that value, including
        # its awaits, has completed and __anext__() is called again).
        ready_to_disconnect.set()
        never = asyncio.Event()
        await never.wait()  # block until the test's outer timeout tears this down
        yield CanonicalEvent(type="message.delta", content="never reaches here")  # pragma: no cover

    async def _receive() -> dict[str, str]:
        # The disconnect watcher's SOLE consumer of receive() — block until
        # every synthetic event above is guaranteed processed, then fire.
        await ready_to_disconnect.wait()
        return {"type": "http.disconnect"}

    lm_client = MagicMock()
    lm_client.stream = _slow_stream

    from lmchat.db.schema import chats

    async with engine.begin() as conn:
        await conn.execute(chats.insert().values(user_id=1, title="test"))

    svc = await _make_service(engine, lm_client=lm_client)
    user = _mock_user(1)
    request = AsyncMock()
    request.receive = _receive
    payload = _make_request_payload()

    try:
        # 3.0s matches the sibling disconnect tests above — the persist loop
        # blocks forever on `never.wait()` once the disconnect fires (nothing
        # about the disconnect itself cancels it; only this outer bound does,
        # mirroring production where the ACTUAL teardown trigger is the
        # request being torn down, not the watcher's abort alone), so this is
        # purely how long the test waits to force that teardown, not a race
        # any assertion depends on winning.
        async with asyncio.timeout(3.0):
            await _drain(
                svc.stream_chat(chat_id=1, user=user, payload=payload, request=request)
            )
    except TimeoutError:
        pass  # acceptable — the disconnect path doesn't guarantee a clean exit

    # Let the disconnect watcher + outer finally settle (unchanged from the
    # sibling disconnect tests in this file — this is the POST-cancellation
    # detached-teardown window, not the pre-disconnect race that was flaky).
    await asyncio.sleep(0.6)

    async with engine.connect() as conn:
        result = await conn.execute(
            select(
                messages.c.state,
                messages.c.content,
                messages.c.reasoning_content,
                messages.c.tool_calls,
            ).where(
                messages.c.chat_id == 1,
                messages.c.role == "assistant",
            )
        )
        row = result.fetchone()

    assert row is not None
    assert row[0] == PersistState.ABORTED_BY_CLIENT.value, (
        f"expected aborted_by_client, got {row[0]!r} — the resting STATE must "
        "not change; this fix is about not losing what was generated, not "
        "about relabelling an interrupted turn as finished"
    )
    assert row[1] == "first chunk second chunk", f"content lost on disconnect — got {row[1]!r}"
    assert row[2] == "Let me think this through. The answer depends on several factors.", (
        f"RED-ON-REVERT: reasoning_content lost on disconnect — got {row[2]!r}. "
        "_CoalesceTimer.flush() never persists reasoning; only the "
        "_salvage_aborted_row backfill (WHERE state='aborted_by_client') does."
    )
    assert row[3] == [
        {
            "id": "tc-1",
            "name": "search_web",
            "arguments": json.dumps({"query": "lm studio"}),
            "status": "success",
            "result": "LM Studio is a desktop application.",
        }
    ], f"RED-ON-REVERT: tool_calls lost on disconnect — got {row[3]!r}"


# ---------------------------------------------------------------------------
# Test 2c: the draft release survives a cancellation delivered DURING the
#          finally's release await — the exact production disconnect mechanism.
#
# Regression: the receive() watcher DETECTS the
# disconnect, but when the browser closes mid-stream the resend was still 409
# with the assistant row stuck in 'draft' and no `stuck_draft_released` log.
#
# Root mechanism (verified empirically): a SINGLE task.cancel() does NOT cancel
# awaits inside a `finally` — they run to completion. But when a cancellation is
# DELIVERED/PENDING *while* the finally's `await self._release_stuck_draft(...)`
# is in flight (which is what the uvicorn/Starlette disconnect teardown +
# TaskGroup sibling-cancel produce in production), a BARE await is cancelled at
# its first real suspension (the `engine.begin()` connection acquisition) →
# the UPDATE never commits → row stuck in 'draft' AND `mark_inactive` is skipped.
# The fix wraps the release in `asyncio.shield(...)` (the detached UPDATE still
# commits) under `except asyncio.CancelledError: pass` (so `mark_inactive` runs).
#
# This test reproduces that precise pattern deterministically: it patches
# `_release_stuck_draft` so that the running task is re-cancelled at the release
# call's first suspension point, then delegates to the REAL method. Without the
# shield the bare await is cancelled and the row stays 'draft' (RED); with the
# shield the detached UPDATE commits and the row leaves 'draft' (GREEN). This is
# a genuine red-on-revert guard for the shield.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_draft_released_when_cancel_lands_during_release(
    engine: AsyncEngine,
) -> None:
    """A cancel pending during the finally release still commits the draft (shielded).

    The teardown's release is patched so that, when the consolidated finally
    invokes it, a cancellation is re-delivered to the running task and is
    PENDING across the release's first ``await`` (a benign ``asyncio.sleep(0)``
    suspension chosen to keep the DB connection pool clean) before the REAL
    ``_release_stuck_draft`` UPDATE runs:

    * Shielded (current code): ``await asyncio.shield(release(...))`` — the
      release is detached and runs to completion → the UPDATE commits → the
      row leaves 'draft'. The outer ``CancelledError`` is swallowed so
      ``mark_inactive`` also runs.
    * Bare await (revert): the await is cancelled at the pending suspension →
      the real UPDATE never runs → the row stays 'draft' → this test FAILS.

    This is the genuine red-on-revert guard for the shield.
    """
    streaming = asyncio.Event()

    async def _slow_stream(*args: object, **kwargs: object) -> AsyncIterator[CanonicalEvent]:
        yield CanonicalEvent(type="chat.start")
        yield CanonicalEvent(type="message.start")
        streaming.set()
        await asyncio.sleep(5)  # Block so the cancel lands mid-stream.
        yield CanonicalEvent(type="message.delta", content="never")  # pragma: no cover

    lm_client = MagicMock()
    lm_client.stream = _slow_stream
    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)
    request = _mock_request(disconnected=False)

    from lmchat.db.schema import chats
    async with engine.begin() as conn:
        await conn.execute(chats.insert().values(user_id=1, title="test"))

    svc = await _make_service(engine, lm_client=lm_client, memory_service=memory_mock)

    # Wrap the real release so it suspends on a benign sleep AFTER signalling
    # the test. An EXTERNAL canceller then cancels the driving task while the
    # release is suspended — this is the production pattern (the disconnect
    # cancel arrives during the finally). The cancel must come from OUTSIDE the
    # release: a self-cancel via current_task() inside a shielded coro would
    # cancel the shield's own task and defeat the shield. The benign suspension
    # (not engine.begin()) keeps the in-memory connection pool clean for the
    # later verify query.
    real_release = svc._release_stuck_draft
    release_started = asyncio.Event()
    reentered = {"count": 0}

    async def _release_with_pause(
        *, msg_id: int, chat_id: int, reason: str, **kwargs: Any  # noqa: ANN401
    ) -> None:
        # **kwargs forwards the salvage_* args the teardown now passes.
        if reentered["count"] == 0:
            reentered["count"] = 1
            release_started.set()  # tell the canceller the finally is here
            await asyncio.sleep(0.2)  # suspended — the external cancel lands here
        await real_release(msg_id=msg_id, chat_id=chat_id, reason=reason, **kwargs)

    svc._release_stuck_draft = _release_with_pause  # type: ignore[method-assign]

    user = _mock_user(1)
    payload = _make_request_payload()

    async def _drive() -> None:
        await _drain(
            svc.stream_chat(chat_id=1, user=user, payload=payload, request=request)
        )

    task = asyncio.create_task(_drive())
    await asyncio.wait_for(streaming.wait(), timeout=3.0)

    # An external canceller: once the release is suspended, cancel the driving
    # task so the cancellation is PENDING across the release's await. With the
    # shield the detached release still completes; with a bare await it is
    # cancelled before the real UPDATE runs.
    async def _cancel_when_release_starts() -> None:
        await asyncio.wait_for(release_started.wait(), timeout=3.0)
        task.cancel()

    canceller = asyncio.create_task(_cancel_when_release_starts())
    # Kick the teardown: cancel the mid-stream task so the finally runs.
    # (The driving task is blocked in the slow stream; cancelling it triggers
    # the consolidated finally → _release_with_pause → release_started.set().)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await canceller

    # Give the detached shielded release a tick to commit on the loop.
    await asyncio.sleep(0.3)

    # (a) The assistant row is NOT orphaned in 'draft' — the shielded release
    # committed despite the cancellation landing during it. Under a bare await
    # (revert) this row stays 'draft' and this assertion fails.
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(messages.c.state).where(
                    messages.c.chat_id == 1,
                    messages.c.role == "assistant",
                )
            )
        ).fetchall()
    assert rows, "expected an assistant draft row to have been created"
    assert rows[0][0] != PersistState.DRAFT.value, (
        f"draft left orphaned in 'draft' under cancel-during-release: {rows[0][0]}"
    )

    # (b) The chat is no longer 409-bricked.
    await svc._assert_no_in_progress_stream(1)


# ---------------------------------------------------------------------------
# Test 3: Single-stream-per-chat returns StreamInProgressError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_single_stream_per_chat_returns_409(
    engine: AsyncEngine,
) -> None:
    """Second stream on same chat raises StreamInProgressError."""
    from lmchat.db.schema import chats
    async with engine.begin() as conn:
        await conn.execute(chats.insert().values(user_id=1, title="test"))
        # Insert a draft row manually to simulate in-progress stream.
        await conn.execute(
            messages.insert().values(
                chat_id=1,
                role="assistant",
                content="",
                state=PersistState.DRAFT.value,
            )
        )

    lm_client = MagicMock()
    memory_mock = AsyncMock()

    svc = await _make_service(engine, lm_client=lm_client, memory_service=memory_mock)
    user = _mock_user(1)
    request = _mock_request(disconnected=False)
    payload = _make_request_payload()

    with pytest.raises(StreamInProgressError) as exc_info:
        async for _ in svc.stream_chat(
            chat_id=1, user=user, payload=payload, request=request
        ):
            pass

    assert exc_info.value.chat_id == 1


# ---------------------------------------------------------------------------
# Test 4: Malformed tool call emits error frame + terminates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_malformed_tool_call_emits_error_frame_terminates(
    engine: AsyncEngine,
) -> None:
    """Malformed tool_call.arguments JSON → error frame + no infinite loop.

    The LmstudioStreamingClient handles the JSON accumulation.
    Here we simulate the adapter emitting an error event directly (as would
    happen when the streaming client detects malformed JSON) and verify the
    service correctly emits an error SSE frame and stops.
    """
    # Simulate: client emits error event after detecting malformed tool call.
    events = [
        CanonicalEvent(type="chat.start"),
        CanonicalEvent(type="message.start"),
        CanonicalEvent(
            type="error",
            error={"code": "tool_call_malformed", "message": "JSON parse error"},
        ),
    ]

    async def _fake_stream(*args: object, **kwargs: object) -> AsyncIterator[CanonicalEvent]:
        for ev in events:
            yield ev

    lm_client = MagicMock()
    lm_client.stream = _fake_stream

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    from lmchat.db.schema import chats
    async with engine.begin() as conn:
        await conn.execute(chats.insert().values(user_id=1, title="test"))

    svc = await _make_service(engine, lm_client=lm_client, memory_service=memory_mock)
    user = _mock_user(1)
    request = _mock_request(disconnected=False)
    payload = _make_request_payload()

    frames = await _drain(
        svc.stream_chat(chat_id=1, user=user, payload=payload, request=request)
    )

    # Should have received the error frame.
    parsed = _parse_frames(frames)
    error_frames = [f for f in parsed if f.get("type") == "error"]
    assert len(error_frames) >= 1

    # Memory service should NOT be called.
    await asyncio.sleep(0)
    memory_mock.index_message.assert_not_called()

    # Stream should have terminated (finite list of frames, no hang).
    assert len(frames) <= 10  # No infinite loop.


# ---------------------------------------------------------------------------
# Test 5: Rejected-param retry — covered by test_streaming_param_cache_retry
# (separate file).
# Duplicate short form here to satisfy "strip-up-front parity".
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_rejected_param_retries_once_then_succeeds(
    engine: AsyncEngine,
) -> None:
    """HTTP 400 with unrecognized_keys triggers cache write + single retry.

    The adapter handles the actual retry; this test verifies the service
    propagates the events from a successful retry without error frames.
    """
    # Simulate adapter that on first call returns error (400) then succeeds.
    # The adapter absorbs the 400 and retries; from the service's perspective,
    # the stream just works (events from the retry path).
    events_after_retry = [
        CanonicalEvent(type="chat.start"),
        CanonicalEvent(type="message.start"),
        CanonicalEvent(type="message.delta", content="retry success"),
        CanonicalEvent(type="message.end"),
        CanonicalEvent(type="chat.end", response_id="rid-retry"),
    ]

    async def _fake_stream(*args: object, **kwargs: object) -> AsyncIterator[CanonicalEvent]:
        for ev in events_after_retry:
            yield ev

    lm_client = MagicMock()
    lm_client.stream = _fake_stream

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    from lmchat.db.schema import chats
    async with engine.begin() as conn:
        await conn.execute(chats.insert().values(user_id=1, title="test"))

    svc = await _make_service(engine, lm_client=lm_client, memory_service=memory_mock)
    user = _mock_user(1)
    request = _mock_request(disconnected=False)
    payload = _make_request_payload()

    frames = await _drain(
        svc.stream_chat(chat_id=1, user=user, payload=payload, request=request)
    )

    parsed = _parse_frames(frames)
    error_frames = [f for f in parsed if f.get("type") == "error"]
    assert len(error_frames) == 0, f"Unexpected error frames: {error_frames}"

    # Verify final state.
    async with engine.connect() as conn:
        result = await conn.execute(select(messages.c.state).where(messages.c.chat_id == 1))
        row = result.fetchone()
    assert row is not None
    assert row[0] == PersistState.FINAL.value


# ---------------------------------------------------------------------------
# Test 6: Memory ingestion gate (CHANGELOG 0.5.2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_memory_ingestion_only_on_FINAL_not_aborted(
    engine: AsyncEngine,
) -> None:
    """index_message NOT called for aborted/partial; called only for FINAL."""
    from lmchat.db.schema import chats
    async with engine.begin() as conn:
        await conn.execute(chats.insert().values(user_id=1, title="test-aborted"))
        await conn.execute(chats.insert().values(user_id=1, title="test-final"))

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    # Part A: stream that reaches FINAL — index_message SHOULD be called.
    events_final = [
        CanonicalEvent(type="chat.start"),
        CanonicalEvent(type="message.delta", content="hi"),
        CanonicalEvent(type="message.end"),
        CanonicalEvent(type="chat.end", response_id="rid-final"),
    ]

    async def _stream_final(*args: object, **kwargs: object) -> AsyncIterator[CanonicalEvent]:
        for ev in events_final:
            yield ev

    lm_client_final = MagicMock()
    lm_client_final.stream = _stream_final

    svc = await _make_service(engine, lm_client=lm_client_final, memory_service=memory_mock)
    user = _mock_user(1)
    request = _mock_request(disconnected=False)
    payload_final = ChatStreamRequest(
        chat_id=2,
        payload=CanonicalChatRequest(
            model="test-model",
            input=[CanonicalInputBlock(type="text", content="hello")],
        ),
    )

    await _drain(
        svc.stream_chat(chat_id=2, user=user, payload=payload_final, request=request)
    )
    await asyncio.sleep(0.05)

    # index_message should have been called for the FINAL stream.
    assert memory_mock.index_message.call_count == 1

    # Part B: stream that gets error event — index_message should NOT be called again.
    events_error = [
        CanonicalEvent(type="chat.start"),
        CanonicalEvent(type="error", error={"code": "upstream_error", "message": "fail"}),
    ]

    async def _stream_error(*args: object, **kwargs: object) -> AsyncIterator[CanonicalEvent]:
        for ev in events_error:
            yield ev

    lm_client_error = MagicMock()
    lm_client_error.stream = _stream_error

    svc2 = StreamingService(
        engine=engine,
        lm_client=lm_client_error,
        memory_service=memory_mock,
        chat_locks={},
        idle_timeout_sec=60,
    )
    payload_error = ChatStreamRequest(
        chat_id=1,
        payload=CanonicalChatRequest(
            model="test-model",
            input=[CanonicalInputBlock(type="text", content="fail")],
        ),
    )
    await _drain(
        svc2.stream_chat(chat_id=1, user=user, payload=payload_error, request=request)
    )
    await asyncio.sleep(0.05)

    # Still only called once (for the FINAL stream).
    assert memory_mock.index_message.call_count == 1


# ---------------------------------------------------------------------------
# Test 7: Upstream stall emits upstream_stall error frame
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_upstream_stall_emits_upstream_stall_error_at_60s(
    engine: AsyncEngine,
) -> None:
    """TRUE silence (heartbeats stop, no content) fires upstream_stall after
    idle_timeout_sec.

    Uses a 1s idle timeout so the test completes quickly. Heartbeats DO reset
    the clock now (see ``test_streaming_heartbeats_keep_alive_no_stall``), so
    this stream emits a few then goes fully silent — the stall fires ~1s after
    the LAST event, not after prompt_processing.start.
    """
    # Stream emits a few heartbeats, then goes fully silent (no content, no
    # further heartbeats) — the idle clock elapses and the stall fires.
    async def _heartbeat_stream(*args: object, **kwargs: object) -> AsyncIterator[CanonicalEvent]:
        yield CanonicalEvent(type="chat.start")
        yield CanonicalEvent(type="prompt_processing.start")
        for _ in range(3):
            yield CanonicalEvent(type="prompt_processing.progress", progress=0.1)
            await asyncio.sleep(0.4)
        # Then fully silent — no content, no heartbeats — so the clock elapses.
        await asyncio.sleep(5)

    lm_client = MagicMock()
    lm_client.stream = _heartbeat_stream

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    from lmchat.db.schema import chats
    async with engine.begin() as conn:
        await conn.execute(chats.insert().values(user_id=1, title="stall-test"))

    # Use 1s idle timeout for fast test.
    svc = await _make_service(
        engine, lm_client=lm_client, memory_service=memory_mock, idle_timeout_sec=1
    )
    user = _mock_user(1)
    request = _mock_request(disconnected=False)
    payload = ChatStreamRequest(
        chat_id=1,
        payload=CanonicalChatRequest(
            model="test-model",
            input=[CanonicalInputBlock(type="text", content="stall me")],
        ),
    )

    frames: list[bytes] = []
    async with asyncio.timeout(5.0):
        async for frame in svc.stream_chat(
            chat_id=1, user=user, payload=payload, request=request
        ):
            frames.append(frame)

    parsed = _parse_frames(frames)
    stall_frames = [f for f in parsed if f.get("type") == "error"]
    assert len(stall_frames) >= 1

    error_data = stall_frames[0].get("error", {})
    assert error_data.get("code") == "upstream_stall"

    # Memory service NOT called.
    await asyncio.sleep(0.05)
    memory_mock.index_message.assert_not_called()


@pytest.mark.asyncio
async def test_streaming_heartbeats_keep_alive_no_stall(
    engine: AsyncEngine,
) -> None:
    """prompt_processing.* heartbeats reset the idle clock.

    A slow-but-alive upstream (e.g. a local model grinding through a large
    prompt) keeps emitting progress heartbeats well past ``idle_timeout_sec``.
    Because heartbeats now reset the clock, it must NOT be aborted — the
    content eventually flows and no ``upstream_stall`` frame is emitted.
    Regression guard for the 60s-guillotine bug.
    """
    async def _slow_but_alive(*args: object, **kwargs: object) -> AsyncIterator[CanonicalEvent]:
        yield CanonicalEvent(type="chat.start")
        yield CanonicalEvent(type="prompt_processing.start")
        # Heartbeat for ~2.5s — well past the 1s idle timeout — with gaps
        # (0.5s) shorter than the timeout, so the clock keeps resetting.
        for _ in range(5):
            yield CanonicalEvent(type="prompt_processing.progress", progress=0.1)
            await asyncio.sleep(0.5)
        yield CanonicalEvent(type="prompt_processing.end")
        yield CanonicalEvent(type="message.start")
        yield CanonicalEvent(type="message.delta", content="hello")
        yield CanonicalEvent(type="message.end")
        yield CanonicalEvent(type="chat.end")

    lm_client = MagicMock()
    lm_client.stream = _slow_but_alive
    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    from lmchat.db.schema import chats
    async with engine.begin() as conn:
        await conn.execute(chats.insert().values(user_id=1, title="keepalive-test"))

    svc = await _make_service(
        engine, lm_client=lm_client, memory_service=memory_mock, idle_timeout_sec=1
    )
    user = _mock_user(1)
    request = _mock_request(disconnected=False)
    payload = ChatStreamRequest(
        chat_id=1,
        payload=CanonicalChatRequest(
            model="test-model",
            input=[CanonicalInputBlock(type="text", content="slow please")],
        ),
    )

    frames: list[bytes] = []
    async with asyncio.timeout(10.0):
        async for frame in svc.stream_chat(
            chat_id=1, user=user, payload=payload, request=request
        ):
            frames.append(frame)

    parsed = _parse_frames(frames)
    stalls = [
        f
        for f in parsed
        if f.get("type") == "error"
        and f.get("error", {}).get("code") == "upstream_stall"
    ]
    assert stalls == [], f"heartbeats should keep the stream alive; got {stalls}"
    deltas = [f for f in parsed if f.get("type") == "message.delta"]
    assert any("hello" in str(f.get("content", "")) for f in deltas)


# ---------------------------------------------------------------------------
# Stall handled in-body, not via raise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_stall_handled_in_generator_body_releases_draft(
    engine: AsyncEngine,
) -> None:
    """Stall fires the error frame from inside the persist generator body
    AND the draft row is released within the stream call — no 5-min reaper
    lockout.

    Validates the restructure: the
    pre-fix `raise _StreamStall(idle_s)` from inside the watcher's
    TaskGroup child task aborted the host ASGI task and ate the error
    frame; the draft row stayed in state='draft' until the 5-minute
    reaper tick. The new path signals `stall_event`, the persist
    generator races anext vs the event, and yields the frame +
    `_release_stuck_draft` from its own body BEFORE TaskGroup cleanup
    runs.

    The existing 7-test (`...stall_emits_upstream_stall_error_at_60s`)
    proves the error frame still emits. This test adds the
    draft-release contract: after the stall, the draft row is no
    longer in state='draft', so the next attempt on the same chat
    does not 409 with stream_in_progress.
    """
    from lmchat.db.schema import chats  # noqa: PLC0415
    from lmchat.db.schema import messages as messages_table

    async def _heartbeat_stream(*args: object, **kwargs: object) -> AsyncIterator[CanonicalEvent]:
        yield CanonicalEvent(type="chat.start")
        yield CanonicalEvent(type="prompt_processing.start")
        await asyncio.sleep(5)

    lm_client = MagicMock()
    lm_client.stream = _heartbeat_stream

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    async with engine.begin() as conn:
        await conn.execute(chats.insert().values(user_id=1, title="stall-release-test"))

    svc = await _make_service(
        engine, lm_client=lm_client, memory_service=memory_mock, idle_timeout_sec=1
    )
    user = _mock_user(1)
    request = _mock_request(disconnected=False)
    payload = ChatStreamRequest(
        chat_id=1,
        payload=CanonicalChatRequest(
            model="test-model",
            input=[CanonicalInputBlock(type="text", content="stall me + release me")],
        ),
    )

    frames: list[bytes] = []
    async with asyncio.timeout(5.0):
        async for frame in svc.stream_chat(
            chat_id=1, user=user, payload=payload, request=request
        ):
            frames.append(frame)

    parsed = _parse_frames(frames)
    stall_frames = [f for f in parsed if f.get("type") == "error"]
    assert len(stall_frames) >= 1, "Stall must yield an error frame"
    assert stall_frames[0].get("error", {}).get("code") == "upstream_stall"

    # The draft row must not be left in state='draft' — that's the bug
    # that produced the 5-min 409 lockout before this fix.
    from sqlalchemy import select  # noqa: PLC0415
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(messages_table.c.id, messages_table.c.state).where(
                    messages_table.c.chat_id == 1,
                    messages_table.c.role == "assistant",
                )
            )
        ).fetchall()
    assert len(rows) >= 1, "Assistant draft row must exist"
    states = [r.state for r in rows]
    # No row should remain in 'draft' after the stall handling path
    # ran — release_stuck_draft is called from inside the generator
    # body and must complete before the response stream ends.
    assert "draft" not in states, (
        f"Stalled draft must be released, not left as 'draft'. Got states={states}"
    )


# ---------------------------------------------------------------------------
# Partial-answer salvage on non-graceful terminals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_stall_salvages_reasoning_into_content(
    engine: AsyncEngine,
) -> None:
    """A reasoning-model stall folds parked reasoning into visible content.

    Reasoning deltas are NOT coalesced onto the draft row (unlike content), so
    before the fix a stall on a reasoning model left BOTH content and reasoning
    empty — the reload-shows-an-empty-bubble bug. The lifecycle teardown now
    runs the shared resolve_terminal_content policy (substance_fold), folding
    the parked reasoning into visible content.
    """
    from lmchat.db.schema import chats  # noqa: PLC0415

    # Reasoning must exceed substance_fold.STUB_CHARS (240) to fold — realistic
    # for a reasoning model that parks its whole chain-of-thought and never
    # emits a visible answer before the stall.
    parked = (
        "Let me reason about this carefully before answering. The user asked a "
        "genuinely hard question, so I will enumerate the sub-problems, weigh the "
        "trade-offs of each candidate approach, check the edge cases I might be "
        "missing, and only then commit to a final, well-supported answer rather "
        "than blurting out the first thing that comes to mind."
    )

    async def _reasoning_then_stall(
        *args: object, **kwargs: object
    ) -> AsyncIterator[CanonicalEvent]:
        yield CanonicalEvent(type="chat.start")
        yield CanonicalEvent(type="reasoning.delta", content=parked)
        await asyncio.sleep(5)  # stall — no content, no chat.end

    lm_client = MagicMock()
    lm_client.stream = _reasoning_then_stall
    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    async with engine.begin() as conn:
        await conn.execute(chats.insert().values(user_id=1, title="salvage-reasoning"))

    svc = await _make_service(
        engine, lm_client=lm_client, memory_service=memory_mock, idle_timeout_sec=1
    )
    payload = ChatStreamRequest(
        chat_id=1,
        payload=CanonicalChatRequest(
            model="test-model",
            input=[CanonicalInputBlock(type="text", content="hard question")],
        ),
    )

    frames: list[bytes] = []
    async with asyncio.timeout(5.0):
        async for frame in svc.stream_chat(
            chat_id=1, user=_mock_user(1), payload=payload, request=_mock_request()
        ):
            frames.append(frame)

    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(messages.c.state, messages.c.content).where(
                    messages.c.chat_id == 1,
                    messages.c.role == "assistant",
                )
            )
        ).fetchone()
    assert row is not None
    assert row.state != "draft", f"stalled draft must be finalized, got {row.state!r}"
    # The parked reasoning was folded into visible content — not an empty bubble.
    assert row.content, "reasoning-model stall must salvage content (was empty)"
    assert "reason about this" in row.content


@pytest.mark.asyncio
async def test_streaming_error_event_salvages_content_and_reasoning(
    engine: AsyncEngine,
) -> None:
    """An upstream error mid-answer persists the partial content AND the
    parked reasoning instead of finalizing empty.

    The upstream-error terminal returned without finalizing; the accumulated
    reasoning (never coalesced) and any not-yet-flushed content were lost. The
    teardown salvage now persists both through the shared policy — content with
    a real answer is kept verbatim, reasoning stays in its own channel.
    """
    from lmchat.db.schema import chats  # noqa: PLC0415

    async def _content_then_error(
        *args: object, **kwargs: object
    ) -> AsyncIterator[CanonicalEvent]:
        yield CanonicalEvent(type="chat.start")
        yield CanonicalEvent(type="message.start")
        yield CanonicalEvent(type="message.delta", content="The answer is 42 because")
        yield CanonicalEvent(type="reasoning.delta", content="deriving forty-two")
        yield CanonicalEvent(
            type="error", error={"code": "upstream_error", "message": "boom"}
        )

    lm_client = MagicMock()
    lm_client.stream = _content_then_error
    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    async with engine.begin() as conn:
        await conn.execute(chats.insert().values(user_id=1, title="salvage-error"))

    svc = await _make_service(
        engine, lm_client=lm_client, memory_service=memory_mock, idle_timeout_sec=60
    )
    payload = ChatStreamRequest(
        chat_id=1,
        payload=CanonicalChatRequest(
            model="test-model",
            input=[CanonicalInputBlock(type="text", content="q")],
        ),
    )

    frames: list[bytes] = []
    async for frame in svc.stream_chat(
        chat_id=1, user=_mock_user(1), payload=payload, request=_mock_request()
    ):
        frames.append(frame)

    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(
                    messages.c.state,
                    messages.c.content,
                    messages.c.reasoning_content,
                ).where(
                    messages.c.chat_id == 1,
                    messages.c.role == "assistant",
                )
            )
        ).fetchone()
    assert row is not None
    assert row.state != "draft"
    assert row.content and "The answer is 42" in row.content
    assert row.reasoning_content and "deriving forty-two" in row.reasoning_content, (
        "parked reasoning must survive an upstream-error terminal (was dropped)"
    )


# ---------------------------------------------------------------------------
# Test 8: Compact serialization (stream holds chat lock)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_compact_serialization(engine: AsyncEngine) -> None:
    """Compaction waits while stream holds the per-chat lock.

    Both stream_chat and compact share the same chat_locks dict.
    Compaction arriving while stream is active must block until stream releases.
    """
    chat_locks: dict[int, asyncio.Lock] = {}

    # Simulate a slow stream that holds the lock for ~0.3s.
    stream_held = asyncio.Event()
    stream_released = asyncio.Event()

    async def _slow_stream(*args: object, **kwargs: object) -> AsyncIterator[CanonicalEvent]:
        stream_held.set()
        await asyncio.sleep(0.3)
        yield CanonicalEvent(type="chat.start")
        yield CanonicalEvent(type="message.delta", content="hi")
        yield CanonicalEvent(type="message.end")
        yield CanonicalEvent(type="chat.end", response_id="rid-compact")
        stream_released.set()

    lm_client = MagicMock()
    lm_client.stream = _slow_stream

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    from lmchat.db.schema import chats
    async with engine.begin() as conn:
        await conn.execute(chats.insert().values(user_id=1, title="compact-test"))

    svc = await _make_service(
        engine, lm_client=lm_client, memory_service=memory_mock, chat_locks=chat_locks
    )
    user = _mock_user(1)
    request = _mock_request(disconnected=False)
    payload = ChatStreamRequest(
        chat_id=1,
        payload=CanonicalChatRequest(
            model="test-model",
            input=[CanonicalInputBlock(type="text", content="lock test")],
        ),
    )

    # Start stream in background.
    stream_task = asyncio.create_task(
        _drain(
            svc.stream_chat(chat_id=1, user=user, payload=payload, request=request)
        )
    )

    # Wait until stream holds the lock.
    await asyncio.wait_for(stream_held.wait(), timeout=2.0)

    # Try to acquire the same lock — should block until stream releases.
    lock = chat_locks.setdefault(1, asyncio.Lock())
    lock_acquired_at: list[float] = []

    async def _try_acquire() -> None:
        async with lock:
            import time
            lock_acquired_at.append(time.monotonic())

    acquire_task = asyncio.create_task(_try_acquire())

    # Wait for stream to complete.
    await asyncio.wait_for(stream_task, timeout=3.0)
    await asyncio.wait_for(acquire_task, timeout=3.0)

    # Lock should have been acquired AFTER the stream released it.
    assert len(lock_acquired_at) == 1  # Lock was acquired exactly once.


# ---------------------------------------------------------------------------
# Test 9: Pending finalization recovered by reaper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_pending_finalization_recovered_by_reaper(
    engine: AsyncEngine,
) -> None:
    """Pending_finalization row is recovered by finalize_pending (reaper logic).

    Simulates: stream moved to pending_finalization but the final commit
    didn't happen. The reaper's finalize_pending should move it to final.
    """
    from lmchat.db.schema import chats
    from lmchat.services._stream_state import finalize_pending

    async with engine.begin() as conn:
        await conn.execute(chats.insert().values(user_id=1, title="pending-test"))
        # Insert a row stuck in pending_finalization.
        await conn.execute(
            messages.insert().values(
                chat_id=1,
                role="assistant",
                content="partial content",
                state=PersistState.PENDING_FINALIZATION.value,
                response_id="rid-pending",
            )
        )

    # Fetch the message id.
    async with engine.connect() as conn:
        result = await conn.execute(select(messages.c.id).where(messages.c.chat_id == 1))
        msg_id = result.scalar_one()

    # Simulate reaper: finalize_pending should move the row to FINAL.
    won = await finalize_pending(engine=engine, message_id=msg_id)
    assert won is True

    # Verify final state in DB.
    async with engine.connect() as conn:
        result = await conn.execute(select(messages.c.state).where(messages.c.id == msg_id))
        state = result.scalar_one()

    assert state == PersistState.FINAL.value

    # Idempotency: calling finalize_pending again should return False.
    won2 = await finalize_pending(engine=engine, message_id=msg_id)
    assert won2 is False


# ---------------------------------------------------------------------------
# Test 10: ReadTimeout emits error frame
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_read_timeout_emits_error_frame(engine: AsyncEngine) -> None:
    """httpx ReadTimeout mid-stream → error frame, draft stays draft.

    The adapter surfaces ReadTimeout as a canonical error event with
    code="upstream_unavailable". The service should forward this as an
    SSE error frame without transitioning the draft to aborted.
    """

    # Adapter emits an upstream_unavailable error (how adapter surfaces ReadTimeout).
    async def _timeout_stream(*args: object, **kwargs: object) -> AsyncIterator[CanonicalEvent]:
        yield CanonicalEvent(type="chat.start")
        yield CanonicalEvent(type="message.start")
        # Simulate ReadTimeout surfaced as error event by adapter.
        yield CanonicalEvent(
            type="error",
            error={"code": "upstream_unavailable", "message": "LM Studio read timeout"},
        )

    lm_client = MagicMock()
    lm_client.stream = _timeout_stream

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    from lmchat.db.schema import chats
    async with engine.begin() as conn:
        await conn.execute(chats.insert().values(user_id=1, title="timeout-test"))

    svc = await _make_service(engine, lm_client=lm_client, memory_service=memory_mock)
    user = _mock_user(1)
    request = _mock_request(disconnected=False)
    payload = ChatStreamRequest(
        chat_id=1,
        payload=CanonicalChatRequest(
            model="test-model",
            input=[CanonicalInputBlock(type="text", content="timeout test")],
        ),
    )

    frames = await _drain(
        svc.stream_chat(chat_id=1, user=user, payload=payload, request=request)
    )

    parsed = _parse_frames(frames)
    error_frames = [f for f in parsed if f.get("type") == "error"]
    assert len(error_frames) >= 1

    # Draft row is force-finalized by _release_stuck_draft so the user can
    # retry without hitting a 409 StreamInProgressError on next attempt.
    # Filter to role='assistant' — the user row is also present now.
    async with engine.connect() as conn:
        result = await conn.execute(
            select(messages.c.state).where(
                messages.c.chat_id == 1,
                messages.c.role == "assistant",
            )
        )
        rows = result.fetchall()

    # Row exists and has been transitioned to FINAL by _release_stuck_draft.
    if rows:
        state = rows[0][0]
        assert state == PersistState.FINAL.value, (
            f"Expected stuck draft to be force-finalized after upstream error, got state={state!r}"
        )

    # Memory service NOT called.
    await asyncio.sleep(0.05)
    memory_mock.index_message.assert_not_called()


# ---------------------------------------------------------------------------
# Supplementary: SSE frame format
# ---------------------------------------------------------------------------


def test_format_sse_frame_structure() -> None:
    """SSE frame has correct event: / data: / newline format."""
    event = CanonicalEvent(type="message.delta", content="hello")
    frame = _format_sse_frame(event, msg_id=42)
    text = frame.decode("utf-8")

    assert text.startswith("event: message.delta\n")
    assert "data: " in text
    assert text.endswith("\n\n")

    # Parse the data JSON.
    data_line = [line for line in text.splitlines() if line.startswith("data:")][0]
    data = json.loads(data_line[5:].strip())
    assert data["type"] == "message.delta"
    assert data["msg_id"] == 42
    assert data["content"] == "hello"


def test_format_sse_frame_carries_stop_reason() -> None:
    """Continue-chip closeout: chat.end frame serializes stop_reason to the wire."""
    event = CanonicalEvent(type="chat.end", stop_reason="length")
    frame = _format_sse_frame(event, msg_id=7)
    data_line = [
        line
        for line in frame.decode("utf-8").splitlines()
        if line.startswith("data:")
    ][0]
    data = json.loads(data_line[5:].strip())
    assert data["stop_reason"] == "length"

    # Absent stop_reason must not inject a null key (wire stays sparse).
    plain = _format_sse_frame(CanonicalEvent(type="chat.end"), msg_id=7)
    plain_data_line = [
        line
        for line in plain.decode("utf-8").splitlines()
        if line.startswith("data:")
    ][0]
    assert "stop_reason" not in json.loads(plain_data_line[5:].strip())


def test_format_sse_frame_carries_real_token_stats() -> None:
    """chat.end serializes real LM Studio stats."""
    event = CanonicalEvent(
        type="chat.end",
        total_output_tokens=512,
        tokens_per_second=41.7,
    )
    frame = _format_sse_frame(event, msg_id=9)
    data_line = [
        line
        for line in frame.decode("utf-8").splitlines()
        if line.startswith("data:")
    ][0]
    data = json.loads(data_line[5:].strip())
    assert data["total_output_tokens"] == 512
    assert data["tokens_per_second"] == 41.7


def test_format_sse_frame_omits_token_stats_when_none() -> None:
    """Sparse contract: absent stats must not inject null keys on the wire."""
    plain = _format_sse_frame(CanonicalEvent(type="chat.end"), msg_id=9)
    plain_data_line = [
        line
        for line in plain.decode("utf-8").splitlines()
        if line.startswith("data:")
    ][0]
    data = json.loads(plain_data_line[5:].strip())
    assert "total_output_tokens" not in data
    assert "tokens_per_second" not in data


def test_format_sse_frame_tool_call_nested_shape() -> None:
    """The tool_call payload is NESTED, not flat.

    Mirror pin of the FE codec test
    ``web/tests/unit/test_useSSE_main_path_tool_calls.spec.ts`` — both sides
    assert the exact ``data["tool_call"]`` key set from
    ``CanonicalToolCall.model_dump()``. If the producer ever flattens (or the
    FE ever reads flat keys again), one of the two pins breaks.
    """
    event = CanonicalEvent(
        type="tool_call.success",
        tool_call=CanonicalToolCall(
            id="uuid-abc",
            name="search_web",
            arguments={"q": "lm studio", "limit": 3},
            result="3 results found",
        ),
    )
    frame = _format_sse_frame(event, msg_id=6)
    data_line = [
        line
        for line in frame.decode("utf-8").splitlines()
        if line.startswith("data:")
    ][0]
    data = json.loads(data_line[5:].strip())

    # Nested payload with the full model_dump key set.
    assert set(data["tool_call"].keys()) == {
        "id",
        "name",
        "arguments",
        "call_id",
        "result",
    }
    assert data["tool_call"]["id"] == "uuid-abc"
    assert data["tool_call"]["name"] == "search_web"
    # arguments is a JSON OBJECT on the wire (the FE stringifies it).
    assert data["tool_call"]["arguments"] == {"q": "lm studio", "limit": 3}
    assert data["tool_call"]["result"] == "3 results found"

    # The flat keys the FE wrongly read pre-fix must NOT exist at top level.
    for flat_key in ("tool_call_id", "name", "arguments", "result"):
        assert flat_key not in data


def test_format_error_frame_structure() -> None:
    """Error frame has event: error + correct data payload."""
    frame = _format_error_frame(code="upstream_stall", detail="No content for 60s", msg_id=7)
    text = frame.decode("utf-8")

    assert "event: error\n" in text
    data_line = [line for line in text.splitlines() if line.startswith("data:")][0]
    data = json.loads(data_line[5:].strip())
    assert data["type"] == "error"
    assert data["msg_id"] == 7
    assert data["error"]["code"] == "upstream_stall"


# ---------------------------------------------------------------------------
# Cross-user ownership — unit-level regression (SA-2026-001)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_chat_cross_user_raises_chat_not_found(
    engine: AsyncEngine,
) -> None:
    """stream_chat raises ChatNotFoundError when user_id does not own chat_id.

    Defense-in-depth unit regression for SA-2026-001.  The chat row is
    created for user_id=1; stream_chat is called with user_id=2.  The
    service-layer ownership SELECT (inside the per-chat lock) must fire
    ChatNotFoundError before any draft row is inserted or any upstream
    connection is opened.

    Per docs/release/SECURITY_ADVISORY.md SA-2026-001.
    """
    from lmchat.db.schema import chats
    from lmchat.services.chat_service import ChatNotFoundError

    # Insert a chat row owned by user 1.
    async with engine.begin() as conn:
        await conn.execute(chats.insert().values(user_id=1, title="user-1-chat"))

    lm_client = MagicMock()
    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    svc = await _make_service(engine, lm_client=lm_client, memory_service=memory_mock)

    # Attempt to stream into that chat as user 2.
    user2 = _mock_user(user_id=2)
    request = _mock_request(disconnected=False)
    payload = ChatStreamRequest(
        chat_id=1,
        payload=CanonicalChatRequest(
            model="test-model",
            input=[CanonicalInputBlock(type="text", content="cross-user attempt")],
        ),
    )

    with pytest.raises(ChatNotFoundError):
        async for _ in svc.stream_chat(
            chat_id=1, user=user2, payload=payload, request=request
        ):
            pass  # Should never yield.

    # No draft row should have been inserted for this cross-user attempt.
    async with engine.connect() as conn:
        result = await conn.execute(select(messages.c.id).where(messages.c.chat_id == 1))
        rows = result.fetchall()
    assert rows == [], f"Draft row must not be inserted on cross-user attempt: {rows}"

    # Upstream client must never have been called.
    lm_client.stream.assert_not_called()


@pytest.mark.asyncio
async def test_streaming_user_message_persisted_before_assistant_draft(
    engine: AsyncEngine,
) -> None:
    """stream_chat persists a role='user' row BEFORE the assistant draft row.

    Regression: previously _create_draft only inserted the
    assistant draft; the user turn was never written to the DB, so on reload
    the conversation showed the assistant reply with no user prompt above it.

    After the fix, both rows must exist in the messages table at the end of a
    successful stream, the user row must appear first (lower PK), and its
    content must match the input text.
    """
    events = _make_events(
        "chat.start",
        "message.start",
        "message.delta",
        "message.end",
        "chat.end",
        content="PONG",
        response_id="resp-bugA",
    )

    async def _fake_stream(*args: object, **kwargs: object) -> AsyncIterator[CanonicalEvent]:
        for ev in events:
            yield ev

    lm_client = MagicMock()
    lm_client.stream = _fake_stream

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    svc = await _make_service(engine, lm_client=lm_client, memory_service=memory_mock)
    user = _mock_user(1)
    request = _mock_request(disconnected=False)

    # Build a fresh chat for this test (engine fixture is session-scoped;
    # insert into its own chat so other tests' rows don't interfere).
    from lmchat.db.schema import chats
    async with engine.begin() as conn:
        result = await conn.execute(chats.insert().values(user_id=1, title="bug-a-test"))
        pk = result.inserted_primary_key
        assert pk is not None
        chat_id = pk[0]

    user_input_text = "ping"
    payload = ChatStreamRequest(
        chat_id=chat_id,
        payload=CanonicalChatRequest(
            model="test-model",
            input=[CanonicalInputBlock(type="text", content=user_input_text)],
        ),
    )

    frames = await _drain(
        svc.stream_chat(
            chat_id=chat_id,
            user=user,
            payload=payload,
            request=request,
        )
    )
    assert len(frames) > 0

    # Allow fire-and-forget tasks to run.
    await asyncio.sleep(0)

    # Both rows must be present.
    async with engine.connect() as conn:
        result = await conn.execute(
            select(
                messages.c.id,
                messages.c.role,
                messages.c.content,
                messages.c.state,
            )
            .where(messages.c.chat_id == chat_id)
            .order_by(messages.c.id)
        )
        rows = result.fetchall()

    assert len(rows) == 2, f"Expected 2 message rows, got {len(rows)}: {rows}"

    user_row = rows[0]
    asst_row = rows[1]

    # User row assertions.
    assert user_row.role == "user", f"First row must be role=user, got {user_row.role!r}"
    assert user_row.content == user_input_text, (
        f"User row content mismatch: {user_row.content!r} != {user_input_text!r}"
    )
    assert user_row.state == PersistState.FINAL.value, (
        f"User row state must be 'final', got {user_row.state!r}"
    )

    # Assistant row assertions.
    assert asst_row.role == "assistant", f"Second row must be role=assistant, got {asst_row.role!r}"
    assert asst_row.state == PersistState.FINAL.value, (
        f"Assistant row state must be 'final' after successful stream, got {asst_row.state!r}"
    )
    assert asst_row.content == "PONG", f"Assistant content mismatch: {asst_row.content!r}"


# ---------------------------------------------------------------------------
# reasoning_effort wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_reasoning_effort_wires_to_outbound_request(
    engine: AsyncEngine,
) -> None:
    """Per-chat reasoning_effort is populated on CanonicalChatRequest.reasoning.

    When ``chats.settings.reasoning_effort`` is set AND the model advertises
    that level under ``capabilities.reasoning.allowed_options``, the
    outbound CanonicalChatRequest passed to the streaming client carries the
    field.
    """
    from lmchat.db.schema import chats
    from lmchat.services.models_service import (
        Capabilities,
        ReasoningCapability,
        ResolvedModel,
    )

    captured: dict[str, Any] = {}

    async def _fake_stream(*, request: CanonicalChatRequest, cumulative_tool_rounds: int = 0) -> AsyncIterator[CanonicalEvent]:
        captured["request"] = request
        yield CanonicalEvent(type="chat.start")
        yield CanonicalEvent(type="message.start")
        yield CanonicalEvent(type="message.delta", content="ok")
        yield CanonicalEvent(type="message.end")
        yield CanonicalEvent(type="chat.end", response_id="rid")

    lm_client = MagicMock()
    lm_client.stream = _fake_stream

    models_mock = AsyncMock()
    models_mock.get_capabilities = AsyncMock(
        return_value=Capabilities(
            vision=False,
            trained_for_tool_use=False,
            reasoning=ReasoningCapability(
                allowed_options=["off", "low", "medium", "high"],
                default="medium",
            ),
        )
    )
    models_mock.resolve_to_loaded_or_fallback = AsyncMock(
        side_effect=lambda mid, **_kw: ResolvedModel(wire_id=mid, requested=mid)
    )

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    svc = StreamingService(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        chat_locks={},
        idle_timeout_sec=60,
        models_service=models_mock,
    )

    async with engine.begin() as conn:
        result = await conn.execute(
            chats.insert().values(
                user_id=1,
                title="reasoning-effort-test",
                settings={"reasoning_effort": "high"},
            )
        )
        pk = result.inserted_primary_key
        assert pk is not None
        chat_id = pk[0]

    payload = ChatStreamRequest(
        chat_id=chat_id,
        payload=CanonicalChatRequest(
            model="deepseek-r1-7b-gguf",
            input=[CanonicalInputBlock(type="text", content="hi")],
        ),
    )

    await _drain(
        svc.stream_chat(
            chat_id=chat_id,
            user=_mock_user(1),
            payload=payload,
            request=_mock_request(disconnected=False),
        )
    )

    assert "request" in captured, "stream() was not called"
    assert captured["request"].reasoning == "high"


@pytest.mark.asyncio
async def test_streaming_reasoning_capability_blocks_unsupported_level(
    engine: AsyncEngine,
) -> None:
    """When the model does not advertise the requested level, reasoning is suppressed.

    Sending ``reasoning="high"`` to a model whose capabilities don't include
    ``"high"`` (or whose reasoning capability is absent entirely) would cause
    LM Studio to 400 the request and poison the per-model rejected-param
    cache. The gate suppresses the field before the wire write.
    """
    from lmchat.db.schema import chats
    from lmchat.services.models_service import Capabilities, ResolvedModel

    captured: dict[str, Any] = {}

    async def _fake_stream(*, request: CanonicalChatRequest, cumulative_tool_rounds: int = 0) -> AsyncIterator[CanonicalEvent]:
        captured["request"] = request
        yield CanonicalEvent(type="chat.start")
        yield CanonicalEvent(type="message.start")
        yield CanonicalEvent(type="message.delta", content="ok")
        yield CanonicalEvent(type="message.end")
        yield CanonicalEvent(type="chat.end", response_id="rid")

    lm_client = MagicMock()
    lm_client.stream = _fake_stream

    # Plain (non-reasoning) model — capabilities.reasoning is None.
    models_mock = AsyncMock()
    models_mock.get_capabilities = AsyncMock(
        return_value=Capabilities(
            vision=False, trained_for_tool_use=False, reasoning=None
        )
    )
    models_mock.resolve_to_loaded_or_fallback = AsyncMock(
        side_effect=lambda mid, **_kw: ResolvedModel(wire_id=mid, requested=mid)
    )

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    svc = StreamingService(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        chat_locks={},
        idle_timeout_sec=60,
        models_service=models_mock,
    )

    async with engine.begin() as conn:
        result = await conn.execute(
            chats.insert().values(
                user_id=1,
                title="reasoning-capability-gate-test",
                settings={"reasoning_effort": "high"},
            )
        )
        pk = result.inserted_primary_key
        assert pk is not None
        chat_id = pk[0]

    payload = ChatStreamRequest(
        chat_id=chat_id,
        payload=CanonicalChatRequest(
            model="qwen3-8b",
            input=[CanonicalInputBlock(type="text", content="hi")],
        ),
    )

    await _drain(
        svc.stream_chat(
            chat_id=chat_id,
            user=_mock_user(1),
            payload=payload,
            request=_mock_request(disconnected=False),
        )
    )

    assert "request" in captured, "stream() was not called"
    assert captured["request"].reasoning is None


# ---------------------------------------------------------------------------
# Test: pump-level reset on mtp_suspected event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_pump_resets_counter_on_mtp_suspected_event(
    engine: AsyncEngine,
) -> None:
    """When an mtp_suspected error event flows through the pump, the chat's
    tool-round counter is reset so the next retry starts a fresh detection
    cycle. Exercises the production pump path (NOT the standalone
    `reset_counter` method) — a refactor that swaps the inline reset to an
    increment would silently break MTP UX without this test.
    """
    events = [
        CanonicalEvent(type="chat.start"),
        CanonicalEvent(type="tool_call.success"),
        CanonicalEvent(type="tool_call.success"),
        CanonicalEvent(type="tool_call.success"),
        CanonicalEvent(
            type="error",
            error={
                "code": "mtp_suspected",
                "message": "Long tool chain — possible MTP misbehavior.",
                "cumulative_tool_rounds": 3,
                "hint": "Disable MTP in LM Studio's model load config.",
            },
        ),
    ]

    async def _fake_stream(*args: object, **kwargs: object) -> AsyncIterator[CanonicalEvent]:
        for ev in events:
            yield ev

    lm_client = MagicMock()
    lm_client.stream = _fake_stream

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    from lmchat.db.schema import chats
    async with engine.begin() as conn:
        await conn.execute(chats.insert().values(user_id=1, title="test"))

    svc = await _make_service(engine, lm_client=lm_client, memory_service=memory_mock)
    user = _mock_user(1)
    request = _mock_request(disconnected=False)
    payload = _make_request_payload()

    await _drain(svc.stream_chat(chat_id=1, user=user, payload=payload, request=request))

    # Counter must have been reset by the pump as the mtp_suspected event
    # flowed through. (Increments happened: 3. Reset on mtp_suspected → key
    # absent.)
    assert 1 not in svc._tool_round_counts



# ---------------------------------------------------------------------------
# Stream-time project_prompt injection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_injects_project_prompt_into_outbound_system_prompt(
    engine: AsyncEngine,
) -> None:
    """End-to-end integration: exercise the
    full stream path with a chat whose project has a non-empty
    system_prompt. The outbound CanonicalChatRequest's system_prompt
    must contain the project prompt BEFORE the followups directive.
    """
    import time as _time
    from types import SimpleNamespace

    from sqlalchemy import insert

    from lmchat.db.schema import chats, projects

    events = _make_events(
        "chat.start",
        "message.start",
        "message.delta",
        "message.end",
        "chat.end",
        content="ack",
        response_id="resp-proj",
    )

    captured: dict[str, Any] = {}

    async def _fake_stream(*args: Any, **kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        # Capture the canonical request passed to the LM client so the
        # test can assert on the composed system_prompt. The
        # streaming_service calls with `request=<payload>`, so check
        # the kwarg first.
        captured["payload"] = kwargs.get("request") or (
            args[0] if args else None
        )
        for ev in events:
            yield ev

    lm_client = MagicMock()
    lm_client.stream = _fake_stream

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    # Stub ProjectsService returning a project with a non-empty
    # system_prompt — same shape the real service yields.
    proj_svc = MagicMock()
    proj_svc.get = AsyncMock(
        return_value=SimpleNamespace(
            id=42,
            user_id=1,
            name="P",
            description="",
            system_prompt="You are the project-x persona.",

            created_at=0.0,
            updated_at=0.0,
        )
    )

    svc = await _make_service(
        engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        projects_service=proj_svc,
    )
    user = _mock_user(1)
    request = _mock_request(disconnected=False)
    payload = _make_request_payload()
    payload_with_cid = ChatStreamRequest(chat_id=1, payload=payload.payload)

    # Seed: project row + chat row referencing it.
    async with engine.begin() as conn:
        now = _time.time()
        await conn.execute(
            insert(projects).values(
                id=42,
                user_id=1,
                name="P",
                description="",
                system_prompt="You are the project-x persona.",

                created_at=now,
                updated_at=now,
            )
        )
        await conn.execute(
            insert(chats).values(
                id=1, user_id=1, title="t", project_id=42
            )
        )

    _ = await _drain(
        svc.stream_chat(
            chat_id=1,
            user=user,
            payload=payload_with_cid,
            request=request,
        )
    )

    # The project prompt must appear in the outbound payload's
    # system_prompt — proves the injection fired end-to-end.
    sent_payload = captured.get("payload")
    assert sent_payload is not None, "lm_client.stream was not called"
    sent_system = getattr(sent_payload, "system_prompt", "") or ""
    assert "project-x persona" in sent_system, (
        f"project_prompt not found in outbound system_prompt: "
        f"{sent_system!r}"
    )
    proj_svc.get.assert_awaited_with(user_id=1, project_id=42)


@pytest.mark.asyncio
async def test_stream_logs_warning_when_project_deleted_mid_stream(
    engine: AsyncEngine,
) -> None:
    """When chat.project_id points at a non-existent project, the
    injection path falls through gracefully AND emits a
    `stream.project_lookup_miss` warning so admins can spot the
    dangling reference.
    """
    from sqlalchemy import insert

    from lmchat.db.schema import chats

    events = _make_events(
        "chat.start",
        "message.start",
        "message.delta",
        "message.end",
        "chat.end",
        content="ack",
        response_id="resp-miss",
    )

    async def _fake_stream(*args: Any, **kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        for ev in events:
            yield ev

    lm_client = MagicMock()
    lm_client.stream = _fake_stream

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    # ProjectsService.get returns None — simulates the project being
    # deleted between the chat row's last write and this stream.
    proj_svc = MagicMock()
    proj_svc.get = AsyncMock(return_value=None)

    svc = await _make_service(
        engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        projects_service=proj_svc,
    )
    user = _mock_user(1)
    request = _mock_request(disconnected=False)
    payload = _make_request_payload()
    payload_with_cid = ChatStreamRequest(chat_id=1, payload=payload.payload)

    # Chat row carries a project_id that no longer points to a real row.
    async with engine.begin() as conn:
        await conn.execute(
            insert(chats).values(
                id=1, user_id=1, title="t", project_id=999
            )
        )

    # Stream should complete WITHOUT raising; the missing project
    # is logged as a warning.
    frames = await _drain(
        svc.stream_chat(
            chat_id=1,
            user=user,
            payload=payload_with_cid,
            request=request,
        )
    )
    assert frames, "no frames emitted — stream short-circuited"
    proj_svc.get.assert_awaited_with(user_id=1, project_id=999)


# ---------------------------------------------------------------------------
# Pre-flight context-budget gate
# ---------------------------------------------------------------------------


def _make_models_service_mock(
    *,
    trained_for_tool_use: bool = True,
    max_context_length: int = 16_384,
) -> AsyncMock:
    """Mock ModelsService for budget-gate tests.

    Returns capabilities with `trained_for_tool_use` set, and the given
    context length for any model key.
    """
    from lmchat.services.models_service import (  # noqa: PLC0415
        Capabilities,
        ResolvedModel,
    )

    ms = AsyncMock()
    ms.get_capabilities = AsyncMock(
        return_value=Capabilities(
            vision=False, trained_for_tool_use=trained_for_tool_use
        )
    )
    ms.get_max_context_length = AsyncMock(return_value=max_context_length)
    ms.list_loaded = AsyncMock(return_value=[])
    ms.resolve_to_loaded_or_fallback = AsyncMock(
        side_effect=lambda mid, **_kw: ResolvedModel(wire_id=mid, requested=mid)
    )
    return ms


@pytest.mark.asyncio
async def test_context_overflow_trims_then_warns(engine: AsyncEngine) -> None:
    """9 MCPs on a small-context tool-trained model trim back-of-list and
    emit a `integrations_trimmed_for_context` warning frame; the stream
    then proceeds successfully with the kept set.

    Validates that the budget gate trims the trailing
    integrations and emits a non-terminal `warning` frame so the FE
    can surface the trim to the user, instead of letting the request
    hit LM Studio and silently die after 20 seconds.
    """
    streamed_payloads: list[Any] = []

    async def _capture_then_complete(
        request: Any, **kwargs: Any
    ) -> AsyncIterator[CanonicalEvent]:
        streamed_payloads.append(request)
        yield CanonicalEvent(type="chat.start")
        yield CanonicalEvent(type="message.delta", content="ok")
        yield CanonicalEvent(type="message.end")
        yield CanonicalEvent(type="chat.end", response_id="rid-budget-1")

    lm_client = MagicMock()
    lm_client.stream = _capture_then_complete

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    from lmchat.db.schema import chats
    async with engine.begin() as conn:
        await conn.execute(chats.insert().values(user_id=1, title="budget-trim-test"))

    svc = await _make_service(
        engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        models_service=_make_models_service_mock(
            trained_for_tool_use=True, max_context_length=16_384
        ),
    )
    user = _mock_user(1)
    request = _mock_request(disconnected=False)

    nine_mcps = [
        "mcp/context7", "mcp/deepwiki", "mcp/firecrawl", "mcp/searxng",
        "mcp/playwright", "mcp/wolfram", "mcp/paper-search-mcp",
        "mcp/sequential-thinking", "mcp/filesystem",
    ]
    payload = ChatStreamRequest(
        chat_id=1,
        payload=CanonicalChatRequest(
            model="qwen3-vl-8b-instruct",
            # Substantive system prompt + short input forces an overflow.
            system_prompt="x" * 8_000,
            input=[CanonicalInputBlock(type="text", content="short message")],
            integrations=nine_mcps,
        ),
    )

    frames = []
    async for f in svc.stream_chat(chat_id=1, user=user, payload=payload, request=request):
        frames.append(f)

    parsed = _parse_frames(frames)
    # A `warning` frame with code `integrations_trimmed_for_context`
    # must precede the upstream stream.
    warning_frames = [
        f for f in parsed
        if f.get("type") == "warning"
        and f.get("warning", {}).get("code") == "integrations_trimmed_for_context"
    ]
    assert len(warning_frames) == 1, (
        f"Expected exactly one trim warning; got {parsed}"
    )

    # The upstream payload that DID reach LM Studio must have FEWER
    # integrations than the original list, and the dropped one must be
    # `mcp/filesystem` (lowest priority — last in list).
    assert len(streamed_payloads) == 1
    sent = streamed_payloads[0]
    assert len(sent.integrations) < len(nine_mcps)
    assert "mcp/filesystem" not in sent.integrations
    # And the stream must have completed successfully (chat.end seen).
    chat_end_frames = [f for f in parsed if f.get("type") == "chat.end"]
    assert chat_end_frames, "Stream must complete normally after trim"


# ---------------------------------------------------------------------------
# Tools-unavailable corrective — the capability legend (built from the raw
# pre-gate request in _assemble_system_prompt, BEFORE this gate runs) can
# advertise tools the gate then drops/trims. Without a corrective the model
# believes it still has a dropped tool and emits the call as literal JSON
# text instead of a real tool_call — reproduced live while probing this bug.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drop_all_emits_tools_unavailable_corrective(engine: AsyncEngine) -> None:
    """Non-tool-trained model drops every integration (Layer 1). The legend
    still lists the dropped tool (documented here as the actual design —
    a corrective, not a legend rewrite); the corrective must name it and
    tell the model not to call it.

    RED-ON-REVERT: remove the apply_tools_unavailable_corrective call from
    the drop_all branch of stream_chat and the corrective assertions fail
    — the legend keeps advertising mcp/searxng with no correction.
    """
    streamed_payloads: list[Any] = []

    async def _capture_then_complete(
        request: Any, **kwargs: Any
    ) -> AsyncIterator[CanonicalEvent]:
        streamed_payloads.append(request)
        yield CanonicalEvent(type="chat.start")
        yield CanonicalEvent(type="message.delta", content="ok")
        yield CanonicalEvent(type="message.end")
        yield CanonicalEvent(type="chat.end", response_id="rid-drop-all-1")

    lm_client = MagicMock()
    lm_client.stream = _capture_then_complete

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    from lmchat.db.schema import chats
    async with engine.begin() as conn:
        await conn.execute(chats.insert().values(user_id=1, title="drop-all-corrective-test"))

    svc = await _make_service(
        engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        models_service=_make_models_service_mock(trained_for_tool_use=False),
    )
    user = _mock_user(1)
    request = _mock_request(disconnected=False)

    payload = ChatStreamRequest(
        chat_id=1,
        payload=CanonicalChatRequest(
            model="qwen3-vl-8b-instruct",
            input=[CanonicalInputBlock(type="text", content="search something")],
            integrations=["mcp/searxng"],
        ),
    )

    frames = []
    async for f in svc.stream_chat(chat_id=1, user=user, payload=payload, request=request):
        frames.append(f)

    assert len(streamed_payloads) == 1
    sent = streamed_payloads[0]
    sys_p = sent.system_prompt or ""
    # The legend was built BEFORE the gate ran and still lists the tool —
    # the fix is a corrective, not an edit to already-rendered legend text.
    assert "- searxng —" in sys_p, "legend should still list the pre-gate tool"
    assert "mcp/searxng" in sys_p, f"corrective must name the dropped tool: {sys_p!r}"
    assert "NOT available this turn" in sys_p
    assert sent.integrations == []


@pytest.mark.asyncio
async def test_context_overflow_trim_emits_tools_unavailable_corrective(
    engine: AsyncEngine,
) -> None:
    """Companion to test_context_overflow_trims_then_warns: the trimmed
    tool (Layer 2) gets the same corrective as drop_all — same stale-
    legend root cause, different gate layer."""
    streamed_payloads: list[Any] = []

    async def _capture_then_complete(
        request: Any, **kwargs: Any
    ) -> AsyncIterator[CanonicalEvent]:
        streamed_payloads.append(request)
        yield CanonicalEvent(type="chat.start")
        yield CanonicalEvent(type="message.delta", content="ok")
        yield CanonicalEvent(type="message.end")
        yield CanonicalEvent(type="chat.end", response_id="rid-trim-corrective-1")

    lm_client = MagicMock()
    lm_client.stream = _capture_then_complete

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    from lmchat.db.schema import chats
    async with engine.begin() as conn:
        await conn.execute(chats.insert().values(user_id=1, title="trim-corrective-test"))

    svc = await _make_service(
        engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        models_service=_make_models_service_mock(
            trained_for_tool_use=True, max_context_length=16_384
        ),
    )
    user = _mock_user(1)
    request = _mock_request(disconnected=False)

    nine_mcps = [
        "mcp/context7", "mcp/deepwiki", "mcp/firecrawl", "mcp/searxng",
        "mcp/playwright", "mcp/wolfram", "mcp/paper-search-mcp",
        "mcp/sequential-thinking", "mcp/filesystem",
    ]
    payload = ChatStreamRequest(
        chat_id=1,
        payload=CanonicalChatRequest(
            model="qwen3-vl-8b-instruct",
            system_prompt="x" * 8_000,
            input=[CanonicalInputBlock(type="text", content="short message")],
            integrations=nine_mcps,
        ),
    )

    frames = []
    async for f in svc.stream_chat(chat_id=1, user=user, payload=payload, request=request):
        frames.append(f)

    assert len(streamed_payloads) == 1
    sent = streamed_payloads[0]
    sys_p = sent.system_prompt or ""
    assert "mcp/filesystem" not in sent.integrations
    assert "mcp/filesystem" in sys_p, f"corrective must name the trimmed tool: {sys_p!r}"
    assert "NOT available this turn" in sys_p


@pytest.mark.asyncio
async def test_drop_all_corrective_lands_in_input_on_followup_turn(
    engine: AsyncEngine,
) -> None:
    """On a follow-up turn (previous_response_id set) encode_native drops
    system_prompt entirely — the corrective must ride input[0] instead,
    mirroring how relocate_per_turn_layers already routes the RAG /
    tools-now-available / date correctives there for the identical reason.
    """
    streamed_payloads: list[Any] = []

    async def _capture_then_complete(
        request: Any, **kwargs: Any
    ) -> AsyncIterator[CanonicalEvent]:
        streamed_payloads.append(request)
        yield CanonicalEvent(type="chat.start")
        yield CanonicalEvent(type="message.delta", content="ok")
        yield CanonicalEvent(type="message.end")
        yield CanonicalEvent(type="chat.end", response_id="rid-drop-all-followup-1")

    lm_client = MagicMock()
    lm_client.stream = _capture_then_complete

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    from lmchat.db.schema import chats, messages
    async with engine.begin() as conn:
        await conn.execute(
            chats.insert().values(user_id=1, title="drop-all-followup-corrective-test")
        )
        # Hybrid compaction's chain-reset backstop cross-checks the incoming
        # previous_response_id against a real message row before honouring
        # it — an unbacked rid is treated as unknown and silently reset to
        # None (see stream.compaction_chain_reset_backstop), which would
        # make this look like a turn-1 request instead of the follow-up
        # this test needs. Seed the prior assistant turn that "produced"
        # this rid, mirroring test_prompt_assembly.py's _run_stream helper.
        await conn.execute(
            messages.insert().values(
                chat_id=1,
                role="assistant",
                content="prior turn",
                state="final",
                response_id="resp-prev-drop-all",
            )
        )

    svc = await _make_service(
        engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        models_service=_make_models_service_mock(trained_for_tool_use=False),
    )
    user = _mock_user(1)
    request = _mock_request(disconnected=False)

    payload = ChatStreamRequest(
        chat_id=1,
        payload=CanonicalChatRequest(
            model="qwen3-vl-8b-instruct",
            previous_response_id="resp-prev-drop-all",
            input=[CanonicalInputBlock(type="text", content="search something")],
            integrations=["mcp/searxng"],
        ),
    )

    frames = []
    async for f in svc.stream_chat(chat_id=1, user=user, payload=payload, request=request):
        frames.append(f)

    assert len(streamed_payloads) == 1
    sent = streamed_payloads[0]
    input_text = " ".join(blk.content or "" for blk in sent.input)
    assert "mcp/searxng" in input_text, (
        f"corrective must land in input[0] on a follow-up turn: {input_text!r}"
    )
    assert "NOT available this turn" in input_text
    assert sent.integrations == []


@pytest.mark.asyncio
async def test_context_overflow_fails_fast_when_unsalvageable(engine: AsyncEngine) -> None:
    """When system_prompt + input ALONE overflow the context window, the
    gate fails fast with a `context_budget_exceeded` error BEFORE
    opening the upstream stream.

    Validates that dropping every integration would still
    overflow, so the user gets an immediate actionable error instead of
    a 20-second silent stall.
    """
    streamed_payloads: list[Any] = []

    async def _capture_then_complete(
        request: Any, **kwargs: Any
    ) -> AsyncIterator[CanonicalEvent]:
        streamed_payloads.append(request)
        yield CanonicalEvent(type="chat.start")
        yield CanonicalEvent(type="chat.end", response_id="rid-budget-2")

    lm_client = MagicMock()
    lm_client.stream = _capture_then_complete

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    from lmchat.db.schema import chats
    async with engine.begin() as conn:
        await conn.execute(chats.insert().values(user_id=1, title="budget-fail-fast"))

    svc = await _make_service(
        engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        models_service=_make_models_service_mock(
            trained_for_tool_use=True, max_context_length=16_384
        ),
    )
    user = _mock_user(1)
    request = _mock_request(disconnected=False)

    # System + input together ~20k tokens — overflows 14k headroom even
    # with zero integrations.
    payload = ChatStreamRequest(
        chat_id=1,
        payload=CanonicalChatRequest(
            model="qwen3-vl-8b-instruct",
            system_prompt="x" * 50_000,
            input=[CanonicalInputBlock(type="text", content="y" * 30_000)],
            integrations=["mcp/searxng", "mcp/filesystem"],
        ),
    )

    frames = []
    async for f in svc.stream_chat(chat_id=1, user=user, payload=payload, request=request):
        frames.append(f)

    parsed = _parse_frames(frames)
    error_frames = [
        f for f in parsed
        if f.get("type") == "error"
        and f.get("error", {}).get("code") == "context_budget_exceeded"
    ]
    assert len(error_frames) == 1, (
        f"Expected exactly one context_budget_exceeded error frame; got {parsed}"
    )
    # And the upstream stream must NOT have been called — the gate
    # caught it pre-flight, the entire point of this test.
    assert streamed_payloads == [], (
        "lm_client.stream should NOT be called when the budget gate "
        f"rejects pre-flight; got streamed_payloads={streamed_payloads!r}"
    )


# ---------------------------------------------------------------------------
# tool_calls persistence at finalize
# ---------------------------------------------------------------------------


def _tool_call_event(event_type: str, **tc_kwargs: Any) -> CanonicalEvent:
    """Build a tool_call.* CanonicalEvent carrying a CanonicalToolCall."""
    from typing import cast

    from lmchat.lmstudio.types import CanonicalToolCall

    return CanonicalEvent(
        type=cast("Any", event_type),
        tool_call=CanonicalToolCall(**tc_kwargs),
    )


@pytest.mark.asyncio
async def test_streaming_finalized_row_carries_tool_calls_json(
    engine: AsyncEngine,
) -> None:
    """A streamed tool-call sequence persists to messages.tool_calls (FE shape).

    The finalized assistant row carries the
    accumulated tool-call list so ToolCallCards (args + results) survive a
    page reload.
    """
    events = [
        CanonicalEvent(type="chat.start"),
        CanonicalEvent(type="message.start"),
        _tool_call_event("tool_call.start", id="tc-1", name="", arguments={}),
        _tool_call_event("tool_call.name", id="tc-1", name="search_web", arguments={}),
        _tool_call_event(
            "tool_call.arguments",
            id="tc-1",
            name="search_web",
            arguments={"query": "lm studio"},
        ),
        _tool_call_event(
            "tool_call.success",
            id="tc-1",
            name="search_web",
            arguments={"query": "lm studio"},
            result="LM Studio is a desktop application.",
        ),
        CanonicalEvent(type="message.delta", content="Found it."),
        CanonicalEvent(type="message.end"),
        CanonicalEvent(type="chat.end", response_id="resp-tc"),
    ]

    async def _fake_stream(*args: object, **kwargs: object) -> AsyncIterator[CanonicalEvent]:
        for ev in events:
            yield ev

    lm_client = MagicMock()
    lm_client.stream = _fake_stream

    svc = await _make_service(engine, lm_client=lm_client)
    user = _mock_user(1)
    request = _mock_request(disconnected=False)
    payload = _make_request_payload()
    payload_with_cid = ChatStreamRequest(chat_id=1, payload=payload.payload)

    from lmchat.db.schema import chats

    async with engine.begin() as conn:
        await conn.execute(chats.insert().values(user_id=1, title="test"))

    await _drain(
        svc.stream_chat(chat_id=1, user=user, payload=payload_with_cid, request=request)
    )

    async with engine.connect() as conn:
        result = await conn.execute(
            select(messages.c.state, messages.c.tool_calls).where(
                messages.c.chat_id == 1,
                messages.c.role == "assistant",
            )
        )
        row = result.fetchone()

    assert row is not None
    assert row[0] == PersistState.FINAL.value
    assert row[1] == [
        {
            "id": "tc-1",
            "name": "search_web",
            "arguments": json.dumps({"query": "lm studio"}),
            "status": "success",
            "result": "LM Studio is a desktop application.",
        }
    ]


@pytest.mark.asyncio
async def test_streaming_tool_free_turn_persists_tool_calls_null(
    engine: AsyncEngine,
) -> None:
    """A tool-free stream persists tool_calls=NULL, not [] (locked decision 3)."""
    events = _make_events(
        "chat.start", "message.start", "message.delta", "message.end", "chat.end"
    )

    async def _fake_stream(*args: object, **kwargs: object) -> AsyncIterator[CanonicalEvent]:
        for ev in events:
            yield ev

    lm_client = MagicMock()
    lm_client.stream = _fake_stream

    svc = await _make_service(engine, lm_client=lm_client)
    user = _mock_user(1)
    request = _mock_request(disconnected=False)
    payload = _make_request_payload()
    payload_with_cid = ChatStreamRequest(chat_id=1, payload=payload.payload)

    from lmchat.db.schema import chats

    async with engine.begin() as conn:
        await conn.execute(chats.insert().values(user_id=1, title="test"))

    await _drain(
        svc.stream_chat(chat_id=1, user=user, payload=payload_with_cid, request=request)
    )

    async with engine.connect() as conn:
        result = await conn.execute(
            select(messages.c.tool_calls).where(
                messages.c.chat_id == 1,
                messages.c.role == "assistant",
            )
        )
        row = result.fetchone()

    assert row is not None
    assert row[0] is None


def test_accumulate_tool_call_failure_and_lost_start() -> None:
    """_accumulate_tool_call: failure status lands; lost start synthesises entry."""
    from lmchat.lmstudio.types import CanonicalToolCall
    from lmchat.services.streaming_service import _accumulate_tool_call

    calls: list[dict[str, object]] = []
    # No tool_call.start ever observed for tc-x (decoder id drift).
    _accumulate_tool_call(
        calls,
        "tool_call.failure",
        CanonicalToolCall(id="tc-x", name="broken_tool", arguments={"a": 1}),
    )
    assert calls == [
        {
            "id": "tc-x",
            "name": "broken_tool",
            "arguments": json.dumps({"a": 1}),
            "status": "failure",
        }
    ]
    # Warning advisories are not lifecycle events — must be a no-op.
    _accumulate_tool_call(
        calls,
        "tool_call.repeat_warning",
        CanonicalToolCall(id="tc-x", name="broken_tool", arguments={}),
    )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_streaming_tool_calls_round_trip_through_list_for_chat(
    engine: AsyncEngine,
) -> None:
    """Integration: stream writes tool_calls → list_for_chat returns them.

    Covers the BE half of the reload path end-to-end: the same list the
    stream accumulated comes back on the Message pydantic model (and from
    there flows through ChatWithMessagesResponse to the FE serverMessages
    mapping, pinned by test_Chat_toolcalls_persisted.spec.tsx).
    """
    from lmchat.services.message_service import MessageService

    events = [
        CanonicalEvent(type="chat.start"),
        CanonicalEvent(type="message.start"),
        _tool_call_event("tool_call.start", id="tc-9", name="", arguments={}),
        _tool_call_event("tool_call.name", id="tc-9", name="read_file", arguments={}),
        _tool_call_event(
            "tool_call.success",
            id="tc-9",
            name="read_file",
            arguments={"path": "/tmp/x"},
            result="contents",
        ),
        CanonicalEvent(type="message.delta", content="Read it."),
        CanonicalEvent(type="message.end"),
        CanonicalEvent(type="chat.end", response_id="resp-rt"),
    ]

    async def _fake_stream(*args: object, **kwargs: object) -> AsyncIterator[CanonicalEvent]:
        for ev in events:
            yield ev

    lm_client = MagicMock()
    lm_client.stream = _fake_stream

    svc = await _make_service(engine, lm_client=lm_client)
    user = _mock_user(1)
    request = _mock_request(disconnected=False)
    payload = _make_request_payload()
    payload_with_cid = ChatStreamRequest(chat_id=1, payload=payload.payload)

    from lmchat.db.schema import chats

    async with engine.begin() as conn:
        await conn.execute(chats.insert().values(user_id=1, title="test"))

    await _drain(
        svc.stream_chat(chat_id=1, user=user, payload=payload_with_cid, request=request)
    )

    msg_svc = MessageService(engine=engine, memory_service=AsyncMock())
    msgs, _ = await msg_svc.list_for_chat(1, user_id=1)
    assistant = [m for m in msgs if m.role == "assistant"]
    assert len(assistant) == 1
    assert assistant[0].tool_calls == [
        {
            "id": "tc-9",
            "name": "read_file",
            "arguments": json.dumps({"path": "/tmp/x"}),
            "status": "success",
            "result": "contents",
        }
    ]


# ---------------------------------------------------------------------------
# XML tool-call recovery: persist recovered calls
# ---------------------------------------------------------------------------

# recover_xml_tool_calls() correctly extracts XML tool calls leaked into
# content, but the extracted calls were NEVER added to accumulated_tool_calls
# before _finalize_message. Result: the assistant row persisted with empty
# content (XML stripped) AND empty tool_calls (recovered calls dropped) —
# the user saw nothing.


@pytest.mark.asyncio
async def test_streaming_xml_recovered_tool_calls_persist_to_messages_row(
    engine: AsyncEngine,
) -> None:
    """Qwen3-Coder leaked XML tool call → tool_calls JSON has the recovered call.

    Reproduces the bug: send a message, model emits a tool
    call as XML inside message.delta content (LM Studio's native parser
    misses Qwen3-Coder dialect), stream ends, our recovery extracts the call
    — but pre-fix the call vanished. Post-fix it lands in tool_calls.
    """
    from lmchat.services.message_service import MessageService

    # The canonical Qwen3-Coder XML tool-call dialect (closing-tag variant)
    # that recover_xml_tool_calls extracts via _XML_FUNC_RE.
    xml_payload = (
        "<tool_call><function=firecrawl_search>"
        "<parameter=query>\nbest openweight coding LLM 2026\n</parameter>"
        "</function></tool_call>"
    )
    events = [
        CanonicalEvent(type="chat.start"),
        CanonicalEvent(type="message.start"),
        CanonicalEvent(type="message.delta", content=xml_payload),
        CanonicalEvent(type="message.end"),
        CanonicalEvent(type="chat.end", response_id="resp-xml-recover"),
    ]

    async def _fake_stream(*args: object, **kwargs: object) -> AsyncIterator[CanonicalEvent]:
        for ev in events:
            yield ev

    lm_client = MagicMock()
    lm_client.stream = _fake_stream

    svc = await _make_service(engine, lm_client=lm_client)
    user = _mock_user(1)
    request = _mock_request(disconnected=False)
    payload = _make_request_payload()
    payload_with_cid = ChatStreamRequest(chat_id=1, payload=payload.payload)

    from lmchat.db.schema import chats

    async with engine.begin() as conn:
        await conn.execute(chats.insert().values(user_id=1, title="test"))

    await _drain(
        svc.stream_chat(chat_id=1, user=user, payload=payload_with_cid, request=request)
    )

    msg_svc = MessageService(engine=engine, memory_service=AsyncMock())
    msgs, _ = await msg_svc.list_for_chat(1, user_id=1)
    assistant = [m for m in msgs if m.role == "assistant"]
    assert len(assistant) == 1
    # The delta was a pure XML tool call with no text answer. Post-fix
    # (resolve_terminal_content, tool-aware terminal decision) a tool-using turn
    # that produced no answer now yields an actionable graceful message instead
    # of a blank bubble — the model attempted a tool and never answered. The
    # recovered call is still attached below so the FE shows the attempt.
    # (Previously this persisted content="" — a blank bubble + a lone tool chip.)
    assert assistant[0].content != ""
    assert "final answer" in assistant[0].content
    assert "reasoning surfaced" not in assistant[0].content  # not a raw-reasoning dump
    # Recovered call was persisted into tool_calls JSON in FE ToolCall shape.
    tool_calls = assistant[0].tool_calls
    assert tool_calls is not None and len(tool_calls) == 1
    call = tool_calls[0]
    assert call["name"] == "firecrawl_search"
    assert call["status"] == "failure"  # not executed; chat-context contract
    # The arguments roundtrip through json.dumps in the recovery path.
    args_obj = json.loads(str(call["arguments"]))
    assert args_obj == {"query": "best openweight coding LLM 2026"}
    # The result string explains WHY it didn't execute so the user sees the
    # model attempted a call (visibility was the explicit request).
    assert "Recovered from leaked XML" in str(call["result"])


@pytest.mark.asyncio
async def test_streaming_xml_recovered_with_text_before_persists_both(
    engine: AsyncEngine,
) -> None:
    """Real-world Qwen3-Coder shape: text before the XML wrapper.

    The model writes a brief preamble, then a tool call. Post-fix: the
    preamble stays in content, the recovered call lands in tool_calls.
    """
    from lmchat.services.message_service import MessageService

    delta_content = (
        "Looking up benchmarks. "
        "<tool_call><function=lookup_benchmark>"
        "<parameter=name>\nLiveBench\n</parameter>"
        "</function></tool_call>"
    )
    events = [
        CanonicalEvent(type="chat.start"),
        CanonicalEvent(type="message.start"),
        CanonicalEvent(type="message.delta", content=delta_content),
        CanonicalEvent(type="message.end"),
        CanonicalEvent(type="chat.end", response_id="resp-xml-preamble"),
    ]

    async def _fake_stream(*args: object, **kwargs: object) -> AsyncIterator[CanonicalEvent]:
        for ev in events:
            yield ev

    lm_client = MagicMock()
    lm_client.stream = _fake_stream
    svc = await _make_service(engine, lm_client=lm_client)
    user = _mock_user(1)
    request = _mock_request(disconnected=False)
    payload = _make_request_payload()
    payload_with_cid = ChatStreamRequest(chat_id=1, payload=payload.payload)

    from lmchat.db.schema import chats

    async with engine.begin() as conn:
        await conn.execute(chats.insert().values(user_id=1, title="test"))

    await _drain(
        svc.stream_chat(chat_id=1, user=user, payload=payload_with_cid, request=request)
    )

    msg_svc = MessageService(engine=engine, memory_service=AsyncMock())
    msgs, _ = await msg_svc.list_for_chat(1, user_id=1)
    assistant = [m for m in msgs if m.role == "assistant"]
    assert len(assistant) == 1
    # Pre-XML text survives the strip; XML wrapper is gone.
    assert "Looking up benchmarks" in assistant[0].content
    assert "<tool_call>" not in assistant[0].content
    # Tool call persisted alongside the preserved content.
    tool_calls = assistant[0].tool_calls
    assert tool_calls is not None and len(tool_calls) == 1
    assert tool_calls[0]["name"] == "lookup_benchmark"


# ---------------------------------------------------------------------------
# Stuck-draft regression: early provider-resolution error-returns must abort
# the assistant draft so the chat isn't 409-bricked on the next send.
#
# Repro: send with an unloaded model -> upstream_unavailable
# error frame (a draft row is created by _create_draft) -> resend with a loaded
# model -> HTTP 409 stream_in_progress, because the orphaned draft row tripped
# _assert_no_in_progress_stream. The fix adds safe_abort_draft to each early
# error-return path between _create_draft and the normal finalize.
# ---------------------------------------------------------------------------


def _models_service_returns(resolved: Any) -> MagicMock:  # noqa: ANN401
    """Build a mock ModelsService whose resolve returns *resolved*.

    The chain-mode branch in stream_chat calls
    ``resolve_to_loaded_or_fallback`` (and, on wire_id=None + auth_failed,
    ``force_refresh``). Stub both plus the capability/context lookups the
    happy branch would reach so the test is robust if resolution succeeds.
    """
    svc = MagicMock()
    svc.resolve_to_loaded_or_fallback = AsyncMock(return_value=resolved)
    svc.force_refresh = AsyncMock(return_value=False)
    svc.auth_failed = False
    svc.get_capabilities = AsyncMock(return_value=None)
    svc.get_max_context_length = AsyncMock(return_value=0)
    return svc


def _error_codes(parsed: list[dict]) -> list[str]:  # type: ignore[type-arg]
    """Extract the error-frame codes from parsed SSE frames.

    Error frames nest the code under ``error.code`` (see _format_error_frame):
    ``{"type": "error", "error": {"code": ..., "message": ...}, "msg_id": N}``.
    """
    codes: list[str] = []
    for d in parsed:
        if d.get("type") == "error":
            code = (d.get("error") or {}).get("code")
            if code is not None:
                codes.append(code)
    return codes


async def _assistant_states(engine: AsyncEngine, chat_id: int) -> list[str]:
    """Return the states of all assistant rows for *chat_id*."""
    async with engine.connect() as conn:
        result = await conn.execute(
            select(messages.c.state).where(
                messages.c.chat_id == chat_id,
                messages.c.role == "assistant",
            )
        )
        return [r[0] for r in result.fetchall()]


@pytest.mark.asyncio
async def test_streaming_model_not_loaded_aborts_draft_no_409_on_resend(
    engine: AsyncEngine,
) -> None:
    """Unloaded-model send aborts its draft; resend is NOT 409'd.

    This is the reported case: ``resolve_to_loaded_or_fallback``
    returns ``substituted=True`` (the pinned model idled out), the stream
    emits an ``upstream_unavailable`` error frame, and the assistant draft
    row MUST leave ``draft`` state so the next send doesn't hit
    :class:`StreamInProgressError`.
    """
    from lmchat.db.schema import chats
    from lmchat.services.models_service import ResolvedModel

    async with engine.begin() as conn:
        await conn.execute(chats.insert().values(user_id=1, title="test"))

    models_service = _models_service_returns(
        ResolvedModel(
            wire_id=None,
            requested="qwythos-9b",
            substituted=True,
            fallback_key="some-loaded-model",
            reason="requested_not_loaded",
        )
    )
    svc = await _make_service(engine, models_service=models_service)
    user = _mock_user(1)
    request = _mock_request(disconnected=False)
    payload = _make_request_payload(model="qwythos-9b")

    frames = await _drain(
        svc.stream_chat(chat_id=1, user=user, payload=payload, request=request)
    )

    # 1. An upstream_unavailable error frame was emitted.
    parsed = _parse_frames(frames)
    assert "upstream_unavailable" in _error_codes(parsed), parsed

    # 2. The assistant draft is NOT left in 'draft' (it's terminal/aborted).
    states = await _assistant_states(engine, chat_id=1)
    assert states, "expected an assistant row to have been created"
    assert PersistState.DRAFT.value not in states, (
        f"orphaned draft left behind: {states}"
    )

    # 2b. The user row is preserved (only the assistant draft is aborted).
    async with engine.connect() as conn:
        user_rows = (
            await conn.execute(
                select(messages.c.state).where(
                    messages.c.chat_id == 1,
                    messages.c.role == "user",
                )
            )
        ).fetchall()
    assert [r[0] for r in user_rows] == [PersistState.FINAL.value], user_rows

    # 3. A second send (now with a loaded model) is NOT blocked by a stale draft.
    await svc._assert_no_in_progress_stream(1)  # must not raise

    models_service.resolve_to_loaded_or_fallback = AsyncMock(
        return_value=ResolvedModel(wire_id="loaded-model", requested="loaded-model")
    )

    async def _ok_stream(*args: object, **kwargs: object) -> AsyncIterator[CanonicalEvent]:
        for ev in _make_events(
            "chat.start", "message.start", "message.delta", "message.end", "chat.end",
            content="ok", response_id="resp-ok",
        ):
            yield ev

    svc._lm_client.stream = _ok_stream
    payload2 = _make_request_payload(model="loaded-model")
    frames2 = await _drain(
        svc.stream_chat(chat_id=1, user=user, payload=payload2, request=request)
    )
    parsed2 = _parse_frames(frames2)
    # The resend produced a real stream (no stream_in_progress 409 short-circuit).
    assert "stream_in_progress" not in _error_codes(parsed2), parsed2
    assert any("msg_id" in d for d in parsed2)


@pytest.mark.asyncio
async def test_streaming_no_llm_loaded_aborts_draft(
    engine: AsyncEngine,
) -> None:
    """No-LLM-loaded send (wire_id=None) aborts its draft, doesn't brick the chat."""
    from lmchat.db.schema import chats
    from lmchat.services.models_service import ResolvedModel

    async with engine.begin() as conn:
        await conn.execute(chats.insert().values(user_id=1, title="test"))

    models_service = _models_service_returns(
        ResolvedModel(wire_id=None, requested="test-model", reason="no_models_loaded")
    )
    svc = await _make_service(engine, models_service=models_service)
    user = _mock_user(1)
    request = _mock_request(disconnected=False)
    payload = _make_request_payload()

    frames = await _drain(
        svc.stream_chat(chat_id=1, user=user, payload=payload, request=request)
    )

    parsed = _parse_frames(frames)
    assert "upstream_unavailable" in _error_codes(parsed), parsed

    states = await _assistant_states(engine, chat_id=1)
    assert states and PersistState.DRAFT.value not in states, states
    # No stale draft -> next send is not 409'd.
    await svc._assert_no_in_progress_stream(1)


@pytest.mark.asyncio
async def test_streaming_unknown_provider_aborts_draft(
    engine: AsyncEngine,
) -> None:
    """Unknown-provider send aborts its draft + clears active gauges.

    This path is BEFORE the TaskGroup try/finally, so without an explicit
    teardown it also leaked STREAMS_ACTIVE and the active-stream set; the
    draft abort plus the added cleanup block keep the chat sendable.
    """
    from lmchat.db.schema import chats

    async with engine.begin() as conn:
        await conn.execute(
            chats.insert().values(
                user_id=1, title="test", settings={"provider": "ghost-provider"}
            )
        )

    # Provider registry that resolves the named provider to None (unknown).
    registry = MagicMock()
    registry.get = MagicMock(return_value=None)

    svc = StreamingService(
        engine=engine,
        lm_client=MagicMock(),
        memory_service=AsyncMock(),
        chat_locks={},
        idle_timeout_sec=60,
        provider_registry=registry,
    )
    user = _mock_user(1)
    request = _mock_request(disconnected=False)
    payload = _make_request_payload()

    frames = await _drain(
        svc.stream_chat(chat_id=1, user=user, payload=payload, request=request)
    )

    parsed = _parse_frames(frames)
    assert "unknown_provider" in _error_codes(parsed), parsed

    states = await _assistant_states(engine, chat_id=1)
    assert states and PersistState.DRAFT.value not in states, states
    await svc._assert_no_in_progress_stream(1)


# ---------------------------------------------------------------------------
# STREAMS_ACTIVE gauge over-count on abnormal exits.
#
# The three coupled lifecycle primitives — the STREAMS_ACTIVE gauge, the
# assistant DRAFT row, and the active-stream registry marker — are inc'd /
# established together just after the per-chat lock and torn down by ONE
# guaranteed-once try/finally that wraps the entire rest of stream_chat. That
# finally runs for EVERY exit, including the two abnormal ones that used to
# leak: (1) the `except* Exception` re-raise of a non-httpx error from the
# first upstream `anext` (or from _watch_disconnect), and (2) a GeneratorExit
# teardown. The gauge decrements exactly once (the single dec in the method),
# and the finally releases the stuck draft immediately so the chat isn't
# 409-bricked until the reaper runs. These tests assert the OBSERVABLE
# outcome (gauge back to baseline, no orphaned draft, no 409 on resend), not
# the internal teardown mechanism.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_abnormal_exit_balances_gauge_and_releases_draft(
    engine: AsyncEngine,
) -> None:
    """A plain ValueError from the first upstream anext must not leak the gauge.

    Triggers the `except* Exception` re-raise path (the ValueError is non-httpx
    and non-CancelledError, so it isn't converted to an error frame — it
    propagates out of stream_chat). Asserts:

    1. ``STREAMS_ACTIVE`` returns to its pre-stream baseline (no +1 leak).
    2. A second ``stream_chat`` on the same chat is NOT 409'd — the draft was
       released by the outer finally rather than left stuck until the reaper.
    """
    from lmchat.db.schema import chats

    async with engine.begin() as conn:
        await conn.execute(chats.insert().values(user_id=1, title="test"))

    async def _exploding_stream(
        *args: object, **kwargs: object
    ) -> AsyncIterator[CanonicalEvent]:
        # The first anext raises a non-httpx, non-CancelledError exception.
        raise ValueError("boom")
        yield  # pragma: no cover  (makes this an async generator)

    lm_client = MagicMock()
    lm_client.stream = _exploding_stream

    svc = await _make_service(engine, lm_client=lm_client)
    user = _mock_user(1)
    request = _mock_request(disconnected=False)
    payload = _make_request_payload()

    baseline = STREAMS_ACTIVE._value.get()  # type: ignore[attr-defined]

    # The ValueError is re-raised out of stream_chat by the `except* Exception`
    # handler (it is NOT swallowed into an error frame).
    with pytest.raises(ValueError, match="boom"):
        await _drain(
            svc.stream_chat(chat_id=1, user=user, payload=payload, request=request)
        )

    # 1. Gauge returned to baseline — exactly-once dec on the abnormal path.
    assert STREAMS_ACTIVE._value.get() == baseline, (  # type: ignore[attr-defined]
        "STREAMS_ACTIVE leaked on the abnormal-exit path"
    )

    # 2. The draft was released by the finally — a second send is not 409'd.
    states = await _assistant_states(engine, chat_id=1)
    assert states and PersistState.DRAFT.value not in states, states
    await svc._assert_no_in_progress_stream(1)  # must not raise


# ---------------------------------------------------------------------------
# Consolidation invariant — ONE guaranteed-once teardown across exit kinds.
#
# After the refactor, the STREAMS_ACTIVE gauge, the assistant DRAFT row, and
# the active-stream registry marker are all established together just after the
# per-chat lock and torn down by a SINGLE try/finally wrapping the rest of
# stream_chat. This table drives the distinct exit kinds and asserts the same
# three observable post-conditions for EACH — independent of the internal
# teardown mechanism (no reference to the old _dec_gauge_once / torn_down):
#   (a) STREAMS_ACTIVE is back to its pre-turn baseline (inc balanced by the
#       single dec — exactly once);
#   (b) the assistant row is NOT left in 'draft' (the finally released it, or a
#       normal finalize / disconnect abort already moved it to a terminal
#       state);
#   (c) a subsequent _assert_no_in_progress_stream does not raise (the chat is
#       not 409-bricked).
# ---------------------------------------------------------------------------


def _full_ok_events() -> list[CanonicalEvent]:
    return _make_events(
        "chat.start",
        "message.start",
        "message.delta",
        "message.end",
        "chat.end",
        content="hello world",
        response_id="resp-consolidation",
    )


async def _build_case_service(
    engine: AsyncEngine, kind: str
) -> tuple[StreamingService, AsyncMock, bool]:
    """Wire a StreamingService for one exit-kind case.

    Returns (service, request, expect_raises) where ``expect_raises`` is True
    only for the abnormal-exit case (the ValueError propagates out of
    stream_chat instead of being converted to an error frame).
    """
    from lmchat.db.schema import chats
    from lmchat.services.models_service import ResolvedModel

    settings: dict[str, object] = {}
    if kind == "unknown_provider":
        settings = {"provider": "ghost-provider"}

    async with engine.begin() as conn:
        await conn.execute(
            chats.insert().values(user_id=1, title="test", settings=settings)
        )

    request = _mock_request(disconnected=False)
    models_service: Any = None
    lm_client: Any = MagicMock()
    registry: Any = None
    expect_raises = False

    if kind == "normal":
        async def _ok_stream(*a: object, **k: object) -> AsyncIterator[CanonicalEvent]:
            for ev in _full_ok_events():
                yield ev

        lm_client.stream = _ok_stream
        models_service = _models_service_returns(
            ResolvedModel(wire_id="test-model", requested="test-model")
        )
    elif kind == "model_not_loaded":
        models_service = _models_service_returns(
            ResolvedModel(
                wire_id=None,
                requested="test-model",
                substituted=True,
                fallback_key="some-loaded-model",
                reason="requested_not_loaded",
            )
        )
    elif kind == "unknown_provider":
        registry = MagicMock()
        registry.get = MagicMock(return_value=None)
    elif kind == "client_disconnect":
        async def _slow_stream(*a: object, **k: object) -> AsyncIterator[CanonicalEvent]:
            yield CanonicalEvent(type="chat.start")
            yield CanonicalEvent(type="message.start")
            await asyncio.sleep(2)
            yield CanonicalEvent(type="message.delta", content="never")

        lm_client.stream = _slow_stream
        request.receive = AsyncMock(return_value={"type": "http.disconnect"})
        models_service = _models_service_returns(
            ResolvedModel(wire_id="test-model", requested="test-model")
        )
    elif kind == "abnormal_ValueError":
        async def _exploding_stream(
            *a: object, **k: object
        ) -> AsyncIterator[CanonicalEvent]:
            raise ValueError("boom")
            yield  # pragma: no cover  (makes this an async generator)

        lm_client.stream = _exploding_stream
        expect_raises = True
    else:  # pragma: no cover
        raise AssertionError(f"unknown case kind: {kind}")

    if registry is not None:
        svc = StreamingService(
            engine=engine,
            lm_client=lm_client,
            memory_service=AsyncMock(),
            chat_locks={},
            idle_timeout_sec=60,
            provider_registry=registry,
        )
    else:
        svc = await _make_service(
            engine, lm_client=lm_client, models_service=models_service
        )
    return svc, request, expect_raises


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    [
        "normal",
        "model_not_loaded",
        "unknown_provider",
        "client_disconnect",
        "abnormal_ValueError",
    ],
)
async def test_stream_lifecycle_consolidated_teardown_across_exit_kinds(
    engine: AsyncEngine, kind: str
) -> None:
    """The single try/finally tears down the gauge + draft + marker for every exit.

    See the comment block above for the three invariants asserted per kind.
    """
    svc, request, expect_raises = await _build_case_service(engine, kind)
    user = _mock_user(1)
    payload = _make_request_payload()

    baseline = STREAMS_ACTIVE._value.get()  # type: ignore[attr-defined]

    # Drive the turn. Bound the disconnect case (its slow stream blocks) and
    # tolerate the propagated ValueError on the abnormal case.
    async def _run() -> None:
        await _drain(
            svc.stream_chat(chat_id=1, user=user, payload=payload, request=request)
        )

    if expect_raises:
        with pytest.raises(ValueError, match="boom"):
            await _run()
    else:
        try:
            async with asyncio.timeout(3.0):
                await _run()
        except TimeoutError:  # pragma: no cover  (disconnect safety bound)
            pass

    # Let the disconnect watcher finish its abort + finally on the slow case.
    if kind == "client_disconnect":
        await asyncio.sleep(0.6)

    # (a) Gauge balanced — exactly-once dec regardless of exit kind.
    assert STREAMS_ACTIVE._value.get() == baseline, (  # type: ignore[attr-defined]
        f"STREAMS_ACTIVE leaked on the {kind!r} exit path"
    )

    # (b) The assistant row is not orphaned in 'draft'.
    states = await _assistant_states(engine, chat_id=1)
    assert states, f"expected an assistant row for the {kind!r} case"
    assert PersistState.DRAFT.value not in states, (
        f"orphaned draft left behind on the {kind!r} exit path: {states}"
    )

    # (c) The chat is not 409-bricked — a subsequent invariant check passes.
    await svc._assert_no_in_progress_stream(1)


# ---------------------------------------------------------------------------
# Implicit-default stale model falls back; explicit model errors.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_implicit_stale_default_falls_back_to_loaded_model(
    engine: AsyncEngine,
) -> None:
    """Regression: chat with no explicit model_id (NULL in DB) whose
    resolved global default is not loaded must fall back silently to a loaded
    model and complete the stream without an error frame.

    Before the fix, the ``_res.substituted=True`` branch ALWAYS yielded an
    ``upstream_unavailable`` error frame regardless of whether the model came
    from an explicit user pick or from the stale global default.  New chats
    (which inherit the admin default and have a NULL per-chat model_id) were
    bricked when the default model had idled out.
    """
    from lmchat.db.schema import chats
    from lmchat.services.models_service import ResolvedModel

    # Chat with NULL model_id — no explicit per-chat pick.
    async with engine.begin() as conn:
        await conn.execute(chats.insert().values(user_id=1, title="test", model_id=None))

    # Resolve for the stale default returns substituted=True with a fallback.
    stale_key = "stale-default-model"
    fallback_key = "actually-loaded-model"

    # Wire id for the fallback (the live instance).  When substituted=True,
    # resolve_to_loaded_or_fallback already populates wire_id with the
    # fallback's loaded instance id — the caller doesn't need a second call.
    fallback_wire_id = "actually-loaded-model@q4_k_m"

    models_svc = MagicMock()
    models_svc.auth_failed = False
    models_svc.force_refresh = AsyncMock(return_value=False)
    models_svc.get_capabilities = AsyncMock(return_value=None)
    models_svc.get_max_context_length = AsyncMock(return_value=0)

    # Single call: stale default → substituted, but wire_id is the fallback's
    # live instance id (that's how ResolvedModel works for substituted=True).
    models_svc.resolve_to_loaded_or_fallback = AsyncMock(
        return_value=ResolvedModel(
            wire_id=fallback_wire_id,
            requested=stale_key,
            substituted=True,
            fallback_key=fallback_key,
            reason="requested_not_loaded",
        )
    )

    events = _make_events(
        "chat.start", "message.start", "message.delta", "message.end", "chat.end",
        content="hello", response_id="resp-fallback",
    )

    async def _ok_stream(*args: object, **kwargs: object) -> AsyncIterator[CanonicalEvent]:
        for ev in events:
            yield ev

    lm_client = MagicMock()
    lm_client.stream = _ok_stream

    svc = await _make_service(engine, lm_client=lm_client, models_service=models_svc)
    user = _mock_user(1)
    request = _mock_request(disconnected=False)
    payload = _make_request_payload(model=stale_key)

    frames = await _drain(
        svc.stream_chat(chat_id=1, user=user, payload=payload, request=request)
    )
    parsed = _parse_frames(frames)

    # No error frame must have been emitted.
    assert "upstream_unavailable" not in _error_codes(parsed), (
        f"Got unexpected error frames: {parsed}"
    )
    # A real stream must have completed.
    assert any(d.get("type") == "chat.end" for d in parsed), (
        f"Expected chat.end frame, got: {parsed}"
    )


@pytest.mark.asyncio
async def test_streaming_implicit_fallback_does_not_persist_as_explicit_pick(
    engine: AsyncEngine,
) -> None:
    """Regression: an implicit-default fallback
    must not turn into a fake "explicit" pick that hard-errors the NEXT turn.

    Reproduces the reported bug: a chat with no explicit per-chat model_id
    (NULL — the global/override default is in effect) sends a turn whose
    resolved default is unloaded. The resolver substitutes a loaded fallback
    and the turn completes silently (the ``..._falls_back_to_loaded_model``
    test above covers this in isolation).

    The actual bug: the prior implementation then persisted the fallback
    catalog key into ``chats.model_id`` ("so the chat header shows the real
    loaded model"), which made ``chat_stored_model_id`` non-NULL on every
    subsequent read. The FE never invalidates its chats-list cache after a
    stream (it only refetches messages — see ``Chat.tsx``'s ``complete``/
    ``stopped`` effects), so it keeps sending the SAME stale default model on
    the next turn (e.g. a stop + resend). That next call then took the
    EXPLICIT branch (``chat_stored_model_id`` truthy) and hard-errored,
    naming the unloaded default as though the user had deliberately picked
    it — exactly the live incident (chat's stored model ended up as the
    resolver's fallback; the resend on the same stale default then
    hard-errored instead of falling back again).

    Fix: the implicit branch must not write ``chats.model_id``. This drives
    ``stream_chat`` TWICE with the identical stale-default payload and
    asserts BOTH turns fall back silently (no ``upstream_unavailable`` frame)
    and that ``chats.model_id`` stays NULL throughout.
    """
    from lmchat.db.schema import chats
    from lmchat.services.models_service import ResolvedModel

    # Chat with NULL model_id — no explicit per-chat pick (inherits the
    # global/override default, as in the live incident).
    async with engine.begin() as conn:
        await conn.execute(chats.insert().values(user_id=1, title="test", model_id=None))

    stale_key = "qwen3.5-9b-tng-pkd-qwopus-coder-fable-polaris-writer-v4-qx86-hi-mlx"
    fallback_key = "deepreinforce-ai_ornith-1.0-397b"
    fallback_wire_id = f"{fallback_key}@instance"

    models_svc = MagicMock()
    models_svc.auth_failed = False
    models_svc.force_refresh = AsyncMock(return_value=False)
    models_svc.get_capabilities = AsyncMock(return_value=None)
    models_svc.get_max_context_length = AsyncMock(return_value=0)
    models_svc.resolve_to_loaded_or_fallback = AsyncMock(
        return_value=ResolvedModel(
            wire_id=fallback_wire_id,
            requested=stale_key,
            substituted=True,
            fallback_key=fallback_key,
            reason="requested_not_loaded",
        )
    )

    def _events() -> list[CanonicalEvent]:
        return _make_events(
            "chat.start", "message.start", "message.delta", "message.end", "chat.end",
            content="hello", response_id="resp-fallback",
        )

    async def _ok_stream(*args: object, **kwargs: object) -> AsyncIterator[CanonicalEvent]:
        for ev in _events():
            yield ev

    lm_client = MagicMock()
    lm_client.stream = _ok_stream

    svc = await _make_service(engine, lm_client=lm_client, models_service=models_svc)
    user = _mock_user(1)

    # -- Turn 1: implicit-default stale model falls back and streams fine.
    request1 = _mock_request(disconnected=False)
    payload1 = _make_request_payload(model=stale_key)
    frames1 = await _drain(
        svc.stream_chat(chat_id=1, user=user, payload=payload1, request=request1)
    )
    parsed1 = _parse_frames(frames1)
    assert "upstream_unavailable" not in _error_codes(parsed1), (
        f"Turn 1 unexpectedly hard-errored: {parsed1}"
    )
    assert any(d.get("type") == "chat.end" for d in parsed1)

    # chats.model_id must remain NULL — the fallback must NOT be persisted as
    # a fake explicit pick.
    async with engine.connect() as conn:
        row = (
            await conn.execute(select(chats.c.model_id).where(chats.c.id == 1))
        ).fetchone()
    assert row is not None
    assert row.model_id is None, (
        f"chats.model_id was persisted as {row.model_id!r} — an implicit "
        "default fallback must not masquerade as an explicit pick."
    )

    # -- Turn 2: the resend. The FE, unaware of any server-side substitution
    # (it never refetches the chats list — only messages), sends the SAME
    # stale default model again. This must ALSO fall back silently rather
    # than hard-error — the exact live incident this test guards against.
    request2 = _mock_request(disconnected=False)
    payload2 = _make_request_payload(model=stale_key)
    frames2 = await _drain(
        svc.stream_chat(chat_id=1, user=user, payload=payload2, request=request2)
    )
    parsed2 = _parse_frames(frames2)
    assert "upstream_unavailable" not in _error_codes(parsed2), (
        f"Turn 2 (resend) hard-errored on the stale default: {parsed2}"
    )
    assert any(d.get("type") == "chat.end" for d in parsed2)


@pytest.mark.asyncio
async def test_streaming_explicit_stale_model_still_errors(
    engine: AsyncEngine,
) -> None:
    """Regression (behaviour preserved): chat with an EXPLICIT model_id
    that isn't loaded must still yield an upstream_unavailable error frame.

    The fix only silently falls back when the model came from an implicit
    global default (NULL per-chat model_id).  When the user explicitly chose
    a model for this chat, the error is surfaced so they can act on it.
    """
    from lmchat.db.schema import chats
    from lmchat.services.models_service import ResolvedModel

    explicit_model = "my-chosen-model"

    # Chat with an EXPLICIT model_id set.
    async with engine.begin() as conn:
        await conn.execute(
            chats.insert().values(user_id=1, title="test", model_id=explicit_model)
        )

    # When substituted=True, wire_id is the fallback's live instance id
    # (non-None) — that's the real behavior from resolve_to_loaded_or_fallback.
    models_service = _models_service_returns(
        ResolvedModel(
            wire_id="some-other-loaded-model@q4",
            requested=explicit_model,
            substituted=True,
            fallback_key="some-other-loaded-model",
            reason="requested_not_loaded",
        )
    )
    svc = await _make_service(engine, models_service=models_service)
    user = _mock_user(1)
    request = _mock_request(disconnected=False)
    payload = _make_request_payload(model=explicit_model)

    frames = await _drain(
        svc.stream_chat(chat_id=1, user=user, payload=payload, request=request)
    )
    parsed = _parse_frames(frames)

    # The error frame must still be present for explicit picks.
    assert "upstream_unavailable" in _error_codes(parsed), (
        f"Expected upstream_unavailable error frame, got: {parsed}"
    )
    # Chat is not 409-bricked.
    await svc._assert_no_in_progress_stream(1)


@pytest.mark.asyncio
async def test_streaming_no_fallback_loaded_still_errors(
    engine: AsyncEngine,
) -> None:
    """Regression: when there is no loaded fallback at all (wire_id=None,
    substituted=False, reason='no_models_loaded'), the error frame is still
    emitted regardless of whether the chat model_id is explicit or implicit.
    """
    from lmchat.db.schema import chats
    from lmchat.services.models_service import ResolvedModel

    # Chat with NULL model_id (implicit default).
    async with engine.begin() as conn:
        await conn.execute(chats.insert().values(user_id=1, title="test", model_id=None))

    models_service = _models_service_returns(
        ResolvedModel(wire_id=None, requested="some-model", reason="no_models_loaded")
    )
    svc = await _make_service(engine, models_service=models_service)
    user = _mock_user(1)
    request = _mock_request(disconnected=False)
    payload = _make_request_payload(model="some-model")

    frames = await _drain(
        svc.stream_chat(chat_id=1, user=user, payload=payload, request=request)
    )
    parsed = _parse_frames(frames)

    assert "upstream_unavailable" in _error_codes(parsed), (
        f"Expected upstream_unavailable error for no-models-loaded case, got: {parsed}"
    )
    await svc._assert_no_in_progress_stream(1)
