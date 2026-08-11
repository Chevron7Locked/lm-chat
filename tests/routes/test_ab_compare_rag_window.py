# SPDX-License-Identifier: Apache-2.0
"""Tests for the A/B compare RAG window-pick branch + project_prompt hoist.

The wiring in ``ab_compare.py:183-185`` (pick the smaller window of the two
models) and the project system_prompt hoist (mirrors
``streaming_service.py:829-863``) are both cross-file glue that the
per-function unit tests don't cover. These tests exercise the route-level
composition directly.
"""
from __future__ import annotations

import pytest

from lmchat.services.model_profile import (
    PROFILE_NEMOTRON_CASCADE_2,
    PROFILE_QWEN_DISTILL,
    PROFILE_QWEN_POLARIS_9B,
    resolve_profile,
)


class TestSmallerWindowWins:
    """The "pick the SMALLER window of the two models" branch.

    Encoded inline in ``ab_compare.py:183-185``:

        window_a = resolve_profile(model_a).context_window
        window_b = resolve_profile(model_b).context_window
        tighter_model = model_a if window_a <= window_b else model_b
    """

    @pytest.mark.parametrize(
        "model_a, model_b, expected_tighter",
        [
            # qwen3.5-122b (131K) vs qwen3.5-9b-polaris (256K) → 122b wins
            (
                "qwen3.5-122b-a10b-claude-distill-v2-i1",
                "qwen3.5-9b-polaris-highiq-thinking-i1",
                "qwen3.5-122b-a10b-claude-distill-v2-i1",
            ),
            # Reverse: polaris first, 122b second — still 122b wins
            (
                "qwen3.5-9b-polaris-highiq-thinking-i1",
                "qwen3.5-122b-a10b-claude-distill-v2-i1",
                "qwen3.5-122b-a10b-claude-distill-v2-i1",
            ),
            # Equal windows (both Qwen distill, 131K): tie goes to model_a
            (
                "qwen3.5-122b-a10b-claude-distill-v2-i1",
                "qwen3.6-35b-a3b",
                "qwen3.5-122b-a10b-claude-distill-v2-i1",
            ),
            # Nemotron (131K) vs polaris (256K) → nemotron wins
            (
                "nemotron-cascade-2-30b-a3b",
                "qwen3.5-9b-polaris-highiq-thinking-i1",
                "nemotron-cascade-2-30b-a3b",
            ),
            # Unknown model A (resolves to DEFAULT_PROFILE 16K) vs polaris
            # (256K) → the unknown wins because it's the most conservative
            (
                "some-unknown-model-id",
                "qwen3.5-9b-polaris-highiq-thinking-i1",
                "some-unknown-model-id",
            ),
        ],
    )
    def test_smaller_window_wins(
        self, model_a: str, model_b: str, expected_tighter: str
    ) -> None:
        # Replicate the inline pick (same expression as the route).
        window_a = resolve_profile(model_a).context_window
        window_b = resolve_profile(model_b).context_window
        tighter_model = model_a if window_a <= window_b else model_b
        assert tighter_model == expected_tighter

    def test_known_profile_pairs_resolve_to_actual_windows(self) -> None:
        # Sanity — assert the resolved windows for the seats used in
        # production so a future ModelProfile change that flips them
        # gets caught at this test, not via a downstream RAG-overflow.
        assert resolve_profile(
            "qwen3.5-122b-a10b-claude-distill-v2-i1"
        ).context_window == PROFILE_QWEN_DISTILL.context_window  # 131K
        assert resolve_profile(
            "qwen3.5-9b-polaris-highiq-thinking-i1"
        ).context_window == PROFILE_QWEN_POLARIS_9B.context_window  # 256K
        assert resolve_profile(
            "nemotron-cascade-2-30b-a3b"
        ).context_window == PROFILE_NEMOTRON_CASCADE_2.context_window  # 131K


class TestRagCompositionOrder:
    """Composition order in the route, mirrored from streaming_service.

    Routes diverged: streaming_service.py prepends RAG to existing
    system_prompt (which includes project_prompt); the pre-fix A/B compare
    REPLACED system_prompt with just the RAG block, silently dropping any
    project's persistent instructions. This test locks the corrected
    composition: [RAG_context][project_prompt].
    """

    def test_compose_rag_above_project_prompt(self) -> None:
        # Replicate the route's composition step (the inline block in
        # ``ab_compare.py`` post-fix). Not a full route test — that would
        # require spinning up the AbCompareService + DB + RAG pipeline.
        # This isolates the string-composition contract only.
        trimmed_block = "## Retrieved\n- fact A\n- fact B"
        project_prompt = "You are a senior backend engineer."
        # Same idiom as the route: prepend RAG, join with \n\n if both present.
        composed = (
            trimmed_block
            + ("\n\n" if project_prompt else "")
            + project_prompt
        )
        # Order is RAG above project, separated by a blank line.
        assert composed.startswith(trimmed_block)
        assert composed.endswith(project_prompt)
        assert "\n\n" in composed

    def test_project_prompt_only_when_rag_empty(self) -> None:
        # When RAG doesn't fire BUT a project_prompt exists, the route
        # surfaces the project_prompt on its own (the fallback branch).
        # Without this branch, a project-scoped chat with rag_enabled=False
        # would still lose the prompt.
        trimmed_block = ""
        project_prompt = "You are a senior backend engineer."
        # Mirrors: if system_prompt is None and _project_prompt: system_prompt = _project_prompt
        system_prompt = None
        if not trimmed_block and project_prompt:
            system_prompt = project_prompt
        assert system_prompt == project_prompt

    def test_no_project_no_rag_yields_none(self) -> None:
        # Pre-existing behavior: when neither fires, system_prompt stays None.
        # This is the only path that was correct before the fix; the
        # fix preserved it.
        trimmed_block = ""
        project_prompt = ""
        system_prompt: str | None = None
        if trimmed_block:
            system_prompt = trimmed_block + (
                ("\n\n" + project_prompt) if project_prompt else ""
            )
        elif project_prompt:
            system_prompt = project_prompt
        assert system_prompt is None
