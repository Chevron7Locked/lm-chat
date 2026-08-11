# SPDX-License-Identifier: Apache-2.0
"""JSON Schema contract tests for the sub.error SSE envelope.

Item 6 (BE half) of the Week-1 test architecture plan (2026-06-08).

The schema is defined at web/src/types/sub-error-schema.json (SSOT shared
with FE vitest).  This file:

  1. Imports validate_sub_error from tests.contracts.sse_envelope_schemas.
  2. Provides a parametrized regression test that drives _sub_session_sse
     through every sub.error emission path and validates each payload against
     the JSON Schema.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, Literal
from unittest.mock import MagicMock

import httpx
import pytest

from lmchat.lmstudio.types import CanonicalEvent, CanonicalToolCall
from lmchat.routes.chats import _sub_session_sse
from lmchat.services.lmstudio_streaming_client import (
    LmstudioStreamingClient,
    StreamingClientUpstreamError,
)
from tests.contracts.sse_envelope_schemas import validate_sub_error

# ---------------------------------------------------------------------------
# Helpers (mirrors test_sub_session_streaming.py pattern)
# ---------------------------------------------------------------------------

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
    "error",
]


def _event(type_: _EventType, **kwargs: Any) -> CanonicalEvent:
    return CanonicalEvent(type=type_, **kwargs)


async def _from_events(events: list[CanonicalEvent]) -> AsyncIterator[CanonicalEvent]:
    for ev in events:
        yield ev


def _make_lm_client(events: list[CanonicalEvent]) -> LmstudioStreamingClient:
    adapter = MagicMock()
    adapter.stream_chat = MagicMock(return_value=_from_events(events))
    return LmstudioStreamingClient(adapter=adapter)


def _make_raising_lm_client(exc: Exception) -> LmstudioStreamingClient:
    """Client whose stream() raises immediately."""
    async def _raiser(*a: Any, **kw: Any) -> AsyncIterator[CanonicalEvent]:
        raise exc
        yield  # make it a generator  # type: ignore[misc]

    adapter = MagicMock()
    adapter.stream_chat = MagicMock(return_value=_raiser())
    return LmstudioStreamingClient(adapter=adapter)


def _parse_sub_error_payloads(blob: bytes) -> list[dict[str, Any]]:
    """Extract all sub.error data dicts from a concatenated SSE stream."""
    payloads: list[dict[str, Any]] = []
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
        if name == "sub.error" and data_text:
            payloads.append(json.loads(data_text))
    return payloads


async def _collect_sub_errors(lm_client: LmstudioStreamingClient) -> list[dict[str, Any]]:
    chunks: list[bytes] = []
    async for chunk in _sub_session_sse(
        lm_client=lm_client,
        model_id="test-model",
        system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}],
    ):
        chunks.append(chunk)
    return _parse_sub_error_payloads(b"".join(chunks))


# ---------------------------------------------------------------------------
# Parametrized regression test
#
# Each tuple: (scenario_id, lm_client)
# The test drives _sub_session_sse, collects all sub.error frames, and
# validates EVERY one against the JSON Schema.
# ---------------------------------------------------------------------------


def _make_scenarios() -> list[tuple[str, LmstudioStreamingClient]]:
    """Return (id, client) pairs covering every sub.error emission path."""
    return [
        # Path 1: inline error event from the model stream
        (
            "inline_error_event",
            _make_lm_client([
                _event("chat.start"),
                _event("message.delta", content="partial"),
                _event("error", error={"code": "context_window_exceeded", "message": "too long"}),
            ]),
        ),
        # Path 2: tool calls started but no final content (no_final_content code)
        (
            "tool_calls_no_final_content",
            _make_lm_client([
                _event("chat.start"),
                _event("tool_call.start", tool_call=CanonicalToolCall(
                    id="tc-1", name="search", arguments={}
                )),
                _event("tool_call.success"),
                _event("chat.end"),
            ]),
        ),
        # Path 3: StreamingClientUpstreamError (upstream_error code path)
        (
            "upstream_error",
            _make_raising_lm_client(
                StreamingClientUpstreamError(
                    CanonicalEvent(
                        type="error",
                        error={"code": "upstream_error", "message": "connection dropped"},
                    )
                )
            ),
        ),
        # Path 4: httpx.ReadTimeout (upstream_connection_lost code path)
        (
            "connection_lost",
            _make_raising_lm_client(httpx.ReadTimeout("timed out")),
        ),
        # Path 5: unexpected exception (stream_error code path)
        (
            "unexpected_exception",
            _make_raising_lm_client(RuntimeError("boom")),
        ),
    ]


_SCENARIOS = _make_scenarios()


@pytest.mark.parametrize("scenario_id,lm_client", _SCENARIOS, ids=[s[0] for s in _SCENARIOS])
@pytest.mark.asyncio
async def test_sub_error_payload_conforms_to_schema(
    scenario_id: str,
    lm_client: LmstudioStreamingClient,
) -> None:
    """Every sub.error payload emitted by _sub_session_sse matches the JSON Schema.

    The schema requires non-empty code and message; optional tally, hint,
    accumulated_chars, truncated fields are allowed but not required.

    Scenarios covered:
      - inline_error_event: model emits an error event mid-stream
      - tool_calls_no_final_content: model completes tool calls with no answer
      - upstream_error: StreamingClientUpstreamError from adapter
      - connection_lost: httpx.HTTPError from adapter
      - unexpected_exception: arbitrary RuntimeError from adapter
    """
    payloads = await _collect_sub_errors(lm_client)

    # Every scenario MUST emit at least one sub.error to be a useful regression.
    assert len(payloads) >= 1, (
        f"Scenario {scenario_id!r} emitted no sub.error frames — "
        "either the scenario setup is wrong or the code path changed"
    )

    for payload in payloads:
        # Raises jsonschema.ValidationError on failure — pytest surfaces the diff.
        validate_sub_error(payload)
