# SPDX-License-Identifier: Apache-2.0
"""T1-7 regression: quality modes carry the assembled system prompt.

Self-Consistency and Chain-of-Verification previously answered from the bare
user text with NO system prompt, so they dropped project instructions, the
date/context block, persona, RAG, AND conversation history — enabling a
quality mode gave amnesiac, context-blind answers. These tests pin that every
internal generation now carries the caller-supplied ``system_prompt``, the
same way integrations are threaded (``test_quality_cove_integrations``).

Mock pattern mirrors ``test_quality_cove_integrations``: ``stream_chat`` is a
sync ``MagicMock`` whose ``side_effect`` returns an async generator, so the
recorded ``req.system_prompt`` reflects exactly what each internal call built.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from lmchat.lmstudio.types import CanonicalChatRequest, CanonicalEvent
from lmchat.services.models_service import Capabilities, ModelInfo
from lmchat.services.quality_modes import QualityModeService

_SYS = (
    "[Context] Today is 2026-07-13.\n## Project instructions\nBe terse.\n"
    "## Prior turns\nuser: my name is Kevin\nassistant: noted"
)


def _text_gen(text: str) -> AsyncIterator[CanonicalEvent]:
    """Return an async generator yielding a single message.delta event."""

    async def _g() -> AsyncIterator[CanonicalEvent]:
        yield CanonicalEvent(type="message.delta", content=text)

    return _g()


@pytest.mark.asyncio
async def test_cove_passes_system_prompt_to_every_internal_call() -> None:
    """Every CoVe step carries the caller's system_prompt.

    Steps: 1 initial answer + 1 verification-questions + N verification-answers
    (here 2) + 1 revision = 5 calls. All must build req.system_prompt == _SYS.
    """
    stream_texts = [
        "Paris is the capital of France.",   # step 1: initial answer
        "- Q1?\n- Q2?",                       # step 2: 2 questions
        "A1.",                                # step 3: answer q1
        "A2.",                                # step 3: answer q2
        "Paris is the capital of France.",    # step 4: revision (converged)
    ]
    seen: list[str | None] = []

    def _rec(req: CanonicalChatRequest, **kw: object) -> AsyncIterator[CanonicalEvent]:
        seen.append(req.system_prompt)
        return _text_gen(stream_texts[len(seen) - 1])

    adapter = MagicMock()
    adapter.stream_chat = MagicMock(side_effect=_rec)
    svc = QualityModeService(
        adapter=adapter,
        embedding_client=AsyncMock(),
        models_service=AsyncMock(),
    )

    await svc.chain_of_verification(
        prompt="What is the capital of France?",
        model_id="test-model",
        system_prompt=_SYS,
    )

    assert len(seen) == 5, f"expected 5 CoVe calls (1+1+2+1), got {len(seen)}"
    for idx, s in enumerate(seen):
        assert s == _SYS, (
            f"CoVe call #{idx + 1} dropped system_prompt (got {s!r}). "
            "Every step must answer WITH the assembled turn context."
        )


@pytest.mark.asyncio
async def test_sc_passes_system_prompt_to_every_draft() -> None:
    """Every self-consistency draft carries the caller's system_prompt."""
    seen: list[str | None] = []

    def _rec(req: CanonicalChatRequest, **kw: object) -> AsyncIterator[CanonicalEvent]:
        seen.append(req.system_prompt)
        return _text_gen("Same answer.")

    adapter = MagicMock()
    adapter.stream_chat = MagicMock(side_effect=_rec)

    # embed_batch + list_loaded so the post-draft convergence math runs.
    embedding_client = AsyncMock()
    embedding_client.embed_batch.return_value = [[1.0, 0.0], [1.0, 0.0], [1.0, 0.0]]
    models_service = AsyncMock()
    models_service.list_loaded.return_value = [
        ModelInfo(
            key="text-embedding-nomic-embed-text-v1.5",
            type="embedding",
            capabilities=Capabilities(vision=False, trained_for_tool_use=False),
            loaded_instance_ids=["text-embedding-nomic-embed-text-v1.5@q8_0"],
        )
    ]
    svc = QualityModeService(
        adapter=adapter,
        embedding_client=embedding_client,
        models_service=models_service,
    )

    await svc.self_consistency(
        prompt="What is the capital of France?",
        n_drafts=3,
        model_id="test-model",
        system_prompt=_SYS,
    )

    assert len(seen) == 3, f"expected 3 SC drafts, got {len(seen)}"
    for idx, s in enumerate(seen):
        assert s == _SYS, f"SC draft #{idx + 1} dropped system_prompt (got {s!r})"


@pytest.mark.asyncio
async def test_quality_default_system_prompt_is_none_backward_compatible() -> None:
    """Not passing system_prompt keeps req.system_prompt None (legacy behaviour).

    Guards that the new parameter is purely additive — existing callers that
    never pass it get byte-identical requests to before the T1-7 fix.
    """
    seen: list[str | None] = []

    def _rec(req: CanonicalChatRequest, **kw: object) -> AsyncIterator[CanonicalEvent]:
        seen.append(req.system_prompt)
        return _text_gen("x")

    adapter = MagicMock()
    adapter.stream_chat = MagicMock(side_effect=_rec)
    svc = QualityModeService(
        adapter=adapter,
        embedding_client=AsyncMock(),
        models_service=AsyncMock(),
    )

    # n_drafts=1 returns early (no embedding needed) — one generation only.
    await svc.self_consistency(prompt="q", n_drafts=1, model_id="test-model")

    assert seen == [None]
