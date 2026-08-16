# SPDX-License-Identifier: Apache-2.0
"""Unit tests: ``_apply_tool_call_delta`` excludes failures from the
consecutive-identical-tool-round backstop (2026-08-15).

Prior behaviour: the signature ``f"{name}\\x00{json.dumps(args)}"`` had no
success/failure component, so a model correctly RETRYING the same call after
a flaky MCP server / transient failure got counted identically to a
hallucinating success-loop, and could trip ``_MAX_IDENTICAL_TOOL_ROUNDS``
after N legitimate retries with zero progress made. Its sibling detector in
``lmstudio_streaming_client.py`` already carved this out — "match only
against prior SUCCESSFUL identical calls (fail -> retry with same args is
legitimate)" — this fix mirrors that carve-out on the service-side backstop.

Exercises ``StreamingService._apply_tool_call_delta`` directly (pure w.r.t.
I/O: ``_increment_tool_round`` only touches an in-memory LRU), not a full
``stream_chat`` drive — mirrors ``test_streaming_loop_cut.py``'s preference
for testing the extracted decision unit directly over a full turn.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import metadata
from lmchat.lmstudio.types import CanonicalEvent, CanonicalToolCall
from lmchat.services.streaming_service import StreamingService


@pytest.fixture()
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


def _svc(engine: AsyncEngine) -> StreamingService:
    return StreamingService(
        engine=engine,
        lm_client=MagicMock(),
        memory_service=AsyncMock(),
        chat_locks={},
    )


def _event(event_type: str, *, name: str = "search_web", args: dict[str, str]) -> CanonicalEvent:
    return CanonicalEvent(
        type=event_type,  # type: ignore[arg-type]
        tool_call=CanonicalToolCall(id="tc-1", name=name, arguments=args),
    )


def _drive(svc: StreamingService, events: list[CanonicalEvent]) -> int:
    """Feed *events* through ``_apply_tool_call_delta`` in order, returning
    the final ``consecutive_identical_rounds``."""
    turn_tool_rounds = 0
    last_tool_sig: str | None = None
    consecutive_identical_rounds = 0
    for ev in events:
        turn_tool_rounds, last_tool_sig, consecutive_identical_rounds = svc._apply_tool_call_delta(
            event=ev,
            chat_id=1,
            state={},
            accumulated_tool_calls=[],
            turn_tool_rounds=turn_tool_rounds,
            last_tool_sig=last_tool_sig,
            consecutive_identical_rounds=consecutive_identical_rounds,
        )
    return consecutive_identical_rounds


@pytest.mark.asyncio
async def test_n_identical_successes_trip_the_streak(engine: AsyncEngine) -> None:
    """N identical SUCCESSFUL calls still count consecutively (unchanged)."""
    svc = _svc(engine)
    args = {"query": "same query"}
    events = [_event("tool_call.success", args=args) for _ in range(5)]
    # 1st success establishes the signature (streak 0); each of the next 4
    # identical successes increments it once more.
    assert _drive(svc, events) == 4


@pytest.mark.asyncio
async def test_n_identical_failures_do_not_trip_the_streak(engine: AsyncEngine) -> None:
    """N identical FAILED calls never move consecutive_identical_rounds —
    the retry-after-failure carve-out (red-on-revert for this fix)."""
    svc = _svc(engine)
    args = {"query": "same query"}
    events = [_event("tool_call.failure", args=args) for _ in range(5)]
    assert _drive(svc, events) == 0


@pytest.mark.asyncio
async def test_failure_does_not_reset_a_prior_success_streak(engine: AsyncEngine) -> None:
    """A failed call with the SAME args as the running success streak is
    skipped entirely — it neither extends nor resets the count."""
    svc = _svc(engine)
    args = {"query": "same query"}
    events = [
        _event("tool_call.success", args=args),  # streak -> 0 (sig set)
        _event("tool_call.success", args=args),  # identical -> streak 1
        _event("tool_call.failure", args=args),  # ignored: streak stays 1
        _event("tool_call.success", args=args),  # identical -> streak 2
    ]
    assert _drive(svc, events) == 2


@pytest.mark.asyncio
async def test_failure_between_different_successes_does_not_bridge_them(
    engine: AsyncEngine,
) -> None:
    """A failure sandwiched between two DIFFERENT successful calls has no
    signature-tracking effect either way — the second success is compared
    against the first success's signature, not the failure's."""
    svc = _svc(engine)
    events = [
        _event("tool_call.success", args={"query": "a"}),  # sig=a, streak 0
        _event("tool_call.failure", args={"query": "b"}),  # ignored
        _event("tool_call.success", args={"query": "a"}),  # identical to sig=a -> streak 1
    ]
    assert _drive(svc, events) == 1


@pytest.mark.asyncio
async def test_mixed_failures_and_varied_successes_never_trip(engine: AsyncEngine) -> None:
    """A model retrying different args on failure, interleaved with
    failures of the SAME args, never accumulates a streak — no false
    positive on legitimate flaky-tool-retry behaviour."""
    svc = _svc(engine)
    events = [
        _event("tool_call.failure", args={"query": "x"}),
        _event("tool_call.failure", args={"query": "x"}),
        _event("tool_call.success", args={"query": "x"}),  # sig=x, streak 0
        _event("tool_call.failure", args={"query": "x"}),  # ignored
        _event("tool_call.success", args={"query": "y"}),  # different -> sig=y, streak 0
    ]
    assert _drive(svc, events) == 0
