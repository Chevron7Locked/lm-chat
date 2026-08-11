# SPDX-License-Identifier: Apache-2.0
"""Tests for ``substance_fold``.

Five named scenarios from the spec + a regression property: the predicate
is deterministic + total (never raises).
"""
from __future__ import annotations

import string
import unicodedata

from hypothesis import given, settings
from hypothesis import strategies as st

from lmchat.services.substance_fold import (
    STUB_CHARS,
    resolve_terminal_content,
    substance_fold,
)

_SALVAGE_PREFIX = (
    "_(reasoning surfaced because the model produced no final answer)_\n\n"
)


def _long(n: int) -> str:
    """Return a string of length n made of printable ASCII (no leading/trailing space)."""
    base = "abcdef ghijkl mnopqr stuvwx yz0123 456789 "
    return (base * (n // len(base) + 1))[:n]


# ─── Named scenarios ────────────────────────────────────────────────────────


def test_a_clean_content_no_fold() -> None:
    """(a) Clean content (>= STUB_CHARS) is preserved byte-identical."""
    content = _long(STUB_CHARS + 50)
    reasoning = "any reasoning"
    final_c, final_r = substance_fold(content, reasoning)
    assert final_c == content
    assert final_r == reasoning


def test_b_empty_content_long_reasoning_folds() -> None:
    """(b) Empty content + prose reasoning → fold; body = prefix + reasoning."""
    content = ""
    reasoning = _long(STUB_CHARS + 100)
    final_c, final_r = substance_fold(content, reasoning)
    assert final_c == _SALVAGE_PREFIX + reasoning
    assert final_r is None


def test_c_terse_real_answer_not_folded() -> None:
    """(c) A terse-but-real answer ("Done.") + long reasoning → NOT folded.

    The old rule treated any ``len(content) < 240`` body as
    a stub and dumped the reasoning onto it. "Done." is a complete answer — it
    carries real text, so it is preserved verbatim and the reasoning stays in
    its own channel. The salvage is reserved for genuinely empty content.
    """
    content = "Done."  # 5 chars, but a real answer
    reasoning = _long(STUB_CHARS + 100)  # >> 2 * 5
    final_c, final_r = substance_fold(content, reasoning)
    assert final_c == content, "terse real answer must be preserved verbatim"
    assert final_r == reasoning, "reasoning stays in its own channel, not dumped"


def test_c2_short_complete_answer_not_folded() -> None:
    """A short complete reply must NOT fold, regardless of reasoning length.

    Regression: "say hello" → the model returned
    a clean one-line greeting plus ~7 KB of grammar-deliberation reasoning. The
    old length-only stub test dumped the entire thinking process onto the reply.
    Real answer text present → keep content + reasoning untouched. (The inline
    ``<!--followups-->`` comment is incidental backward-compat realism for
    pre-decouple DB rows; the answer text alone is what blocks the fold.)
    """
    content = (
        'Hello! How\'s your Tuesday going? '
        '<!--followups:["What are you working on today?","How do you usually '
        'spend your Tuesdays?"]-->'
    )
    assert len(content) < STUB_CHARS  # would have folded under the old rule
    reasoning = _long(STUB_CHARS + 500)
    final_c, final_r = substance_fold(content, reasoning)
    assert final_c == content, "short complete answer must be preserved verbatim"
    assert final_r == reasoning, "reasoning stays in its own channel, not dumped"


def test_c3_punctuation_only_content_folds() -> None:
    """Punctuation/whitespace-only content carries no answer → fold."""
    content = "...   —  "  # no letters or digits
    reasoning = _long(STUB_CHARS + 100)
    final_c, final_r = substance_fold(content, reasoning)
    assert final_c == content.strip() + "\n\n" + _SALVAGE_PREFIX + reasoning
    assert final_r is None


def test_c4_emoji_only_answer_not_folded() -> None:
    """An emoji-only reply ("✅", "👍") is a REAL answer → NOT folded.

    Symbols (Unicode S*) count as a genuine reply — a model answering a yes/no
    question with just a thumbs-up produced an answer, and its reasoning must
    not be dumped over it.
    """
    reasoning = _long(STUB_CHARS + 200)
    for content in ("✅", "👍", "🎉🎉", "$"):
        final_c, final_r = substance_fold(content, reasoning)
        assert final_c == content, f"{content!r} is a real answer, must be kept"
        assert final_r == reasoning, f"{content!r}: reasoning stays in its channel"


def test_d_both_empty_no_fold_no_raise() -> None:
    """(d) Both fields empty — return ('', None) without raising."""
    final_c, final_r = substance_fold("", "")
    assert final_c == ""
    assert final_r is None

    # None inputs too.
    final_c, final_r = substance_fold(None, None)
    assert final_c == ""
    assert final_r is None


def test_e_duplicate_short_no_fold() -> None:
    """(e) content == reasoning (mirror server) → no fold.

    Under the revised rule the body "short answer" carries real answer text,
    so it is kept regardless of length or ratio; the bubble shows the content
    and the reasoning event stream stays intact. (The 2× ratio guard would
    also fail here since they're equal — but the answer-present check is what
    binds.)
    """
    text = "short answer"
    final_c, final_r = substance_fold(text, text)
    assert final_c == text
    assert final_r == text


# ─── Boundary regression ────────────────────────────────────────────────────


def test_strict_gt_at_stub_chars_boundary() -> None:
    """``len(reasoning) == STUB_CHARS`` does NOT fold (strict ``>``).

    Matches the reference implementation's strict ``>`` predicate; a model whose reasoning
    is exactly at the threshold is treated as too-short for substance fold.
    """
    content = ""  # stub
    reasoning = _long(STUB_CHARS)  # equal, not greater
    final_c, final_r = substance_fold(content, reasoning)
    assert final_c == ""  # no fold
    assert final_r == reasoning


def test_two_times_ratio_boundary() -> None:
    """``len(reasoning) == 2 * len(base)`` does NOT fold (strict ``>``).

    Under the revised rule the ratio guard only governs ANSWERLESS content (a
    real answer is kept regardless of length). Base is 130 punctuation chars
    (no answer, and > STUB_CHARS/2 so the ratio — not the length floor — is the
    binding constraint); reasoning of exactly 2× that (260 > STUB_CHARS) does
    not fold because ``260 > 2*130 == 260`` is False (strict).
    """
    base = "." * 130  # no answer (punctuation only); 130 > STUB_CHARS/2
    reasoning = _long(2 * len(base))  # exactly 2 * len(base) = 260, > STUB_CHARS
    final_c, final_r = substance_fold(base, reasoning)
    # No answer + reasoning > STUB_CHARS, so ONLY the strict 2× ratio prevents
    # the fold — this is the boundary that the guard is here to defend.
    assert final_c == base
    assert final_r == reasoning


# ─── resolve_terminal_content: the tool-aware terminal decision ─────────────
#
# These cover the axis substance_fold is BLIND to — whether the turn used
# tools — which is the root of the "raw reasoning surfaced as the answer" bug.


def test_rt_genuine_answer_kept_verbatim() -> None:
    """A real answer is kept byte-identical; reasoning stays in its channel."""
    r = resolve_terminal_content(
        "Here is the answer.", _long(STUB_CHARS + 100), had_tool_calls=True, tool_rounds=3
    )
    assert r.kind == "answer"
    assert r.content == "Here is the answer."
    assert r.reasoning == _long(STUB_CHARS + 100)


def test_rt_tool_no_answer_is_graceful_NOT_reasoning_dump() -> None:
    """REGRESSION (reported live): a tool turn that produced no answer must get
    an actionable message, NOT the raw reasoning chain dumped as the body.

    This is the exact scenario the user hit — the model looping in reasoning,
    chat.end with empty content + long reasoning. Before the fix this surfaced
    ``_SALVAGE_PREFIX + <raw chain of thought>``.
    """
    reasoning = _long(STUB_CHARS + 500)
    r = resolve_terminal_content("", reasoning, had_tool_calls=True, tool_rounds=6)
    assert r.kind == "graceful"
    assert _SALVAGE_PREFIX not in r.content, "must NOT dump raw reasoning as the answer"
    assert "reasoning" not in r.content.lower()
    assert "rephrase" in r.content.lower() or "integrations" in r.content.lower()
    # The reasoning is preserved in its own collapsed channel, not discarded.
    assert r.reasoning == reasoning


def test_rt_tool_no_answer_does_not_assert_tool_failure() -> None:
    """The graceful message must NOT assert the tools failed.

    A tool turn that produced no answer happens whether a tool errored OR the
    model simply never converged — and a model that leaks a tool call as XML
    text is even recorded as a "failure" tool-call, so any failure-count signal
    here misfires (this was a real live finding). One honest, non-blaming
    message covers both causes instead of claiming the tools failed.
    """
    reasoning = _long(STUB_CHARS + 500)
    r = resolve_terminal_content("", reasoning, had_tool_calls=True, tool_rounds=6)
    assert r.kind == "graceful"
    assert "may have been failing" not in r.content.lower()
    assert "converge" in r.content.lower()


def test_rt_no_tools_parked_answer_still_salvages() -> None:
    """With NO tools, an empty body + long reasoning is the parked-answer case
    substance_fold exists for — surface it (original behaviour preserved)."""
    reasoning = _long(STUB_CHARS + 100)
    r = resolve_terminal_content("", reasoning, had_tool_calls=False)
    assert r.kind == "salvaged"
    assert r.content == _SALVAGE_PREFIX + reasoning
    assert r.reasoning is None


def test_rt_answer_with_loop_cut_appends_note() -> None:
    """A real answer produced before a loop was cut keeps the answer + a note."""
    r = resolve_terminal_content(
        "Partial finding.",
        _long(STUB_CHARS + 100),
        had_tool_calls=True,
        tool_rounds=4,
        loop_cut_reason="repeat_loop",
    )
    assert r.kind == "answer_capped"
    assert r.content.startswith("Partial finding.")
    assert "kept repeating the same tool call" in r.content
    assert "4 tool calls" in r.content


def test_rt_loop_cut_empty_uses_reason_wording() -> None:
    """Loop cut with no partial answer → graceful message carrying the reason."""
    r = resolve_terminal_content(
        "",
        _long(STUB_CHARS + 100),
        had_tool_calls=True,
        tool_rounds=5,
        loop_cut_reason="failure_streak",
    )
    assert r.kind == "graceful"
    assert "kept failing on the same tool" in r.content
    assert "5 rounds" in r.content
    assert _SALVAGE_PREFIX not in r.content


def test_rt_empty_no_tools_no_reasoning_is_empty() -> None:
    """Nothing to show and no tools → an empty terminal, not a fabricated body."""
    r = resolve_terminal_content("", "", had_tool_calls=False)
    assert r.kind == "empty"
    assert r.content == ""


@given(
    content=st.one_of(st.none(), st.text(max_size=400)),
    reasoning=st.one_of(st.none(), st.text(max_size=400)),
    had_tools=st.booleans(),
    rounds=st.integers(min_value=0, max_value=99),
    cut=st.one_of(
        st.none(), st.sampled_from(["repeat_loop", "failure_streak", "tool_loop_cap"])
    ),
)
@settings(max_examples=200, deadline=None)
def test_rt_total_never_raises(
    content: str | None,
    reasoning: str | None,
    had_tools: bool,
    rounds: int,
    cut: str | None,
) -> None:
    """resolve_terminal_content is total — never raises, always returns a str body."""
    r = resolve_terminal_content(
        content, reasoning, had_tool_calls=had_tools, tool_rounds=rounds, loop_cut_reason=cut
    )
    assert isinstance(r.content, str)
    assert r.reasoning is None or isinstance(r.reasoning, str)
    assert r.kind in {"answer", "answer_capped", "graceful", "salvaged", "empty"}


# ─── Property: total + deterministic ────────────────────────────────────────


@given(
    content=st.one_of(
        st.none(),
        st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=600),
    ),
    reasoning=st.one_of(
        st.none(),
        st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=600),
    ),
)
@settings(max_examples=200, deadline=None)
def test_total_never_raises(content: str | None, reasoning: str | None) -> None:
    """``substance_fold`` is total on (str | None, str | None) — never raises."""
    final_c, final_r = substance_fold(content, reasoning)
    assert isinstance(final_c, str)
    assert final_r is None or isinstance(final_r, str)


@given(
    content=st.text(alphabet=string.printable, min_size=0, max_size=400),
    reasoning=st.text(alphabet=string.printable, min_size=0, max_size=400),
)
@settings(max_examples=200, deadline=None)
def test_deterministic(content: str, reasoning: str) -> None:
    """Same input → same output. No internal mutable state."""
    a = substance_fold(content, reasoning)
    b = substance_fold(content, reasoning)
    assert a == b


@given(
    content=st.text(alphabet=string.printable, min_size=0, max_size=400),
    reasoning=st.text(alphabet=string.printable, min_size=0, max_size=400),
)
@settings(max_examples=200, deadline=None)
def test_fold_decision_matches_explicit_predicate(
    content: str,
    reasoning: str,
) -> None:
    """Whether the fold fires is a pure function of the spec's predicate.

    The predicate folds only when the body has NO real
    answer — empty / whitespace / punctuation-only — never merely when it is
    short. ``expected_fold`` re-derives that independently (its own regex for
    "has a letter or digit") rather than importing the impl's ``_HAS_ANSWER``,
    so the test is a real oracle and not a tautology.
    """
    base = content.strip()
    extra = reasoning.strip()
    # Independent re-derivation of "has a real answer" (Letter/Number/Symbol/
    # Mark) — NOT importing the impl's helper, so a category drift in the impl
    # is caught here rather than tautologically mirrored.
    base_has_answer = any(
        unicodedata.category(ch)[0] in ("L", "N", "S", "M") for ch in base
    )
    expected_fold = (
        not base_has_answer
        and len(extra) > STUB_CHARS
        and len(extra) > 2 * max(len(base), 1)
    )
    final_c, final_r = substance_fold(content, reasoning)
    folded_content_changed = final_c != (content or "")
    folded_reasoning_zeroed = final_r is None and (reasoning or "") != ""
    fold_fired = folded_content_changed or folded_reasoning_zeroed
    assert fold_fired == expected_fold
