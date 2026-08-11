# SPDX-License-Identifier: Apache-2.0
"""Tests for the shared content → reasoning_content OOB text primitive."""
from __future__ import annotations

from lmchat.lmstudio.oob_text import oob_message_text


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
