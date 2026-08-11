# SPDX-License-Identifier: Apache-2.0
"""Tests for the tolerant tool-call argument coercer.

Mirrors the reference implementation ``test_tool_args.py`` shape — the four steps of
:func:`lmchat.services.tool_args.coerce_tool_args` each get a dedicated
test plus the integration cases that motivated the port.
"""
from __future__ import annotations

import json

from lmchat.services.tool_args import (
    coerce_tool_args,
    find_json_object,
    recover_xml_tool_calls,
    repair_truncated_json,
)


class TestStrictPath:
    """Step 1: a well-formed dict-string parses without any repair."""

    def test_strict_object(self) -> None:
        assert coerce_tool_args('{"q": "hi"}') == {"q": "hi"}

    def test_already_a_dict(self) -> None:
        assert coerce_tool_args({"already": "dict"}) == {"already": "dict"}

    def test_empty_string_is_empty_dict(self) -> None:
        # The accumulator's existing pre-check returns {} on empty raw; the
        # coercer matches that semantic for callers that route empty through.
        assert coerce_tool_args("") == {}

    def test_whitespace_only_is_empty_dict(self) -> None:
        assert coerce_tool_args("   \n  ") == {}

    def test_non_string_non_dict_returns_none(self) -> None:
        # The accumulator only sends string/dict, but the contract is
        # explicit — anything else fails closed, not a TypeError.
        assert coerce_tool_args(42) is None
        assert coerce_tool_args(None) is None
        assert coerce_tool_args([1, 2]) is None


class TestStep2CodeFenceAndTrailingProse:
    """Step 2: strip ``` fences and trim trailing prose via balanced-object scan."""

    def test_fenced_json(self) -> None:
        raw = "```json\n{\"q\": \"hi\"}\n```"
        assert coerce_tool_args(raw) == {"q": "hi"}

    def test_fenced_no_lang_marker(self) -> None:
        raw = "```\n{\"q\": \"hi\"}\n```"
        assert coerce_tool_args(raw) == {"q": "hi"}

    def test_trailing_prose_after_object(self) -> None:
        # 9b polaris regularly appends a sentence after the tool-call JSON.
        raw = '{"q": "hi"} -- here is the query'
        assert coerce_tool_args(raw) == {"q": "hi"}

    def test_nested_braces_balanced(self) -> None:
        raw = '{"outer": {"inner": 1}} extra'
        assert coerce_tool_args(raw) == {"outer": {"inner": 1}}

    def test_braces_inside_string_dont_unbalance(self) -> None:
        raw = '{"q": "the } closer is inside"}'
        assert coerce_tool_args(raw) == {"q": "the } closer is inside"}

    def test_escaped_quote_inside_string(self) -> None:
        raw = r'{"q": "he said \"hi\""}'
        assert coerce_tool_args(raw) == {"q": 'he said "hi"'}


class TestStep3SingleQuoteFlip:
    """Step 3: flip single-quoted keys + values to double-quoted, then retry."""

    def test_single_quoted_keys(self) -> None:
        raw = "{'q': \"hi\"}"
        assert coerce_tool_args(raw) == {"q": "hi"}

    def test_single_quoted_values(self) -> None:
        raw = "{\"q\": 'hi'}"
        assert coerce_tool_args(raw) == {"q": "hi"}

    def test_both_keys_and_values_single_quoted(self) -> None:
        raw = "{'q': 'hi', 'n': 5}"
        assert coerce_tool_args(raw) == {"q": "hi", "n": 5}


class TestStep4TruncationRepair:
    """Step 4: ``repair_truncated_json`` — close unclosed strings + open brackets,
    or trim back to last complete pair. The motivating failure mode."""

    def test_unclosed_string(self) -> None:
        raw = '{"query": "what is the late'
        assert coerce_tool_args(raw) == {"query": "what is the late"}

    def test_unclosed_object(self) -> None:
        raw = '{"a": 1, "b": 2'
        assert coerce_tool_args(raw) == {"a": 1, "b": 2}

    def test_unclosed_nested(self) -> None:
        raw = '{"outer": {"inner": "val"'
        assert coerce_tool_args(raw) == {"outer": {"inner": "val"}}

    def test_dangling_key_colon_trims_to_last_pair(self) -> None:
        # The 2026-06-07 motivating shape: model truncated mid-key after the
        # comma. Attempt 1 (close-strings-and-brackets) would produce
        # ``{"a": 1, "b":}`` which is invalid JSON; attempt 2 trims back to
        # the last complete pair and closes.
        raw = '{"a": 1, "b":'
        assert coerce_tool_args(raw) == {"a": 1}

    def test_dangling_key_only_trims_to_last_pair(self) -> None:
        raw = '{"a": 1, "b"'
        assert coerce_tool_args(raw) == {"a": 1}

    def test_garbage_returns_none(self) -> None:
        assert coerce_tool_args("not json at all") is None

    def test_unbalanced_unrecoverable_returns_none(self) -> None:
        # Nothing repair can do — no opening brace at all.
        assert coerce_tool_args("just a closing} brace") is None


class TestFindJsonObject:
    """The balanced-object scanner used by step 2."""

    def test_simple_object(self) -> None:
        assert find_json_object('{"a": 1}') == '{"a": 1}'

    def test_drops_leading_prose(self) -> None:
        assert find_json_object('let me think… {"a": 1}') == '{"a": 1}'

    def test_drops_trailing_prose(self) -> None:
        assert find_json_object('{"a": 1} and then…') == '{"a": 1}'

    def test_array_top_level(self) -> None:
        assert find_json_object("[1, 2, 3]") == "[1, 2, 3]"

    def test_no_object_returns_none(self) -> None:
        assert find_json_object("no braces here") is None

    def test_unbalanced_returns_none(self) -> None:
        # No closing brace ever — scan exhausts.
        assert find_json_object('{"a": 1') is None


class TestRepairTruncatedJson:
    """Direct tests of the qwen-code-adapted repair shape."""

    def test_no_brace_at_all_returns_none(self) -> None:
        assert repair_truncated_json("garbage") is None

    def test_already_balanced_passes_through(self) -> None:
        out = repair_truncated_json('{"a": 1}')
        assert out is not None
        assert json.loads(out) == {"a": 1}

    def test_unclosed_string_then_brace(self) -> None:
        out = repair_truncated_json('{"a": "open')
        assert out is not None
        assert json.loads(out) == {"a": "open"}


class TestIntegration2026_06_07:
    """The shapes that motivated this port.

    LM Studio's ``tool_format_generation_error`` after the 9b polaris model
    ran two successful firecrawl tool calls and started a malformed third.
    The exact failure mode varies — sometimes a code fence, sometimes a
    truncation, sometimes both."""

    def test_truncated_after_two_successful_calls(self) -> None:
        # Mimics the 2026-06-07 incident shape: a third tool call that ran
        # out of structured-output discipline.
        raw = '{"query": "openai latest model release date'
        result = coerce_tool_args(raw)
        assert result == {"query": "openai latest model release date"}

    def test_fenced_and_quoted_mix(self) -> None:
        raw = "```json\n{'query': 'something'}\n```"
        assert coerce_tool_args(raw) == {"query": "something"}


class TestRecoverXmlToolCalls:
    """The XML tool-call recovery — for any Qwen3-Coder-derived model that
    emits ``<tool_call><function=...>`` inside ``message.delta`` content when
    LM Studio's native parser misses it (the 9b polaris was the first model
    observed emitting a list_directory call as raw XML)."""

    def test_no_wrapper_returns_none(self) -> None:
        # Plain answer with no tool-call XML — no recovery, no cost.
        assert recover_xml_tool_calls("Just a regular answer.") is None

    def test_empty_returns_none(self) -> None:
        assert recover_xml_tool_calls("") is None

    def test_bare_function_tag_without_wrapper_does_not_trigger(self) -> None:
        # A chat message that QUOTES tool-call syntax (e.g. documentation)
        # must NOT false-trigger. Only wrapped <tool_call> counts.
        bare = 'Use <function=foo> syntax to call tools.'
        assert recover_xml_tool_calls(bare) is None

    def test_xml_recovery_list_directory(self) -> None:
        # Pinned shape: list_directory call emitted as raw XML by a local model.
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
        args = json.loads(calls[0]["function"]["arguments"])
        assert args == {"path": "~/Documents/project"}
        # Surrounding prose survives; the XML block is stripped.
        assert cleaned == "Let me explore the structure."

    def test_multiple_calls_in_one_content(self) -> None:
        raw = (
            "<tool_call><function=read_file>\n"
            "<parameter=path>\na.txt\n</parameter>\n"
            "</function></tool_call>"
            "<tool_call><function=read_file>\n"
            "<parameter=path>\nb.txt\n</parameter>\n"
            "</function></tool_call>"
        )
        result = recover_xml_tool_calls(raw)
        assert result is not None
        calls, _cleaned = result
        assert len(calls) == 2
        assert calls[0]["function"]["name"] == "read_file"
        assert json.loads(calls[0]["function"]["arguments"]) == {"path": "a.txt"}
        assert json.loads(calls[1]["function"]["arguments"]) == {"path": "b.txt"}
        assert calls[0]["id"] != calls[1]["id"]

    def test_numeric_value_json_parsed(self) -> None:
        # When the value looks like JSON (number, bool, object), parse it.
        raw = (
            "<tool_call><function=search>\n"
            "<parameter=limit>\n5\n</parameter>\n"
            "<parameter=safe>\ntrue\n</parameter>\n"
            "</function></tool_call>"
        )
        result = recover_xml_tool_calls(raw)
        assert result is not None
        calls, _cleaned = result
        args = json.loads(calls[0]["function"]["arguments"])
        assert args == {"limit": 5, "safe": True}

    def test_string_value_with_intentional_whitespace_survives(self) -> None:
        # Matching vLLM's qwen3coder parser exactly: strip ONE newline only,
        # so a string param keeps any other surrounding whitespace.
        raw = (
            "<tool_call><function=greet>\n"
            "<parameter=name>\n  with leading spaces  \n</parameter>\n"
            "</function></tool_call>"
        )
        result = recover_xml_tool_calls(raw)
        assert result is not None
        calls, _ = result
        args = json.loads(calls[0]["function"]["arguments"])
        assert args == {"name": "  with leading spaces  "}

    def test_opening_only_dialect_operator_observed_2026_06_11(self) -> None:
        """Live 2026-06-11: ``qwen3.5-9b-polaris-highiq-thinking``
        emitted an opening-only XML variant after a successful first
        firecrawl_search round. No ``</function>``, no ``</parameter>`` —
        parameter values run inline until the next ``<parameter=`` opener
        or the ``</tool_call>`` wrapper close.

        The closing-tag variant regex doesn't match this dialect, so before
        this fix the XML leaked into displayed content as plain text and
        the turn ended without a real reply. The recovery's fallback
        opening-only path now catches it.
        """
        # The exact shape pasted (one tool call, three params,
        # last param has a JSON object value).
        raw = (
            "<tool_call> <function=firecrawl_search>"
            " <parameter=query> Paris France breaking news today"
            " <parameter=limit> 6"
            " <parameter=sources> [{\"type\": \"news\"}]"
            " </tool_call>"
        )
        result = recover_xml_tool_calls(raw)
        assert result is not None, (
            "opening-only XML variant must recover (reported case)"
        )
        calls, cleaned = result
        assert len(calls) == 1
        assert calls[0]["function"]["name"] == "firecrawl_search"
        args = json.loads(calls[0]["function"]["arguments"])
        # String values trimmed; numeric values JSON-parsed; nested JSON
        # value JSON-parsed.
        assert args == {
            "query": "Paris France breaking news today",
            "limit": 6,
            "sources": [{"type": "news"}],
        }
        # Wrapper stripped from displayed content.
        assert "<tool_call>" not in cleaned
        assert "<function=" not in cleaned

    def test_opening_only_dialect_multi_function_per_wrapper(self) -> None:
        """In the opening-only dialect, each function's parameter scan MUST
        be bounded at the next ``<function=`` opener, not run to the end
        of the wrapper.

        Pre-fix the fallback used ``blk[fmatch.end():]`` for every
        function's body, so in a multi-function wrapper function 1's
        parameter scan swallowed function 2's parameters AND function 2
        re-emitted them — duplicating args across both calls. Latent in
        lm-chat (we've only observed single-function emissions), but a
        real bug for the reference implementation back-port whose dispatcher
        executes recovered calls. This test pins the fix.
        """
        # Two distinct functions in one wrapper with DIFFERENT params.
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
        # Function 1: search(query=...) ONLY — no topic.
        assert calls[0]["function"]["name"] == "search"
        args0 = json.loads(calls[0]["function"]["arguments"])
        assert args0 == {"query": "Paris weather"}
        assert "topic" not in args0
        # Function 2: lookup(topic=...) ONLY — no query.
        assert calls[1]["function"]["name"] == "lookup"
        args1 = json.loads(calls[1]["function"]["arguments"])
        assert args1 == {"topic": "events"}
        assert "query" not in args1

    def test_opening_only_dialect_with_prose_around_wrapper(self) -> None:
        """The reasoning prose around the wrapper is preserved on cleanup,
        only the XML is stripped — matches the closing-tag variant's
        ``cleaned`` contract.
        """
        raw = (
            "I should use firecrawl_search to find recent news.\n\n"
            "<tool_call> <function=lookup>"
            " <parameter=topic> weather"
            " </tool_call>\n\n"
            "Let me also try a follow-up."
        )
        result = recover_xml_tool_calls(raw)
        assert result is not None
        calls, cleaned = result
        assert len(calls) == 1
        assert calls[0]["function"]["name"] == "lookup"
        args = json.loads(calls[0]["function"]["arguments"])
        assert args == {"topic": "weather"}
        # Surrounding prose preserved.
        assert "I should use firecrawl_search" in cleaned
        assert "Let me also try a follow-up." in cleaned
