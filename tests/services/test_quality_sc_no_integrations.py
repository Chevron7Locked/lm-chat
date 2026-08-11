# SPDX-License-Identifier: Apache-2.0
"""Regression pin: self_consistency (SC) is deliberately tool-free.

Self-Consistency (Wang et al. 2022, embedding-cosine variant — see the
``QualityModeService.self_consistency`` docstring and the module-level
"CHANGELOG-0.5.3 regression contract" note) measures the MODEL's own
answer-distribution stability under repeated i.i.d. sampling of the
identical prompt/context. Unlike ``chain_of_verification``, whose
verification step exists specifically to ground claims via tool lookups
(pinned by ``tests/services/test_quality_cove_integrations.py``), SC has no
verification step — introducing tools would let the N parallel drafts
diverge for reasons unrelated to the model's own sampling variance (tool
call decisions, retrieved-fact skew, stateful side effects firing N times
per turn), confounding exactly the signal SC exists to measure.

This file pins that SC:
  1. Has no ``integrations`` parameter in its public signature — a future
     refactor that "helpfully" mirrors CoVe's passthrough pattern here is a
     deliberate product decision, not a drive-by fix, and must update this
     test + the docstring rationale together.
  2. Never forwards integrations to any of its internal adapter calls.

Mock pattern note
------------------
LmstudioAdapter.stream_chat is an async generator function. Mock with
MagicMock (sync) whose side_effect is also a sync function returning an
async generator — mirrors test_quality_cove_integrations.py.
"""
from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lmchat.lmstudio.types import CanonicalChatRequest, CanonicalEvent
from lmchat.services.quality_modes import QualityModeService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _text_gen(text: str) -> AsyncIterator[CanonicalEvent]:
    """Return an async generator yielding a single message.delta event."""

    async def _g() -> AsyncIterator[CanonicalEvent]:
        yield CanonicalEvent(type="message.delta", content=text)

    return _g()


def _models_service_with_embedding(model_id: str = "embed-model") -> AsyncMock:
    """Return a models-service mock whose list_loaded returns one embedding
    model — mirrors the helper in test_quality_self_consistency.py."""
    from lmchat.services.models_service import Capabilities, ModelInfo

    model = ModelInfo(
        key=model_id,
        type="embedding",
        capabilities=Capabilities(vision=False, trained_for_tool_use=False),
        loaded_instance_ids=[f"{model_id}@q8_0"],
    )
    svc = AsyncMock()
    svc.list_loaded = AsyncMock(return_value=[model])
    return svc


# ---------------------------------------------------------------------------
# test_self_consistency_signature_has_no_integrations_param
# ---------------------------------------------------------------------------


def test_self_consistency_signature_has_no_integrations_param() -> None:
    """SC's public signature carries no ``integrations`` keyword.

    Deliberate: SC is a tool-free consistency measurement (see the module
    docstring's CHANGELOG-0.5.3 contract section and the self_consistency
    docstring). If a future change adds this parameter, that is a product
    decision requiring an explicit rationale update here AND in the
    docstrings — not a silent mirror of chain_of_verification's pattern.
    """
    params = inspect.signature(QualityModeService.self_consistency).parameters
    assert "integrations" not in params, (
        "self_consistency gained an 'integrations' param — SC is "
        "deliberately tool-free (see QualityModeService.self_consistency "
        "docstring for the rationale). If this is an intentional product "
        "change, update this pin AND the docstring rationale together, and "
        "thread integrations through every draft call the same way "
        "chain_of_verification does."
    )


# ---------------------------------------------------------------------------
# test_self_consistency_never_forwards_integrations_to_drafts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_consistency_never_forwards_integrations_to_drafts() -> None:
    """Every SC draft call carries integrations=[] — SC has no path today
    through which tools could reach its independent-sampling drafts.

    Mirrors test_cove_passes_integrations_to_every_internal_call's structure
    but pins the OPPOSITE contract: 3 drafts, all 3 stream_chat calls must
    show an empty integrations list.
    """
    stream_texts = ["draft one", "draft two", "draft three"]

    calls_seen: list[list[str]] = []

    def _recording_stream(
        req: CanonicalChatRequest, **kw: object
    ) -> AsyncIterator[CanonicalEvent]:
        calls_seen.append(list(req.integrations or []))
        return _text_gen(stream_texts[len(calls_seen) - 1])

    adapter = MagicMock()
    adapter.stream_chat = MagicMock(side_effect=_recording_stream)

    # Orthogonal unit vectors so the convergence math runs without error;
    # the actual chosen draft / convergence value is irrelevant to this test.
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
        await svc.self_consistency(
            prompt="What is the capital of France?",
            n_drafts=3,
            model_id="test-model",
        )

    assert len(calls_seen) == 3, f"Expected 3 stream_chat calls, got {len(calls_seen)}"
    for idx, seen in enumerate(calls_seen):
        assert seen == [], (
            f"SC draft call #{idx + 1} carried integrations={seen!r}; SC is "
            "deliberately tool-free (see self_consistency docstring). "
            "CHANGELOG-0.5.3-adjacent regression: SC must NEVER forward "
            "integrations to its drafts."
        )


# ---------------------------------------------------------------------------
# test_self_consistency_single_draft_never_forwards_integrations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_consistency_single_draft_never_forwards_integrations() -> None:
    """n_drafts=1 edge case (no embedding step) also carries integrations=[]."""
    calls_seen: list[list[str]] = []

    def _recording_stream(
        req: CanonicalChatRequest, **kw: object
    ) -> AsyncIterator[CanonicalEvent]:
        calls_seen.append(list(req.integrations or []))
        return _text_gen("only draft")

    adapter = MagicMock()
    adapter.stream_chat = MagicMock(side_effect=_recording_stream)

    svc = QualityModeService(
        adapter=adapter,
        embedding_client=AsyncMock(),
        models_service=AsyncMock(),
    )

    await svc.self_consistency(prompt="test", n_drafts=1, model_id="m")

    assert calls_seen == [[]]
