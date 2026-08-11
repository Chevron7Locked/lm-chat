# SPDX-License-Identifier: Apache-2.0
"""Tests for upstream error handling in QualityModeService.

Tests for quality-modes upstream error handling.

When the adapter yields an ``error`` event, quality modes must surface it
as QualityModeUpstreamError immediately — no partial results, no silent
swallowing.

Mock pattern note
-----------------
LmstudioAdapter.stream_chat is an async generator function.  Mock with
MagicMock (sync) whose side_effect is a sync function returning an async
generator.  AsyncMock wraps the result in a coroutine, breaking iteration.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lmchat.lmstudio.types import CanonicalChatRequest, CanonicalEvent
from lmchat.services.quality_modes import QualityModeService, QualityModeUpstreamError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _error_gen(*, code: str, message: str) -> AsyncIterator[CanonicalEvent]:
    """Async generator yielding a single error event."""

    async def _g() -> AsyncIterator[CanonicalEvent]:
        yield CanonicalEvent(type="error", error={"code": code, "message": message})

    return _g()


def _ok_gen(text: str) -> AsyncIterator[CanonicalEvent]:
    """Async generator yielding a single message.delta event."""

    async def _g() -> AsyncIterator[CanonicalEvent]:
        yield CanonicalEvent(type="message.delta", content=text)

    return _g()


def _none_error_gen() -> AsyncIterator[CanonicalEvent]:
    """Async generator yielding an error event with error=None."""

    async def _g() -> AsyncIterator[CanonicalEvent]:
        yield CanonicalEvent(type="error", error=None)

    return _g()


# ---------------------------------------------------------------------------
# Self-consistency error tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_consistency_on_adapter_error_raises_quality_mode_upstream_error() -> None:
    """When any SC draft generation emits an error event, SC raises QualityModeUpstreamError."""

    def _error_side(req: CanonicalChatRequest, **kw: object) -> AsyncIterator[CanonicalEvent]:
        return _error_gen(code="upstream_unavailable", message="LM Studio is not running")

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
            await svc.self_consistency(
                prompt="test",
                n_drafts=3,
                model_id="test-model",
            )

    assert exc_info.value.error_code == "upstream_unavailable"


@pytest.mark.asyncio
async def test_self_consistency_error_code_preserved() -> None:
    """The error code from the error event is preserved in the exception."""

    def _error_side(req: CanonicalChatRequest, **kw: object) -> AsyncIterator[CanonicalEvent]:
        return _error_gen(code="400", message="invalid param")

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

    assert exc_info.value.error_code == "400"
    assert "invalid param" in exc_info.value.message


# ---------------------------------------------------------------------------
# CoVe error tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cove_on_adapter_error_step1_raises_before_step2() -> None:
    """If step 1 errors, CoVe raises immediately — step 2 is never called."""
    call_count = 0

    def _counting_stream(
        req: CanonicalChatRequest, **kw: object
    ) -> AsyncIterator[CanonicalEvent]:
        nonlocal call_count
        call_count += 1
        return _error_gen(code="upstream_unavailable", message="down")

    adapter = MagicMock()
    adapter.stream_chat = MagicMock(side_effect=_counting_stream)

    svc = QualityModeService(
        adapter=adapter,
        embedding_client=AsyncMock(),
        models_service=AsyncMock(),
    )

    with pytest.raises(QualityModeUpstreamError) as exc_info:
        await svc.chain_of_verification(
            prompt="test",
            model_id="test-model",
        )

    assert call_count == 1, (
        f"Expected 1 stream_chat call (step 1 only), got {call_count}"
    )
    assert exc_info.value.error_code == "upstream_unavailable"


@pytest.mark.asyncio
async def test_cove_on_adapter_error_step2_raises_before_step3() -> None:
    """If step 2 errors, CoVe raises before attempting any step 3 calls."""
    call_count = 0

    def _stream(req: CanonicalChatRequest, **kw: object) -> AsyncIterator[CanonicalEvent]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _ok_gen("ok answer")  # step 1 succeeds
        return _error_gen(code="500", message="internal error")  # step 2 errors

    adapter = MagicMock()
    adapter.stream_chat = MagicMock(side_effect=_stream)

    svc = QualityModeService(
        adapter=adapter,
        embedding_client=AsyncMock(),
        models_service=AsyncMock(),
    )

    with pytest.raises(QualityModeUpstreamError) as exc_info:
        await svc.chain_of_verification(
            prompt="test",
            model_id="test-model",
        )

    assert call_count == 2, (
        f"Expected 2 calls (step1 ok + step2 error), got {call_count}"
    )
    assert exc_info.value.error_code == "500"


@pytest.mark.asyncio
async def test_cove_error_event_with_none_error_dict() -> None:
    """An error event with error=None yields code='unknown' on the exception."""

    def _none_side(req: CanonicalChatRequest, **kw: object) -> AsyncIterator[CanonicalEvent]:
        return _none_error_gen()

    adapter = MagicMock()
    adapter.stream_chat = MagicMock(side_effect=_none_side)

    svc = QualityModeService(
        adapter=adapter,
        embedding_client=AsyncMock(),
        models_service=AsyncMock(),
    )

    with pytest.raises(QualityModeUpstreamError) as exc_info:
        await svc.self_consistency(prompt="test", n_drafts=1, model_id="m")

    assert exc_info.value.error_code == "unknown"
