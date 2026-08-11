# SPDX-License-Identifier: Apache-2.0
"""Tests for QualityModeService.self_consistency.

Tests for quality self-consistency.

Covers:
- Most-central draft is selected (highest mean pairwise cosine).
- Convergence threshold honoured: max pairwise < 0.85 → converged=False.
- Parallel dispatch: stream_chat called n_drafts times concurrently.
- n_drafts=1 edge case: returns the draft, converged=False.
- n_drafts=0 raises QualityModeConvergenceFailure.

Mock pattern note
-----------------
LmstudioAdapter.stream_chat is an async generator function (uses ``yield``).
Calling it returns an AsyncGenerator directly — it is NOT a coroutine and must
NOT be awaited.  Tests mock it with a plain MagicMock (not AsyncMock) whose
side_effect is a sync function returning an async generator.  Using AsyncMock
here would wrap the call result in a coroutine, causing "got coroutine" errors.
"""
from __future__ import annotations

import math
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lmchat.lmstudio.types import CanonicalChatRequest, CanonicalEvent
from lmchat.services.quality_modes import (
    QualityModeConvergenceFailure,
    QualityModeService,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _async_text_stream(text: str) -> AsyncIterator[CanonicalEvent]:
    """Return an async iterator yielding a single message.delta event."""

    async def _gen() -> AsyncIterator[CanonicalEvent]:
        yield CanonicalEvent(type="message.delta", content=text)

    return _gen()


def _async_error_stream(*, code: str, message: str) -> AsyncIterator[CanonicalEvent]:
    """Return an async iterator yielding a single error event."""

    async def _gen() -> AsyncIterator[CanonicalEvent]:
        yield CanonicalEvent(type="error", error={"code": code, "message": message})

    return _gen()


def _models_service_with_embedding(model_id: str = "embed-model") -> AsyncMock:
    """Return a models-service mock whose list_loaded returns one embedding model."""
    from lmchat.services.models_service import Capabilities, ModelInfo

    model = ModelInfo(
        key=model_id,
        type="embedding",
        capabilities=Capabilities(vision=False, trained_for_tool_use=False),
        # Genuinely loaded — the resolver filters on loaded_instance_ids, so a
        # realistic loaded embedder must carry at least one instance id.
        loaded_instance_ids=[f"{model_id}@q8_0"],
    )
    svc = AsyncMock()
    svc.list_loaded = AsyncMock(return_value=[model])
    return svc


def _make_adapter_with_draft_sequence(drafts: list[str]) -> MagicMock:
    """Return an adapter mock that serves drafts in sequence, one per call."""
    call_count = 0

    def _side(req: CanonicalChatRequest, **kw: object) -> AsyncIterator[CanonicalEvent]:
        nonlocal call_count
        text = drafts[call_count % len(drafts)]
        call_count += 1
        return _async_text_stream(text)

    adapter = MagicMock()
    adapter.stream_chat = MagicMock(side_effect=_side)
    return adapter


# ---------------------------------------------------------------------------
# test_self_consistency_returns_most_central_draft
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_consistency_returns_most_central_draft() -> None:
    """The draft with the highest mean cosine to its peers is returned.

    Geometry:
      v0 = [1.0, 0.0]            → angle 0°
      v1 = [cos10°, sin10°]      → angle 10°
      v2 = [cos100°, sin100°]    → angle 100°

    Pairwise cosines (= cosine of angle between 2D unit vectors):
      cos(v0,v1) = cos(10°) ≈ 0.9848
      cos(v0,v2) = cos(100°) ≈ -0.1736
      cos(v1,v2) = cos(90°) = 0.0

    Mean cosines:
      mean(v0) = (0.9848 + (-0.1736))/2 ≈ 0.4056
      mean(v1) = (0.9848 + 0.0)/2 ≈ 0.4924  ← highest → v1 ("beta") chosen
      mean(v2) = ((-0.1736) + 0.0)/2 ≈ -0.0868

    max pairwise = 0.9848 > 0.85 → converged=True.
    """
    rad = math.pi / 180
    v0 = [1.0, 0.0]
    v1 = [math.cos(10 * rad), math.sin(10 * rad)]
    v2 = [math.cos(100 * rad), math.sin(100 * rad)]

    adapter = _make_adapter_with_draft_sequence(["alpha", "beta", "gamma"])

    embedding_client = AsyncMock()
    embedding_client.embed_batch = AsyncMock(return_value=[v0, v1, v2])

    svc = QualityModeService(
        adapter=adapter,
        embedding_client=embedding_client,
        models_service=_models_service_with_embedding(),
    )

    with patch("lmchat.services.quality_modes.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(
            lm_chat_sc_threshold=0.85,
            lm_studio_default_model="test-model",
        )
        result = await svc.self_consistency(
            prompt="What is the capital of France?",
            n_drafts=3,
            model_id="test-model",
        )

    assert result == "beta", f"Expected 'beta' (most central), got {result!r}"
    assert adapter.stream_chat.call_count == 3


# ---------------------------------------------------------------------------
# test_self_consistency_convergence_threshold_honored
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_consistency_convergence_threshold_honored() -> None:
    """When max pairwise cosine < threshold, the log records converged=False.

    Vectors at 60° apart give max pairwise cosine = 0.5 < 0.85.
    """
    rad = math.pi / 180
    v0 = [1.0, 0.0]
    v1 = [math.cos(60 * rad), math.sin(60 * rad)]   # cos(v0,v1)=0.5
    v2 = [math.cos(120 * rad), math.sin(120 * rad)]  # cos(v0,v2)=-0.5, cos(v1,v2)=0.5

    adapter = _make_adapter_with_draft_sequence(["draft-a", "draft-b", "draft-c"])

    embedding_client = AsyncMock()
    embedding_client.embed_batch = AsyncMock(return_value=[v0, v1, v2])

    svc = QualityModeService(
        adapter=adapter,
        embedding_client=embedding_client,
        models_service=_models_service_with_embedding(),
    )

    import structlog.testing

    with structlog.testing.capture_logs() as captured, patch(
        "lmchat.services.quality_modes.get_settings"
    ) as mock_settings:
        mock_settings.return_value = MagicMock(
            lm_chat_sc_threshold=0.85,
            lm_studio_default_model="test-model",
        )
        result = await svc.self_consistency(
            prompt="test prompt",
            n_drafts=3,
            model_id="test-model",
        )

    assert isinstance(result, str)

    sc_result_events = [e for e in captured if "sc.result" in e.get("event", "")]
    assert sc_result_events, f"Expected sc.result log event; got: {captured}"
    assert sc_result_events[0]["converged"] is False


# ---------------------------------------------------------------------------
# test_self_consistency_parallel_dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_consistency_parallel_dispatch() -> None:
    """stream_chat is called exactly n_drafts=3 times."""
    adapter = _make_adapter_with_draft_sequence(["t1", "t2", "t3"])

    # Orthogonal unit vectors — all pairwise cosines = 0 → tie → first wins.
    vectors = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    embedding_client = AsyncMock()
    embedding_client.embed_batch = AsyncMock(return_value=vectors)

    svc = QualityModeService(
        adapter=adapter,
        embedding_client=embedding_client,
        models_service=_models_service_with_embedding(),
    )

    with patch("lmchat.services.quality_modes.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(
            lm_chat_sc_threshold=0.85,
            lm_studio_default_model="test-model",
        )
        await svc.self_consistency(prompt="test", n_drafts=3, model_id="test-model")

    assert adapter.stream_chat.call_count == 3, (
        f"Expected 3 stream_chat calls, got {adapter.stream_chat.call_count}"
    )


# ---------------------------------------------------------------------------
# test_self_consistency_single_draft_edge_case
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_consistency_single_draft_edge_case() -> None:
    """n_drafts=1 returns the single draft; no embedding call is made."""
    adapter = _make_adapter_with_draft_sequence(["only draft"])
    embedding_client = AsyncMock()

    svc = QualityModeService(
        adapter=adapter,
        embedding_client=embedding_client,
        models_service=_models_service_with_embedding(),
    )

    result = await svc.self_consistency(prompt="test", n_drafts=1, model_id="m")
    assert result == "only draft"
    embedding_client.embed_batch.assert_not_called()


# ---------------------------------------------------------------------------
# test_self_consistency_zero_drafts_raises
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_consistency_zero_drafts_raises() -> None:
    """n_drafts=0 raises QualityModeConvergenceFailure."""
    svc = QualityModeService(
        adapter=MagicMock(),
        embedding_client=AsyncMock(),
        models_service=AsyncMock(),
    )
    with pytest.raises(QualityModeConvergenceFailure):
        await svc.self_consistency(prompt="test", n_drafts=0, model_id="m")


# ---------------------------------------------------------------------------
# test_self_consistency_on_adapter_error_raises
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_consistency_on_adapter_error_raises_quality_mode_upstream_error() -> None:
    """When a draft generation yields an error event, SC raises QualityModeUpstreamError."""
    from lmchat.services.quality_modes import QualityModeUpstreamError

    def _error_side(req: CanonicalChatRequest, **kw: object) -> AsyncIterator[CanonicalEvent]:
        return _async_error_stream(code="upstream_unavailable", message="LM Studio down")

    adapter = MagicMock()
    adapter.stream_chat = MagicMock(side_effect=_error_side)

    svc = QualityModeService(
        adapter=adapter,
        embedding_client=AsyncMock(),
        models_service=AsyncMock(),
    )

    with pytest.raises(QualityModeUpstreamError) as exc_info:
        with patch("lmchat.services.quality_modes.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                lm_chat_sc_threshold=0.85,
                lm_studio_default_model="test-model",
            )
            await svc.self_consistency(prompt="test", n_drafts=1, model_id="m")

    assert exc_info.value.error_code == "upstream_unavailable"
