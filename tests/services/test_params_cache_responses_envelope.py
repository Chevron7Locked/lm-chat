# SPDX-License-Identifier: Apache-2.0
"""Param-cache compatibility test for the /v1/responses 400 envelope.

Tests for params-cache with the /v1/responses envelope.

/v1/responses 400 errors carry the same ``error.param``
field that services/params_service.py keys on.  The ``type`` field differs
between surfaces (``invalid_request_error`` vs ``invalid_request``) but
_parse_rejection in lmstudio_adapter does not check ``type`` — only ``code``
and ``param``.

This test exercises the full reactive-learn-and-retry loop against a stubbed
/v1/responses endpoint that returns a /v1/responses-style 400 body.  Confirms:

1. _parse_rejection correctly extracts the ``param`` from a /v1/responses 400.
2. record_rejection + strip_rejected + retry succeeds end-to-end.
3. The ``type`` field difference (``invalid_request_error`` vs
   ``invalid_request``) does NOT prevent the param from being extracted.

The test also verifies ``invalid_type`` (the code sent by /v1/responses for
wrong-type params) is treated as a known rejection code.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from lmchat.lmstudio.types import (
    CanonicalChatRequest,
    CanonicalInputBlock,
    CanonicalTool,
)
from lmchat.services.lmstudio_adapter import LmstudioAdapter, _parse_rejection
from lmchat.services.params_service import ParamsService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def params_service() -> ParamsService:
    """Fresh per-test ParamsService instance."""
    return ParamsService()


@pytest.fixture()
def mock_http_client() -> MagicMock:
    """Mock httpx.AsyncClient."""
    return MagicMock(spec=httpx.AsyncClient)


@pytest.fixture()
def adapter(mock_http_client: MagicMock, params_service: ParamsService) -> LmstudioAdapter:
    """LmstudioAdapter wired to mock HTTP client + real ParamsService."""
    return LmstudioAdapter(
        http_client=mock_http_client,
        base_url="http://localhost:1234",
        params_service=params_service,
    )


def _mock_non_streaming_response(body_bytes: bytes, status_code: int) -> AsyncMock:
    response = AsyncMock(spec=httpx.Response)
    response.status_code = status_code
    response.aread = AsyncMock(return_value=body_bytes)
    response.aclose = AsyncMock()
    return response


def _mock_streaming_response(body_bytes: bytes, status_code: int = 200) -> AsyncMock:
    response = AsyncMock(spec=httpx.Response)
    response.status_code = status_code

    async def _aiter_bytes() -> AsyncIterator[bytes]:
        yield body_bytes

    response.aiter_bytes = _aiter_bytes
    response.aread = AsyncMock(return_value=body_bytes)
    response.aclose = AsyncMock()
    return response


def _make_responses_sse(response_id: str, text: str) -> bytes:
    """Minimal /v1/responses SSE stream for a plain text response."""
    lines: list[str] = []

    def _event(event_type: str, data: dict) -> None:  # type: ignore[type-arg]
        lines.append(f"event: {event_type}")
        lines.append(f"data: {json.dumps(data)}")
        lines.append("")

    _event("response.created", {"id": response_id, "status": "in_progress"})
    item_id = "msg_001"
    _event("response.output_item.added", {"item": {"id": item_id, "type": "message"}})
    _event("response.output_text.delta", {"item_id": item_id, "delta": text})
    _event("response.output_item.done", {"item": {"id": item_id, "type": "message"}})
    _event("response.completed", {"response": {"id": response_id}})
    return "\n".join(lines).encode()


# ---------------------------------------------------------------------------
# _parse_rejection — /v1/responses 400 envelope shapes
# ---------------------------------------------------------------------------


def test_parse_rejection_responses_unrecognized_keys() -> None:
    """_parse_rejection correctly extracts param from /v1/responses unrecognized_keys body."""
    body = json.dumps({
        "error": {
            "message": "Unrecognized field 'reasoning'",
            "type": "invalid_request_error",
            "param": "reasoning",
            "code": "unrecognized_keys",
        }
    }).encode()
    result = _parse_rejection(body)
    assert result == "reasoning", (
        "Must extract param='reasoning' from /v1/responses unrecognized_keys body"
    )


def test_parse_rejection_responses_invalid_type() -> None:
    """_parse_rejection handles /v1/responses invalid_type codes.

    /v1/responses returns ``invalid_type`` (not ``invalid_param``)
    for wrong-type params.  _parse_rejection must extract the param so the
    reactive-learn-and-retry loop caches and strips it.

    ``invalid_type`` is now in
    _REJECTION_CODES as defence-in-depth.  encode_responses() does not
    currently pass any param that trips this code, but a future change that
    does will be automatically caught and cached.
    """
    from lmchat.services.lmstudio_adapter import _REJECTION_CODES

    # Confirm the code is registered so the loop will key on it.
    assert "invalid_type" in _REJECTION_CODES, (
        "'invalid_type' must be in _REJECTION_CODES (defence-in-depth)"
    )

    body = json.dumps({
        "error": {
            "message": "Expected object, received string",
            "type": "invalid_request_error",
            "param": "reasoning",
            "code": "invalid_type",
        }
    }).encode()
    result = _parse_rejection(body)
    # Now that "invalid_type" is in _REJECTION_CODES, the param is extracted.
    assert result == "reasoning", (
        f"_parse_rejection must return 'reasoning' for invalid_type body; "
        f"got {result!r}"
    )


def test_parse_rejection_responses_type_field_difference_does_not_block() -> None:
    """The type field difference between /v1/responses and /api/v1/chat does not break extraction.

    /v1/responses uses 'invalid_request_error'; /api/v1/chat uses 'invalid_request'.
    _parse_rejection does NOT check the 'type' field — only 'code' and 'param'.
    Both return the param correctly for recognised codes.
    """
    # /v1/responses style
    body_responses = json.dumps({
        "error": {
            "message": "Unrecognized param",
            "type": "invalid_request_error",
            "param": "top_k",
            "code": "unrecognized_keys",
        }
    }).encode()
    # /api/v1/chat style
    body_native = json.dumps({
        "error": {
            "message": "Unrecognized param",
            "type": "invalid_request",
            "param": "top_k",
            "code": "unrecognized_keys",
        }
    }).encode()

    assert _parse_rejection(body_responses) == "top_k", (
        "Must extract param from /v1/responses-style 400 body"
    )
    assert _parse_rejection(body_native) == "top_k", (
        "Must extract param from /api/v1/chat-style 400 body"
    )


# ---------------------------------------------------------------------------
# Full reactive-learn-and-retry loop on /v1/responses path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_responses_surface_400_records_rejection_and_retries(
    adapter: LmstudioAdapter,
    mock_http_client: MagicMock,
    params_service: ParamsService,
) -> None:
    """400 unrecognized_keys on /v1/responses → record_rejection + strip + retry.

    Exercises the full reactive-learn-and-retry loop on the /v1/responses code path.
    Verifies the 400 envelope from /v1/responses is correctly parsed and the
    reactive-learn-and-retry loop fires as designed.
    """
    # Simulate /v1/responses rejecting a param with its envelope style.
    rejection_body = json.dumps({
        "error": {
            "message": "Unrecognized key(s) in object: 'min_p'",
            "type": "invalid_request_error",
            "param": "min_p",
            "code": "unrecognized_keys",
        }
    }).encode()
    bad_response = _mock_non_streaming_response(rejection_body, status_code=400)

    ok_sse = _make_responses_sse("resp_ok", "OK after retry")
    ok_response = _mock_streaming_response(ok_sse, status_code=200)

    call_count = 0

    async def _send(request: Any, *, stream: bool) -> AsyncMock:
        nonlocal call_count
        call_count += 1
        return bad_response if call_count == 1 else ok_response

    mock_http_client.build_request = MagicMock(return_value=MagicMock())
    mock_http_client.send = _send

    import lmchat.services.lmstudio_adapter as adapter_mod

    original_encode_responses = adapter_mod.encode_responses

    def _encode_with_min_p(
        req: CanonicalChatRequest,
        history: list,
    ) -> dict:  # type: ignore[type-arg]
        body = original_encode_responses(req, history)
        body["min_p"] = 0.05  # inject param that will be rejected
        return body

    req = CanonicalChatRequest(
        model="test-model",
        input=[CanonicalInputBlock(type="text", content="hello")],
        tools=[CanonicalTool(name="calc", description="math", parameters={})],
    )

    with patch.object(adapter_mod, "encode_responses", _encode_with_min_p):
        events = [ev async for ev in adapter.stream_chat(req, history=[])]

    # Verify rejection was recorded.
    rejected = await params_service.get_rejected(model_id="test-model")
    assert "min_p" in rejected, "min_p must be recorded as rejected"

    # Verify retry happened.
    assert call_count == 2, f"Expected 2 upstream calls (attempt + retry), got {call_count}"

    # Verify events from retry stream are present.
    types = [e.type for e in events]
    assert "error" not in types, "No error event should be present after successful retry"
    assert "chat.end" in types, "chat.end must be present on successful retry"


@pytest.mark.asyncio()
async def test_responses_surface_tool_choice_always_auto(
    adapter: LmstudioAdapter,
    mock_http_client: MagicMock,
) -> None:
    """encode_responses always sends tool_choice='auto' (capability gap).

    /v1/responses 'required' is non-binding; named tool_choice returns 400.
    lm-chat locks tool_choice to 'auto' in encode_responses.
    """
    ok_sse = _make_responses_sse("resp_tc", "ok")
    mock_response = _mock_streaming_response(ok_sse, status_code=200)
    mock_http_client.send = AsyncMock(return_value=mock_response)

    captured_bodies: list[dict] = []  # type: ignore[type-arg]

    def _build_request(method: str, url: str, **kwargs: Any) -> MagicMock:
        if "json" in kwargs:
            captured_bodies.append(kwargs["json"])
        return MagicMock()

    mock_http_client.build_request = _build_request

    req = CanonicalChatRequest(
        model="test-model",
        input=[CanonicalInputBlock(type="text", content="hello")],
        tools=[CanonicalTool(name="calc", description="math", parameters={})],
    )
    async for _ in adapter.stream_chat(req, history=[]):
        pass

    assert len(captured_bodies) == 1
    assert captured_bodies[0].get("tool_choice") == "auto", (
        "tool_choice must be 'auto' — 'required' is non-binding and named "
        "tool_choice returns 400 on /v1/responses"
    )
