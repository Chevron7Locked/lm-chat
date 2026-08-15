# SPDX-License-Identifier: Apache-2.0
"""Tests for the shared content → reasoning_content OOB text primitive."""
from __future__ import annotations

from lmchat.lmstudio.oob_text import oob_message_text, oob_salvage


def test_prefers_content_when_present() -> None:
    assert (
        oob_message_text({"content": "final answer", "reasoning_content": "thinking"})
        == "final answer"
    )


def test_falls_back_to_reasoning_when_content_empty() -> None:
    # The reasoning-model case: content empty, answer parked in reasoning.
    assert (
        oob_message_text({"content": "", "reasoning_content": "the salvaged answer"})
        == "the salvaged answer"
    )


def test_falls_back_when_content_is_whitespace_only() -> None:
    assert (
        oob_message_text({"content": "   \n ", "reasoning_content": "salvaged"})
        == "salvaged"
    )


def test_falls_back_when_content_key_missing() -> None:
    assert oob_message_text({"reasoning_content": "only reasoning"}) == "only reasoning"


def test_falls_back_when_content_is_none() -> None:
    assert (
        oob_message_text({"content": None, "reasoning_content": "reasoning here"})
        == "reasoning here"
    )


def test_empty_when_both_absent_or_none() -> None:
    assert oob_message_text({}) == ""
    assert oob_message_text({"content": None, "reasoning_content": None}) == ""
    assert oob_message_text({"content": "", "reasoning_content": ""}) == ""


def test_strips_surrounding_whitespace() -> None:
    assert oob_message_text({"content": "  padded answer  "}) == "padded answer"


def test_scenario_agnostic_no_model_id_dependency() -> None:
    # The primitive keys only off which field is populated — no model id.
    for cid in ("ornith-1.0-35b", "general", "some-future-model"):
        msg = {"model": cid, "content": "", "reasoning_content": f"ans for {cid}"}
        assert oob_message_text(msg) == f"ans for {cid}"


# ─── oob_salvage — the generic content → reasoning_content primitive ──────


def test_oob_salvage_uses_content_when_extraction_succeeds() -> None:
    msg = {"content": "final answer", "reasoning_content": "thinking"}
    assert oob_salvage(msg, str.strip) == "final answer"


def test_oob_salvage_falls_back_when_content_field_is_empty() -> None:
    msg = {"content": "", "reasoning_content": "salvaged"}
    assert oob_salvage(msg, str.strip) == "salvaged"


def test_oob_salvage_falls_back_when_content_extraction_yields_nothing() -> None:
    """The load-bearing distinction from a plain "field empty" fallback:
    ``content`` is NON-empty here, but the extractor finds nothing useful
    in it — the primitive must still try ``reasoning_content``, not stop
    just because ``content`` had SOME text in it."""

    def _digits_only(text: str) -> str:
        return "".join(ch for ch in text if ch.isdigit())

    msg = {"content": "no numbers here", "reasoning_content": "the answer is 42"}
    assert oob_salvage(msg, _digits_only) == "42"


def test_oob_salvage_returns_extractors_empty_value_when_both_fail() -> None:
    msg = {"content": "nope", "reasoning_content": "also nope"}
    assert oob_salvage(msg, lambda t: [w for w in t.split() if w == "found"]) == []


def test_oob_salvage_works_with_list_extractor() -> None:
    """T need not be str — any type the extractor returns works, as long
    as its "nothing found" value is falsy."""

    def _extract_upper_words(text: str) -> list[str]:
        return [w for w in text.split() if w.isupper()]

    msg = {"content": "no shouting", "reasoning_content": "FINAL DECISION reached"}
    assert oob_salvage(msg, _extract_upper_words) == ["FINAL", "DECISION"]


def test_oob_salvage_works_with_optional_extractor() -> None:
    """T can be Optional — the falsy check treats None as empty, matching
    _last_valid_mode_id's str | None contract."""
    vocab = {"apple", "banana"}

    def _extract_one_of(text: str) -> str | None:
        for word in text.split():
            if word in vocab:
                return word
        return None

    msg = {"content": "unrelated chatter", "reasoning_content": "the pick is apple"}
    assert oob_salvage(msg, _extract_one_of) == "apple"


def test_oob_salvage_missing_fields_treated_as_empty() -> None:
    assert oob_salvage({}, str.strip) == ""
