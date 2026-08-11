# SPDX-License-Identifier: Apache-2.0
"""Property tests for the SSE codec round-trip.

Item 2 of the Week-1 test architecture plan (2026-06-08).

Strategy: generate arbitrary CanonicalEvent instances, encode them via
_format_sse_frame, parse the resulting bytes back, and assert per-field
equivalence.  Total equality on the raw bytes is not the contract; what
matters is that every field present in the input can be recovered from the
emitted frame.
"""
from __future__ import annotations

import json
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from lmchat.lmstudio.types import CanonicalEvent, CanonicalToolCall
from lmchat.services.streaming_service import _format_sse_frame

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# All concrete CanonicalEvent.type literals.
_ALL_EVENT_TYPES = [
    "chat.start",
    "chat.end",
    "model_load.start",
    "model_load.progress",
    "model_load.end",
    "prompt_processing.start",
    "prompt_processing.progress",
    "prompt_processing.end",
    "message.start",
    "message.delta",
    "message.end",
    "reasoning.start",
    "reasoning.delta",
    "reasoning.end",
    "tool_call.start",
    "tool_call.name",
    "tool_call.arguments",
    "tool_call.success",
    "tool_call.failure",
    "tool_call.repeat_warning",
    "tool_call.failure_streak_warning",
    "tool_call.name_warning",
    "error",
]

# CanonicalToolCall strategy: fixed schema, arbitrary string fields.
_tool_call_st = st.builds(
    CanonicalToolCall,
    id=st.text(min_size=1, max_size=64),
    name=st.text(min_size=1, max_size=64),
    arguments=st.fixed_dictionaries({}),  # arguments is a dict; keep it empty for round-trip
    call_id=st.none() | st.text(min_size=1, max_size=64),
)

# CanonicalEvent strategy: sample a random event type; fill optional fields
# with plausible values.  Only one of content/response_id/progress/tool_call/
# error/model_instance_id is populated at a time (mirroring real production
# events) to avoid pyright-invisible invariant violations.
_canonical_event_st = st.one_of(
    # Plain lifecycle events with no payload
    st.builds(CanonicalEvent, type=st.sampled_from([
        "chat.start", "chat.end", "model_load.start", "model_load.end",
        "prompt_processing.start", "prompt_processing.end",
        "message.start", "message.end", "reasoning.start", "reasoning.end",
        "tool_call.start", "tool_call.success", "tool_call.failure",
        "tool_call.repeat_warning", "tool_call.failure_streak_warning",
        "tool_call.name_warning",
    ])),
    # Events with a content delta
    st.builds(
        CanonicalEvent,
        type=st.sampled_from(["message.delta", "reasoning.delta"]),
        content=st.text(max_size=256),
    ),
    # prompt_processing.progress with a float
    st.builds(
        CanonicalEvent,
        type=st.just("prompt_processing.progress"),
        progress=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    ),
    # chat.start with model_instance_id
    st.builds(
        CanonicalEvent,
        type=st.just("chat.start"),
        model_instance_id=st.text(min_size=1, max_size=64),
    ),
    # chat.end with response_id
    st.builds(
        CanonicalEvent,
        type=st.just("chat.end"),
        response_id=st.text(min_size=1, max_size=64),
    ),
    # error event
    st.builds(
        CanonicalEvent,
        type=st.just("error"),
        error=st.fixed_dictionaries({
            "code": st.text(min_size=1, max_size=32),
            "message": st.text(min_size=1, max_size=256),
        }),
    ),
    # tool_call events carrying a CanonicalToolCall
    st.builds(
        CanonicalEvent,
        type=st.sampled_from(["tool_call.name", "tool_call.arguments"]),
        tool_call=_tool_call_st,
    ),
)


# ---------------------------------------------------------------------------
# Helper: parse a single SSE frame back to (event_type, data_dict).
# ---------------------------------------------------------------------------

def _decode_frame(frame: bytes) -> tuple[str, dict[str, Any]]:
    """Parse one SSE frame produced by _format_sse_frame."""
    text = frame.decode("utf-8")
    assert text.endswith("\n\n"), f"Frame must end with double-newline, got: {text!r}"
    event_line: str | None = None
    data_line: str | None = None
    for raw_line in text.splitlines():
        if raw_line.startswith("event: "):
            event_line = raw_line[len("event: "):]
        elif raw_line.startswith("data: "):
            data_line = raw_line[len("data: "):]
    assert event_line is not None, "Frame must contain an event: line"
    assert data_line is not None, "Frame must contain a data: line"
    return event_line, json.loads(data_line)


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


@given(_canonical_event_st, st.integers(min_value=1, max_value=2**31 - 1))
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_sse_codec_roundtrip_type_and_msg_id(event: CanonicalEvent, msg_id: int) -> None:
    """Encoding any CanonicalEvent preserves type and injects msg_id.

    Strategy: _canonical_event_st × positive integers.
    """
    frame = _format_sse_frame(event, msg_id=msg_id)
    event_type, data = _decode_frame(frame)

    assert event_type == event.type, (
        f"Event type mismatch: encoded {event.type!r} but decoded {event_type!r}"
    )
    assert data["type"] == event.type, "data.type must mirror the event: header"
    assert data["msg_id"] == msg_id, "msg_id must be injected into every frame"


@given(_canonical_event_st, st.integers(min_value=1, max_value=2**31 - 1))
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_sse_codec_roundtrip_optional_fields(event: CanonicalEvent, msg_id: int) -> None:
    """Non-None optional fields survive the codec unchanged.

    Strategy: _canonical_event_st × positive integers.
    """
    frame = _format_sse_frame(event, msg_id=msg_id)
    _, data = _decode_frame(frame)

    if event.content is not None:
        assert data.get("content") == event.content, "content must survive round-trip"
    if event.response_id is not None:
        assert data.get("response_id") == event.response_id
    if event.progress is not None:
        assert abs(data.get("progress", float("nan")) - event.progress) < 1e-9
    if event.model_instance_id is not None:
        assert data.get("model_instance_id") == event.model_instance_id
    if event.error is not None:
        assert data.get("error") == event.error
    if event.tool_call is not None:
        tc_data = data.get("tool_call")
        assert tc_data is not None, "tool_call must be present in data when event has one"
        assert tc_data["id"] == event.tool_call.id
        assert tc_data["name"] == event.tool_call.name


@given(st.integers(min_value=1, max_value=2**31 - 1))
@settings(max_examples=100)
def test_sse_codec_frame_is_valid_utf8_bytes(msg_id: int) -> None:
    """_format_sse_frame always returns bytes decodable as UTF-8.

    Strategy: positive msg_ids; uses a fixed event to isolate the output shape.
    """
    event = CanonicalEvent(type="message.delta", content="hello")
    frame = _format_sse_frame(event, msg_id=msg_id)
    assert isinstance(frame, bytes)
    decoded = frame.decode("utf-8")
    assert "\n\n" in decoded, "Frame must contain a blank-line terminator"
