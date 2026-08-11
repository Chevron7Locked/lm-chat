# SPDX-License-Identifier: Apache-2.0
"""Unit tests for structural tool-name validation.

Tests:
- Valid snake_case name: no warning emitted.
- Name with embedded space: warning emitted.
- CamelCase name: warning emitted.
- Too-short name (2 chars): warning emitted (regex requires 3-64 chars).
- Empty name: no warning (truthy guard in the condition suppresses it).
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from prometheus_client import REGISTRY  # type: ignore[import-untyped]

from lmchat.lmstudio.types import (
    CanonicalChatRequest,
    CanonicalEvent,
    CanonicalInputBlock,
    CanonicalToolCall,
)
from lmchat.services.lmstudio_streaming_client import LmstudioStreamingClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(model: str = "test-model") -> CanonicalChatRequest:
    return CanonicalChatRequest(
        model=model,
        input=[CanonicalInputBlock(type="text", content="hello")],
    )


def _ev(type_: str, **kwargs: Any) -> CanonicalEvent:
    return CanonicalEvent(type=type_, **kwargs)  # type: ignore[arg-type]


def _tool_ev(type_: str, tc: CanonicalToolCall) -> CanonicalEvent:
    return CanonicalEvent(type=type_, tool_call=tc)  # type: ignore[arg-type]


def _name_event_stream(tool_name: str) -> list[CanonicalEvent]:
    """Minimal stream: chat.start → tool_call.name (only) → chat.end.

    Validation fires on tool_call.name events only; we don't need a full
    tool-call sequence to test the validation path.
    """
    tc = CanonicalToolCall(id="c1", name=tool_name, arguments={})
    return [
        _ev("chat.start"),
        _tool_ev("tool_call.name", tc),
        _ev("chat.end"),
    ]


async def _events_from(events: list[CanonicalEvent]) -> AsyncIterator[CanonicalEvent]:
    for ev in events:
        yield ev


def _make_client(events: list[CanonicalEvent]) -> LmstudioStreamingClient:
    adapter = MagicMock()
    adapter.stream_chat = MagicMock(return_value=_events_from(events))
    return LmstudioStreamingClient(adapter=adapter)


async def _collect(
    client: LmstudioStreamingClient, callback=None
) -> list[CanonicalEvent]:
    req = _make_request()
    return [ev async for ev in client.stream(request=req, on_tool_call=callback)]


def _get_name_warning_counter(kind: str = "structural") -> float:
    """Read TOOL_CALL_NAME_WARNINGS_TOTAL{kind=...} from the Prometheus registry."""
    for metric in REGISTRY.collect():
        if metric.name == "lmchat_tool_call_name_warnings":
            for sample in metric.samples:
                if (
                    sample.name == "lmchat_tool_call_name_warnings_total"
                    and sample.labels.get("kind") == kind
                ):
                    return sample.value
    return 0.0


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_snake_case_name_no_warning() -> None:
    """Valid snake_case name (e.g. search_web) emits no tool_call.name_warning."""
    events = _name_event_stream("search_web")
    client = _make_client(events)

    result = await _collect(client)

    warnings = [ev for ev in result if ev.type == "tool_call.name_warning"]
    assert len(warnings) == 0, (
        f"False positive on valid name 'search_web'. Events: {[ev.type for ev in result]}"
    )


@pytest.mark.asyncio
async def test_name_with_space_emits_warning() -> None:
    """Name with embedded space ('get results') is structurally invalid — warning emitted."""
    events = _name_event_stream("get results")
    client = _make_client(events)

    before = _get_name_warning_counter()
    result = await _collect(client)
    after = _get_name_warning_counter()

    assert after == before + 1.0, f"Counter not incremented: before={before} after={after}"

    warnings = [ev for ev in result if ev.type == "tool_call.name_warning"]
    assert len(warnings) == 1, f"Expected 1 warning, got {len(warnings)}"

    assert warnings[0].error is not None
    assert warnings[0].error["code"] == "tool_name_malformed"
    assert warnings[0].error["attempted"] == "get results"

    # Warning appears BEFORE the original tool_call.name event.
    types = [ev.type for ev in result]
    warning_idx = types.index("tool_call.name_warning")
    name_idx = types.index("tool_call.name")
    assert warning_idx < name_idx, (
        f"Warning should precede the original event. Order: {types}"
    )


@pytest.mark.asyncio
async def test_camel_case_name_emits_warning() -> None:
    """CamelCase name ('SearchWeb') is structurally invalid — warning emitted."""
    events = _name_event_stream("SearchWeb")
    client = _make_client(events)

    before = _get_name_warning_counter()
    result = await _collect(client)
    after = _get_name_warning_counter()

    assert after == before + 1.0
    warnings = [ev for ev in result if ev.type == "tool_call.name_warning"]
    assert len(warnings) == 1, f"Expected 1 warning for CamelCase, got {len(warnings)}"


@pytest.mark.asyncio
async def test_too_short_name_emits_warning() -> None:
    """Name with only 2 chars ('ab') is below the 3-char minimum — warning emitted."""
    events = _name_event_stream("ab")
    client = _make_client(events)

    before = _get_name_warning_counter()
    result = await _collect(client)
    after = _get_name_warning_counter()

    assert after == before + 1.0
    warnings = [ev for ev in result if ev.type == "tool_call.name_warning"]
    assert len(warnings) == 1, f"Expected 1 warning for too-short name, got {len(warnings)}"


@pytest.mark.asyncio
async def test_empty_name_no_warning() -> None:
    """Empty name string suppressed by truthy guard in the validation condition — no warning."""
    events = _name_event_stream("")
    client = _make_client(events)

    result = await _collect(client)

    warnings = [ev for ev in result if ev.type == "tool_call.name_warning"]
    assert len(warnings) == 0, (
        f"Empty name should be suppressed by truthy guard, got {len(warnings)} warnings"
    )
