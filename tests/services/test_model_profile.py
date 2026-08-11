# SPDX-License-Identifier: Apache-2.0
"""Tests for the ModelProfile registry — §1.2.

LMSTUDIO-MULTIMODEL-HARDENING-PLAN-2026-06-06 §1.2 acceptance.
"""
from __future__ import annotations

import pytest

from lmchat.services.model_profile import (
    DEFAULT_PROFILE,
    PROFILE_DEEPSEEK_R1_DISTILL,
    PROFILE_NEMOTRON_CASCADE_2,
    PROFILE_QWEN_DISTILL,
    ModelProfile,
    resolve_profile,
)


@pytest.mark.parametrize(
    "model_id,expected",
    [
        ("nemotron-cascade-2-30b-a3b", PROFILE_NEMOTRON_CASCADE_2),
        ("Qwen/Qwen3.6-35B-A3B-MLX", PROFILE_QWEN_DISTILL),
        ("qwen3.5-122b-a10b-claude-distill-v2-i1", PROFILE_QWEN_DISTILL),
        ("deepseek-r1-distill-7b", PROFILE_DEEPSEEK_R1_DISTILL),
        ("llama-3.3-70b-instruct", DEFAULT_PROFILE),
        ("", DEFAULT_PROFILE),
    ],
)
def test_resolve_profile(model_id: str, expected: ModelProfile) -> None:
    assert resolve_profile(model_id) is expected


def test_resolve_profile_none_returns_default() -> None:
    assert resolve_profile(None) is DEFAULT_PROFILE


def test_default_profile_is_safe() -> None:
    p = DEFAULT_PROFILE
    assert p.max_tokens is None
    assert p.strip_reasoning_from_history is False
    assert p.substance_fold is True
    assert p.sampler_family == "none"


def test_quirk_profiles_strip_reasoning() -> None:
    """Every known-quirk profile strips reasoning_content from history."""
    for p in (
        PROFILE_NEMOTRON_CASCADE_2,
        PROFILE_QWEN_DISTILL,
        PROFILE_DEEPSEEK_R1_DISTILL,
    ):
        assert p.strip_reasoning_from_history is True


def test_modelprofile_is_frozen() -> None:
    """ModelProfile is frozen — accidental in-place mutation raises."""
    with pytest.raises((AttributeError, TypeError)):
        DEFAULT_PROFILE.max_tokens = 8192  # type: ignore[misc]


def test_resolve_profile_is_case_insensitive() -> None:
    """Uppercase model_id substrings resolve to the same row."""
    assert resolve_profile("QWEN3.6-35B-A3B") is PROFILE_QWEN_DISTILL
    assert resolve_profile("NEMOTRON-CASCADE-2-anything") is PROFILE_NEMOTRON_CASCADE_2
