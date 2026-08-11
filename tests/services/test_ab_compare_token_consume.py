# SPDX-License-Identifier: Apache-2.0
"""Unit tests for AbCompareService token-quota consumption.

Verifies that after both panes of a dual stream complete, ``_safe_consume_tokens``
fires exactly once with the combined content of both panes.

The single-stream ``StreamingService._safe_consume_tokens`` path is covered in
``test_streaming_service.py``.  These tests are scoped to the A/B-specific gap
identified in the audit: the dual-stream path previously skipped the token-
consume call entirely.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from lmchat.lmstudio.types import CanonicalEvent
from lmchat.services.ab_compare_service import AbCompareService
from lmchat.services.lmstudio_streaming_client import LmstudioStreamingClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Minimal event sequences using Literal-typed event names.
_EVENTS_A = [
    CanonicalEvent(type="chat.start", content=None),
    CanonicalEvent(type="message.delta", content="Hello "),
    CanonicalEvent(type="message.delta", content="from A"),
    CanonicalEvent(type="chat.end", content=None),
]

_EVENTS_B = [
    CanonicalEvent(type="chat.start", content=None),
    CanonicalEvent(type="message.delta", content="Greetings "),
    CanonicalEvent(type="message.delta", content="from B"),
    CanonicalEvent(type="chat.end", content=None),
]

_EVENTS_SHORT_A = [
    CanonicalEvent(type="chat.start", content=None),
    CanonicalEvent(type="message.delta", content="A text"),
    CanonicalEvent(type="chat.end", content=None),
]

_EVENTS_SHORT_B = [
    CanonicalEvent(type="chat.start", content=None),
    CanonicalEvent(type="message.delta", content="B text"),
    CanonicalEvent(type="chat.end", content=None),
]

_EVENTS_CONTENT = [
    CanonicalEvent(type="chat.start", content=None),
    CanonicalEvent(type="message.delta", content="content"),
    CanonicalEvent(type="chat.end", content=None),
]


def _make_lm_client(
    events_a: list[CanonicalEvent],
    events_b: list[CanonicalEvent],
) -> LmstudioStreamingClient:
    """Return a mock LmstudioStreamingClient.

    ``stream()`` returns ``events_a`` on the first call and ``events_b`` on
    the second call (matching pane-a and pane-b dispatch order).
    """
    call_count = 0

    async def _stream_side(*, request):  # noqa: ANN001
        nonlocal call_count
        call_count += 1
        chosen = events_a if call_count == 1 else events_b
        for ev in chosen:
            yield ev

    client = MagicMock(spec=LmstudioStreamingClient)
    client.stream = MagicMock(side_effect=_stream_side)
    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dual_stream_consume_fires_once_with_combined_content() -> None:
    """After both panes finish, _safe_consume_tokens fires once with combined content."""
    mock_engine = MagicMock()
    client = _make_lm_client(_EVENTS_A, _EVENTS_B)
    svc = AbCompareService(lm_client=client, engine=mock_engine)

    consumed_calls: list[tuple[int, int]] = []

    async def _fake_consume(user_id: int, amount: int, engine) -> None:  # noqa: ANN001
        consumed_calls.append((user_id, amount))

    with patch("lmchat.services.quota_service.consume_tokens", new=_fake_consume):
        async for _ in svc.stream_both(
            prompt="hi",
            model_a="model-a",
            model_b="model-b",
            user_id=7,
        ):
            pass

    # Token consume must fire exactly once.
    assert len(consumed_calls) == 1, f"Expected 1 consume call, got {consumed_calls}"

    uid, amount = consumed_calls[0]
    assert uid == 7

    # Combined content from both panes — ASCII, so byte-count == codepoint-count.
    from lmchat.services._token_budget import approx_token_count

    expected_content = "Hello " + "from A" + "Greetings " + "from B"
    expected_amount = approx_token_count(expected_content)
    assert amount == expected_amount, f"Expected {expected_amount}, got {amount}"


@pytest.mark.asyncio
async def test_dual_stream_consume_no_op_when_user_id_none() -> None:
    """When user_id is None, no token consume call is made."""
    mock_engine = MagicMock()
    client = _make_lm_client(_EVENTS_SHORT_A, _EVENTS_SHORT_B)
    svc = AbCompareService(lm_client=client, engine=mock_engine)

    consumed_calls: list = []

    async def _fake_consume(user_id: int, amount: int, engine) -> None:  # noqa: ANN001
        consumed_calls.append((user_id, amount))

    with patch("lmchat.services.quota_service.consume_tokens", new=_fake_consume):
        async for _ in svc.stream_both(
            prompt="hi",
            model_a="model-a",
            model_b="model-b",
            user_id=None,
        ):
            pass

    assert consumed_calls == [], f"Expected no consume calls, got {consumed_calls}"


@pytest.mark.asyncio
async def test_dual_stream_consume_no_op_when_engine_none() -> None:
    """When engine is None, no token consume call is made."""
    client = _make_lm_client(_EVENTS_CONTENT, _EVENTS_CONTENT)
    svc = AbCompareService(lm_client=client, engine=None)

    consumed_calls: list = []

    async def _fake_consume(user_id: int, amount: int, engine) -> None:  # noqa: ANN001
        consumed_calls.append((user_id, amount))

    with patch("lmchat.services.quota_service.consume_tokens", new=_fake_consume):
        async for _ in svc.stream_both(
            prompt="hi",
            model_a="model-a",
            model_b="model-b",
            user_id=5,
        ):
            pass

    assert consumed_calls == [], f"Expected no consume calls with no engine, got {consumed_calls}"


@pytest.mark.asyncio
async def test_dual_stream_consume_non_fatal_on_exception() -> None:
    """A consume_tokens exception does NOT propagate to the caller."""
    mock_engine = MagicMock()
    client = _make_lm_client(_EVENTS_CONTENT, _EVENTS_CONTENT)
    svc = AbCompareService(lm_client=client, engine=mock_engine)

    async def _raise_consume(user_id: int, amount: int, engine) -> None:  # noqa: ANN001
        raise RuntimeError("DB exploded")

    with patch("lmchat.services.quota_service.consume_tokens", new=_raise_consume):
        # Must not raise.
        async for _ in svc.stream_both(
            prompt="hi",
            model_a="model-a",
            model_b="model-b",
            user_id=3,
        ):
            pass
