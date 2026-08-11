# SPDX-License-Identifier: Apache-2.0
"""Tests for ChainOfVerificationResult convergence semantics.

Tests for CoVe quality convergence.

Convergence: revised_answer.strip() == initial_answer.strip().

Mock pattern note
-----------------
LmstudioAdapter.stream_chat is an async generator function.  Mock with
MagicMock (sync) whose side_effect is a sync function returning an async
generator.  AsyncMock wraps the result in a coroutine, breaking iteration.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from lmchat.lmstudio.types import CanonicalChatRequest, CanonicalEvent
from lmchat.services.quality_modes import ChainOfVerificationResult, QualityModeService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _text_gen(text: str) -> AsyncIterator[CanonicalEvent]:
    """Async generator yielding a single message.delta event."""

    async def _g() -> AsyncIterator[CanonicalEvent]:
        if text:
            yield CanonicalEvent(type="message.delta", content=text)

    return _g()


def _make_cove_adapter(
    initial: str,
    questions_block: str,
    answer: str,
    revised: str,
) -> MagicMock:
    """Build an adapter mock returning fixed strings for each CoVe step.

    Assumes exactly 1 verification question is returned in step 2.
    Call sequence: step1 → step2 → step3 → step4 (4 total calls).
    """
    texts = [initial, questions_block, answer, revised]
    call_count = 0

    def _stream(req: CanonicalChatRequest, **kw: object) -> AsyncIterator[CanonicalEvent]:
        nonlocal call_count
        text = texts[call_count]
        call_count += 1
        return _text_gen(text)

    adapter = MagicMock()
    adapter.stream_chat = MagicMock(side_effect=_stream)
    return adapter


# ---------------------------------------------------------------------------
# test_cove_converged_true_when_revised_equals_initial
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cove_converged_true_when_revised_equals_initial() -> None:
    """converged=True when revised_answer.strip() == initial_answer.strip()."""
    text = "Paris is the capital of France."
    adapter = _make_cove_adapter(
        initial=text,
        questions_block="- Is Paris the capital of France?",
        answer="Yes.",
        revised=text,
    )

    svc = QualityModeService(
        adapter=adapter,
        embedding_client=AsyncMock(),
        models_service=AsyncMock(),
    )

    result = await svc.chain_of_verification(
        prompt="What is the capital of France?",
        model_id="test-model",
    )

    assert isinstance(result, ChainOfVerificationResult)
    assert result.converged is True
    assert result.initial_answer == text
    assert result.revised_answer == text


@pytest.mark.asyncio
async def test_cove_converged_true_when_revised_equals_initial_with_whitespace() -> None:
    """converged=True when strings differ only in surrounding whitespace."""
    initial = "Paris is the capital."
    revised = "  Paris is the capital.  "  # strip makes them equal

    adapter = _make_cove_adapter(
        initial=initial,
        questions_block="- Is Paris the capital?",
        answer="Yes.",
        revised=revised,
    )

    svc = QualityModeService(
        adapter=adapter,
        embedding_client=AsyncMock(),
        models_service=AsyncMock(),
    )

    result = await svc.chain_of_verification(
        prompt="test",
        model_id="test-model",
    )

    assert result.converged is True


# ---------------------------------------------------------------------------
# test_cove_converged_false_when_revised_differs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cove_converged_false_when_revised_differs() -> None:
    """converged=False when revised_answer != initial_answer (after strip)."""
    initial = "The capital is London."
    revised = "The capital is Paris, not London."

    adapter = _make_cove_adapter(
        initial=initial,
        questions_block="- Is the capital London?",
        answer="No, it is Paris.",
        revised=revised,
    )

    svc = QualityModeService(
        adapter=adapter,
        embedding_client=AsyncMock(),
        models_service=AsyncMock(),
    )

    result = await svc.chain_of_verification(
        prompt="What is the capital of France?",
        model_id="test-model",
    )

    assert result.converged is False
    assert result.initial_answer == initial
    assert result.revised_answer == revised


# ---------------------------------------------------------------------------
# test_cove_empty_questions_returns_initial_as_converged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cove_empty_questions_returns_initial_as_converged() -> None:
    """If step 2 returns no parseable questions, initial_answer is returned as revised.

    Empty list → log warning, return initial_answer as revised with
    converged=True.
    """
    initial = "Some answer."
    call_count = 0

    def _stream(req: CanonicalChatRequest, **kw: object) -> AsyncIterator[CanonicalEvent]:
        nonlocal call_count
        texts = [initial, ""]  # step 2 returns blank → no questions
        text = texts[call_count] if call_count < len(texts) else ""
        call_count += 1
        return _text_gen(text)

    adapter = MagicMock()
    adapter.stream_chat = MagicMock(side_effect=_stream)

    svc = QualityModeService(
        adapter=adapter,
        embedding_client=AsyncMock(),
        models_service=AsyncMock(),
    )

    result = await svc.chain_of_verification(
        prompt="test",
        model_id="test-model",
    )

    assert result.verification_questions == []
    assert result.verification_answers == []
    assert result.revised_answer == initial
    assert result.converged is True
    # Step 3 and 4 must NOT have been called (only 2 calls: step1 + step2).
    assert adapter.stream_chat.call_count == 2


# ---------------------------------------------------------------------------
# test_cove_result_fields_populated_correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cove_result_fields_populated_correctly() -> None:
    """All ChainOfVerificationResult fields are populated with correct values."""
    initial = "Eiffel Tower is in Rome."
    questions_block = "- Where is the Eiffel Tower?"
    q_answer = "Paris, France."
    revised = "Eiffel Tower is in Paris, France."

    adapter = _make_cove_adapter(
        initial=initial,
        questions_block=questions_block,
        answer=q_answer,
        revised=revised,
    )

    svc = QualityModeService(
        adapter=adapter,
        embedding_client=AsyncMock(),
        models_service=AsyncMock(),
    )

    result = await svc.chain_of_verification(
        prompt="Where is the Eiffel Tower?",
        model_id="test-model",
    )

    assert result.initial_answer == initial
    assert result.verification_questions == ["Where is the Eiffel Tower?"]
    assert result.verification_answers == [q_answer]
    assert result.revised_answer == revised
    assert result.converged is False
