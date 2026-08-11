# SPDX-License-Identifier: Apache-2.0
"""Tests for the sub-session SSE bridge (chats.py:_sub_session_sse).

PR-E closes ISSUE-13: the raw httpx block in chats.py was replaced with a
call to ``LmstudioStreamingClient.stream()`` so reasoning_content events
reach the UI and upstream errors land in the canonical shape.

Tests assert that:
- ``message.delta`` events translate to ``sub.delta`` SSE frames.
- ``reasoning.{start,delta,end}`` events translate to ``sub.reasoning.*``
  SSE frames (previously dropped).
- A mid-stream ``error`` event translates to a ``sub.error`` SSE frame
  with the canonical ``code``/``message`` payload.
- The stream emits ``sub.complete`` with the accumulated final content
  when ``chat.end`` arrives.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, Literal
from unittest.mock import MagicMock

import pytest

from lmchat.lmstudio.types import CanonicalEvent, CanonicalToolCall
from lmchat.routes.chats import _sub_session_sse
from lmchat.services.lmstudio_streaming_client import LmstudioStreamingClient

# Subset of canonical event types this test exercises — keeps pyright happy.
_EventType = Literal[
    "chat.start",
    "chat.end",
    "message.start",
    "message.delta",
    "message.end",
    "reasoning.start",
    "reasoning.delta",
    "reasoning.end",
    "tool_call.start",
    "tool_call.success",
    "tool_call.failure",
    "error",
]


def _event(type_: _EventType, **kwargs: Any) -> CanonicalEvent:
    """Build a CanonicalEvent of the given type."""
    return CanonicalEvent(type=type_, **kwargs)


async def _from_events(events: list[CanonicalEvent]) -> AsyncIterator[CanonicalEvent]:
    """Async-yield the given events."""
    for ev in events:
        yield ev


def _make_lm_client(events: list[CanonicalEvent]) -> LmstudioStreamingClient:
    """Build an LmstudioStreamingClient backed by a fake adapter."""
    adapter = MagicMock()
    adapter.stream_chat = MagicMock(return_value=_from_events(events))
    return LmstudioStreamingClient(adapter=adapter)


def _parse_sse_frames(blob: bytes) -> list[tuple[str, dict[str, Any]]]:
    """Parse a concatenated SSE stream into (event_name, data_dict) tuples."""
    out: list[tuple[str, dict[str, Any]]] = []
    for frame in blob.split(b"\n\n"):
        if not frame.strip():
            continue
        name: str | None = None
        data_text: str | None = None
        for line in frame.splitlines():
            if line.startswith(b"event: "):
                name = line[len(b"event: "):].decode()
            elif line.startswith(b"data: "):
                data_text = line[len(b"data: "):].decode()
        if name is None or data_text is None:
            continue
        out.append((name, json.loads(data_text)))
    return out


@pytest.mark.asyncio
async def test_sub_session_translates_delta_reasoning_and_error_in_order() -> None:
    """SSE bridge propagates delta + reasoning.* + a mid-stream error in order."""
    events = [
        _event("chat.start"),
        _event("message.start"),
        _event("reasoning.start"),
        _event("reasoning.delta", content="thinking..."),
        _event("reasoning.end"),
        _event("message.delta", content="Hello "),
        _event("message.delta", content="world"),
        _event(
            "error",
            error={"code": "context_window_exceeded", "message": "ctx overflow"},
        ),
    ]
    lm_client = _make_lm_client(events)

    chunks: list[bytes] = []
    async for chunk in _sub_session_sse(
        lm_client=lm_client,
        model_id="test-model",
        system_prompt="you are a tester",
        messages=[{"role": "user", "content": "hi"}],
    ):
        chunks.append(chunk)
    blob = b"".join(chunks)

    frames = _parse_sse_frames(blob)
    names = [n for n, _ in frames]

    # Fix #21: chat.start now emits sub.processing.start for FE liveness.
    # Reasoning frames + delta frames + a single sub.error terminator, in order.
    assert names == [
        "sub.processing.start",
        "sub.reasoning.start",
        "sub.reasoning.delta",
        "sub.reasoning.end",
        "sub.delta",
        "sub.delta",
        "sub.error",
    ]

    reasoning_delta = next(d for n, d in frames if n == "sub.reasoning.delta")
    assert reasoning_delta == {"delta": "thinking..."}

    deltas = [d["delta"] for n, d in frames if n == "sub.delta"]
    assert deltas == ["Hello ", "world"]

    err = next(d for n, d in frames if n == "sub.error")
    assert err == {"code": "context_window_exceeded", "message": "ctx overflow"}


@pytest.mark.asyncio
async def test_sub_session_emits_sub_complete_with_accumulated_content() -> None:
    """A clean chat.end terminator yields sub.complete with the joined deltas."""
    events = [
        _event("chat.start"),
        _event("message.delta", content="alpha"),
        _event("message.delta", content="-bravo"),
        _event("chat.end"),
    ]
    lm_client = _make_lm_client(events)

    chunks: list[bytes] = []
    async for chunk in _sub_session_sse(
        lm_client=lm_client,
        model_id="test-model",
        system_prompt="sys",
        messages=[{"role": "user", "content": "go"}],
    ):
        chunks.append(chunk)
    frames = _parse_sse_frames(b"".join(chunks))
    names = [n for n, _ in frames]

    # Fix #21: chat.start now emits sub.processing.start for FE liveness.
    assert names == ["sub.processing.start", "sub.delta", "sub.delta", "sub.complete"]
    complete = next(d for n, d in frames if n == "sub.complete")
    assert complete == {"final_content": "alpha-bravo"}


# ---------------------------------------------------------------------------
# Reasoning-only salvage
# ---------------------------------------------------------------------------

# Symptom: a /research turn where the model parks the answer in
# reasoning_content and never emits message.delta exited the sub-session as
# sub.complete with final_content="" — the user saw "thinking, then it
# stopped." The streaming_service main pump already applied substance_fold
# (§1.1); the sub-session route did not. These tests pin the parity.


def _long_reasoning(n_chars: int = 600) -> str:
    """Build a reasoning blob long enough to exceed substance_fold's
    STUB_CHARS = 240 threshold + the 2× base-length predicate."""
    return ("The answer is Paris. " * 40)[:n_chars]


@pytest.mark.asyncio
async def test_sub_session_salvages_reasoning_only_completion() -> None:
    """Empty content + long reasoning → sub.complete carries the folded text."""
    reasoning = _long_reasoning()
    events = [
        _event("chat.start"),
        _event("reasoning.start"),
        _event("reasoning.delta", content=reasoning),
        _event("reasoning.end"),
        _event("chat.end"),
    ]
    lm_client = _make_lm_client(events)

    chunks: list[bytes] = []
    async for chunk in _sub_session_sse(
        lm_client=lm_client,
        model_id="test-model",
        system_prompt="sys",
        messages=[{"role": "user", "content": "go"}],
    ):
        chunks.append(chunk)
    frames = _parse_sse_frames(b"".join(chunks))
    complete = next(d for n, d in frames if n == "sub.complete")
    # final_content carries the salvaged reasoning (substance_fold injects
    # its salvage prefix when base is empty).
    assert complete["final_content"], (
        "salvaged sub.complete must carry the reasoning as final_content"
    )
    assert "reasoning surfaced" in complete["final_content"], (
        "salvage prefix must be present so the FE can render it distinctly"
    )
    assert reasoning in complete["final_content"]


@pytest.mark.asyncio
async def test_sub_session_does_not_salvage_when_content_is_substantive() -> None:
    """Non-stub content with reasoning → reasoning stays in reasoning,
    final_content is unchanged. (substance_fold's no-fold path.)"""
    substantive = "A" * 300  # > STUB_CHARS=240
    events = [
        _event("chat.start"),
        _event("message.delta", content=substantive),
        _event("reasoning.start"),
        _event("reasoning.delta", content="thinking..."),
        _event("reasoning.end"),
        _event("chat.end"),
    ]
    lm_client = _make_lm_client(events)

    chunks: list[bytes] = []
    async for chunk in _sub_session_sse(
        lm_client=lm_client,
        model_id="test-model",
        system_prompt="sys",
        messages=[{"role": "user", "content": "go"}],
    ):
        chunks.append(chunk)
    frames = _parse_sse_frames(b"".join(chunks))
    complete = next(d for n, d in frames if n == "sub.complete")
    assert complete == {"final_content": substantive}
    # The reasoning is NOT spliced into final_content here — it stays in the
    # sub.reasoning.delta stream the FE already rendered.
    assert "reasoning surfaced" not in complete["final_content"]


@pytest.mark.asyncio
async def test_sub_session_exhausted_generator_also_salvages_reasoning() -> None:
    """Generator exhausts without chat.end + reasoning-only → fallback salvages
    the reasoning into final_content rather than emitting empty sub.complete."""
    reasoning = _long_reasoning()
    events = [
        _event("chat.start"),
        _event("reasoning.start"),
        _event("reasoning.delta", content=reasoning),
        # No reasoning.end, no chat.end — upstream cut.
    ]
    lm_client = _make_lm_client(events)

    chunks: list[bytes] = []
    async for chunk in _sub_session_sse(
        lm_client=lm_client,
        model_id="test-model",
        system_prompt="sys",
        messages=[{"role": "user", "content": "go"}],
    ):
        chunks.append(chunk)
    frames = _parse_sse_frames(b"".join(chunks))
    complete = next(d for n, d in frames if n == "sub.complete")
    assert reasoning in complete["final_content"], (
        "exhausted-stream fallback must still salvage reasoning into final_content"
    )


# ---------------------------------------------------------------------------
# Grammar-parse degrade (sub-session parity, 2026-07-04)
# ---------------------------------------------------------------------------
#
# REAL PATH: LM Studio rejects the tool turn (bad MCP schema, e.g. firecrawl)
# with a 400 "failed to parse grammar" that arrives as chat.start + a bare
# exhaust (NO chat.end, NO error event). The exhausted branch probes for the
# real error via probe_for_error and, on a grammar match with integrations,
# warns + retries the sub-session tool-less. These tests drive that path.

_GRAMMAR_MSG = (
    "Engine protocol predict request returned 400: "
    "Failed to initialize samplers: failed to parse grammar"
)


def _make_attempt_lm_client(
    *,
    attempt_events: list[list[CanonicalEvent]],
    probe_returns: list[str | None],
) -> tuple[LmstudioStreamingClient, list[int]]:
    """Build a client whose stream()/probe_for_error() are attempt-indexed.

    ``attempt_events[i]`` is what the i-th upstream stream yields (then
    exhausts). ``probe_returns[i]`` is what the i-th probe_for_error returns.
    Returns (client, stream_call_counter).
    """
    stream_calls: list[int] = [0]
    probe_calls: list[int] = [0]

    def _stream_chat(*args: Any, **kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        idx = stream_calls[0]
        stream_calls[0] += 1
        events = attempt_events[idx] if idx < len(attempt_events) else []
        return _from_events(events)

    adapter = MagicMock()
    adapter.stream_chat = MagicMock(side_effect=_stream_chat)
    client = LmstudioStreamingClient(adapter=adapter)

    async def _probe(req: Any) -> str | None:
        idx = probe_calls[0]
        probe_calls[0] += 1
        return probe_returns[idx] if idx < len(probe_returns) else None

    client.probe_for_error = _probe  # type: ignore[method-assign]
    return client, stream_calls


async def _drain_sub(client: LmstudioStreamingClient, **kwargs: Any) -> list[bytes]:
    chunks: list[bytes] = []
    async for chunk in _sub_session_sse(lm_client=client, **kwargs):
        chunks.append(chunk)
    return chunks


@pytest.mark.asyncio
async def test_sub_session_grammar_degrade_probe_warns_and_retries() -> None:
    """chat.start → exhaust → grammar probe + integrations → warn + tool-less retry.

    Attempt 1: [chat.start] then exhaust (empty death).  probe → grammar.
    Attempt 2 (tool-less): a normal answer with a tool_call + chat.end.

    Asserts: a sub.warning naming the integrations, then the retry's content
    flows (sub.delta), then sub.complete — and NO sub.error.
    """
    client, stream_calls = _make_attempt_lm_client(
        attempt_events=[
            [_event("chat.start")],  # attempt 1: empty death
            [
                _event("chat.start"),
                _event("message.delta", content="Paris is the capital."),
                _event("chat.end"),
            ],  # attempt 2 (tool-less): answers
        ],
        probe_returns=[_GRAMMAR_MSG],
    )

    chunks = await _drain_sub(
        client,
        model_id="test-model",
        system_prompt="research",
        messages=[{"role": "user", "content": "capital of France?"}],
        integrations=["mcp/firecrawl", "mcp/searxng", "mcp/sequential-thinking"],
    )
    frames = _parse_sse_frames(b"".join(chunks))
    names = [n for n, _ in frames]

    assert "sub.warning" in names, f"Expected sub.warning, got: {names}"
    warn = next(d for n, d in frames if n == "sub.warning")
    assert warn["code"] == "tool_schema_parse_failed"
    assert "mcp/firecrawl" in warn["message"] and "mcp/searxng" in warn["message"], (
        f"Warning must name active integrations. Got: {warn['message']!r}"
    )

    deltas = [d["delta"] for n, d in frames if n == "sub.delta"]
    assert "Paris is the capital." in deltas, f"Retry answer must flow, got: {names}"
    assert "sub.complete" in names, f"Retry must complete, got: {names}"
    assert not any(n == "sub.error" for n in names), f"No sub.error expected, got: {names}"
    assert stream_calls[0] == 2, f"Expected 2 stream attempts, got {stream_calls[0]}"


@pytest.mark.asyncio
async def test_sub_session_grammar_degrade_no_integrations_no_retry() -> None:
    """Grammar probe with NO integrations → sub.error, no retry (nothing to strip)."""
    client, stream_calls = _make_attempt_lm_client(
        attempt_events=[[_event("chat.start")]],
        probe_returns=[_GRAMMAR_MSG],
    )
    chunks = await _drain_sub(
        client,
        model_id="test-model",
        system_prompt="research",
        messages=[{"role": "user", "content": "hi"}],
        integrations=[],
    )
    frames = _parse_sse_frames(b"".join(chunks))
    names = [n for n, _ in frames]

    # With no integrations the degrade never fires. The probe isn't even called
    # (short-circuited), so the exhausted-salvage emits an (empty) sub.complete.
    assert "sub.warning" not in names, f"No warning without integrations, got: {names}"
    assert stream_calls[0] == 1, f"Expected 1 attempt (no retry), got {stream_calls[0]}"


@pytest.mark.asyncio
async def test_sub_session_non_grammar_exhaust_surfaces_error_not_silent() -> None:
    """A non-grammar probe on an empty exhaust → sub.error surfaced (not silent).

    Before this fix the empty-death exhaust emitted a silent empty sub.complete.
    Now, when a probe recovers a real (non-grammar) error, it is surfaced.
    """
    client, stream_calls = _make_attempt_lm_client(
        attempt_events=[[_event("chat.start")]],
        probe_returns=["exceed_context_size_error: too many integrations"],
    )
    chunks = await _drain_sub(
        client,
        model_id="test-model",
        system_prompt="research",
        messages=[{"role": "user", "content": "hi"}],
        integrations=["mcp/searxng"],
    )
    frames = _parse_sse_frames(b"".join(chunks))
    names = [n for n, _ in frames]

    assert "sub.error" in names, f"Non-grammar exhaust must surface sub.error, got: {names}"
    err = next(d for n, d in frames if n == "sub.error")
    assert "exceed_context_size_error" in err["message"], (
        f"Probed detail must surface, got: {err['message']!r}"
    )
    assert "sub.warning" not in names, f"No warning for non-grammar, got: {names}"
    assert stream_calls[0] == 1, f"No retry for non-grammar, got {stream_calls[0]}"


@pytest.mark.asyncio
async def test_sub_session_grammar_degrade_retry_also_fails_degrade_once() -> None:
    """Degrade-once: if the tool-less retry ALSO exhausts, the second probe's
    error is surfaced — no third attempt.

    Attempt 1: exhaust; probe → grammar (degrade fires, warning).
    Attempt 2 (retry): exhaust; probe → non-grammar error → surfaced.
    """
    client, stream_calls = _make_attempt_lm_client(
        attempt_events=[[_event("chat.start")], [_event("chat.start")]],
        probe_returns=[_GRAMMAR_MSG, "Model not responding"],
    )
    chunks = await _drain_sub(
        client,
        model_id="test-model",
        system_prompt="research",
        messages=[{"role": "user", "content": "hi"}],
        integrations=["mcp/searxng"],
    )
    frames = _parse_sse_frames(b"".join(chunks))
    names = [n for n, _ in frames]

    assert names.count("sub.warning") == 1, f"Exactly one degrade warning, got: {names}"
    assert "sub.error" in names, f"Retry failure must surface sub.error, got: {names}"
    err = next(d for n, d in frames if n == "sub.error")
    assert "Model not responding" in err["message"], (
        f"Second probe detail must surface, got: {err['message']!r}"
    )
    assert stream_calls[0] == 2, f"Degrade-once: exactly 2 attempts, got {stream_calls[0]}"


@pytest.mark.asyncio
async def test_sub_session_grammar_degrade_retry_also_grammar_no_third() -> None:
    """Even if the retry probe ALSO returns grammar, no 3rd attempt (latched)."""
    client, stream_calls = _make_attempt_lm_client(
        attempt_events=[[_event("chat.start")], [_event("chat.start")]],
        probe_returns=[_GRAMMAR_MSG, _GRAMMAR_MSG],
    )
    chunks = await _drain_sub(
        client,
        model_id="test-model",
        system_prompt="research",
        messages=[{"role": "user", "content": "hi"}],
        integrations=["mcp/searxng"],
    )
    frames = _parse_sse_frames(b"".join(chunks))
    names = [n for n, _ in frames]

    assert names.count("sub.warning") == 1, f"Only one degrade warning, got: {names}"
    assert "sub.error" in names, f"Second grammar must surface as error, got: {names}"
    assert stream_calls[0] == 2, f"No third attempt, got {stream_calls[0]}"


@pytest.mark.asyncio
async def test_sub_session_normal_tool_turn_unchanged_no_probe() -> None:
    """A normal tool turn (integrations + real chat.end) must NOT probe or degrade.

    Guards that the degrade is inert for healthy tool turns — probe_for_error is
    never called and there is no sub.warning.
    """
    probe_called: list[int] = [0]

    events = [
        _event("chat.start"),
        _event("message.delta", content="answer with tools"),
        _event("chat.end"),
    ]
    adapter = MagicMock()
    adapter.stream_chat = MagicMock(return_value=_from_events(events))
    client = LmstudioStreamingClient(adapter=adapter)

    async def _probe(req: Any) -> str | None:
        probe_called[0] += 1
        return None

    client.probe_for_error = _probe  # type: ignore[method-assign]

    chunks = await _drain_sub(
        client,
        model_id="test-model",
        system_prompt="research",
        messages=[{"role": "user", "content": "hi"}],
        integrations=["mcp/searxng"],
    )
    frames = _parse_sse_frames(b"".join(chunks))
    names = [n for n, _ in frames]

    assert names == ["sub.processing.start", "sub.delta", "sub.complete"], (
        f"Normal tool turn must be unchanged, got: {names}"
    )
    assert probe_called[0] == 0, "probe_for_error must NOT be called on a healthy turn"


@pytest.mark.asyncio
async def test_sub_session_grammar_degrade_on_yielded_error_event() -> None:
    """Belt-and-suspenders: a grammar error arriving as an EVENT (pre-content)
    also degrades + retries tool-less."""
    client, stream_calls = _make_attempt_lm_client(
        attempt_events=[
            [
                _event("chat.start"),
                _event("error", error={"code": "upstream_error", "message": _GRAMMAR_MSG}),
            ],
            [
                _event("chat.start"),
                _event("message.delta", content="recovered answer"),
                _event("chat.end"),
            ],
        ],
        probe_returns=[],  # no probe needed on the event path
    )
    chunks = await _drain_sub(
        client,
        model_id="test-model",
        system_prompt="research",
        messages=[{"role": "user", "content": "hi"}],
        integrations=["mcp/firecrawl"],
    )
    frames = _parse_sse_frames(b"".join(chunks))
    names = [n for n, _ in frames]

    assert "sub.warning" in names, f"Yielded grammar error must warn, got: {names}"
    deltas = [d["delta"] for n, d in frames if n == "sub.delta"]
    assert "recovered answer" in deltas, f"Retry answer must flow, got: {names}"
    assert not any(n == "sub.error" for n in names), f"No sub.error on degrade, got: {names}"
    assert stream_calls[0] == 2, f"Expected 2 attempts, got {stream_calls[0]}"


# ---------------------------------------------------------------------------
# streaming-3: proactive per-turn tool-loop cap
# ---------------------------------------------------------------------------
#
# The main pump (streaming_service.py) caps runaway tool loops via
# _decide_loop_cut; the sub-session previously only counted rounds for
# diagnostics and never aborted. These tests pin the shared-policy cap.


def _tool_call_event(idx: int, *, success: bool = True) -> CanonicalEvent:
    """A standalone tool_call.success/.failure event (no start/name/arguments
    needed — LmstudioStreamingClient.stream forwards the event unmodified
    when the accumulator never saw a matching tool_call.start)."""
    tc = CanonicalToolCall(id=f"tc-{idx}", name="search_web", arguments={"q": str(idx)})
    return _event(
        "tool_call.success" if success else "tool_call.failure",
        tool_call=tc,
    )


@pytest.mark.asyncio
async def test_sub_session_tool_loop_cap_cuts_and_stops_consuming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the shared cap monkeypatched to 2, a 3rd successful tool round
    trips ``_decide_loop_cut`` — the SSE stream emits sub.warning
    (code=tool_loop_cap) then sub.complete, and the 4th round + the final
    chat.end (both later in the upstream event list) are NEVER consumed.

    RED-ON-REVERT: remove the streaming-3 cap block in chats.py and this
    test fails — all 4 rounds would flow through and the stream would end
    in the tool-turn-no-answer sub.error instead of sub.warning/sub.complete.
    """
    monkeypatch.setattr("lmchat.services.streaming_service._MAX_TOOL_ROUNDS_PER_TURN", 2)

    events = [
        _event("chat.start"),
        _tool_call_event(1),
        _tool_call_event(2),
        _tool_call_event(3),  # turn_tool_rounds becomes 3 > cap(2) -> cut here
        _tool_call_event(4),  # must NEVER be consumed
        _event("chat.end"),  # must NEVER be consumed
    ]
    lm_client = _make_lm_client(events)

    on_final_calls: list[tuple[str, str]] = []

    def _on_final(content: str, kind: str) -> None:
        on_final_calls.append((content, kind))

    chunks: list[bytes] = []
    async for chunk in _sub_session_sse(
        lm_client=lm_client,
        model_id="test-model",
        system_prompt="sys",
        messages=[{"role": "user", "content": "go"}],
        on_final=_on_final,
    ):
        chunks.append(chunk)
    frames = _parse_sse_frames(b"".join(chunks))
    names = [n for n, _ in frames]

    success_frames = [n for n in names if n == "sub.tool_call.success"]
    assert len(success_frames) == 3, (
        f"Expected exactly 3 tool_call.success frames (cut after the 3rd "
        f"round; the 4th must never be consumed), got: {names}"
    )
    assert "sub.warning" in names, f"Expected a tool_loop_cap warning, got: {names}"
    warn = next(d for n, d in frames if n == "sub.warning")
    assert warn["code"] == "tool_loop_cap"
    assert "3 rounds" in warn["message"], f"Warning must cite the round count, got: {warn!r}"

    assert names[-1] == "sub.complete", f"Must terminate with sub.complete, got: {names}"
    assert names.count("sub.complete") == 1
    assert not any(n == "sub.error" for n in names), (
        f"No sub.error expected on a clean cap cut, got: {names}"
    )

    # A pure tool-loop cap (no message content) resolves to a GRACEFUL
    # no-answer terminal — its content is a system hint, not a real answer,
    # so on_final must NOT fire (distilling the hint would pollute memory).
    # Mirrors the main pump, which never distills its tool_loop_cap terminal.
    # (streaming-3/4 panel finding, 122b P2.)
    assert on_final_calls == [], (
        f"graceful (no-answer) cap must not fire on_final, got: {on_final_calls}"
    )


@pytest.mark.asyncio
async def test_sub_session_tool_loop_cap_inert_when_under_cap() -> None:
    """Guard the guard: with the DEFAULT (high) cap, a couple of tool rounds
    followed by a normal chat.end must be completely unaffected — no
    sub.warning, both rounds' frames present, all events consumed."""
    events = [
        _event("chat.start"),
        _tool_call_event(1),
        _tool_call_event(2),
        _event("message.delta", content="the answer"),
        _event("chat.end"),
    ]
    lm_client = _make_lm_client(events)

    chunks: list[bytes] = []
    async for chunk in _sub_session_sse(
        lm_client=lm_client,
        model_id="test-model",
        system_prompt="sys",
        messages=[{"role": "user", "content": "go"}],
    ):
        chunks.append(chunk)
    frames = _parse_sse_frames(b"".join(chunks))
    names = [n for n, _ in frames]

    assert names.count("sub.tool_call.success") == 2
    assert "sub.warning" not in names, f"No cap warning expected under the cap, got: {names}"
    complete = next(d for n, d in frames if n == "sub.complete")
    assert complete == {"final_content": "the answer"}


@pytest.mark.asyncio
async def test_sub_session_tool_loop_cap_with_salvaged_answer_distills(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cap that HAS a salvaged answer (non-graceful) DOES fire on_final with
    kind="capped", so a real partial answer is still distilled — the graceful
    guard only suppresses no-answer hints.

    RED-ON-REVERT: drop the ``_cap_terminal.kind != "graceful"`` guard in
    chats.py and the graceful-cap test above flips (on_final would fire on the
    hint); break this terminal's ``_fire_on_final`` and this test fails.
    """
    monkeypatch.setattr("lmchat.services.streaming_service._MAX_TOOL_ROUNDS_PER_TURN", 2)

    events = [
        _event("chat.start"),
        _event("message.delta", content="Partial answer so far. "),
        _tool_call_event(1),
        _tool_call_event(2),
        _tool_call_event(3),  # turn_tool_rounds becomes 3 > cap(2) -> cut here
    ]
    lm_client = _make_lm_client(events)

    on_final_calls: list[tuple[str, str]] = []

    def _on_final(content: str, kind: str) -> None:
        on_final_calls.append((content, kind))

    chunks: list[bytes] = []
    async for chunk in _sub_session_sse(
        lm_client=lm_client,
        model_id="test-model",
        system_prompt="sys",
        messages=[{"role": "user", "content": "go"}],
        on_final=_on_final,
    ):
        chunks.append(chunk)
    frames = _parse_sse_frames(b"".join(chunks))
    names = [n for n, _ in frames]

    assert "sub.warning" in names, f"Expected a tool_loop_cap warning, got: {names}"
    assert names[-1] == "sub.complete", f"Must terminate with sub.complete, got: {names}"
    # Real accumulated content -> non-graceful terminal -> distillation fires.
    assert len(on_final_calls) == 1, (
        f"a salvaged-answer cap must fire on_final once, got: {on_final_calls}"
    )
    assert on_final_calls[0][1] == "capped"
    assert "Partial answer so far." in on_final_calls[0][0], (
        f"the salvaged partial answer must be the distilled content, got: {on_final_calls}"
    )


# ---------------------------------------------------------------------------
# streaming-4: structural on_final callback (replaces the sub.complete
# byte-scan formerly done by _sse_with_distill in sub_session_stream)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sub_session_on_final_fires_on_chat_end_complete() -> None:
    """on_final fires exactly once with (content, "complete") on a normal
    chat.end terminal.

    RED-ON-REVERT: remove the `_fire_on_final(_terminal.content, "complete")`
    call at the chat.end terminal in chats.py and this test fails (empty
    on_final_calls).
    """
    events = [
        _event("chat.start"),
        _event("message.delta", content="the answer"),
        _event("chat.end"),
    ]
    lm_client = _make_lm_client(events)

    calls: list[tuple[str, str]] = []

    def _on_final(content: str, kind: str) -> None:
        calls.append((content, kind))

    async for _ in _sub_session_sse(
        lm_client=lm_client,
        model_id="test-model",
        system_prompt="sys",
        messages=[{"role": "user", "content": "go"}],
        on_final=_on_final,
    ):
        pass

    assert calls == [("the answer", "complete")], (
        f"on_final must fire exactly once with the resolved content, got: {calls}"
    )


@pytest.mark.asyncio
async def test_sub_session_on_final_not_fired_on_sub_error() -> None:
    """on_final must NOT fire on a sub.error terminal (e.g. the tool-turn
    no_final_content graceful error) — mirrors the old byte-scan, which only
    ever matched b"event: sub.complete" and therefore never fired on
    sub.error either.
    """
    # resolve_terminal_content's graceful no_final_content path requires
    # had_tool_calls, which _sub_session_sse derives from the tool_call.start
    # tally — so a real tool_call.start must precede the terminating success.
    events = [
        _event("chat.start"),
        _event(
            "tool_call.start",
            tool_call=CanonicalToolCall(id="tc-1", name="search_web", arguments={}),
        ),
        _tool_call_event(1),
        _event("chat.end"),  # tools ran, no answer -> graceful no_final_content sub.error
    ]
    lm_client = _make_lm_client(events)

    calls: list[tuple[str, str]] = []

    def _on_final(content: str, kind: str) -> None:
        calls.append((content, kind))

    chunks: list[bytes] = []
    async for chunk in _sub_session_sse(
        lm_client=lm_client,
        model_id="test-model",
        system_prompt="sys",
        messages=[{"role": "user", "content": "go"}],
        on_final=_on_final,
    ):
        chunks.append(chunk)
    frames = _parse_sse_frames(b"".join(chunks))
    names = [n for n, _ in frames]

    assert "sub.error" in names, f"Expected the no_final_content sub.error terminal, got: {names}"
    err = next(d for n, d in frames if n == "sub.error")
    assert err["code"] == "no_final_content"
    assert calls == [], f"on_final must not fire on a sub.error terminal, got: {calls}"
