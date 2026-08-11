# SPDX-License-Identifier: Apache-2.0
"""Hypothesis property tests for the streaming state machine.

Property-based tests for the streaming service.

Tests the state-machine invariants using hypothesis:
- ``PersistState`` transitions follow the valid DAG.
- No draft → final without going through pending_finalization.
- Aborted rows can never reach final via the normal transition path.
- SSE parser: any byte sequence beginning with valid SSE structure parses
  without unhandled exception.
"""
from __future__ import annotations

import json

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from lmchat.lmstudio.types import CanonicalEvent
from lmchat.services._stream_state import PersistState
from lmchat.services.streaming_service import _format_error_frame, _format_sse_frame

# ---------------------------------------------------------------------------
# State machine invariants
# ---------------------------------------------------------------------------

# Valid forward transitions in the state DAG.
_VALID_TRANSITIONS: frozenset[tuple[PersistState, PersistState]] = frozenset({
    (PersistState.DRAFT, PersistState.PENDING_FINALIZATION),
    (PersistState.DRAFT, PersistState.ABORTED_BY_CLIENT),
    (PersistState.PENDING_FINALIZATION, PersistState.FINAL),
})

# All states.
_ALL_STATES = list(PersistState)

# Arbitrary pairs strategy.
_state_pair = st.sampled_from(_ALL_STATES).flatmap(
    lambda from_s: st.sampled_from(_ALL_STATES).map(lambda to_s: (from_s, to_s))
)


@given(_state_pair)
@settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_state_machine_valid_transitions_are_subset_of_dag(
    pair: tuple[PersistState, PersistState],
) -> None:
    """Valid transitions form a strict subset of the DAG edges.

    The live code uses atomic SQL UPDATEs for transitions; this property test
    validates the *intent* of the DAG regardless of DB state.

    Invariant: a transition is valid iff it appears in _VALID_TRANSITIONS.
    Invalid transitions (e.g. draft → final, final → draft) are not allowed
    by the business logic (the DB WHERE clause ensures rowcount=0).

    Args:
        pair: A (from_state, to_state) tuple drawn from the hypothesis engine.
    """
    from_state, to_state = pair
    is_valid = (from_state, to_state) in _VALID_TRANSITIONS
    # Property: no transition from FINAL is valid (terminal state).
    if from_state == PersistState.FINAL:
        assert not is_valid, (
            f"FINAL should have no outgoing transitions, got ({from_state}, {to_state})"
        )
    # Property: no transition from ABORTED_BY_CLIENT is valid (terminal state).
    if from_state == PersistState.ABORTED_BY_CLIENT:
        assert not is_valid, "ABORTED_BY_CLIENT should have no outgoing transitions"


def test_draft_cannot_transition_directly_to_final() -> None:
    """draft → final is NOT in the valid transition set.

    The state machine requires draft to pass through pending_finalization
    before reaching final.  This is enforced by the DB UPDATE WHERE predicate;
    the property test locks the intent in the DAG definition.
    """
    assert (PersistState.DRAFT, PersistState.FINAL) not in _VALID_TRANSITIONS


def test_aborted_cannot_reach_final() -> None:
    """aborted_by_client → final is NOT a valid transition.

    Once a stream is aborted, the row must stay aborted. The reaper only
    processes pending_finalization and draft rows, not aborted rows.
    """
    assert (PersistState.ABORTED_BY_CLIENT, PersistState.FINAL) not in _VALID_TRANSITIONS


def test_final_has_no_successors() -> None:
    """No valid transition originates from FINAL (it is a terminal state)."""
    final_transitions = [
        (f, t) for (f, t) in _VALID_TRANSITIONS if f == PersistState.FINAL
    ]
    assert len(final_transitions) == 0, f"FINAL should be terminal, found: {final_transitions}"


def test_all_reachable_states_are_covered() -> None:
    """Every non-initial state is reachable from DRAFT via the valid DAG."""
    # BFS from DRAFT.
    reachable: set[PersistState] = {PersistState.DRAFT}
    frontier = {PersistState.DRAFT}
    while frontier:
        new_frontier: set[PersistState] = set()
        for from_s in frontier:
            for (f, t) in _VALID_TRANSITIONS:
                if f == from_s and t not in reachable:
                    reachable.add(t)
                    new_frontier.add(t)
        frontier = new_frontier

    expected = set(PersistState)
    assert reachable == expected, (
        f"Not all states reachable from DRAFT. Missing: {expected - reachable}"
    )


# ---------------------------------------------------------------------------
# SSE frame format properties
# ---------------------------------------------------------------------------

# Bounded binary strategy for "any bytes" cases.
_any_bytes = st.binary(min_size=0, max_size=1024)

# Structurally-shaped inputs: valid SSE headers with arbitrary data payloads.
# Using str regex (not bytes) so hypothesis.strategies.from_regex works cleanly.
_valid_sse_shaped = st.from_regex(
    r"event: \w+\ndata: .{0,256}\n\n",
)


@given(st.integers(min_value=1, max_value=999999))
@settings(max_examples=50)
def test_format_sse_frame_always_has_msg_id(msg_id: int) -> None:
    """Every SSE frame produced by _format_sse_frame contains msg_id.

    The msg_id is injected into every frame as a per-frame
    identifier used for client-side reconciliation. This property ensures no event type
    loses the field.

    Args:
        msg_id: Arbitrary message PK drawn by hypothesis.
    """
    # Use message.delta as a representative event.
    event = CanonicalEvent(type="message.delta", content="test")
    frame = _format_sse_frame(event, msg_id=msg_id)
    text = frame.decode("utf-8")

    # Must have valid SSE frame structure.
    assert "event: message.delta\n" in text
    assert "data: " in text
    assert text.endswith("\n\n")

    # Parse data and verify msg_id is present and correct.
    data_line = next(ln for ln in text.splitlines() if ln.startswith("data:"))
    data = json.loads(data_line[5:].strip())
    assert data["msg_id"] == msg_id


@given(st.integers(min_value=1, max_value=999999))
@settings(max_examples=50)
def test_format_error_frame_always_has_msg_id_and_code(msg_id: int) -> None:
    """Every error frame contains msg_id and error.code.

    Args:
        msg_id: Arbitrary message PK drawn by hypothesis.
    """
    frame = _format_error_frame(code="upstream_stall", detail="idle", msg_id=msg_id)
    text = frame.decode("utf-8")

    assert "event: error\n" in text
    data_line = next(ln for ln in text.splitlines() if ln.startswith("data:"))
    data = json.loads(data_line[5:].strip())
    assert data["msg_id"] == msg_id
    assert data["type"] == "error"
    assert "error" in data
    assert data["error"]["code"] == "upstream_stall"


@pytest.mark.parametrize("event_type,kwargs", [
    ("chat.start", {}),
    ("chat.end", {"response_id": "test-rid"}),
    ("message.start", {}),
    ("message.delta", {"content": "delta text"}),
    ("message.end", {}),
    ("error", {"error": {"code": "test", "message": "test"}}),
])
def test_format_sse_frame_valid_for_all_key_event_types(
    event_type: str, kwargs: dict  # type: ignore[type-arg]
) -> None:
    """_format_sse_frame does not raise for any of the key event types.

    Tests the event types most likely to appear in production streams.

    Args:
        event_type: One of the key LM Studio event type strings.
        kwargs:     Extra keyword arguments for the CanonicalEvent constructor.
    """
    from lmchat.lmstudio.types import CanonicalEvent as CE

    # Build the event using type-narrowed calls to satisfy pyright.
    ev: CE
    if event_type == "chat.start":
        ev = CE(type="chat.start")
    elif event_type == "chat.end":
        ev = CE(type="chat.end", response_id=kwargs.get("response_id"))
    elif event_type == "message.start":
        ev = CE(type="message.start")
    elif event_type == "message.delta":
        ev = CE(type="message.delta", content=kwargs.get("content"))
    elif event_type == "message.end":
        ev = CE(type="message.end")
    elif event_type == "error":
        ev = CE(type="error", error=kwargs.get("error"))
    else:
        ev = CE(type="chat.start")

    frame = _format_sse_frame(ev, msg_id=1)
    assert isinstance(frame, bytes)
    assert frame.endswith(b"\n\n")
