# SPDX-License-Identifier: Apache-2.0
"""CHANGELOG-0.5.3 regression: integrations passthrough in CoVe.

Tests for CoVe quality integrations.

Every adapter.stream_chat call issued by chain_of_verification — step 1
(initial answer), step 2 (verification questions), every step 3
(verification answer, one per question), and step 4 (revision) — MUST
carry the integrations list verbatim from the caller.

This file is the regression pin.  If the passthrough ever breaks, this
test catches it before it ships.

Mock pattern note
-----------------
LmstudioAdapter.stream_chat is an async generator function.  Mock with
MagicMock (sync) whose side_effect is also a sync function returning an
async generator.  Using AsyncMock wraps the result in a coroutine which
then cannot be used with ``async for``.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import MagicMock

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


# ---------------------------------------------------------------------------
# test_cove_passes_integrations_to_every_internal_call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cove_passes_integrations_to_every_internal_call() -> None:
    """CHANGELOG-0.5.3 regression: every CoVe step carries the caller's integrations.

    With integrations=["mcp/context7"], we verify stream_chat is called with
    req.integrations == ["mcp/context7"] on EVERY call:
      - step 1: initial answer
      - step 2: verification questions
      - step 3: one call per question parsed (here: 1 question)
      - step 4: revision
    Total: 4 calls.
    """
    target_integrations = ["mcp/context7"]

    # Step 2 returns exactly 1 question.
    stream_texts = [
        "Paris is the capital of France.",   # step 1: initial answer
        "- Is Paris the capital of France?",  # step 2: 1 question
        "Yes, Paris is the capital.",         # step 3: answer to question
        "Paris is the capital of France.",    # step 4: revision (same = converged)
    ]

    calls_seen: list[list[str]] = []

    def _recording_stream(
        req: CanonicalChatRequest, **kw: object
    ) -> AsyncIterator[CanonicalEvent]:
        calls_seen.append(list(req.integrations or []))
        return _text_gen(stream_texts[len(calls_seen) - 1])

    adapter = MagicMock()
    adapter.stream_chat = MagicMock(side_effect=_recording_stream)

    from unittest.mock import AsyncMock

    svc = QualityModeService(
        adapter=adapter,
        embedding_client=AsyncMock(),
        models_service=AsyncMock(),
    )

    result = await svc.chain_of_verification(
        prompt="What is the capital of France?",
        model_id="test-model",
        integrations=target_integrations,
    )

    assert len(calls_seen) == 4, (
        f"Expected 4 stream_chat calls (step1+step2+step3x1+step4), "
        f"got {len(calls_seen)}"
    )

    for idx, seen in enumerate(calls_seen):
        assert seen == target_integrations, (
            f"stream_chat call #{idx + 1} had integrations={seen!r}; "
            f"expected {target_integrations!r}. "
            "CHANGELOG-0.5.3 regression: integrations MUST be forwarded to "
            "every internal adapter call."
        )

    assert result.initial_answer == "Paris is the capital of France."
    assert result.verification_questions == ["Is Paris the capital of France?"]


# ---------------------------------------------------------------------------
# test_cove_with_no_integrations_passes_none_or_empty
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cove_with_no_integrations_passes_none_or_empty() -> None:
    """When integrations=None is passed, every call gets integrations=[].

    CanonicalChatRequest coerces None → [] since integrations defaults to [].
    """
    stream_texts = [
        "Answer one.",          # step 1
        "- Is this correct?",   # step 2
        "Yes.",                 # step 3
        "Answer one.",          # step 4
    ]

    calls_seen: list[list[str]] = []

    def _recording_stream(
        req: CanonicalChatRequest, **kw: object
    ) -> AsyncIterator[CanonicalEvent]:
        calls_seen.append(list(req.integrations or []))
        return _text_gen(stream_texts[len(calls_seen) - 1])

    adapter = MagicMock()
    adapter.stream_chat = MagicMock(side_effect=_recording_stream)

    from unittest.mock import AsyncMock

    svc = QualityModeService(
        adapter=adapter,
        embedding_client=AsyncMock(),
        models_service=AsyncMock(),
    )

    await svc.chain_of_verification(
        prompt="test",
        model_id="test-model",
        integrations=None,
    )

    for idx, seen in enumerate(calls_seen):
        assert seen == [], (
            f"stream_chat call #{idx + 1} had integrations={seen!r}; "
            "expected [] when caller passed integrations=None"
        )


# ---------------------------------------------------------------------------
# test_cove_multiple_questions_all_step3_calls_carry_integrations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cove_multiple_questions_all_step3_calls_carry_integrations() -> None:
    """With 3 verification questions, all 3 step-3 calls carry integrations.

    Total calls: step1(1) + step2(1) + step3(3) + step4(1) = 6.
    All must carry integrations=["mcp/filesystem", "mcp/searxng"].
    """
    target_integrations = ["mcp/filesystem", "mcp/searxng"]

    stream_texts = [
        "Initial answer text.",    # step 1
        "- Q1?\n- Q2?\n- Q3?",    # step 2: 3 questions
        "A1.",                     # step 3 for Q1
        "A2.",                     # step 3 for Q2
        "A3.",                     # step 3 for Q3
        "Revised answer text.",    # step 4
    ]

    calls_seen: list[list[str]] = []

    def _recording_stream(
        req: CanonicalChatRequest, **kw: object
    ) -> AsyncIterator[CanonicalEvent]:
        calls_seen.append(list(req.integrations or []))
        return _text_gen(stream_texts[len(calls_seen) - 1])

    adapter = MagicMock()
    adapter.stream_chat = MagicMock(side_effect=_recording_stream)

    from unittest.mock import AsyncMock

    svc = QualityModeService(
        adapter=adapter,
        embedding_client=AsyncMock(),
        models_service=AsyncMock(),
    )

    result = await svc.chain_of_verification(
        prompt="test",
        model_id="test-model",
        integrations=target_integrations,
    )

    assert len(calls_seen) == 6, (
        f"Expected 6 calls (1+1+3+1), got {len(calls_seen)}"
    )
    for idx, seen in enumerate(calls_seen):
        assert seen == target_integrations, (
            f"Call #{idx + 1} integrations={seen!r}; expected {target_integrations!r}"
        )

    assert len(result.verification_questions) == 3
    assert len(result.verification_answers) == 3
