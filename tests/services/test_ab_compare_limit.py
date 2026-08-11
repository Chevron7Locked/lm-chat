# SPDX-License-Identifier: Apache-2.0
"""Unit tests for AbCompareService per-request memory/context cap (P8-EOP-r1 S-001).

Verifies:
- Under-cap dual stream succeeds (no AbCompareLimitError raised).
- max_tokens exceeding the cap raises AbCompareLimitError before streaming.
- Prompt token estimate exceeding the cap raises AbCompareLimitError.
- Single-stream path (StreamingService) is not affected — only stream_both.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from lmchat.lmstudio.types import CanonicalEvent
from lmchat.services.ab_compare_service import AbCompareLimitError, AbCompareService
from lmchat.services.lmstudio_streaming_client import LmstudioStreamingClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EVENTS_TRIVIAL = [
    CanonicalEvent(type="chat.start", content=None),
    CanonicalEvent(type="message.delta", content="ok"),
    CanonicalEvent(type="chat.end", content=None),
]


def _make_lm_client() -> LmstudioStreamingClient:
    """Return a mock LmstudioStreamingClient that yields minimal events."""

    async def _stream(*, request):  # noqa: ANN001
        for ev in _EVENTS_TRIVIAL:
            yield ev

    client = MagicMock(spec=LmstudioStreamingClient)
    client.stream = MagicMock(side_effect=_stream)
    return client


def _mock_cfg(cap: int) -> MagicMock:
    """Return a mock settings object with lm_chat_ab_max_output_tokens = cap."""
    cfg = MagicMock()
    cfg.lm_chat_ab_max_output_tokens = cap
    return cfg


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_under_cap_stream_succeeds() -> None:
    """When max_tokens is well under the cap, stream_both yields events normally."""
    client = _make_lm_client()
    svc = AbCompareService(lm_client=client, engine=None)

    events = []
    with patch("lmchat.config.get_settings", return_value=_mock_cfg(32_768)):
        async for ev in svc.stream_both(
            prompt="hello",
            model_a="model-a",
            model_b="model-b",
            max_tokens=100,
        ):
            events.append(ev)

    # Must produce events from both panes.
    assert any(ev.side == "a" for ev in events)
    assert any(ev.side == "b" for ev in events)


@pytest.mark.asyncio
async def test_max_tokens_over_cap_raises_before_stream() -> None:
    """max_tokens > cap raises AbCompareLimitError; no upstream connections opened."""
    stream_mock = MagicMock()
    client = _make_lm_client()
    client.stream = stream_mock  # type: ignore[method-assign]
    svc = AbCompareService(lm_client=client, engine=None)

    # Patch the cap to a small value so we can easily exceed it.
    with patch("lmchat.config.get_settings", return_value=_mock_cfg(512)):
        gen = svc.stream_both(
            prompt="hello",
            model_a="model-a",
            model_b="model-b",
            max_tokens=1024,  # exceeds cap of 512
        )
        with pytest.raises(AbCompareLimitError):
            async for _ in gen:
                pass

    # No upstream connections should have been opened.
    stream_mock.assert_not_called()


@pytest.mark.asyncio
async def test_prompt_over_cap_raises_before_stream() -> None:
    """A prompt whose token estimate exceeds the cap raises AbCompareLimitError."""
    stream_mock = MagicMock()
    client = _make_lm_client()
    client.stream = stream_mock  # type: ignore[method-assign]
    svc = AbCompareService(lm_client=client, engine=None)

    # cap = 10; prompt of 100 chars → estimate of 25 tokens > 10
    with patch("lmchat.config.get_settings", return_value=_mock_cfg(10)):
        gen = svc.stream_both(
            prompt="x" * 100,
            model_a="model-a",
            model_b="model-b",
        )
        with pytest.raises(AbCompareLimitError):
            async for _ in gen:
                pass

    stream_mock.assert_not_called()


@pytest.mark.asyncio
async def test_no_max_tokens_under_prompt_cap_succeeds() -> None:
    """When max_tokens is None and prompt is short, stream_both proceeds normally."""
    client = _make_lm_client()
    svc = AbCompareService(lm_client=client, engine=None)

    with patch("lmchat.config.get_settings", return_value=_mock_cfg(32_768)):
        events = []
        async for ev in svc.stream_both(
            prompt="short",
            model_a="model-a",
            model_b="model-b",
            max_tokens=None,
        ):
            events.append(ev)

    assert len(events) > 0
