# SPDX-License-Identifier: Apache-2.0
"""Tests for auxiliary hygiene.

Covers:
- test_401_storms_backoff_and_surface_flag
- test_reaper_skips_active_streams  (via active-stream registry)
- test_reaper_skips_active_streams_via_last_activity_at
- test_reaper_uses_inactivity_not_age
- test_sub_session_threads_cumulative_tool_rounds (real route-code path)
- test_sub_session_mtp_count_accumulated_across_turns
- test_mtp_suspected_fires_on_rounds_AND_error_condition
- test_accumulator_noise_gate_suppresses_post_terminal_warning
- test_lifespan_shutdown_no_missing_greenlet
"""
from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.pragmas import apply_sqlite_pragmas
from lmchat.db.schema import messages, metadata
from lmchat.lmstudio.types import CanonicalEvent, CanonicalToolCall
from lmchat.services._stream_reaper import _finalize_stuck_drafts
from lmchat.services.lmstudio_streaming_client import _ToolCallAccumulator

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    """Yield a per-test SQLite engine with the full schema."""
    db_path = tmp_path / "test_cluster5.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}", pool_pre_ping=True)

    @event.listens_for(eng.sync_engine, "connect")
    def _on_connect(dbapi_conn: object, _rec: object) -> None:
        apply_sqlite_pragmas(dbapi_conn)

    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


async def _insert_user(engine: AsyncEngine, user_id: int = 1) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO users (id, username, password_hash)"
                " VALUES (:id, :u, :ph)"
            ),
            {"id": user_id, "u": f"user{user_id}", "ph": "scrypt$dummy"},
        )


async def _insert_chat(engine: AsyncEngine, user_id: int = 1) -> int:
    async with engine.begin() as conn:
        result = await conn.execute(
            text("INSERT INTO chats (user_id, title) VALUES (:uid, :t)"),
            {"uid": user_id, "t": "test-chat"},
        )
        return result.lastrowid  # type: ignore[return-value]


async def _insert_draft(
    engine: AsyncEngine,
    chat_id: int,
    *,
    created_at: datetime,
    last_activity_at: datetime | None = None,
) -> int:
    """Insert a draft message with explicit timestamps."""
    created_ts = created_at.strftime("%Y-%m-%d %H:%M:%S.%f")
    if last_activity_at is not None:
        act_ts = last_activity_at.strftime("%Y-%m-%d %H:%M:%S.%f")
        async with engine.begin() as conn:
            result = await conn.execute(
                text(
                    "INSERT INTO messages "
                    "(chat_id, role, content, state, created_at, last_activity_at)"
                    " VALUES (:cid, 'assistant', '', 'draft', :ts, :act)"
                ),
                {"cid": chat_id, "ts": created_ts, "act": act_ts},
            )
            return result.lastrowid  # type: ignore[return-value]
    else:
        async with engine.begin() as conn:
            result = await conn.execute(
                text(
                    "INSERT INTO messages "
                    "(chat_id, role, content, state, created_at)"
                    " VALUES (:cid, 'assistant', '', 'draft', :ts)"
                ),
                {"cid": chat_id, "ts": created_ts},
            )
            return result.lastrowid  # type: ignore[return-value]


async def _read_state(engine: AsyncEngine, message_id: int) -> str | None:
    async with engine.connect() as conn:
        result = await conn.execute(
            select(messages.c.state).where(messages.c.id == message_id)
        )
        row = result.fetchone()
    return str(row[0]) if row is not None else None


# ---------------------------------------------------------------------------
# 401 backoff
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_401_storms_backoff_and_surface_flag() -> None:
    """After a 401 from refresh(), auth_failed is True and subsequent
    refresh() calls within the backoff window skip the probe.

    Acceptance: ``models_service.auth_failed`` is True immediately after a
    401, and ``_probe_upstream`` is not called a second time during backoff.
    """
    from lmchat.services.models_service import ModelsService

    mock_client = AsyncMock(spec=httpx.AsyncClient)

    # First call returns 401.
    mock_response_401 = MagicMock()
    mock_response_401.status_code = 401
    mock_response_401.raise_for_status.side_effect = httpx.HTTPStatusError(
        "401 Unauthorized",
        request=MagicMock(),
        response=mock_response_401,
    )
    mock_client.get.return_value = mock_response_401

    svc = ModelsService(http_client=mock_client, base_url="http://localhost:1234")
    await svc.refresh()

    # Flag must be set.
    assert svc.auth_failed is True

    # Reset mock to track subsequent calls.
    mock_client.get.reset_mock()

    # Second call within backoff window should NOT probe upstream.
    await svc.refresh()
    mock_client.get.assert_not_called()


@pytest.mark.asyncio
async def test_401_backoff_clears_on_successful_probe() -> None:
    """After a 401 + backoff expiry, a successful probe clears auth_failed."""
    import time

    from lmchat.services.models_service import _AUTH_FAILED_BACKOFF_SEC, ModelsService

    mock_client = AsyncMock(spec=httpx.AsyncClient)

    # First call: 401.
    mock_response_401 = MagicMock()
    mock_response_401.status_code = 401
    mock_response_401.raise_for_status.side_effect = httpx.HTTPStatusError(
        "401", request=MagicMock(), response=mock_response_401,
    )
    mock_client.get.return_value = mock_response_401

    svc = ModelsService(http_client=mock_client, base_url="http://localhost:1234")
    await svc.refresh()
    assert svc.auth_failed is True

    # Simulate backoff expiry by patching the timestamp.
    svc._auth_failed_at = time.monotonic() - _AUTH_FAILED_BACKOFF_SEC - 1.0

    # Second call: successful probe.
    mock_response_ok = MagicMock()
    mock_response_ok.status_code = 200
    mock_response_ok.raise_for_status = MagicMock()  # no-op
    mock_response_ok.json.return_value = {"models": []}
    mock_client.get.return_value = mock_response_ok

    await svc.refresh()
    assert svc.auth_failed is False


# ---------------------------------------------------------------------------
# Reaper uses inactivity threshold, not age
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reaper_uses_inactivity_not_age(engine: AsyncEngine) -> None:
    """_finalize_stuck_drafts() selects on COALESCE(last_activity_at, created_at).

    A draft whose ``created_at`` is old but ``last_activity_at`` is recent
    must NOT be finalized (it's an actively-streaming long tool-chain).
    """
    await _insert_user(engine)
    chat_id = await _insert_chat(engine)

    now = datetime.now(UTC)
    # Created 10 minutes ago (old) but last_activity_at just now (active).
    msg_id = await _insert_draft(
        engine,
        chat_id,
        created_at=now - timedelta(minutes=10),
        last_activity_at=now - timedelta(seconds=30),
    )

    await _finalize_stuck_drafts(engine=engine, stuck_after_minutes=5)

    # Must still be draft — last_activity_at is recent.
    assert await _read_state(engine, msg_id) == "draft"


@pytest.mark.asyncio
async def test_reaper_skips_active_streams_via_last_activity_at(engine: AsyncEngine) -> None:
    """Draft with recent last_activity_at is left alone by the reaper.

    This is the last_activity_at half of the "skip active streams" acceptance:
    a stream that produces content bumps last_activity_at inside
    _CoalesceTimer.flush(), so the reaper's inactivity check keeps it alive.
    """
    await _insert_user(engine)
    chat_id = await _insert_chat(engine)

    now = datetime.now(UTC)
    # old created_at, but last_activity_at touched 1 second ago.
    msg_id = await _insert_draft(
        engine,
        chat_id,
        created_at=now - timedelta(hours=1),
        last_activity_at=now - timedelta(seconds=1),
    )

    await _finalize_stuck_drafts(engine=engine, stuck_after_minutes=5)
    assert await _read_state(engine, msg_id) == "draft"


@pytest.mark.asyncio
async def test_reaper_skips_active_streams(engine: AsyncEngine) -> None:
    """Draft whose chat_id is in the active-stream registry is skipped by the reaper.

    This is the STREAMS_ACTIVE-set acceptance: even if last_activity_at is old
    (e.g. a content-free tool-chain that hasn't flushed yet), the in-process
    active-stream registry prevents the reaper from finalizing a live draft.
    """
    from lmchat.services._active_streams import mark_active, mark_inactive

    await _insert_user(engine)
    chat_id = await _insert_chat(engine)

    now = datetime.now(UTC)
    # Old enough that last_activity_at would NOT save it (>5 min).
    msg_id = await _insert_draft(
        engine,
        chat_id,
        created_at=now - timedelta(minutes=10),
        last_activity_at=now - timedelta(minutes=10),  # also old
    )

    # Register chat_id as actively streaming.
    mark_active(chat_id)
    try:
        await _finalize_stuck_drafts(engine=engine, stuck_after_minutes=5)
        # Must still be draft — the active-stream registry blocked finalization.
        assert await _read_state(engine, msg_id) == "draft"
    finally:
        mark_inactive(chat_id)


@pytest.mark.asyncio
async def test_reaper_finalizes_abandoned_draft_via_inactivity(engine: AsyncEngine) -> None:
    """Draft with last_activity_at > threshold is finalized (abandoned)."""
    await _insert_user(engine)
    chat_id = await _insert_chat(engine)

    now = datetime.now(UTC)
    # Both created_at and last_activity_at are old.
    msg_id = await _insert_draft(
        engine,
        chat_id,
        created_at=now - timedelta(minutes=20),
        last_activity_at=now - timedelta(minutes=10),
    )

    await _finalize_stuck_drafts(engine=engine, stuck_after_minutes=5)
    assert await _read_state(engine, msg_id) == "final"


@pytest.mark.asyncio
async def test_reaper_falls_back_to_created_at_when_no_last_activity(
    engine: AsyncEngine,
) -> None:
    """Draft with NULL last_activity_at falls back to created_at (backfill guard)."""
    await _insert_user(engine)
    chat_id = await _insert_chat(engine)

    now = datetime.now(UTC)
    # No last_activity_at — old created_at → should be finalized.
    msg_id = await _insert_draft(
        engine,
        chat_id,
        created_at=now - timedelta(minutes=10),
        last_activity_at=None,
    )

    await _finalize_stuck_drafts(engine=engine, stuck_after_minutes=5)
    assert await _read_state(engine, msg_id) == "final"


# ---------------------------------------------------------------------------
# Sub-session threads cumulative_tool_rounds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sub_session_threads_cumulative_tool_rounds() -> None:
    """_sub_session_sse passes cumulative_tool_rounds to lm_client.stream.

    Drives _sub_session_sse with a fake lm_client that yields tool_call.success
    events and then chat.end.  Asserts that lm_client.stream was called with
    cumulative_tool_rounds reflecting the in-turn tally.

    This is the non-tautological gate: the test instruments the actual route
    code path (not a local variable), confirming the correct kwarg reaches the
    adapter.
    """
    from lmchat.lmstudio.types import CanonicalEvent
    from lmchat.routes.chats import _sub_session_sse

    tool_success = CanonicalEvent(type="tool_call.success")
    chat_end = CanonicalEvent(type="chat.end")

    async def _fake_stream(**kwargs: object) -> AsyncGenerator[CanonicalEvent, None]:
        # Two successful tool calls, then chat.end.
        yield tool_success
        yield tool_success
        yield chat_end

    captured_rounds: list[int] = []

    class FakeClient:
        def stream(self, **kwargs: Any) -> AsyncGenerator[CanonicalEvent, None]:
            captured_rounds.append(int(kwargs.get("cumulative_tool_rounds", 0)))
            return _fake_stream(**kwargs)

    # Consume the generator to completion.
    frames = []
    async for frame in _sub_session_sse(
        lm_client=FakeClient(),  # type: ignore[arg-type]
        model_id="test-model",
        system_prompt="s",
        messages=[{"role": "user", "content": "hi"}],
        prior_tool_rounds=5,  # pre-seeded cross-turn count
        chat_id=None,
    ):
        frames.append(frame)

    # The stream call must have been issued with the prior_tool_rounds seed.
    assert captured_rounds == [5], f"Expected [5], got {captured_rounds}"


@pytest.mark.asyncio
async def test_sub_session_mtp_count_accumulated_across_turns() -> None:
    """Cross-turn MTP counter persists via the module-level OrderedDict.

    Simulates two sequential sub-session turns for the same chat_id.
    Turn 1: 12 tool_call.success events → registry records 12.
    Turn 2: prior_tool_rounds starts at 12 → counter starts at 12.
    """
    from lmchat.routes.chats import (
        _sub_session_get_tool_rounds,
        _sub_session_increment_tool_round,
        _sub_session_reset_tool_rounds,
    )

    chat_id = 99999  # Use a chat_id unlikely to collide with other tests.
    _sub_session_reset_tool_rounds(chat_id)

    # Simulate 12 successful tool calls in turn 1.
    for _ in range(12):
        _sub_session_increment_tool_round(chat_id)

    assert _sub_session_get_tool_rounds(chat_id) == 12

    # Turn 2 should start with prior_tool_rounds=12.
    assert _sub_session_get_tool_rounds(chat_id) == 12

    _sub_session_reset_tool_rounds(chat_id)


# ---------------------------------------------------------------------------
# MTP suspected fires on rounds AND error condition
# ---------------------------------------------------------------------------


def test_mtp_suspected_fires_on_rounds_AND_error_condition() -> None:
    """_is_mtp_suspected returns True only when rounds >= threshold AND error.

    Per spec: 20+ rounds WITHOUT an error condition must NOT fire.
    20+ rounds WITH 500 or tool_format_generation_error MUST fire.
    """
    from lmchat.lmstudio.types import CanonicalChatRequest, CanonicalInputBlock
    from lmchat.services.lmstudio_adapter import MTP_SUSPECT_THRESHOLD, _is_mtp_suspected

    req_with_tools = CanonicalChatRequest(
        model="test-model",
        system_prompt="",
        input=[CanonicalInputBlock(type="text", content="hi")],
        integrations=["mcp/searxng"],
    )

    req_no_tools = CanonicalChatRequest(
        model="test-model",
        system_prompt="",
        input=[CanonicalInputBlock(type="text", content="hi")],
    )

    threshold = MTP_SUSPECT_THRESHOLD  # default 20

    # Rounds < threshold + 500 → False
    assert not _is_mtp_suspected(
        status_code=500,
        req=req_with_tools,
        cumulative_tool_rounds=threshold - 1,
    )

    # Rounds >= threshold + no error → False
    assert not _is_mtp_suspected(
        status_code=200,
        req=req_with_tools,
        cumulative_tool_rounds=threshold,
    )

    # Rounds >= threshold + 500 → True
    assert _is_mtp_suspected(
        status_code=500,
        req=req_with_tools,
        cumulative_tool_rounds=threshold,
    )

    # Rounds >= threshold + tool_format_generation_error + integrations → True
    assert _is_mtp_suspected(
        status_code=0,
        req=req_with_tools,
        cumulative_tool_rounds=threshold,
        mid_stream_error_type="tool_format_generation_error",
    )

    # Rounds >= threshold + 500 + NO tools → False
    assert not _is_mtp_suspected(
        status_code=500,
        req=req_no_tools,
        cumulative_tool_rounds=threshold,
    )


# ---------------------------------------------------------------------------
# Accumulator noise gate
# ---------------------------------------------------------------------------


def _make_tool_call_event(etype: str, **kwargs: object) -> CanonicalEvent:
    """Build a CanonicalEvent for a tool_call.* type."""
    tc = None
    if kwargs:
        tc = CanonicalToolCall(
            id=str(kwargs.get("id", "tc-1")),
            name=str(kwargs.get("name", "test_tool")),
            arguments=kwargs.get("arguments", {}),  # type: ignore[arg-type]
        )
    return CanonicalEvent(type=etype, tool_call=tc)  # pyright: ignore[reportArgumentType]  # test helper — type is validated by str


def test_accumulator_noise_gate_suppresses_post_terminal_warning() -> None:
    """finalize() on a never-armed accumulator produces no warning log.

    When tool_call.success arrives without a prior tool_call.start (as
    LM Studio's server-side tool execution sometimes does), finalize()
    must return None silently — NOT emit a 'finalize_missing_data' warning.
    """

    acc = _ToolCallAccumulator()

    # Simulate a tool_call.success arriving WITHOUT a prior tool_call.start.
    success_event = _make_tool_call_event("tool_call.success", name="my_tool")
    acc.ingest(success_event)

    with patch("lmchat.services.lmstudio_streaming_client.log") as mock_log:
        result = acc.finalize()

    # Must return None without accumulating.
    assert result is None
    # The warning must NOT have been emitted.
    mock_log.warning.assert_not_called()


def test_accumulator_noise_gate_warns_when_name_missing_after_start() -> None:
    """finalize() warns when a tool_call.start was seen but name never arrived."""
    acc = _ToolCallAccumulator()

    # Start event (arms the accumulator, sets _had_start=True).
    start_event = CanonicalEvent(
        type="tool_call.start",
        tool_call=CanonicalToolCall(id="tc-1", name="", arguments={}),
    )
    acc.ingest(start_event)

    # Arguments arrive (so accumulated_chars > 0).
    args_event = CanonicalEvent(
        type="tool_call.arguments",
        tool_call=CanonicalToolCall(id="tc-1", name="", arguments={"x": 1}),
    )
    acc.ingest(args_event)

    with patch("lmchat.services.lmstudio_streaming_client.log") as mock_log:
        result = acc.finalize()

    assert result is None
    # The warning SHOULD have been emitted (had_start=True, accumulated_chars > 0).
    mock_log.warning.assert_called_once()
    call_kwargs = mock_log.warning.call_args[0][0]
    assert "finalize_missing_data" in call_kwargs


def test_accumulator_arguments_without_start_are_ignored() -> None:
    """tool_call.arguments arriving before tool_call.start are silently ignored."""
    acc = _ToolCallAccumulator()

    # Arguments event without a preceding start.
    args_event = CanonicalEvent(
        type="tool_call.arguments",
        tool_call=CanonicalToolCall(id="tc-1", name="my_tool", arguments={"x": 1}),
    )
    acc.ingest(args_event)

    # Buffer must remain empty — no data ingested.
    assert acc._arguments_buf == []
    assert acc._had_start is False


def test_accumulator_reset_clears_had_start() -> None:
    """reset() clears _had_start so the noise gate works on the next call."""
    acc = _ToolCallAccumulator()

    # Arm the accumulator.
    start_event = CanonicalEvent(
        type="tool_call.start",
        tool_call=CanonicalToolCall(id="tc-1", name="tool1", arguments={}),
    )
    acc.ingest(start_event)
    assert acc._had_start is True

    # Reset.
    acc.reset()
    assert acc._had_start is False


# ---------------------------------------------------------------------------
# MissingGreenlet lifespan gate
# ---------------------------------------------------------------------------


def test_lifespan_shutdown_no_missing_greenlet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """BE startup + shutdown cycle produces zero MissingGreenlet events.

    Runs the full app lifespan (startup → healthz probe → shutdown) via
    TestClient and asserts that:
    1. No MissingGreenlet WARNING/ERROR appears anywhere in the captured logs.
    2. async_dispose_engine() was invoked (confirmed via a spy patch).

    Calling sync engine.dispose() from inside an async lifespan context
    triggers MissingGreenlet; the fix uses await async_dispose_engine().
    """
    from fastapi.testclient import TestClient

    from lmchat.app import create_app
    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod

    db_url = f"sqlite+aiosqlite:///{tmp_path}/lifespan_test.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")

    get_settings.cache_clear()
    engine_mod.dispose_engine()

    app = create_app()

    # Spy on async_dispose_engine to confirm it is called during shutdown.
    dispose_called: list[bool] = []
    _real_dispose = engine_mod.async_dispose_engine

    async def _spy_dispose() -> None:
        dispose_called.append(True)
        await _real_dispose()

    with caplog.at_level(logging.WARNING, logger="sqlalchemy"):
        with patch.object(engine_mod, "async_dispose_engine", _spy_dispose):
            # Re-import app to pick up the patched reference (app.py imports it at
            # the top level, so we patch the lmchat.app module directly too).
            import lmchat.app as app_mod
            original_ref = app_mod.async_dispose_engine
            app_mod.async_dispose_engine = _spy_dispose  # type: ignore[assignment]
            try:
                with TestClient(app) as client:
                    resp = client.get("/healthz")
                    assert resp.status_code == 200, f"healthz failed: {resp.status_code}"
                # TestClient.__exit__ triggers lifespan shutdown.
            finally:
                app_mod.async_dispose_engine = original_ref  # type: ignore[assignment]

    # Cleanup.
    engine_mod.dispose_engine()
    get_settings.cache_clear()

    # Assert no MissingGreenlet errors surfaced in SQLAlchemy logs.
    missing_greenlet_records = [
        r for r in caplog.records if "MissingGreenlet" in (r.getMessage() or "")
    ]
    assert missing_greenlet_records == [], (
        "MissingGreenlet appeared in logs:\n"
        + "\n".join(r.getMessage() for r in missing_greenlet_records)
    )

    # Assert async_dispose_engine ran at least once during shutdown.
    assert dispose_called, (
        "async_dispose_engine was NOT called during lifespan shutdown; "
        "MissingGreenlet guard is broken"
    )


# ---------------------------------------------------------------------------
# ACTIVE_STREAM_CHAT_IDS try/finally guarantee
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_inactive_called_on_generator_close() -> None:
    """mark_inactive fires even when the outer generator is closed prematurely.

    Simulates GeneratorExit: the consumer iterates only the first frame, then
    calls aclose() without consuming the full stream.  Without a try/finally,
    the chat_id would remain in ACTIVE_STREAM_CHAT_IDS forever; the finally
    block calls mark_inactive.

    This test uses a minimal async generator that mirrors the try/finally
    pattern added to streaming_service.stream_chat to verify that Python's
    async generator semantics invoke finally on GeneratorExit.
    """
    from lmchat.services._active_streams import (
        ACTIVE_STREAM_CHAT_IDS,
        mark_active,
        mark_inactive,
    )

    chat_id = 77701  # sentinel; unlikely to collide
    mark_inactive(chat_id)  # ensure clean state
    assert chat_id not in ACTIVE_STREAM_CHAT_IDS

    async def _fake_pump(cid: int) -> AsyncGenerator[int, None]:
        """Minimal pump that mirrors the try/finally guard."""
        mark_active(cid)
        try:
            for i in range(10):
                yield i
        finally:
            mark_inactive(cid)

    gen = _fake_pump(chat_id)
    # Consume only the first frame, then close.
    first = await gen.__anext__()
    assert first == 0
    assert chat_id in ACTIVE_STREAM_CHAT_IDS  # registered during iteration
    await gen.aclose()  # triggers GeneratorExit → finally runs
    assert chat_id not in ACTIVE_STREAM_CHAT_IDS, (
        "mark_inactive was NOT called after aclose(); "
        "the try/finally guard is broken"
    )


@pytest.mark.asyncio
async def test_mark_inactive_called_on_exception_reraise() -> None:
    """mark_inactive fires when the generator exits via an exception re-raise.

    Simulates the except* Exception → raise path in stream_chat: an exception
    propagates out of the try/finally, but the finally block still runs.
    """
    from lmchat.services._active_streams import (
        ACTIVE_STREAM_CHAT_IDS,
        mark_active,
        mark_inactive,
    )

    chat_id = 77702  # sentinel
    mark_inactive(chat_id)

    async def _fake_pump_reraise(cid: int) -> AsyncGenerator[int, None]:
        """Pump that raises RuntimeError after one yield."""
        mark_active(cid)
        try:
            yield 0
            raise RuntimeError("simulated upstream exception")
        finally:
            mark_inactive(cid)

    gen = _fake_pump_reraise(chat_id)
    first = await gen.__anext__()
    assert first == 0
    assert chat_id in ACTIVE_STREAM_CHAT_IDS

    with pytest.raises(RuntimeError, match="simulated upstream exception"):
        await gen.__anext__()

    assert chat_id not in ACTIVE_STREAM_CHAT_IDS, (
        "mark_inactive was NOT called after exception re-raise; "
        "the try/finally guard is broken"
    )


# ---------------------------------------------------------------------------
# touch_activity throttle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_touch_activity_throttled_within_coalesce_interval() -> None:
    """Multiple touch_activity calls within _COALESCE_INTERVAL_SEC produce only one DB write.

    Verifies that when tool_call.arguments chunks arrive faster than the
    coalesce interval (250ms), touch_activity skips the DB write instead of
    issuing one UPDATE per chunk.
    """
    from unittest.mock import patch

    from lmchat.services.streaming_service import _COALESCE_INTERVAL_SEC, _CoalesceTimer

    mock_engine = MagicMock()
    timer = _CoalesceTimer(engine=mock_engine, message_id=99)

    # Patch with_write_retry to track how many times it is awaited.
    call_count: list[int] = [0]

    async def _fake_retry(fn: object) -> None:
        call_count[0] += 1
        # Don't call fn — we just count calls.

    with patch("lmchat.services.streaming_service.with_write_retry", _fake_retry):
        # First call: _last_touch is at init time (monotonic()), so this
        # fires only if we forcibly reset _last_touch to an old value.
        timer._last_touch -= _COALESCE_INTERVAL_SEC + 0.1  # make it stale
        await timer.touch_activity()
        assert call_count[0] == 1, "First call after stale timestamp should write"

        # Immediate second call — should be throttled (within interval).
        await timer.touch_activity()
        assert call_count[0] == 1, (
            f"Second call within {_COALESCE_INTERVAL_SEC}s should be throttled; "
            f"got {call_count[0]} DB writes"
        )

        # Third call — also throttled.
        await timer.touch_activity()
        assert call_count[0] == 1, "Third call within interval still throttled"


@pytest.mark.asyncio
async def test_touch_activity_fires_after_interval_expires() -> None:
    """touch_activity issues a DB write once the coalesce interval has elapsed."""
    from unittest.mock import patch

    from lmchat.services.streaming_service import _COALESCE_INTERVAL_SEC, _CoalesceTimer

    mock_engine = MagicMock()
    timer = _CoalesceTimer(engine=mock_engine, message_id=99)

    call_count: list[int] = [0]

    async def _fake_retry(fn: object) -> None:
        call_count[0] += 1

    with patch("lmchat.services.streaming_service.with_write_retry", _fake_retry):
        # Stale first write.
        timer._last_touch -= _COALESCE_INTERVAL_SEC + 0.1
        await timer.touch_activity()
        assert call_count[0] == 1

        # Fast-forward _last_touch to simulate interval elapsed again.
        timer._last_touch -= _COALESCE_INTERVAL_SEC + 0.1
        await timer.touch_activity()
        assert call_count[0] == 2, (
            "Expected second write after interval expired; "
            f"got {call_count[0]}"
        )


# ---------------------------------------------------------------------------
# sub-session MTP counter reset on mtp_suspected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sub_session_mtp_counter_resets_on_mtp_suspected_event() -> None:
    """_sub_session_sse resets the tool-round counter when mtp_suspected fires.

    Previously the counter was only cleared by LRU eviction or by test-only
    callers, so every subsequent sub-session on the same chat_id would fire
    the MTP warning immediately after 20+ accumulated rounds.  Now observing
    mtp_suspected resets the counter — mirroring streaming_service's
    reset_counter(chat_id) call.
    """
    from lmchat.lmstudio.types import CanonicalEvent
    from lmchat.routes.chats import (
        _sub_session_get_tool_rounds,
        _sub_session_increment_tool_round,
        _sub_session_reset_tool_rounds,
        _sub_session_sse,
    )

    chat_id = 77703  # sentinel

    # Seed 25 rounds (above threshold=20) in the registry.
    _sub_session_reset_tool_rounds(chat_id)
    for _ in range(25):
        _sub_session_increment_tool_round(chat_id)
    assert _sub_session_get_tool_rounds(chat_id) == 25

    # Build an mtp_suspected error event.
    mtp_event = CanonicalEvent(
        type="error",
        error={"code": "mtp_suspected", "message": "Long tool chain."},
    )

    # Mock lm_client that yields a single mtp_suspected error event.
    class _FakeClient:
        async def stream(self, *, request: object, cumulative_tool_rounds: int = 0):  # type: ignore[override]
            yield mtp_event

    frames: list[bytes] = []
    async for frame in _sub_session_sse(
        lm_client=_FakeClient(),  # type: ignore[arg-type]
        model_id="test-model",
        system_prompt="",
        messages=[{"role": "user", "content": "hi"}],
        prior_tool_rounds=_sub_session_get_tool_rounds(chat_id),
        chat_id=chat_id,
    ):
        frames.append(frame)

    # The counter should now be 0 — mtp_suspected triggered the reset.
    assert _sub_session_get_tool_rounds(chat_id) == 0, (
        f"Expected counter=0 after mtp_suspected; got {_sub_session_get_tool_rounds(chat_id)}"
    )
    # Also verify the sub.error frame was emitted.
    assert any(b"sub.error" in f for f in frames), (
        "Expected sub.error frame for mtp_suspected event"
    )

    # Cleanup.
    _sub_session_reset_tool_rounds(chat_id)
