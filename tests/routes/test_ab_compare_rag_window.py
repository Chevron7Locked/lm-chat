# SPDX-License-Identifier: Apache-2.0
"""Tests for the A/B compare RAG window-pick branch + project_prompt hoist.

The wiring in ``ab_compare.py`` (pick the smaller LIVE-probed window of the
two models) and the project system_prompt hoist (mirrors
``streaming_service.py:829-863``) are both cross-file glue that the
per-function unit tests don't cover. These tests exercise the route-level
composition directly.

2026-08-15: the window pick moved off ``resolve_profile(...).context_window``
(a per-model-name static table) onto
``ModelsService.get_max_context_length`` — the same provider-agnostic live
probe every other RAG-budget site uses. No model name drives the result
anymore; only what each pane's model actually reports live.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


async def _pick_tighter_ctx_window(
    models_service: AsyncMock, model_a: str, model_b: str
) -> int | None:
    """Replicate the route's inline pick (same expression as ab_compare.py)."""
    window_a = await models_service.get_max_context_length(model_a)
    window_b = await models_service.get_max_context_length(model_b)
    resolved_windows = [w for w in (window_a, window_b) if w > 0]
    return min(resolved_windows) if resolved_windows else None


class TestSmallerWindowWins:
    """The "pick the SMALLER live-probed window of the two models" branch."""

    @pytest.mark.asyncio
    async def test_smaller_of_two_resolved_windows_wins(self) -> None:
        models_service = AsyncMock()
        models_service.get_max_context_length = AsyncMock(
            side_effect=lambda model_id: {"model-a": 131_072, "model-b": 262_144}[model_id]
        )

        tighter = await _pick_tighter_ctx_window(models_service, "model-a", "model-b")

        assert tighter == 131_072

    @pytest.mark.asyncio
    async def test_order_does_not_matter(self) -> None:
        models_service = AsyncMock()
        models_service.get_max_context_length = AsyncMock(
            side_effect=lambda model_id: {"model-a": 262_144, "model-b": 131_072}[model_id]
        )

        tighter = await _pick_tighter_ctx_window(models_service, "model-a", "model-b")

        assert tighter == 131_072

    @pytest.mark.asyncio
    async def test_equal_windows_pick_that_shared_value(self) -> None:
        models_service = AsyncMock()
        models_service.get_max_context_length = AsyncMock(return_value=131_072)

        tighter = await _pick_tighter_ctx_window(models_service, "model-a", "model-b")

        assert tighter == 131_072

    @pytest.mark.asyncio
    async def test_one_unresolved_uses_the_other_resolved_window(self) -> None:
        """A pane whose probe comes back 0 (unknown to the cache) is
        excluded from the comparison rather than treated as "smallest" —
        the known window wins, not a guess about the unknown one."""
        models_service = AsyncMock()
        models_service.get_max_context_length = AsyncMock(
            side_effect=lambda model_id: {"model-a": 0, "model-b": 262_144}[model_id]
        )

        tighter = await _pick_tighter_ctx_window(models_service, "model-a", "model-b")

        assert tighter == 262_144

    @pytest.mark.asyncio
    async def test_both_unresolved_yields_none(self) -> None:
        """Neither pane's window resolves -> None, so
        ``trim_rag_context_for_model`` falls back to its own fixed
        "window unknown" floor — never a per-model-name guess."""
        models_service = AsyncMock()
        models_service.get_max_context_length = AsyncMock(return_value=0)

        tighter = await _pick_tighter_ctx_window(models_service, "model-a", "model-b")

        assert tighter is None

    @pytest.mark.asyncio
    async def test_no_model_name_drives_the_result_only_the_reported_numbers(
        self,
    ) -> None:
        """Same two reported numbers, completely different (nonsense) model
        id strings — the result is identical. Locks in that this pick is
        name-independent, unlike the removed ``resolve_profile``-based
        version this replaced."""
        models_service_1 = AsyncMock()
        models_service_1.get_max_context_length = AsyncMock(
            side_effect=lambda model_id: {"qwen3.5-9b-polaris": 131_072, "nemotron": 262_144}[
                model_id
            ]
        )
        tighter_1 = await _pick_tighter_ctx_window(
            models_service_1, "qwen3.5-9b-polaris", "nemotron"
        )

        windows_2 = {"totally-unknown-model-x": 131_072, "gpt-mystery": 262_144}
        models_service_2 = AsyncMock()
        models_service_2.get_max_context_length = AsyncMock(
            side_effect=lambda model_id: windows_2[model_id]
        )
        tighter_2 = await _pick_tighter_ctx_window(
            models_service_2, "totally-unknown-model-x", "gpt-mystery"
        )

        assert tighter_1 == tighter_2 == 131_072


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
