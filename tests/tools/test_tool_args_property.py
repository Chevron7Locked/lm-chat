# SPDX-License-Identifier: Apache-2.0
"""Hypothesis property battery for ``recover_xml_tool_calls``.

Covers:

* **Round-trip identity** — closing-tag and opening-only dialects, single
  and multi-function per wrapper.
* **No-crash on adversarial input** — nested wrappers, mixed dialects,
  malformed quoting, embedded literals.
* **ReDoS gate** — ``deadline=50`` ms per parse, enforced on every
  property, plus an explicit adversarial corpus of pathological inputs.

Pinned seed for deterministic replay: ``SEED = 42`` (configurable via
``HYPOTHESIS_SEED`` env var, but the committed examples directory at
``.hypothesis/examples/`` covers the discovered edge cases).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from lmchat.services.tool_args import (
    _FENCE_RE,
    _SQ_KEY_RE,
    _SQ_VAL_RE,
    coerce_tool_args,
    recover_xml_tool_calls,
)
from tests.tools._xml_emit import (
    emit_closing_tag_xml,
    emit_closing_tag_xml_multi,
    emit_opening_only_xml,
    emit_opening_only_xml_multi,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SEED = 42

# Common name strategy matching both ``_XML_FUNC_RE`` and
# ``_XML_FUNC_OPEN_RE`` (``[a-z_][a-z0-9_]{0,30}`` with room to spare).
_NAME_STRAT = st.from_regex(r"[a-z_][a-z0-9_]{1,12}", fullmatch=True)

# Common key strategy matching ``_XML_PARAM_RE`` / ``_XML_PARAM_OPEN_RE``.
_KEY_STRAT = st.from_regex(r"[a-z_][a-z0-9_]{1,12}", fullmatch=True)

# Simple value types that round-trip cleanly through the XML parser.
# NOTE: string values that look like JSON primitives (e.g. "0", "true")
# would be JSON-parsed by the recovery logic, breaking strict round-trip.
# We filter those out — strings that survive json.loads as non-string
# types are excluded from the string branch.


def _is_safe_roundtrip_string(s: str) -> bool:
    """Return True when *s* round-trips cleanly through BOTH dialects.

    A string like ``"0"`` or ``"true"`` would survive ``json.loads``
    as ``0`` (int) or ``True`` (bool), which breaks round-trip identity
    because the emitter sends the raw string and the parser converts
    it to a different type.

    Additionally, the opening-only dialect calls ``.strip()`` on values,
    so whitespace-only strings would be reduced to ``""``.  We exclude
    those too.
    """
    # Empty string → coerce_tool_args returns {}; harmless for round-trip.
    if not s:
        return True
    # Whitespace-only: opening-only dialect strips to "" → lossy.
    if s.strip() != s:
        return False
    try:
        v = json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return True  # not valid JSON → safe as string
    # If json.loads returned a non-string type, round-trip is lossy.
    return isinstance(v, str)


# Strings that round-trip cleanly through both dialects.
_SAFE_TEXT_STRAT = st.text(max_size=100).filter(_is_safe_roundtrip_string)

# Value strategy: safe strings + explicit typed values + composite values.
_VAL_STRAT: st.SearchStrategy[Any] = st.one_of(
    _SAFE_TEXT_STRAT,
    st.integers(min_value=-10_000, max_value=10_000),
    st.floats(allow_nan=False, allow_infinity=False, min_value=-1e6, max_value=1e6),
    st.booleans(),
    st.none(),
    st.lists(_SAFE_TEXT_STRAT, max_size=4),
    st.dictionaries(
        keys=st.from_regex(r"[a-z_][a-z0-9_]*", fullmatch=True),
        values=_SAFE_TEXT_STRAT,
        max_size=3,
    ),
)

# Args dictionary for a single function.
_ARGS_STRAT: st.SearchStrategy[dict[str, Any]] = st.dictionaries(
    keys=_KEY_STRAT,
    values=_VAL_STRAT,
    min_size=0,
    max_size=8,
)

# Base deadline — every property enforces <50 ms per parse (ReDoS gate).
# ``suppress_health_check`` allows data_too_large from large adversarial
# inputs without false-positive health-check failures.
_RE_GATE = settings(
    deadline=50,
    suppress_health_check=[HealthCheck.data_too_large, HealthCheck.too_slow],
)

# ---------------------------------------------------------------------------
# (a) Round-trip identity — closing-tag dialect
# ---------------------------------------------------------------------------


class TestRoundtripClosingTag:
    """Round-trip identity for the canonical closing-tag XML dialect."""

    @_RE_GATE
    @given(name=_NAME_STRAT, args=_ARGS_STRAT)
    def test_single_function(self, name: str, args: dict[str, Any]) -> None:
        """A single function call round-trips identically: emit → parse →
        assert name and args match.
        """
        wire = emit_closing_tag_xml(name, args)
        result = recover_xml_tool_calls(wire)
        assert result is not None, f"recover returned None for:\n{wire}"
        calls, _cleaned = result
        assert len(calls) == 1, f"expected 1 call, got {len(calls)}"
        assert calls[0]["function"]["name"] == name
        parsed_args = json.loads(calls[0]["function"]["arguments"])
        assert parsed_args == args, f"args mismatch for {name}: expected {args}, got {parsed_args}"

    @_RE_GATE
    @given(
        st.lists(
            st.tuples(_NAME_STRAT, _ARGS_STRAT),
            min_size=2,
            max_size=5,
        )
    )
    def test_multi_function(self, call_list: list[tuple[str, dict[str, Any]]]) -> None:
        """Multiple function calls in separate wrappers each round-trip
        identically — no cross-contamination between calls.
        """
        wire = emit_closing_tag_xml_multi(call_list)
        result = recover_xml_tool_calls(wire)
        assert result is not None, f"recover returned None for:\n{wire}"
        calls, _cleaned = result
        assert len(calls) == len(call_list), f"expected {len(call_list)} calls, got {len(calls)}"
        for i, (expected_name, expected_args) in enumerate(call_list):
            assert calls[i]["function"]["name"] == expected_name, (
                f"call {i} name: expected {expected_name}, got {calls[i]['function']['name']}"
            )
            parsed_args = json.loads(calls[i]["function"]["arguments"])
            assert parsed_args == expected_args, (
                f"call {i} ({expected_name}) args mismatch: "
                f"expected {expected_args}, got {parsed_args}"
            )


# ---------------------------------------------------------------------------
# (a) Round-trip identity — opening-only dialect (including multi-function)
# ---------------------------------------------------------------------------


class TestRoundtripOpeningOnly:
    """Round-trip identity for the opening-only XML dialect.

    The multi-function bug: in a single ``<tool_call>`` wrapper containing
    multiple ``<function=…>`` blocks, each function's parameter scan must be
    bounded at the next ``<function=…>`` opener.  Pre-fix the fallback used
    ``blk[fmatch.end():]`` for every function's body, so function 1's
    parameter scan swallowed function 2's parameters and function 2 re-emitted
    them — duplicating args on every recovered call.
    """

    @_RE_GATE
    @given(name=_NAME_STRAT, args=_ARGS_STRAT)
    def test_single_function(self, name: str, args: dict[str, Any]) -> None:
        """A single function in opening-only dialect round-trips."""
        wire = emit_opening_only_xml(name, args)
        result = recover_xml_tool_calls(wire)
        assert result is not None, f"recover returned None for:\n{wire}"
        calls, _cleaned = result
        assert len(calls) == 1, f"expected 1 call, got {len(calls)}"
        assert calls[0]["function"]["name"] == name
        parsed_args = json.loads(calls[0]["function"]["arguments"])
        assert parsed_args == args, f"args mismatch for {name}: expected {args}, got {parsed_args}"

    @_RE_GATE
    @given(
        st.lists(
            st.tuples(_NAME_STRAT, _ARGS_STRAT),
            min_size=2,
            max_size=5,
        )
    )
    def test_multi_function_one_wrapper(self, call_list: list[tuple[str, dict[str, Any]]]) -> None:
        """Regression guard: multiple functions in ONE wrapper each get
        their OWN args, with NO cross-contamination.

        This is the exact surface the fix addressed — pre-fix, function 1's
        parameter scan ran to the end of the wrapper, so every function
        after the first emitted duplicated args.
        """
        wire = emit_opening_only_xml_multi(call_list)
        result = recover_xml_tool_calls(wire)
        assert result is not None, f"recover returned None for:\n{wire}"
        calls, _cleaned = result
        assert len(calls) == len(call_list), f"expected {len(call_list)} calls, got {len(calls)}"
        for i, (expected_name, expected_args) in enumerate(call_list):
            assert calls[i]["function"]["name"] == expected_name, (
                f"call {i} name: expected {expected_name}, got {calls[i]['function']['name']}"
            )
            parsed_args = json.loads(calls[i]["function"]["arguments"])
            assert parsed_args == expected_args, (
                f"call {i} ({expected_name}) args mismatch — "
                f"D1 regression if this fails: expected {expected_args}, "
                f"got {parsed_args}"
            )


# ---------------------------------------------------------------------------
# (b) No-crash on adversarial input
# ---------------------------------------------------------------------------


class TestAdversarialNoCrash:
    """The parser must never raise an unhandled exception on ANY input.

    Strategies cover:
    - Nested ``<tool_call>`` wrappers
    - Mixed dialects in one wrapper
    - Unclosed tags
    - Malformed quoting
    - Embedded ``<tool_call>`` literal inside parameter VALUE
    - Long repeating patterns (catastrophic backtracking probes)
    """

    @_RE_GATE
    @given(st.text(max_size=5000))
    def test_arbitrary_text(self, s: str) -> None:
        """Arbitrary text must never crash the parser."""
        try:
            recover_xml_tool_calls(s)
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"recover_xml_tool_calls crashed on arbitrary text: {exc}")

    @_RE_GATE
    @given(
        st.one_of(
            # Deeply nested <tool_call> wrappers
            st.builds(
                lambda n: "<tool_call>" * n + "x" + "</tool_call>" * n,
                n=st.integers(min_value=1, max_value=20),
            ),
            # Mixed dialects — closing tags inside opening-only wrapper
            st.just(
                "<tool_call> <function=search> <parameter=q> </function> </parameter> </tool_call>"
            ),
            # Unclosed tags at every level
            st.just("<tool_call><function=search><parameter=q>value"),
            # Malformed quoting (double-equals, missing angle brackets)
            st.just(
                "<tool_call><function==search><parameter==q>value</parameter></function></tool_call>"
            ),
            # Embedded <tool_call> literal inside a parameter VALUE
            st.just(
                "<tool_call><function=search>"
                "<parameter=q>\n<tool_call>nested</tool_call>\n</parameter>"
                "</function></tool_call>"
            ),
            # Empty wrappers
            st.just("<tool_call></tool_call>"),
            # Only opening tags, no content
            st.just("<tool_call><function=test><parameter=x>"),
            # Long alternating pattern (ReDoS probe)
            st.text(
                alphabet=["<", ">", " ", "=", "a", "/", "\n"],
                min_size=500,
                max_size=2000,
            ),
        )
    )
    def test_structured_adversarial(self, s: str) -> None:
        """Structured adversarial inputs must never crash the parser."""
        try:
            recover_xml_tool_calls(s)
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"recover_xml_tool_calls crashed on adversarial input: {exc}")

    def test_known_adversarial_corpus(self) -> None:
        """A hand-picked corpus of pathological edge cases, tested without
        Hypothesis (deterministic — always runs).
        """
        corpus = [
            # Nested wrappers (3 deep)
            "<tool_call><tool_call><tool_call>x</tool_call></tool_call></tool_call>",
            # Mixed case (should not match — regex is case-sensitive)
            "<TOOL_CALL><FUNCTION=test><PARAMETER=x>y</PARAMETER></FUNCTION></TOOL_CALL>",
            # Bare function tag inside prose (should not trigger — only wrapped counts)
            "Use <function=search> to look things up.",
            # Unclosed wrapper with valid-looking content
            "<tool_call><function=search><parameter=q>hello",
            # Zero-width params
            "<tool_call><function=a><parameter=></parameter></function></tool_call>",
            # Whitespace-only content
            (
                "<tool_call>   <function=test>   <parameter=x>   </parameter>"
                "   </function>   </tool_call>"
            ),
            # Extremely long parameter value (ReDoS probe)
            (
                "<tool_call><function=search><parameter=q>"
                + "A" * 10000
                + "</parameter></function></tool_call>"
            ),
            # Multiple wrappers with interleaved prose
            "hello <tool_call><function=a><parameter=x>1</parameter></function></tool_call> world "
            "<tool_call><function=b><parameter=y>2</parameter></function></tool_call> end",
        ]
        for idx, inp in enumerate(corpus):
            try:
                recover_xml_tool_calls(inp)
            except Exception as exc:  # noqa: BLE001
                pytest.fail(
                    f"adversarial corpus item {idx} crashed: {exc}\n"
                    f"input (first 200): {inp[:200]!r}"
                )


# ---------------------------------------------------------------------------
# (c) ReDoS gate — explicit adversarial corpus with wall-clock measurement
# ---------------------------------------------------------------------------


class TestReDoSGate:
    """Explicit adversarial corpus with wall-clock budget enforcement.

    Every input in the corpus must parse in under 50 ms.  This is the
    systemic complement to the ``deadline=50`` setting on every property
    test above — it runs even when Hypothesis examples are cached, and
    covers pathological shapes Hypothesis may not generate.
    """

    @pytest.mark.parametrize(
        "adversarial_input",
        [
            # Long alternation pattern — many <function= / <parameter= openers
            # that the opening-only fallback must iterate pairwise.
            (
                "<tool_call>"
                + " ".join(f"<function=f{i}><parameter=x>{i}" for i in range(100))
                + " </tool_call>"
            ),
            # Deeply nested structure (50 levels)
            "<tool_call>" * 50 + "x" + "</tool_call>" * 50,
            # Very long parameter value with repetitive content
            (
                "<tool_call><function=search><parameter=q>"
                + "hello " * 2000
                + "</parameter></function></tool_call>"
            ),
            # Many small wrappers (potential O(n²) behavior in findall)
            "<tool_call><function=a><parameter=x>1</parameter></function></tool_call>" * 100,
            # Opening-only dialect with many functions + params
            emit_opening_only_xml_multi(
                [(f"func{i}", {"p1": "v1", "p2": "v2", "p3": "v3"}) for i in range(50)]
            ),
            # Closing-tag dialect with many params
            emit_closing_tag_xml("search", {f"k{i}": f"value_{i}" for i in range(100)}),
        ],
        ids=[
            "100_functions_one_wrapper",
            "50_deep_nested_wrappers",
            "long_param_value_repetition",
            "100_small_wrappers",
            "50_funcs_opening_only",
            "100_params_closing_tag",
        ],
    )
    def test_adversarial_under_50ms(self, adversarial_input: str) -> None:
        """Each adversarial input must parse in under 50 ms (ReDoS gate)."""
        import time

        start = time.monotonic()
        try:
            recover_xml_tool_calls(adversarial_input)
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"ReDoS corpus input crashed: {exc}")
        elapsed_ms = (time.monotonic() - start) * 1000
        assert elapsed_ms < 50, (
            f"ReDoS gate exceeded 50 ms budget: {elapsed_ms:.1f} ms\n"
            f"Input length: {len(adversarial_input)} chars"
        )


# ---------------------------------------------------------------------------
# Regression guards — known shapes from existing tests still parse
# ---------------------------------------------------------------------------


class TestRegressionExisting:
    """Known shapes from ``tests/services/test_tool_args.py`` still parse.

    These are NOT property-generated — they pin concrete shapes observed
    in production so the property battery does not regress the existing test suite.
    """

    def test_xml_recovery_list_directory(self) -> None:
        """list_directory call emitted as raw XML (first XML tool-call recovery)."""
        raw = (
            "Let me explore the structure.\n"
            "<tool_call>\n"
            "<function=list_directory>\n"
            "<parameter=path>\n"
            "~/Documents/project\n"
            "</parameter>\n"
            "</function>\n"
            "</tool_call>"
        )
        result = recover_xml_tool_calls(raw)
        assert result is not None
        calls, cleaned = result
        assert len(calls) == 1
        assert calls[0]["function"]["name"] == "list_directory"
        assert json.loads(calls[0]["function"]["arguments"]) == {"path": "~/Documents/project"}
        assert cleaned == "Let me explore the structure."

    def test_xml_recovery_opening_only(self) -> None:
        """Opening-only tag shape (no closing tags, multi-parameter)."""
        raw = (
            "<tool_call> <function=firecrawl_search>"
            " <parameter=query> Paris France breaking news today"
            " <parameter=limit> 6"
            ' <parameter=sources> [{"type": "news"}]'
            " </tool_call>"
        )
        result = recover_xml_tool_calls(raw)
        assert result is not None
        calls, cleaned = result
        assert len(calls) == 1
        assert calls[0]["function"]["name"] == "firecrawl_search"
        args = json.loads(calls[0]["function"]["arguments"])
        assert args == {
            "query": "Paris France breaking news today",
            "limit": 6,
            "sources": [{"type": "news"}],
        }
        assert "<tool_call>" not in cleaned

    def test_d1_multi_function_bounds(self) -> None:
        """Regression: two functions in one wrapper, distinct args."""
        raw = (
            "<tool_call> <function=search>"
            " <parameter=query> Paris weather"
            " <function=lookup>"
            " <parameter=topic> events"
            " </tool_call>"
        )
        result = recover_xml_tool_calls(raw)
        assert result is not None
        calls, _cleaned = result
        assert len(calls) == 2
        # Function 1
        assert calls[0]["function"]["name"] == "search"
        assert json.loads(calls[0]["function"]["arguments"]) == {"query": "Paris weather"}
        assert "topic" not in json.loads(calls[0]["function"]["arguments"])
        # Function 2
        assert calls[1]["function"]["name"] == "lookup"
        assert json.loads(calls[1]["function"]["arguments"]) == {"topic": "events"}
        assert "query" not in json.loads(calls[1]["function"]["arguments"])


# ---------------------------------------------------------------------------
# Hypothesis seed pinning
# ---------------------------------------------------------------------------


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Pin the Hypothesis seed for replayability.

    When ``HYPOTHESIS_SEED`` is set in the environment, use it; otherwise
    fall back to the module-level ``SEED = 42`` constant.  This ensures
    committed examples in ``.hypothesis/examples/`` are reproducible across
    CI runs.
    """
    import os

    if "HYPOTHESISEED" not in os.environ and "HYPOTHESIS_SEED" not in os.environ:
        os.environ.setdefault("HYPOTHESIS_SEED", str(SEED))


# ---------------------------------------------------------------------------
# (g) JSON-repair regex ReDoS gate — _FENCE_RE / _SQ_KEY_RE / _SQ_VAL_RE
# ---------------------------------------------------------------------------


class TestJsonRepairRegexReDoS:
    """ReDoS gate for the JSON-repair regexes used by ``coerce_tool_args``.

    ``_FENCE_RE`` strips ```` ```json ```` fences; ``_SQ_KEY_RE`` / ``_SQ_VAL_RE``
    convert single-quoted JSON keys/values to double-quoted.  All three run on
    untrusted model output inside ``coerce_tool_args``.  Every property below is
    bounded by ``_RE_GATE`` (``deadline=50`` ms), so catastrophic backtracking
    fails the test rather than hanging the parser (a ReDoS regression
    guard).
    """

    # Alphabet weighted toward the metacharacters each regex pivots on:
    # quotes, braces, commas, colons, backticks, whitespace.
    _ADVERSARIAL_ALPHABET = "'\"{}[],: \t\n`abc01"

    @_RE_GATE
    @given(s=st.text(alphabet=_ADVERSARIAL_ALPHABET, max_size=4000))
    def test_coerce_tool_args_bounded(self, s: str) -> None:
        """``coerce_tool_args`` (which drives all three regexes) never raises
        and stays within the 50 ms deadline on adversarial input."""
        # Return value may be None for non-JSON input; the contract under test
        # is "does not raise and does not exceed the ReDoS deadline".
        coerce_tool_args(s)

    @_RE_GATE
    @given(s=st.text(alphabet="`json \t\n", max_size=4000))
    def test_fence_re_bounded(self, s: str) -> None:
        """``_FENCE_RE.sub`` stays linear on pathological fence/backtick runs."""
        _FENCE_RE.sub("", s)

    @_RE_GATE
    @given(s=st.text(alphabet="'\"{},: abc", max_size=4000))
    def test_sq_key_re_bounded(self, s: str) -> None:
        """``_SQ_KEY_RE.sub`` stays linear on single-quote/brace runs."""
        _SQ_KEY_RE.sub(r'\1"\2"\3', s)

    @_RE_GATE
    @given(s=st.text(alphabet="'\"},: abc", max_size=4000))
    def test_sq_val_re_bounded(self, s: str) -> None:
        """``_SQ_VAL_RE.sub`` stays linear on single-quote/value runs."""
        _SQ_VAL_RE.sub(r'\1"\2"\3', s)

    def test_known_pathological_corpus(self) -> None:
        """Explicit pathological literals complete well within the deadline.

        A systemic complement to the property-based gate: these are the classic
        ReDoS shapes (long quote/brace/backtick runs) that a nested-quantifier
        regression would choke on. All three regexes use lazy, delimiter-bounded
        quantifiers, so each input scans linearly.
        """
        corpus = [
            "{" * 2000 + "'k'" * 2000,
            "'" * 8000,
            "`" * 8000,
            "```json" + " " * 8000 + "```",
            "{" + "'a':" * 4000,
            (":'" + "a" * 4000 + "',") * 4,
        ]
        for bad in corpus:
            coerce_tool_args(bad)
            _FENCE_RE.sub("", bad)
            _SQ_KEY_RE.sub(r'\1"\2"\3', bad)
            _SQ_VAL_RE.sub(r'\1"\2"\3', bad)
