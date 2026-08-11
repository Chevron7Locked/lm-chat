# SPDX-License-Identifier: Apache-2.0
"""Pure-function tests for ``_sanitise_generated_title``.

Spec lmchat-A-autotitle-verify §AC14-AC22. Every branch of
``_sanitise_generated_title`` is pinned here so a future refactor cannot
regress the cleanup behavior model-emitted titles depend on.
"""
from __future__ import annotations

from lmchat.services.chat_service import (
    _AUTO_TITLE_MAX_CHARS,
    _AUTO_TITLE_SYSTEM_PROMPT,
    _AUTO_TITLE_USER_INSTRUCTION,
    _salvage_title_from_reasoning,
    _sanitise_generated_title,
)


class TestSalvageTitleFromReasoning:
    """Regression (live dogfood 2026-07-18): a reasoning bg model may leave the
    title call's content empty with the title in reasoning_content. Recover the
    last quoted title from reasoning; return '' (safe user-message fallback) when
    there's no plausible quoted title, so it never invents a garbage title."""

    def test_recovers_last_quoted_title(self) -> None:
        assert (
            _salvage_title_from_reasoning('thinking... a good title: "Marine Biology Chat"')
            == "Marine Biology Chat"
        )

    def test_picks_the_last_quoted_candidate(self) -> None:
        assert (
            _salvage_title_from_reasoning('draft "Bad One" then settle on "Good Title"')
            == "Good Title"
        )

    def test_no_quoted_title_returns_empty(self) -> None:
        assert _salvage_title_from_reasoning("just reasoning prose, no quoted title") == ""

    def test_over_long_quote_is_not_a_title(self) -> None:
        # >80 chars is not a title (a quoted sentence in the reasoning).
        assert _salvage_title_from_reasoning('"' + "x" * 200 + '"') == ""


class TestEmptyAfterStrip:
    """AC14 — whitespace-only input returns empty string."""

    def test_whitespace_only(self) -> None:
        assert _sanitise_generated_title("   ") == ""

    def test_tabs_and_newlines(self) -> None:
        assert _sanitise_generated_title("\n\n\t  \t\n") == ""

    def test_empty_string(self) -> None:
        assert _sanitise_generated_title("") == ""


class TestFollowupsCommentStrip:
    """AC15 — the hidden ``<!--followups:[...]-->`` marker is stripped."""

    def test_marker_prefix(self) -> None:
        result = _sanitise_generated_title('<!--followups:["a","b"]-->Hello')
        assert result == "Hello"

    def test_marker_suffix(self) -> None:
        result = _sanitise_generated_title('Hello<!--followups:["a"]-->')
        assert result == "Hello"

    def test_marker_with_internal_whitespace(self) -> None:
        # The regex uses re.DOTALL — should also match newlines inside the marker.
        result = _sanitise_generated_title(
            'Title<!--followups:\n  ["a", "b"]\n-->'
        )
        assert result == "Title"


class TestCodeFenceStrip:
    """AC16 — markdown code-fence markers are stripped."""

    def test_fenced_with_lang(self) -> None:
        assert _sanitise_generated_title("```markdown\nHello```") == "Hello"

    def test_bare_fence_markers(self) -> None:
        assert _sanitise_generated_title("```\nHello\n```") == "Hello"

    def test_inline_fence_lang(self) -> None:
        assert _sanitise_generated_title("```python Hello```") == "Hello"


class TestWhitespaceCollapse:
    """AC17 — newlines collapse to spaces; runs dedupe."""

    def test_newline_to_space(self) -> None:
        assert _sanitise_generated_title("Multi\nline\ntitle") == "Multi line title"

    def test_tab_to_space(self) -> None:
        assert _sanitise_generated_title("Multi\tline\ttitle") == "Multi line title"

    def test_double_newline_collapses(self) -> None:
        assert _sanitise_generated_title("Multi\n\nline\ttitle") == "Multi line title"

    def test_run_of_spaces_dedupes(self) -> None:
        assert _sanitise_generated_title("Multi   line   title") == "Multi line title"


class TestPrefixStrip:
    """AC18 — common preamble prefixes are stripped."""

    def test_title_colon(self) -> None:
        assert _sanitise_generated_title("Title: Foo") == "Foo"

    def test_title_colon_lowercase(self) -> None:
        assert _sanitise_generated_title("title: Foo") == "Foo"

    def test_heres_the_title(self) -> None:
        assert _sanitise_generated_title("Here's the title: Foo") == "Foo"

    def test_here_is_the_title(self) -> None:
        assert _sanitise_generated_title("Here is the title: Foo") == "Foo"

    def test_chat_title_dash(self) -> None:
        assert _sanitise_generated_title("chat title - Foo") == "Foo"

    def test_here_is_a_title(self) -> None:
        assert _sanitise_generated_title("Here is a title: Foo") == "Foo"


class TestWrappingQuoteStrip:
    """AC19 — matched wrapping quotes are stripped (straight + smart)."""

    def test_straight_double_quotes(self) -> None:
        assert _sanitise_generated_title('"Foo"') == "Foo"

    def test_curly_double_quotes(self) -> None:
        assert _sanitise_generated_title("“Foo”") == "Foo"

    def test_curly_single_quotes(self) -> None:
        assert _sanitise_generated_title("‘Foo’") == "Foo"

    def test_backticks(self) -> None:
        assert _sanitise_generated_title("`Foo`") == "Foo"

    def test_straight_single_quotes(self) -> None:
        assert _sanitise_generated_title("'Foo'") == "Foo"

    def test_only_one_side_does_not_strip(self) -> None:
        # If only the opener matches, do nothing.
        assert _sanitise_generated_title('"Foo') == '"Foo'


class TestTrailingPunctuationStrip:
    """AC20 — trailing punctuation a model commonly appends is stripped."""

    def test_period(self) -> None:
        assert _sanitise_generated_title("Foo.") == "Foo"

    def test_multiple_trailing_punct(self) -> None:
        assert _sanitise_generated_title("Foo!?,;:") == "Foo"

    def test_exclamation(self) -> None:
        assert _sanitise_generated_title("Foo!") == "Foo"

    def test_question(self) -> None:
        assert _sanitise_generated_title("Foo?") == "Foo"

    def test_no_inner_punct_stripped(self) -> None:
        # Inner punctuation must survive — only trailing.
        assert _sanitise_generated_title("Foo. Bar.") == "Foo. Bar"


class TestWordBoundaryTruncation:
    """AC21 — truncated at last space within cap window; ellipsis appended."""

    def test_cap_is_80(self) -> None:
        # Confirm the constant matches the spec.
        assert _AUTO_TITLE_MAX_CHARS == 80

    def test_truncates_at_word_boundary(self) -> None:
        # 120 chars with spaces, expect truncation with ellipsis.
        raw = (
            "this is a very long generated chat title that goes well "
            "beyond the eighty character limit and should be cut"
        )
        result = _sanitise_generated_title(raw)
        assert result.endswith("…")
        # The cap counts toward the OUTPUT length including the ellipsis;
        # head is _AUTO_TITLE_MAX_CHARS-1 then ellipsis appended, so the
        # final length is ≤ _AUTO_TITLE_MAX_CHARS.
        assert len(result) <= _AUTO_TITLE_MAX_CHARS
        # Must have been cut on a space within the cap window.
        head = result[:-1]  # drop ellipsis
        assert not head.endswith(" ")  # rstrip applied
        # The last word in head must be complete (because we cut at last space).
        words = head.split()
        assert words[-1] in raw

    def test_under_cap_passes_through(self) -> None:
        assert _sanitise_generated_title("Short title") == "Short title"

    def test_no_space_within_cap_falls_back_to_hard_cut(self) -> None:
        # An 81-char single-word string has no space to cut at, so the
        # implementation falls back to the first `_AUTO_TITLE_MAX_CHARS-1`
        # chars + ellipsis.
        raw = "x" * 81
        result = _sanitise_generated_title(raw)
        assert result.endswith("…")
        assert len(result) <= _AUTO_TITLE_MAX_CHARS


class TestSmartQuotesCombinedWithPrefix:
    """AC22 — regression for the common LM Studio pattern."""

    def test_prefix_plus_quoted(self) -> None:
        # Title: "Foo Bar" — both the prefix and the quotes strip.
        assert _sanitise_generated_title('Title: "Foo Bar"') == "Foo Bar"

    def test_curly_prefix_plus_quoted(self) -> None:
        assert _sanitise_generated_title("Title: “Foo Bar”") == "Foo Bar"

    def test_followups_plus_prefix_plus_quoted(self) -> None:
        raw = '<!--followups:["a"]-->Title: "Foo Bar"'
        assert _sanitise_generated_title(raw) == "Foo Bar"


class TestInstructionEchoRejected:
    """Regression (live dogfood 2026-08-09): a reasoning/background model may echo
    the title-generation prompt verbatim instead of emitting a title (a chat was
    titled 'Write a concise 3-6 word title for this conversation. Output ONLY the
    title'). Such an echo must sanitise to '' so generate_title falls back to the
    first user message rather than storing the instruction as the title."""

    def test_exact_user_instruction_echo_rejected(self) -> None:
        assert (
            _sanitise_generated_title(
                "Write a concise 3-6 word title for this conversation. "
                "Output ONLY the title"
            )
            == ""
        )

    def test_system_prompt_echo_rejected(self) -> None:
        assert (
            _sanitise_generated_title(
                "You generate a concise 3-6 word title for a conversation. "
                "Output ONLY the title — no quotes, no explanation."
            )
            == ""
        )

    def test_output_only_fragment_rejected(self) -> None:
        assert _sanitise_generated_title("Output ONLY the title") == ""

    def test_genuine_title_containing_word_title_still_passes(self) -> None:
        # A real conversation ABOUT titles must NOT be falsely rejected — the
        # markers are specific instruction phrases, not the bare word "title".
        assert _sanitise_generated_title("Choosing a Book Title") == "Choosing a Book Title"
        assert _sanitise_generated_title("Title Case Rules") == "Title Case Rules"

    def test_live_system_prompt_echo_is_rejected(self) -> None:
        # Drift guard: feed the ACTUAL system prompt the model receives. If its
        # wording changes without the markers being updated, this goes red
        # rather than silently reintroducing the echo bug.
        assert _sanitise_generated_title(_AUTO_TITLE_SYSTEM_PROMPT) == ""

    def test_live_user_instruction_echo_is_rejected(self) -> None:
        # Drift guard for the appended user instruction — the exact string that
        # was echoed verbatim in the live dogfood incident.
        assert _sanitise_generated_title(_AUTO_TITLE_USER_INSTRUCTION) == ""
