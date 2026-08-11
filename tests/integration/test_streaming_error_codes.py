# SPDX-License-Identifier: Apache-2.0
"""Regression tests for the existing prod bug (error code truncation).

Per PR-S3 spec: backend tests section 10.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from lmchat.lmstudio.types import CanonicalInputBlock, CanonicalTool
from lmchat.services.lmstudio_adapter import (
    MTP_SUSPECT_THRESHOLD,
    LmstudioAdapter,
    _make_error_event,
)
from lmchat.services.lmstudio_streaming_client import CanonicalChatRequest


@pytest.fixture
def adapter() -> LmstudioAdapter:
    """Create an adapter instance for testing."""
    # Create a minimal httpx.AsyncClient with auth header
    client = httpx.AsyncClient(
        headers={"Authorization": "Bearer test-key"},
        base_url="http://localhost:1234",
    )
    # Use MagicMock for params_service since we're mocking strip_rejected anyway
    params_service = MagicMock()
    params_service.strip_rejected = MagicMock(side_effect=lambda body, model_id: body)
    return LmstudioAdapter(
        http_client=client,
        base_url="http://localhost:1234",
        params_service=params_service,
    )


@pytest.mark.asyncio
async def test_mtp_suspected_threshold_predicate(adapter: LmstudioAdapter) -> None:
    """Test the MTP-suspect threshold predicate at stream_chat.

    This test verifies that:
    1. When status_code == 500 AND len(tools) > 0 AND cumulative_tool_rounds >= threshold,
       the iterator yields exactly ONE error event with code "mtp_suspected".
    2. The early return prevents the existing 500 fall-through from also firing.
    3. Below threshold, the generic 500 event is yielded instead.

    Regression risk: flipping the operator or dropping the tools guard would not be caught.
    """
    # Create a request with non-empty tools
    req = CanonicalChatRequest(
        model="test-model",
        input=[CanonicalInputBlock(type="text", content="test")],
        tools=[CanonicalTool(name="tool1", description="test tool", parameters={})],
    )

    # Mock the HTTP 500 response
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.aread = AsyncMock(return_value=b'{"error":"tool timeout"}')
    mock_response.aclose = AsyncMock()

    # Create a mock params_service to avoid None attribute errors
    mock_params_service = MagicMock()
    mock_params_service.strip_rejected = MagicMock(side_effect=lambda body, model_id: body)

    # Patch select_surface to force "native" surface (avoids history requirement)
    # and patch http_client.send to return our mock response
    with (
        patch.object(adapter, "select_surface", return_value="native"),
        patch.object(adapter._http_client, "send") as mock_send,
        patch.object(adapter, "_params_service", mock_params_service),
    ):
        mock_send.return_value = mock_response

        # Test 1: cumulative_tool_rounds >= threshold (should yield mtp_suspected)
        cumulative_tool_rounds = MTP_SUSPECT_THRESHOLD  # Exactly at threshold
        events = []
        async for event in adapter.stream_chat(req, cumulative_tool_rounds=cumulative_tool_rounds):
            events.append(event)

        assert len(events) == 1, f"Expected exactly 1 event, got {len(events)}"
        assert events[0].type == "error"
        assert events[0].error is not None
        assert events[0].error["code"] == "mtp_suspected"
        assert events[0].error["cumulative_tool_rounds"] == cumulative_tool_rounds
        assert "hint" in events[0].error
        # Verify early return prevented fall-through (no generic 500 event)

    # Test 2: cumulative_tool_rounds < threshold (should yield generic 500)
    mock_response.status_code = 500
    mock_response.aread = AsyncMock(return_value=b'{"error":"tool timeout"}')

    with (
        patch.object(adapter, "select_surface", return_value="native"),
        patch.object(adapter._http_client, "send") as mock_send,
        patch.object(adapter, "_params_service", mock_params_service),
    ):
        mock_send.return_value = mock_response

        below_threshold_rounds = MTP_SUSPECT_THRESHOLD - 1
        events = []
        async for event in adapter.stream_chat(req, cumulative_tool_rounds=below_threshold_rounds):
            events.append(event)

        assert len(events) == 1, f"Expected exactly 1 event, got {len(events)}"
        assert events[0].type == "error"
        # Should be the generic 500 error, NOT mtp_suspected
        assert events[0].error is not None
        assert events[0].error["code"] == "500"
        assert events[0].error["message"] == '{"error":"tool timeout"}'
        # Verify mtp_suspected fields are NOT present
        assert "cumulative_tool_rounds" not in events[0].error  # type: ignore[arg-type]
        assert "hint" not in events[0].error  # type: ignore[arg-type]


def test_error_event_preserves_code() -> None:
    """Test that _make_error_event preserves the error code."""
    event = _make_error_event(code="context_window_exceeded", message="Test message")
    assert event.type == "error"
    assert event.error is not None
    assert event.error["code"] == "context_window_exceeded"
    assert event.error["message"] == "Test message"


def test_error_event_with_extra_fields() -> None:
    """Test that _make_error_event can include extra fields."""
    event = _make_error_event(
        code="mtp_suspected",
        message="Long tool chain",
        extra={
            "cumulative_tool_rounds": 20,
            "hint": "Disable MTP in LM Studio",
        },
    )
    assert event.type == "error"
    assert event.error is not None
    assert event.error["code"] == "mtp_suspected"
    assert event.error["message"] == "Long tool chain"
    assert event.error["cumulative_tool_rounds"] == 20
    assert event.error["hint"] == "Disable MTP in LM Studio"


def test_error_event_without_extra() -> None:
    """Test that _make_error_event works without extra fields (backwards compatibility)."""
    event = _make_error_event(code="upstream_unavailable", message="Server error")
    assert event.type == "error"
    assert event.error is not None
    assert event.error["code"] == "upstream_unavailable"
    assert event.error["message"] == "Server error"
    assert "cumulative_tool_rounds" not in event.error  # type: ignore[arg-type]
    assert "hint" not in event.error  # type: ignore[arg-type]
