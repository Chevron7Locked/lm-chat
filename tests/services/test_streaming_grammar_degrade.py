# SPDX-License-Identifier: Apache-2.0
"""Tests for the grammar-parse robustness degrade in StreamingService.

REAL-PATH NOTE (verified live 2026-07-04): on LM Studio's native chain surface
a bad tool schema does NOT arrive as a yielded ``error`` event.  The upstream
yields only ``chat.start`` then exhausts; the exhausted-without-terminal branch
then runs ``self._lm_client.probe_for_error(...)`` (a non-streaming re-issue)
whose returned string carries "Failed to initialize samplers: failed to parse
grammar".  These tests therefore mock the upstream to yield ``[chat.start]``
then exhaust, and mock ``probe_for_error`` to return the grammar message.

Covers:
1. Exhausted + integrations + grammar probe → warning emitted (naming active
   integrations) AND a tool-less retry runs and produces a normal answer.
2. Exhausted + NO integrations + grammar probe → surfaced as an error (nothing
   to strip, no retry).
3. Grammar error AFTER content already streamed → surfaced (no retry).
4. Retry also exhausts/errors → the second error is surfaced (degrade-once).
5. A non-grammar probe (e.g. context-size error) → normal error, no retry.
6. _is_grammar_parse_error helper unit tests.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
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
    _is_grammar_parse_error,
)

_GRAMMAR_MSG = (
    "Engine protocol predict request returned 400: "
    "Failed to initialize samplers: failed to parse grammar"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_engine() -> AsyncEngine:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return engine


async def _insert_chat(engine: AsyncEngine, settings: dict | None = None) -> int:  # type: ignore[type-arg]
    async with engine.begin() as conn:
        result = await conn.execute(
            chats.insert().values(user_id=1, title="test", settings=settings or {})
        )
        return result.inserted_primary_key[0]  # type: ignore[index]


def _make_payload(
    model: str = "test-model",
    chat_text: str = "search for something",
    *,
    integrations: list[str] | None = None,
) -> ChatStreamRequest:
    req = CanonicalChatRequest(
        model=model,
        input=[CanonicalInputBlock(type="text", content=chat_text)],
    )
    if integrations is not None:
        req = req.model_copy(update={"integrations": integrations})
    return ChatStreamRequest(chat_id=1, payload=req)


def _chat_start_then_exhaust() -> list[CanonicalEvent]:
    """LM Studio's collapse: only chat.start, then the iterator exhausts."""
    return [CanonicalEvent(type="chat.start")]


def _happy_events(response_id: str = "rid-retry") -> list[CanonicalEvent]:
    return [
        CanonicalEvent(type="chat.start"),
        CanonicalEvent(type="message.start"),
        CanonicalEvent(type="message.delta", content="answer without tools"),
        CanonicalEvent(type="message.end"),
        CanonicalEvent(type="chat.end", response_id=response_id),
    ]


def _mock_user(user_id: int = 1) -> MagicMock:
    u = MagicMock()
    u.id = user_id
    return u


def _mock_request() -> AsyncMock:
    from tests.services.conftest import make_disconnect_receive

    req = AsyncMock()
    req.receive = make_disconnect_receive(disconnected=False)
    return req


def _make_models_service(wire_id: str = "test-model") -> AsyncMock:
    svc = AsyncMock()
    svc.auth_failed = False
    res = MagicMock()
    res.wire_id = wire_id
    res.substituted = False
    svc.resolve_to_loaded_or_fallback = AsyncMock(return_value=res)
    svc.get_capabilities = AsyncMock(side_effect=KeyError(wire_id))
    svc.get_max_context_length = AsyncMock(return_value=0)
    return svc


async def _drain(gen: AsyncIterator[bytes]) -> list[bytes]:
    frames: list[bytes] = []
    async for frame in gen:
        frames.append(frame)
    return frames


def _parse_frames(frames: list[bytes]) -> list[dict]:  # type: ignore[type-arg]
    results = []
    for frame in frames:
        for line in frame.decode("utf-8").splitlines():
            if line.startswith("data:"):
                results.append(json.loads(line[5:].strip()))
    return results


def _make_lm_client(
    *,
    attempt_events: list[list[CanonicalEvent]],
    probe_returns: list[str | None],
) -> tuple[MagicMock, list[int]]:
    """Build an lm_client whose stream()/probe_for_error() are attempt-indexed.

    ``attempt_events[i]`` is the list of events the i-th ``stream()`` call
    yields (then exhausts).  ``probe_returns[i]`` is what the i-th
    ``probe_for_error()`` returns.  A shared counter list is returned so tests
    can assert the number of stream attempts.

    Args:
        attempt_events: Per-attempt event lists.
        probe_returns:  Per-probe return strings.

    Returns:
        (lm_client mock, stream_call_counter list).
    """
    stream_calls: list[int] = [0]
    probe_calls: list[int] = [0]

    def _stream(*, request: CanonicalChatRequest, **kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        idx = stream_calls[0]
        stream_calls[0] += 1
        events = attempt_events[idx] if idx < len(attempt_events) else []

        async def _gen() -> AsyncIterator[CanonicalEvent]:
            for ev in events:
                yield ev

        return _gen()

    async def _probe(req: CanonicalChatRequest) -> str | None:
        idx = probe_calls[0]
        probe_calls[0] += 1
        return probe_returns[idx] if idx < len(probe_returns) else None

    lm_client = MagicMock()
    lm_client.stream = _stream
    lm_client.probe_for_error = _probe
    return lm_client, stream_calls


def _make_service(engine: AsyncEngine, lm_client: MagicMock) -> StreamingService:
    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)
    return StreamingService(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        chat_locks={},
        models_service=_make_models_service(),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = await _make_engine()
    yield eng
    await eng.dispose()


# ---------------------------------------------------------------------------
# _is_grammar_parse_error helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message,expected",
    [
        (_GRAMMAR_MSG, True),
        ("failed to parse grammar for tool firecrawl_scrape", True),
        ("Failed to Initialize Samplers: bad schema", True),
        ("FAILED TO PARSE GRAMMAR", True),  # case-insensitive
        ("Connection refused", False),
        ("Model not found", False),
        ("exceed_context_size_error: too many tokens", False),
        ("", False),
        ("400 invalid_request_error", False),
    ],
)
def test_is_grammar_parse_error_patterns(message: str, expected: bool) -> None:
    """_is_grammar_parse_error returns True only for grammar/sampler patterns."""
    assert _is_grammar_parse_error(message) == expected


# ---------------------------------------------------------------------------
# Test 1: exhausted + grammar probe + integrations → warning + tool-less retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grammar_degrade_probe_path_warns_and_retries(engine: AsyncEngine) -> None:
    """The REAL firecrawl path: chat.start → exhaust → grammar probe → degrade.

    Attempt 1 upstream: yields [chat.start] then exhausts (no error event).
    probe_for_error (attempt 1) returns the grammar message.
    Attempt 2 upstream (tool-less retry): yields a normal answer.

    Asserts:
    - A warning frame is emitted naming the active integrations.
    - The retry answer's chat.end is present.
    - No error frame reaches the client.
    - Exactly 2 stream attempts (original + one retry).
    - The retry request had integrations stripped.
    """
    chat_id = await _insert_chat(engine)

    lm_client, stream_calls = _make_lm_client(
        attempt_events=[_chat_start_then_exhaust(), _happy_events()],
        probe_returns=[_GRAMMAR_MSG],
    )

    # Capture each stream request's integrations by wrapping stream.
    captured_reqs: list[CanonicalChatRequest] = []
    _orig_stream = lm_client.stream

    def _capturing_stream(*, request: CanonicalChatRequest, **kwargs: Any) -> Any:
        captured_reqs.append(request)
        return _orig_stream(request=request, **kwargs)

    lm_client.stream = _capturing_stream

    svc = _make_service(engine, lm_client)
    payload = _make_payload(integrations=["mcp/searxng", "mcp/firecrawl"])

    frames = await _drain(
        svc.stream_chat(
            chat_id=chat_id,
            user=_mock_user(1),
            payload=payload,
            request=_mock_request(),
        )
    )

    parsed = _parse_frames(frames)
    types = [d.get("type") for d in parsed]

    warning_frames = [d for d in parsed if d.get("type") == "warning"]
    assert warning_frames, f"Expected a warning frame, got: {types}"

    warning_msg = str(warning_frames[0].get("warning", {}).get("message", ""))
    assert warning_frames[0].get("warning", {}).get("code") == "tool_schema_parse_failed", (
        f"Warning code must be tool_schema_parse_failed. Got: {warning_frames[0]}"
    )
    assert "mcp/searxng" in warning_msg and "mcp/firecrawl" in warning_msg, (
        f"Warning must name active integrations. Got: {warning_msg!r}"
    )

    assert any(d.get("type") == "chat.end" for d in parsed), (
        f"Expected chat.end from retry, got: {types}"
    )
    # The retry's answer delta must have been forwarded.
    delta_texts = [d.get("content") for d in parsed if d.get("type") == "message.delta"]
    assert "answer without tools" in delta_texts, (
        f"Retry answer must be forwarded, got deltas: {delta_texts}"
    )

    assert not any(d.get("type") == "error" for d in parsed), (
        f"No error frame must reach the client, got: {types}"
    )

    assert stream_calls[0] == 2, f"Expected 2 stream attempts, got {stream_calls[0]}"

    # The retry request (2nd stream call) must have integrations stripped.
    assert len(captured_reqs) == 2, f"Expected 2 captured requests, got {len(captured_reqs)}"
    assert not captured_reqs[1].integrations, (
        f"Retry must strip integrations, got {captured_reqs[1].integrations!r}"
    )
    # And store must not be False on the retry (tool-less).
    assert captured_reqs[1].store is not False, (
        f"Retry store must not be False, got {captured_reqs[1].store!r}"
    )


# ---------------------------------------------------------------------------
# Test 2: exhausted + grammar probe but NO integrations → error, no retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grammar_degrade_no_integrations_no_retry(engine: AsyncEngine) -> None:
    """Grammar probe with no integrations → surfaced as error (nothing to strip)."""
    chat_id = await _insert_chat(engine)

    lm_client, stream_calls = _make_lm_client(
        attempt_events=[_chat_start_then_exhaust()],
        probe_returns=[_GRAMMAR_MSG],
    )
    svc = _make_service(engine, lm_client)
    payload = _make_payload(integrations=[])

    frames = await _drain(
        svc.stream_chat(
            chat_id=chat_id,
            user=_mock_user(1),
            payload=payload,
            request=_mock_request(),
        )
    )

    parsed = _parse_frames(frames)
    types = [d.get("type") for d in parsed]

    assert any(d.get("type") == "error" for d in parsed), (
        f"Error must be forwarded when no integrations, got: {types}"
    )
    assert not any(d.get("type") == "warning" for d in parsed), (
        f"No warning when no integrations, got: {types}"
    )
    assert stream_calls[0] == 1, f"Expected 1 stream attempt (no retry), got {stream_calls[0]}"


# ---------------------------------------------------------------------------
# Test 3: grammar (as yielded error) AFTER content streamed → surfaced, no retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grammar_degrade_error_after_content_no_retry(engine: AsyncEngine) -> None:
    """A grammar error arriving after content has streamed is NOT retried.

    This exercises the belt-and-suspenders yielded-error path: content flows
    first (latching _content_emitted), then a grammar error event arrives. The
    degrade must NOT fire — the error is surfaced.
    """
    chat_id = await _insert_chat(engine)

    def _events_content_then_grammar_error() -> list[CanonicalEvent]:
        return [
            CanonicalEvent(type="chat.start"),
            CanonicalEvent(type="message.start"),
            CanonicalEvent(type="message.delta", content="partial answer"),
            CanonicalEvent(
                type="error",
                error={"code": "upstream_error", "message": _GRAMMAR_MSG},
            ),
        ]

    lm_client, stream_calls = _make_lm_client(
        attempt_events=[_events_content_then_grammar_error()],
        probe_returns=[],
    )
    svc = _make_service(engine, lm_client)
    payload = _make_payload(integrations=["mcp/searxng"])

    frames = await _drain(
        svc.stream_chat(
            chat_id=chat_id,
            user=_mock_user(1),
            payload=payload,
            request=_mock_request(),
        )
    )

    parsed = _parse_frames(frames)
    types = [d.get("type") for d in parsed]

    assert any(d.get("type") == "error" for d in parsed), (
        f"Error must be forwarded when content already streamed, got: {types}"
    )
    assert stream_calls[0] == 1, (
        f"Expected 1 stream attempt (no retry after content), got {stream_calls[0]}"
    )
    delta_texts = [d.get("content") for d in parsed if d.get("type") == "message.delta"]
    assert "partial answer" in delta_texts, (
        f"Partial content must be forwarded, got deltas: {delta_texts}"
    )


# ---------------------------------------------------------------------------
# Test 4: retry also exhausts/errors → second error surfaced (degrade-once)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grammar_degrade_retry_also_fails(engine: AsyncEngine) -> None:
    """Degrade-once: if the tool-less retry also fails, the error is surfaced.

    Attempt 1: chat.start → exhaust; probe → grammar (degrade fires, warning).
    Attempt 2 (retry): chat.start → exhaust; probe → a non-grammar error.

    The second error must reach the client; no third stream attempt is made.
    """
    chat_id = await _insert_chat(engine)

    lm_client, stream_calls = _make_lm_client(
        attempt_events=[_chat_start_then_exhaust(), _chat_start_then_exhaust()],
        probe_returns=[_GRAMMAR_MSG, "Model not responding"],
    )
    svc = _make_service(engine, lm_client)
    payload = _make_payload(integrations=["mcp/searxng"])

    frames = await _drain(
        svc.stream_chat(
            chat_id=chat_id,
            user=_mock_user(1),
            payload=payload,
            request=_mock_request(),
        )
    )

    parsed = _parse_frames(frames)
    types = [d.get("type") for d in parsed]

    assert any(d.get("type") == "warning" for d in parsed), (
        f"Warning must be emitted on first grammar degrade, got: {types}"
    )
    error_frames = [d for d in parsed if d.get("type") == "error"]
    assert error_frames, f"Retry error must be forwarded, got: {types}"
    # The surfaced error is the retry's probed detail.
    err_msg = str(error_frames[-1].get("error", {}).get("message", ""))
    assert "Model not responding" in err_msg, (
        f"Second error's message must surface, got: {err_msg!r}"
    )
    assert stream_calls[0] == 2, (
        f"Expected exactly 2 stream attempts (degrade-once), got {stream_calls[0]}"
    )


@pytest.mark.asyncio
async def test_grammar_degrade_retry_also_grammar_no_third_attempt(
    engine: AsyncEngine,
) -> None:
    """Even if the retry probe ALSO returns a grammar message, no 3rd attempt.

    _grammar_degraded latches after the first retry, so a second grammar probe
    is surfaced as an error rather than triggering another retry.
    """
    chat_id = await _insert_chat(engine)

    lm_client, stream_calls = _make_lm_client(
        attempt_events=[_chat_start_then_exhaust(), _chat_start_then_exhaust()],
        probe_returns=[_GRAMMAR_MSG, _GRAMMAR_MSG],
    )
    svc = _make_service(engine, lm_client)
    payload = _make_payload(integrations=["mcp/searxng"])

    frames = await _drain(
        svc.stream_chat(
            chat_id=chat_id,
            user=_mock_user(1),
            payload=payload,
            request=_mock_request(),
        )
    )

    parsed = _parse_frames(frames)
    assert any(d.get("type") == "error" for d in parsed), "Second grammar must surface as error"
    # Exactly one warning (the first degrade), and exactly two stream attempts.
    warnings = [d for d in parsed if d.get("type") == "warning"]
    assert len(warnings) == 1, f"Only ONE degrade warning expected, got {len(warnings)}"
    assert stream_calls[0] == 2, f"No third attempt; got {stream_calls[0]} attempts"


# ---------------------------------------------------------------------------
# Test 5: non-grammar probe → normal error, no retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grammar_degrade_non_grammar_probe_no_retry(engine: AsyncEngine) -> None:
    """A non-grammar probe (e.g. context-size error) is surfaced normally.

    The degrade gate must NOT fire for a non-grammar message even with
    integrations present.
    """
    chat_id = await _insert_chat(engine)

    lm_client, stream_calls = _make_lm_client(
        attempt_events=[_chat_start_then_exhaust()],
        probe_returns=["exceed_context_size_error: too many integrations"],
    )
    svc = _make_service(engine, lm_client)
    payload = _make_payload(integrations=["mcp/searxng", "mcp/context7"])

    frames = await _drain(
        svc.stream_chat(
            chat_id=chat_id,
            user=_mock_user(1),
            payload=payload,
            request=_mock_request(),
        )
    )

    parsed = _parse_frames(frames)
    types = [d.get("type") for d in parsed]

    error_frames = [d for d in parsed if d.get("type") == "error"]
    assert error_frames, f"Non-grammar error must be forwarded, got: {types}"
    err_msg = str(error_frames[-1].get("error", {}).get("message", ""))
    assert "exceed_context_size_error" in err_msg, (
        f"Non-grammar probe detail must surface, got: {err_msg!r}"
    )
    assert not any(d.get("type") == "warning" for d in parsed), (
        f"No warning for non-grammar error, got: {types}"
    )
    assert stream_calls[0] == 1, (
        f"Expected 1 stream attempt for non-grammar error, got {stream_calls[0]}"
    )



# ---------------------------------------------------------------------------
# streaming-2: shared grammar-degrade decision + warning helpers (used by BOTH
# the main pump and the sub-session pump — extract-the-decision, keep the retry
# mechanics/emission per-caller).
# ---------------------------------------------------------------------------


def test_grammar_degrade_eligible_truth_table() -> None:
    """The shared eligibility rule is True ONLY when it's a grammar-parse error
    AND native path AND integrations present AND no content emitted AND not
    already degraded. Each condition individually blocks it.

    RED-ON-REVERT: inline either pump's decision back to a hand-rolled boolean
    and this stops guarding the shared rule.
    """
    from lmchat.services.streaming_service import _grammar_degrade_eligible

    grammar = "failed to parse grammar for tool firecrawl_scrape"
    base = {
        "is_native_path": True,
        "has_integrations": True,
        "content_emitted": False,
        "already_degraded": False,
        "error_detail": grammar,
    }
    assert _grammar_degrade_eligible(**base) is True  # type: ignore[arg-type]
    assert _grammar_degrade_eligible(**{**base, "is_native_path": False}) is False  # type: ignore[arg-type]
    assert _grammar_degrade_eligible(**{**base, "has_integrations": False}) is False  # type: ignore[arg-type]
    assert _grammar_degrade_eligible(**{**base, "content_emitted": True}) is False  # type: ignore[arg-type]
    assert _grammar_degrade_eligible(**{**base, "already_degraded": True}) is False  # type: ignore[arg-type]
    # Non-grammar error is never eligible even with everything else satisfied.
    assert (
        _grammar_degrade_eligible(**{**base, "error_detail": "Connection refused"})  # type: ignore[arg-type]
        is False
    )


def test_grammar_degrade_warning_message() -> None:
    """The shared warning text names the active integrations (both pumps frame
    it differently, but the message is identical)."""
    from lmchat.services.streaming_service import _grammar_degrade_warning

    msg = _grammar_degrade_warning(["mcp/searxng", "mcp/context7"])
    assert "bad tool schema" in msg
    assert "Retrying without tools" in msg
    assert "mcp/searxng, mcp/context7" in msg
    assert _grammar_degrade_warning([]).endswith("Active integrations were: .")
