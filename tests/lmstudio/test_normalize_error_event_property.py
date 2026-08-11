# SPDX-License-Identifier: Apache-2.0
"""Property tests for _normalize_error_event.

Item 3 of the Week-1 test architecture plan (2026-06-08).

Contract from canonical sub.error: the output MUST always have a non-empty
``code`` string and a non-empty ``message`` string, regardless of what
arbitrary input comes in.

Known bugs surfaced by Hypothesis (flagged per AGENTS.md — do not fix here):
  BUG-A: When a dict has a list value for "type", _LM_ERROR_TYPE_TO_CODE.get()
         raises TypeError (unhashable type: list) because ``err_type`` is
         assigned directly from ``err_payload.get("type")`` without a str check.
         Repro: _normalize_error_event({"type": []})
  BUG-B: When a dict has a bool value for "code" (e.g. True), the result
         ``canonical_code`` is bool, violating the "must be str" invariant.
         Repro: _normalize_error_event({"code": True, "message": "x"})

Input domain is intentionally constrained to JSON-decodable dicts with
scalar values (matching the realistic LM Studio wire domain) so that the
failing test suite baseline is not perturbed while the bugs are open.
The full mixed-type strategy is included but skipped with xfail so the
bug repros are still checked into test history.
"""
from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from lmchat.lmstudio.native import _normalize_error_event

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# JSON-scalar leaf strategy: only string / int / float / None / bool.
# Lists and nested dicts are excluded here because they would expose BUG-A
# (list-typed dict values used as hash keys).
_scalar_leaf_st = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**31), max_value=2**31),
    st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False),
    st.text(max_size=256),
)

# Realistic dict strategy: keys are strings, values are scalars only.
# The "type" key is always str because it's used as a dict key in _LM_ERROR_TYPE_TO_CODE
# (BUG-A is triggered by non-str types there; we constrain to avoid it here).
_realistic_dict_st = st.dictionaries(
    keys=st.one_of(
        st.sampled_from(["type", "message", "error", "code", "hint",
                         "n_prompt_tokens", "n_ctx"]),
        st.text(min_size=1, max_size=32),
    ),
    values=_scalar_leaf_st,
    max_size=10,
)

# String-typed "type" key strategy — ensures BUG-A is not triggered so we can
# exercise the fast paths in _normalize_error_event.
# The optional dict is typed as Any to avoid pyright inference issues with
# mixed SearchStrategy value types.
_optional_str_type: dict[str, st.SearchStrategy] = {  # type: ignore[type-arg]
    "message": st.text(max_size=256),
    "code": st.text(max_size=64),       # string code only (avoids BUG-B)
    "hint": st.text(max_size=128),
    "n_prompt_tokens": st.integers(min_value=0),
    "n_ctx": st.integers(min_value=0),
}
_str_type_dict_st = st.fixed_dictionaries(
    {"type": st.text(max_size=64)},
    optional=_optional_str_type,  # type: ignore[arg-type]
)

# Non-dict payload strategy (scalar wrapping path — always safe).
_scalar_payload_st: st.SearchStrategy[object] = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=512),
)


# ---------------------------------------------------------------------------
# Properties — safe domain (all expected to pass)
# ---------------------------------------------------------------------------


@given(_scalar_payload_st)
@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
def test_normalize_error_event_scalar_always_has_non_empty_code(
    payload: object,
) -> None:
    """Non-dict scalars always produce code='upstream_error' (non-empty str).

    Strategy: None / bool / int / float / str.
    """
    result = _normalize_error_event(payload)
    assert isinstance(result, dict)
    code = result.get("code")
    assert isinstance(code, str), f"code must be str, got {type(code)}: {code!r}"
    assert len(code) > 0, f"code must be non-empty for payload {payload!r}"
    assert result["code"] == "upstream_error"


@given(_scalar_payload_st)
@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
def test_normalize_error_event_scalar_always_has_non_empty_message(
    payload: object,
) -> None:
    """Non-dict scalars always produce a non-empty message.

    Strategy: None / bool / int / float / str.
    """
    result = _normalize_error_event(payload)
    message = result.get("message")
    assert isinstance(message, str), f"message must be str, got {type(message)}"
    assert len(message) > 0, f"message must be non-empty for payload {payload!r}"


@given(_str_type_dict_st)
@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
def test_normalize_error_event_str_type_dict_never_raises(
    payload: dict,  # type: ignore[type-arg]
) -> None:
    """Dicts with string 'type' and string 'code' never raise.

    Strategy: fixed_dictionaries with str-typed 'type' key (avoids BUG-A)
    and optional str 'code' (avoids BUG-B).
    """
    result = _normalize_error_event(payload)
    assert isinstance(result, dict)


@given(_str_type_dict_st)
@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
def test_normalize_error_event_str_type_dict_has_non_empty_code_and_message(
    payload: dict,  # type: ignore[type-arg]
) -> None:
    """Dicts with str-type values always produce non-empty code + message.

    Strategy: _str_type_dict_st (str-typed type/code, optional other keys).
    """
    result = _normalize_error_event(payload)
    code = result.get("code")
    message = result.get("message")
    assert isinstance(code, str) and len(code) > 0, (
        f"code must be non-empty str, got {code!r} from {payload!r}"
    )
    assert isinstance(message, str) and len(message) > 0, (
        f"message must be non-empty str, got {message!r} from {payload!r}"
    )


# ---------------------------------------------------------------------------
# Bug-documenting xfail tests — repros for BUG-A and BUG-B.
# Remove xfail markers when the bugs are fixed in src/lmchat/lmstudio/native.py.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"type": ["abc"], "message": "test"},   # non-empty list (main repro)
        {"type": [], "message": "test"},         # empty list
        {"type": None, "message": "test"},       # None
        {"message": "test"},                     # missing type entirely
        {"type": 42, "message": "test"},         # non-string non-list truthy
        {"type": {"nested": "dict"}, "message": "test"},  # dict-as-type
    ],
)
def test_bug_a_non_str_type_value_produces_valid_output(
    payload: dict,  # type: ignore[type-arg]
) -> None:
    """BUG-A variants: any non-str 'type' must not raise and must return non-empty code+message."""
    result = _normalize_error_event(payload)
    assert isinstance(result, dict), f"must return a dict for {payload!r}"
    code = result.get("code")
    assert isinstance(code, str) and len(code) > 0, (
        f"code must be non-empty str, got {code!r} for {payload!r}"
    )
    message = result.get("message")
    assert isinstance(message, str) and len(message) > 0, (
        f"message must be non-empty str, got {message!r} for {payload!r}"
    )


@pytest.mark.parametrize(
    "payload,expected_code",
    [
        # type-lookup hit wins: _LM_ERROR_TYPE_TO_CODE maps "tool_format_generation_error"
        # to itself, so the bool code is ignored entirely.
        (
            {"type": "tool_format_generation_error", "code": True, "message": "x"},
            "tool_format_generation_error",
        ),
        # type-lookup miss + int code → str(42)
        (
            {"type": "unknown_xyz", "code": 42, "message": "x"},
            "42",
        ),
        # type-lookup miss + bool code → str(True) = "True"
        (
            {"type": "unknown_xyz", "code": True, "message": "x"},
            "True",
        ),
    ],
)
def test_bug_b_non_str_code_is_coerced_to_str(
    payload: dict,  # type: ignore[type-arg]
    expected_code: str,
) -> None:
    """BUG-B variants: non-str 'code' must be str()-coerced to an exact known value.

    Asserts the CONCRETE output string, not just isinstance, so mutations that
    swap fallback order or remove str() coercion are caught.
    """
    result = _normalize_error_event(payload)
    assert isinstance(result.get("code"), str), (
        f"Expected str, got {type(result.get('code'))}: {result.get('code')!r}"
    )
    assert result["code"] == expected_code, (
        f"Expected code={expected_code!r}, got {result['code']!r} for {payload!r}"
    )
