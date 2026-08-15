# SPDX-License-Identifier: Apache-2.0
"""SSE event-name contract: BE wire vocabulary == sse-event-names.json.

The structural fix for a class of bug where the BE emits an event the FE
silently dropped.  The shared SSOT is ``web/src/types/sse-event-names.json``;
this file asserts the BE side of the contract:

    {CanonicalEvent.type Literal members}
  ∪ {event names of the synthetic frames from _format_error_frame /
     _format_warning_frame / _format_followups_frame /
     _format_memory_saved_frame / _format_mode_adopt_frame}
  == set(sse-event-names.json)

The FE side is asserted by vitest
(``web/tests/unit/test_sse_event_names_contract.spec.ts``) against the
``CANONICAL_EVENT_TYPES`` array that derives ``CanonicalEventType`` in
``useSSE.ts``.  Either side drifting fails its test loudly.
"""
from __future__ import annotations

import json
import re
import typing
from pathlib import Path

from lmchat.lmstudio.types import CanonicalEvent
from lmchat.services.streaming_service import (
    _format_error_frame,
    _format_followups_frame,
    _format_memory_saved_frame,
    _format_mode_adopt_frame,
    _format_warning_frame,
)

# ---------------------------------------------------------------------------
# SSOT loading — same layout as sse_envelope_schemas.py.
# ---------------------------------------------------------------------------

_SSOT_PATH = (
    Path(__file__).parent.parent.parent
    / "web" / "src" / "types" / "sse-event-names.json"
)


def _ssot_event_names() -> set[str]:
    with _SSOT_PATH.open() as fh:
        doc = json.load(fh)
    names = doc["event_names"]
    assert isinstance(names, list)
    assert all(isinstance(n, str) for n in names)
    return set(names)


def _canonical_literal_names() -> set[str]:
    """Enumerate the ``CanonicalEvent.type`` Literal members."""
    annotation = CanonicalEvent.model_fields["type"].annotation
    names = typing.get_args(annotation)
    assert names, "CanonicalEvent.type Literal yielded no members"
    return set(names)


def _frame_event_name(frame: bytes) -> str:
    """Extract the ``event:`` name from a formatted SSE frame."""
    match = re.match(r"event: (\S+)\n", frame.decode("utf-8"))
    assert match is not None, f"frame missing event line: {frame!r}"
    return match.group(1)


def _synthetic_frame_names() -> set[str]:
    """Event names of the five synthetic frames, from the real formatters."""
    return {
        _frame_event_name(
            _format_error_frame(code="contract_probe", detail="x", msg_id=1)
        ),
        _frame_event_name(
            _format_warning_frame(code="contract_probe", detail="x", msg_id=1)
        ),
        _frame_event_name(
            _format_followups_frame(followups=[], msg_id=1)
        ),
        _frame_event_name(
            _format_memory_saved_frame(count=1, msg_id=1)
        ),
        _frame_event_name(
            _format_mode_adopt_frame(preset_id=None, msg_id=1)
        ),
    }


# ---------------------------------------------------------------------------
# Contract tests
# ---------------------------------------------------------------------------


def test_be_emittable_event_names_match_ssot_exactly() -> None:
    """CanonicalEvent Literal + synthetic frames == sse-event-names.json."""
    be_names = _canonical_literal_names() | _synthetic_frame_names()
    ssot = _ssot_event_names()

    missing_from_ssot = be_names - ssot
    stale_in_ssot = ssot - be_names

    assert not missing_from_ssot, (
        "BE can emit SSE events absent from sse-event-names.json "
        f"(the FE would never see a case for them): {sorted(missing_from_ssot)}. "
        "Add them to web/src/types/sse-event-names.json AND to the FE "
        "CANONICAL_EVENT_TYPES/handleEvent in useSSE.ts."
    )
    assert not stale_in_ssot, (
        "sse-event-names.json lists events the BE can no longer emit: "
        f"{sorted(stale_in_ssot)}. Remove them from the SSOT and from the FE "
        "CANONICAL_EVENT_TYPES/handleEvent in useSSE.ts."
    )


def test_synthetic_frames_are_error_warning_followups_memory_saved() -> None:
    """Pin the synthetic frame names — the wire grammar the FE special-cases."""
    assert _synthetic_frame_names() == {
        "error",
        "warning",
        "followups",
        "memory.saved",
        "mode_adopt",
    }
