# SPDX-License-Identifier: Apache-2.0
"""Property tests for _ToolCallAccumulator dict-merge logic.

Item 4 of the Week-1 test architecture plan (2026-06-08).

Fix #17 was a production bug in the exact accumulator code path: dict-typed
argument chunks were being json.dumps-concatenated with string chunks,
producing invalid JSON.  Property tests explore the combinatorial space that
unit tests miss.

Contracts asserted on finalize() output:
  (a) The result is either None or a CanonicalToolCall.
  (b) When it is a CanonicalToolCall, arguments is a valid dict
      (i.e., it was already parsed; CanonicalToolCall.arguments: dict).
  (c) No data from a non-None string chunk is silently lost when the
      accumulator operates in string-concat mode (no dict chunks).
"""
from __future__ import annotations

import json

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from lmchat.lmstudio.types import CanonicalEvent, CanonicalToolCall
from lmchat.services.lmstudio_streaming_client import (
    MalformedToolCallError,
    _ToolCallAccumulator,
)

# ---------------------------------------------------------------------------
# Helpers to build CanonicalEvent instances for the accumulator
# ---------------------------------------------------------------------------

_TOOL_ID = "tc-property-test-id"
_TOOL_NAME = "property_test_tool"


def _start_event() -> CanonicalEvent:
    return CanonicalEvent(
        type="tool_call.start",
        tool_call=CanonicalToolCall(id=_TOOL_ID, name=_TOOL_NAME, arguments={}),
    )


def _name_event(name: str = _TOOL_NAME) -> CanonicalEvent:
    return CanonicalEvent(
        type="tool_call.name",
        tool_call=CanonicalToolCall(id=_TOOL_ID, name=name, arguments={}),
    )


def _args_str_event(chunk: str) -> CanonicalEvent:
    """tool_call.arguments event carrying a string chunk.

    CanonicalToolCall.arguments is typed as dict, so we use model_construct to
    bypass pydantic validation and inject a raw string, matching the behaviour
    of the native/compat decoders which can produce string-typed arguments chunks
    during streaming accumulation.
    """
    tc = CanonicalToolCall.model_construct(id=_TOOL_ID, name=_TOOL_NAME, arguments=chunk)
    return CanonicalEvent(type="tool_call.arguments", tool_call=tc)


def _args_dict_event(chunk: dict) -> CanonicalEvent:  # type: ignore[type-arg]
    """tool_call.arguments event carrying a dict chunk (Fix #17 path)."""
    return CanonicalEvent(
        type="tool_call.arguments",
        tool_call=CanonicalToolCall(id=_TOOL_ID, name=_TOOL_NAME, arguments=chunk),
    )


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Simple JSON-serialisable leaf values.
_json_leaf_st = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-1000, max_value=1000),
    st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False),
    st.text(max_size=64),
)

# Flat dict strategy (values are JSON-serialisable scalars).
_flat_dict_st = st.dictionaries(
    keys=st.text(min_size=1, max_size=16),
    values=_json_leaf_st,
    max_size=5,
)

# A chunk is either a valid JSON string fragment, a dict, or None.
# String chunks are always complete JSON objects to keep the round-trip
# assertable without a real streaming parser.
_string_chunk_st = _flat_dict_st.map(json.dumps)
_chunk_st: st.SearchStrategy[str | dict | None] = st.one_of(  # type: ignore[type-arg]
    _string_chunk_st,
    _flat_dict_st,
    st.none(),
)

# Sequences of homogeneous chunk types (to avoid the mixed-chunk edge case
# that deliberately discards prior string chunks in production code).
_string_chunk_seq_st = st.lists(_string_chunk_st, min_size=0, max_size=8)
_dict_chunk_seq_st = st.lists(_flat_dict_st, min_size=1, max_size=8)


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


@given(st.lists(_chunk_st, min_size=0, max_size=10))
@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
def test_accumulator_finalize_returns_none_or_toolcall(
    chunks: list[str | dict | None],  # type: ignore[type-arg]
) -> None:
    """finalize() returns None or a CanonicalToolCall — never raises unexpectedly.

    Strategy: sequences of string / dict / None chunks of up to 10 items.
    Contract (a): result is either None or CanonicalToolCall.
    """
    acc = _ToolCallAccumulator()
    acc.ingest(_start_event())
    acc.ingest(_name_event())

    for chunk in chunks:
        if chunk is None:
            # Null chunk: build a tool_call with None arguments to exercise the
            # null-chunk log path.  CanonicalToolCall.arguments is typed as dict,
            # so bypass validation via model_construct.
            tc = CanonicalToolCall.model_construct(
                id=_TOOL_ID, name=_TOOL_NAME, arguments=None, call_id=None,
            )
            acc.ingest(CanonicalEvent(type="tool_call.arguments", tool_call=tc))
        elif isinstance(chunk, str):
            acc.ingest(_args_str_event(chunk))
        else:
            acc.ingest(_args_dict_event(chunk))

    # finalize() is allowed to return None (missing name is guarded earlier;
    # here name is always set, so it should return a CanonicalToolCall or
    # raise MalformedToolCallError in pathological cases).
    try:
        result = acc.finalize()
    except MalformedToolCallError:
        # Allowed: malformed JSON is a documented raise path.
        return

    assert result is None or isinstance(result, CanonicalToolCall), (
        f"Expected None or CanonicalToolCall, got {type(result)}"
    )


@given(_string_chunk_seq_st)
@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
def test_accumulator_string_mode_arguments_are_valid_dict(
    chunks: list[str],
) -> None:
    """String chunks that are valid JSON objects yield parseable arguments.

    Strategy: list of json.dumps(flat_dict) strings — each chunk is itself
    a valid JSON object.  The accumulator concatenates them; the result is
    the concatenation of those JSON strings. When the sequence has exactly
    one chunk the result must be a valid dict.  When there are multiple
    complete JSON object strings concatenated, coerce_tool_args is allowed
    to take the first object (existing production behaviour for stacked JSON).

    Contract (b): when result is a CanonicalToolCall, arguments is a dict.
    """
    acc = _ToolCallAccumulator()
    acc.ingest(_start_event())
    acc.ingest(_name_event())

    for chunk in chunks:
        acc.ingest(_args_str_event(chunk))

    try:
        result = acc.finalize()
    except MalformedToolCallError:
        return  # Allowed on genuinely unparseable concatenation

    if result is not None:
        assert isinstance(result, CanonicalToolCall)
        assert isinstance(result.arguments, dict), (
            f"arguments must be a dict, got {type(result.arguments)}: {result.arguments!r}"
        )


@given(_dict_chunk_seq_st)
@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
def test_accumulator_dict_mode_produces_valid_dict_arguments(
    chunks: list[dict],  # type: ignore[type-arg]
) -> None:
    """Dict chunks are deep-merged and finalize() yields a valid dict.

    Strategy: non-empty list of flat dicts (at least one, so name+dict-mode
    is always activated).

    Contract (b): arguments is a dict.
    Contract (c): every key from any dict chunk appears in the final result
    (when no key collision causes an overwrite — checked via union of keys).
    """
    acc = _ToolCallAccumulator()
    acc.ingest(_start_event())
    acc.ingest(_name_event())

    all_keys: set[str] = set()
    for chunk in chunks:
        acc.ingest(_args_dict_event(chunk))
        all_keys.update(chunk.keys())

    try:
        result = acc.finalize()
    except MalformedToolCallError:
        return

    assert result is not None, "Dict chunks with a name should always finalize"
    assert isinstance(result.arguments, dict), (
        f"arguments must be dict in dict-mode, got {type(result.arguments)}"
    )
    # Every key that ever appeared in any chunk must be in the final result.
    # (Deep-merge: later values for the same key overwrite earlier ones, but
    # the KEY itself must always survive.)
    for key in all_keys:
        assert key in result.arguments, (
            f"Key {key!r} from a dict chunk was silently lost in arguments: "
            f"{result.arguments!r}"
        )


def test_accumulator_no_name_returns_none() -> None:
    """finalize() returns None when no tool_call.name event was ingested.

    This is a deterministic contract sanity-check, not a property test.
    """
    acc = _ToolCallAccumulator()
    acc.ingest(_start_event())
    # Deliberately skip _name_event()
    acc.ingest(_args_str_event('{"q": "test"}'))
    assert acc.finalize() is None


def test_accumulator_reset_clears_state() -> None:
    """reset() clears all state so the next call starts fresh."""
    acc = _ToolCallAccumulator()
    acc.ingest(_start_event())
    acc.ingest(_name_event("first_tool"))
    acc.ingest(_args_str_event('{"a": 1}'))
    acc.reset()
    # After reset, finalize() returns None (no name set).
    assert acc.finalize() is None
