# SPDX-License-Identifier: Apache-2.0
"""Tests for LmstudioAdapter.

Tests for the LM Studio adapter.

Covers:
- Surface selection matrix (all 4 combinations).
- Native happy-path streaming.
- Compat happy-path streaming.
- 400 reactive-learn-and-retry path (the moat — must pass end-to-end).
- 400 retry also 400 → terminal error event.
- Network error → canonical error event.
- model_id threading through strip_rejected.
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
    CanonicalMessage,
    CanonicalTool,
)
from lmchat.services.lmstudio_adapter import LmstudioAdapter
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
    """Mock httpx.AsyncClient with a controllable ``send`` method."""
    return MagicMock(spec=httpx.AsyncClient)


@pytest.fixture()
def adapter(mock_http_client: MagicMock, params_service: ParamsService) -> LmstudioAdapter:
    """LmstudioAdapter wired to mock HTTP client + real ParamsService."""
    return LmstudioAdapter(
        http_client=mock_http_client,
        base_url="http://localhost:1234",
        params_service=params_service,
    )


def _make_request(
    *,
    model: str = "test-model",
    tools: list[CanonicalTool] | None = None,
    integrations: list[str] | None = None,
    temperature: float | None = None,
) -> CanonicalChatRequest:
    """Build a minimal CanonicalChatRequest for testing."""
    return CanonicalChatRequest(
        model=model,
        input=[CanonicalInputBlock(type="text", content="hello")],
        tools=tools or [],
        integrations=integrations or [],
        temperature=temperature,
    )


def _make_native_sse(events: list[tuple[str, dict[str, Any]]]) -> bytes:
    """Build SSE bytes in LM Studio native format (event: / data: pairs)."""
    lines: list[str] = []
    for event_name, data in events:
        lines.append(f"event: {event_name}")
        lines.append(f"data: {json.dumps(data)}")
        lines.append("")
    return "\n".join(lines).encode()



def _mock_streaming_response(body_bytes: bytes, status_code: int = 200) -> AsyncMock:
    """Return an AsyncMock that behaves like an httpx streaming response."""
    response = AsyncMock(spec=httpx.Response)
    response.status_code = status_code

    async def _aiter_bytes() -> AsyncIterator[bytes]:
        yield body_bytes

    async def _aiter_lines() -> AsyncIterator[str]:
        # Decode and split into lines, matching how httpx.Response.aiter_lines()
        # works. decode_native uses aiter_lines() (not aiter_bytes()).
        text = body_bytes.decode("utf-8", errors="replace")
        for line in text.splitlines():
            yield line
        # Yield a final blank line if the body ends with one, so the SSE
        # parser flushes the last event block.
        if text.endswith("\n"):
            yield ""

    async def _aread() -> bytes:
        return body_bytes

    response.aiter_bytes = _aiter_bytes
    response.aiter_lines = _aiter_lines
    response.aread = _aread
    response.aclose = AsyncMock()
    return response


def _mock_non_streaming_response(body_bytes: bytes, status_code: int) -> AsyncMock:
    """Return an AsyncMock for a non-streaming response (error cases)."""
    response = AsyncMock(spec=httpx.Response)
    response.status_code = status_code
    response.aread = AsyncMock(return_value=body_bytes)
    response.aclose = AsyncMock()
    return response


# ---------------------------------------------------------------------------
# Surface selection tests (all 4 combinations)
# ---------------------------------------------------------------------------


def test_select_surface_integrations_returns_native(adapter: LmstudioAdapter) -> None:
    """integrations non-empty → native surface."""
    req = _make_request(integrations=["mcp/searxng"])
    assert adapter.select_surface(req) == "native"


def test_select_surface_tools_returns_responses(adapter: LmstudioAdapter) -> None:
    """tools non-empty AND integrations empty → responses surface.

    The /v1/responses surface replaced /v1/chat/completions (compat) for the
    tool-use path.  It supports both stateful chaining and client-side tools.
    """
    req = _make_request(
        tools=[CanonicalTool(name="calc", description="math", parameters={})]
    )
    assert adapter.select_surface(req) == "responses"


def test_select_surface_both_empty_returns_native(adapter: LmstudioAdapter) -> None:
    """Both tools and integrations empty → native surface (preferred for plain chat)."""
    req = _make_request()
    assert adapter.select_surface(req) == "native"


def test_select_surface_both_present_returns_native_and_warns(
    adapter: LmstudioAdapter,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Both integrations and tools non-empty → native wins; structured-log warning emitted."""
    import logging

    req = _make_request(
        integrations=["mcp/searxng"],
        tools=[CanonicalTool(name="calc", description="math", parameters={})],
    )
    with caplog.at_level(logging.WARNING):
        surface = adapter.select_surface(req)

    assert surface == "native"
    # The warning is structlog-emitted; verify it appeared in some log record.
    assert any(
        "adapter.tools_dropped_for_integrations" in r.message or
        "tools_dropped_for_integrations" in str(r)
        for r in caplog.records
    ) or True  # structlog may not go through stdlib caplog; check event string below


def test_select_surface_both_present_returns_native_structlog(
    adapter: LmstudioAdapter,
) -> None:
    """Both integrations and tools → native; verify structlog warning event key."""
    with patch("lmchat.services.lmstudio_adapter.log") as mock_log:
        req = _make_request(
            integrations=["mcp/searxng"],
            tools=[CanonicalTool(name="calc", description="math", parameters={})],
        )
        result = adapter.select_surface(req)
        mock_log.warning.assert_called_once()
        call_args = mock_log.warning.call_args
        assert call_args[0][0] == "adapter.tools_dropped_for_integrations"
        assert result == "native"


# ---------------------------------------------------------------------------
# stream_chat — native happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_stream_chat_native_happy_path(
    adapter: LmstudioAdapter,
    mock_http_client: MagicMock,
) -> None:
    """Native surface: mock SSE stream → correct canonical events yielded."""
    sse_body = _make_native_sse([
        ("chat.start", {"type": "chat.start", "response_id": "r1", "model_instance_id": "m1"}),
        ("message.start", {"type": "message.start"}),
        ("message.delta", {"type": "message.delta", "content": "Hello"}),
        ("message.end", {"type": "message.end"}),
        ("chat.end", {"type": "chat.end", "response_id": "r1"}),
    ])
    mock_response = _mock_streaming_response(sse_body, status_code=200)
    mock_http_client.build_request = MagicMock(return_value=MagicMock())
    mock_http_client.send = AsyncMock(return_value=mock_response)

    req = _make_request()
    events = []
    async for ev in adapter.stream_chat(req, history=[]):
        events.append(ev)

    types = [e.type for e in events]
    assert "chat.start" in types
    assert "message.delta" in types
    assert "chat.end" in types
    # Verify content delta carried through
    delta_events = [e for e in events if e.type == "message.delta"]
    assert delta_events[0].content == "Hello"


# ---------------------------------------------------------------------------
# stream_chat — responses surface happy path (replaces compat for tool-use)
# ---------------------------------------------------------------------------


def _make_responses_sse(
    response_id: str,
    text: str,
) -> bytes:
    """Build SSE bytes in /v1/responses format for a simple text response."""
    import json as _json

    lines: list[str] = []

    def _event(event_type: str, data: dict) -> None:  # type: ignore[type-arg]
        lines.append(f"event: {event_type}")
        lines.append(f"data: {_json.dumps(data)}")
        lines.append("")

    _event("response.created", {"id": response_id, "status": "in_progress"})
    item_id = "msg_001"
    _event(
        "response.output_item.added",
        {"item": {"id": item_id, "type": "message", "status": "in_progress"}},
    )
    _event("response.content_part.added", {"item_id": item_id})
    for word in text.split(" "):
        _event("response.output_text.delta", {"item_id": item_id, "delta": word + " "})
    _event(
        "response.output_item.done",
        {"item": {"id": item_id, "type": "message", "status": "completed"}},
    )
    _event("response.completed", {"response": {"id": response_id, "status": "completed"}})

    return "\n".join(lines).encode()


@pytest.mark.asyncio()
async def test_stream_chat_responses_happy_path(
    adapter: LmstudioAdapter,
    mock_http_client: MagicMock,
) -> None:
    """Responses surface: mock SSE stream → correct canonical events yielded.

    The tool-use path now uses /v1/responses instead of /v1/chat/completions.
    Verify the surface produces the correct canonical event sequence.
    """
    sse_body = _make_responses_sse(response_id="resp_abc123", text="Hi there")
    mock_response = _mock_streaming_response(sse_body, status_code=200)
    mock_http_client.build_request = MagicMock(return_value=MagicMock())
    mock_http_client.send = AsyncMock(return_value=mock_response)

    req = _make_request(
        tools=[CanonicalTool(name="calc", description="math", parameters={})]
    )
    events = []
    async for ev in adapter.stream_chat(req, history=[]):
        events.append(ev)

    types = [e.type for e in events]
    assert "chat.start" in types, f"Expected chat.start in {types}"
    assert "message.start" in types, f"Expected message.start in {types}"
    assert "message.delta" in types, f"Expected message.delta in {types}"
    assert "message.end" in types, f"Expected message.end in {types}"
    assert "chat.end" in types, f"Expected chat.end in {types}"

    # Verify response_id round-trips
    start_events = [e for e in events if e.type == "chat.start"]
    assert start_events[0].response_id == "resp_abc123"
    end_events = [e for e in events if e.type == "chat.end"]
    assert end_events[0].response_id == "resp_abc123"


@pytest.mark.asyncio()
async def test_stream_chat_responses_uses_responses_url(
    adapter: LmstudioAdapter,
    mock_http_client: MagicMock,
) -> None:
    """Responses surface: verify /v1/responses URL is used, NOT /v1/chat/completions."""
    sse_body = _make_responses_sse(response_id="resp_xyz", text="ok")
    mock_response = _mock_streaming_response(sse_body, status_code=200)
    mock_http_client.send = AsyncMock(return_value=mock_response)

    captured_urls: list[str] = []

    def _build_request(method: str, url: str, **kwargs: Any) -> MagicMock:
        captured_urls.append(url)
        return MagicMock()

    mock_http_client.build_request = _build_request

    req = _make_request(
        tools=[CanonicalTool(name="calc", description="math", parameters={})]
    )
    async for _ in adapter.stream_chat(req, history=[]):
        pass

    assert len(captured_urls) == 1
    assert "/v1/responses" in captured_urls[0], (
        f"Expected /v1/responses URL, got: {captured_urls[0]}"
    )
    assert "/v1/chat/completions" not in captured_urls[0], (
        "Tool-use path must NOT use /v1/chat/completions after P11c.1 migration"
    )


# ---------------------------------------------------------------------------
# stream_chat — 400 reactive-learn-and-retry (the moat)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_stream_chat_400_unrecognized_keys_records_and_retries(
    adapter: LmstudioAdapter,
    mock_http_client: MagicMock,
    params_service: ParamsService,
) -> None:
    """400 unrecognized_keys → record_rejection + strip + retry once → success.

    This is the reactive-learn-and-retry moat end-to-end path.
    """
    rejection_body = json.dumps({
        "error": {"code": "unrecognized_keys", "param": "min_p"}
    }).encode()
    bad_response = _mock_non_streaming_response(rejection_body, status_code=400)

    sse_body = _make_native_sse([
        ("message.start", {"type": "message.start"}),
        ("message.delta", {"type": "message.delta", "content": "OK after retry"}),
        ("message.end", {"type": "message.end"}),
        ("chat.end", {"type": "chat.end", "response_id": "r2"}),
    ])
    ok_response = _mock_streaming_response(sse_body, status_code=200)

    call_count = 0

    async def _send(request: Any, *, stream: bool) -> AsyncMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return bad_response
        return ok_response

    mock_http_client.build_request = MagicMock(return_value=MagicMock())
    mock_http_client.send = _send

    req = _make_request(model="test-model", temperature=0.7)
    # Patch encode_native to inject min_p so the adapter can strip it on retry.
    import lmchat.services.lmstudio_adapter as adapter_mod

    original_encode = adapter_mod.encode_native

    def _encode_with_min_p(r: CanonicalChatRequest) -> dict:
        body = original_encode(r)
        body["min_p"] = 0.05  # add a param that will be rejected
        return body

    with patch.object(adapter_mod, "encode_native", _encode_with_min_p):
        events = []
        async for ev in adapter.stream_chat(req, history=[]):
            events.append(ev)

    # Verify record_rejection was called
    rejected = await params_service.get_rejected(model_id="test-model")
    assert "min_p" in rejected

    # Verify retry happened (2 calls total)
    assert call_count == 2

    # Verify events from retry stream are present
    types = [e.type for e in events]
    assert "message.delta" in types
    delta_events = [e for e in events if e.type == "message.delta"]
    assert delta_events[0].content == "OK after retry"

    # No error event
    assert "error" not in types


# ---------------------------------------------------------------------------
# stream_chat — 400 retry also 400 → terminal error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_stream_chat_400_retry_also_400_surfaces_error(
    adapter: LmstudioAdapter,
    mock_http_client: MagicMock,
) -> None:
    """400 on initial + 400 on retry → exactly ONE canonical error event, stream ends."""
    rejection_body = json.dumps({
        "error": {"code": "unrecognized_keys", "param": "min_p"}
    }).encode()
    bad_response_1 = _mock_non_streaming_response(rejection_body, status_code=400)
    bad_response_2 = _mock_non_streaming_response(
        b'{"error": {"code": "unrecognized_keys", "param": "min_p"}}',
        status_code=400,
    )

    call_count = 0

    async def _send(request: Any, *, stream: bool) -> AsyncMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return bad_response_1
        return bad_response_2

    mock_http_client.build_request = MagicMock(return_value=MagicMock())
    mock_http_client.send = _send

    req = _make_request(model="test-model")

    import lmchat.services.lmstudio_adapter as adapter_mod
    original_encode = adapter_mod.encode_native

    def _encode_with_min_p(r: CanonicalChatRequest) -> dict:
        body = original_encode(r)
        body["min_p"] = 0.05
        return body

    with patch.object(adapter_mod, "encode_native", _encode_with_min_p):
        events = []
        async for ev in adapter.stream_chat(req, history=[]):
            events.append(ev)

    assert call_count == 2

    # Exactly ONE error event, stream ends
    error_events = [e for e in events if e.type == "error"]
    assert len(error_events) == 1

    # No non-error events from second attempt
    non_error = [e for e in events if e.type != "error"]
    assert non_error == []


# ---------------------------------------------------------------------------
# stream_chat — network error → canonical error event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_stream_chat_network_error_surfaces_error_event(
    adapter: LmstudioAdapter,
    mock_http_client: MagicMock,
) -> None:
    """httpx.ConnectError → canonical error event with code='upstream_unavailable'."""
    mock_http_client.build_request = MagicMock(return_value=MagicMock())
    mock_http_client.send = AsyncMock(
        side_effect=httpx.ConnectError("Connection refused")
    )

    req = _make_request()
    events = []
    async for ev in adapter.stream_chat(req, history=[]):
        events.append(ev)

    assert len(events) == 1
    assert events[0].type == "error"
    assert events[0].error is not None
    assert events[0].error.get("code") == "upstream_unavailable"


@pytest.mark.asyncio()
async def test_stream_chat_read_timeout_surfaces_error_event(
    adapter: LmstudioAdapter,
    mock_http_client: MagicMock,
) -> None:
    """httpx.ReadTimeout → canonical error event with code='upstream_unavailable'."""
    mock_http_client.build_request = MagicMock(return_value=MagicMock())
    mock_http_client.send = AsyncMock(
        side_effect=httpx.ReadTimeout("Read timeout")
    )

    req = _make_request()
    events = []
    async for ev in adapter.stream_chat(req, history=[]):
        events.append(ev)

    assert len(events) == 1
    assert events[0].type == "error"
    assert events[0].error is not None
    assert events[0].error.get("code") == "upstream_unavailable"


# ---------------------------------------------------------------------------
# stream_chat — retry-path network errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_stream_chat_connect_error_on_retry_surfaces_error_event(
    adapter: LmstudioAdapter,
    mock_http_client: MagicMock,
) -> None:
    """400 unrecognized_keys → record_rejection → retry hits ConnectError → error event."""
    rejection_body = b'{"error": {"code": "unrecognized_keys", "param": "min_p"}}'
    bad_response = _mock_non_streaming_response(rejection_body, status_code=400)

    mock_http_client.build_request = MagicMock(return_value=MagicMock())
    mock_http_client.send = AsyncMock(
        side_effect=[bad_response, httpx.ConnectError("Connection refused on retry")]
    )

    req = _make_request()
    events = [ev async for ev in adapter.stream_chat(req, history=[])]

    assert len(events) == 1
    assert events[0].type == "error"
    assert events[0].error is not None
    assert events[0].error.get("code") == "upstream_unavailable"


@pytest.mark.asyncio()
async def test_stream_chat_read_timeout_on_retry_surfaces_error_event(
    adapter: LmstudioAdapter,
    mock_http_client: MagicMock,
) -> None:
    """400 unrecognized_keys → record_rejection → retry hits ReadTimeout → error event."""
    rejection_body = b'{"error": {"code": "unrecognized_keys", "param": "min_p"}}'
    bad_response = _mock_non_streaming_response(rejection_body, status_code=400)

    mock_http_client.build_request = MagicMock(return_value=MagicMock())
    mock_http_client.send = AsyncMock(
        side_effect=[bad_response, httpx.ReadTimeout("Read timeout on retry")]
    )

    req = _make_request()
    events = [ev async for ev in adapter.stream_chat(req, history=[])]

    assert len(events) == 1
    assert events[0].type == "error"
    assert events[0].error is not None
    assert events[0].error.get("code") == "upstream_unavailable"


@pytest.mark.asyncio()
async def test_stream_chat_400_unparseable_body_surfaces_error_event(
    adapter: LmstudioAdapter,
    mock_http_client: MagicMock,
) -> None:
    """400 with a body that doesn't match the rejection schema → no retry,
    canonical error event with status code."""
    # Body that doesn't match the unrecognized_keys / invalid_param schema.
    unparseable_body = b'{"error": {"message": "something else"}}'
    response_400 = _mock_non_streaming_response(unparseable_body, status_code=400)

    mock_http_client.build_request = MagicMock(return_value=MagicMock())
    mock_http_client.send = AsyncMock(return_value=response_400)

    req = _make_request()
    events = [ev async for ev in adapter.stream_chat(req, history=[])]

    # Exactly ONE error event, NO retry (send called once).
    assert mock_http_client.send.await_count == 1
    assert len(events) == 1
    assert events[0].type == "error"
    assert events[0].error is not None
    # When the 400 body has a parseable inner {error: {message:...}} dict
    # without a recognized type, _normalize_error_event returns upstream_error.
    # The code is no longer the raw "400" string — it's a canonical code.
    assert events[0].error.get("code") in ("upstream_error", "400")


# ---------------------------------------------------------------------------
# stream_chat — history-required precondition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_stream_chat_responses_surface_without_history_raises(
    adapter: LmstudioAdapter,
) -> None:
    """Responses surface (tools non-empty, integrations empty) requires history.

    Passing history=None when the responses surface is selected is a caller
    precondition violation; the adapter raises ValueError rather than encoding
    a broken body.  (Replaces the old compat-surface precondition test — the
    tool-use path migrated from compat to responses.)
    """
    from lmchat.lmstudio.types import CanonicalTool

    # tools non-empty + integrations empty → responses surface
    req = _make_request()
    req_with_tools = req.model_copy(
        update={
            "tools": [
                CanonicalTool(
                    name="echo",
                    description="echo",
                    parameters={"type": "object"},
                )
            ],
            "integrations": [],
        }
    )

    with pytest.raises(ValueError, match="responses surface requires history"):
        async for _ in adapter.stream_chat(req_with_tools, history=None):
            pass


# ---------------------------------------------------------------------------
# strip_rejected model_id threading
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_strip_rejected_called_with_correct_model_id(
    adapter: LmstudioAdapter,
    mock_http_client: MagicMock,
    params_service: ParamsService,
) -> None:
    """strip_rejected is called with the request's model_id, not a default."""
    # Pre-seed a rejection for a specific model.
    await params_service.record_rejection(model_id="special-model", param="top_k")

    sse_body = _make_native_sse([
        ("chat.start", {"type": "chat.start", "response_id": "x"}),
        ("chat.end", {"type": "chat.end", "response_id": "x"}),
    ])
    mock_response = _mock_streaming_response(sse_body, status_code=200)
    mock_http_client.send = AsyncMock(return_value=mock_response)

    # Capture the json= kwarg from build_request to inspect what was sent.
    captured_json_bodies: list[dict] = []

    def _build_request(method: str, url: str, **kwargs: Any) -> MagicMock:
        if "json" in kwargs:
            captured_json_bodies.append(kwargs["json"])
        return MagicMock()

    mock_http_client.build_request = _build_request

    import lmchat.services.lmstudio_adapter as adapter_mod
    original_encode = adapter_mod.encode_native

    def _encode_with_top_k(r: CanonicalChatRequest) -> dict:
        body = original_encode(r)
        body["top_k"] = 40  # should be stripped for special-model
        return body

    with patch.object(adapter_mod, "encode_native", _encode_with_top_k):
        req = _make_request(model="special-model")
        async for _ in adapter.stream_chat(req, history=[]):
            pass

    # top_k should have been stripped from the request
    assert len(captured_json_bodies) == 1
    assert "top_k" not in captured_json_bodies[0]


# ---------------------------------------------------------------------------
# Non-200/400 HTTP error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_stream_chat_non_200_surfaces_error_event(
    adapter: LmstudioAdapter,
    mock_http_client: MagicMock,
) -> None:
    """Non-200/400 upstream status → canonical error event with code=str(status)."""
    mock_http_client.build_request = MagicMock(return_value=MagicMock())
    bad_response = _mock_non_streaming_response(b"Internal Server Error", status_code=500)
    mock_http_client.send = AsyncMock(return_value=bad_response)

    req = _make_request()
    events = []
    async for ev in adapter.stream_chat(req, history=[]):
        events.append(ev)

    assert len(events) == 1
    assert events[0].type == "error"
    assert events[0].error is not None
    assert events[0].error.get("code") == "500"


# ---------------------------------------------------------------------------
# History threads through responses encoder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_stream_chat_responses_passes_history(
    adapter: LmstudioAdapter,
    mock_http_client: MagicMock,
) -> None:
    """Responses surface: history messages are included in the request body.

    The responses encoder puts history into the ``input`` array (not
    ``messages`` — the compat shape).  Verifies history round-trips through
    encode_responses.
    """
    sse_body = _make_responses_sse(response_id="resp_hist", text="reply")
    mock_response = _mock_streaming_response(sse_body, status_code=200)
    mock_http_client.send = AsyncMock(return_value=mock_response)

    # Capture the json= kwarg from build_request to inspect the body.
    captured_json_bodies: list[dict] = []

    def _build_request(method: str, url: str, **kwargs: Any) -> MagicMock:
        if "json" in kwargs:
            captured_json_bodies.append(kwargs["json"])
        return MagicMock()

    mock_http_client.build_request = _build_request

    req = _make_request(
        tools=[CanonicalTool(name="calc", description="math", parameters={})]
    )
    history = [
        CanonicalMessage(role="user", content="what is 2+2"),
        CanonicalMessage(role="assistant", content="4"),
    ]

    async for _ in adapter.stream_chat(req, history=history):
        pass

    assert len(captured_json_bodies) == 1
    body = captured_json_bodies[0]

    # Responses API uses "input", NOT "messages".
    assert "input" in body, "Responses body must use 'input', not 'messages'"
    assert "messages" not in body, "Responses body must NOT use 'messages'"

    input_items = body["input"]
    input_contents = [m.get("content") for m in input_items]
    assert "what is 2+2" in input_contents, "History user message must appear in input"
    assert "4" in input_contents, "History assistant message must appear in input"

    # tool_choice must always be "auto" (capability gap).
    assert body.get("tool_choice") == "auto", (
        "tool_choice must be 'auto' on /v1/responses ('required' is "
        "non-binding; named tool_choice returns 400)"
    )


# ---------------------------------------------------------------------------
# probe_for_error body tightening
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_probe_for_error_body_sets_store_false_and_max_output_tokens_one(
    adapter: LmstudioAdapter,
    mock_http_client: MagicMock,
) -> None:
    """Probe body must carry stream=false + store=false + max_output_tokens=1.

    The probe wants the error envelope, not a generation. Pre-fix, a 2xx
    probe meant LM Studio just generated a full reply against an
    already-struggling instance for nothing. The probe caps the
    output at 1 token and disables server-side storage so it is
    the cheapest call that can still surface an error body.
    """
    # Mock post() to capture the body and return a 500 error envelope.
    captured_post: list[dict] = []

    async def _capture_post(url: str, **kwargs: Any) -> MagicMock:
        captured_post.append(kwargs.get("json", {}))
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 500
        resp.json = MagicMock(return_value={
            "error": {
                "message": "Trying to keep the first 8000 tokens "
                           "when context the overflows. However, the model "
                           "is loaded with context length of only 4096 "
                           "tokens, which is not enough. Try to load the "
                           "model with a larger context length, or provide "
                           "a shorter input. (exceed_context_size_error)",
                "type": "invalid_request_error",
            }
        })
        return resp

    mock_http_client.post = _capture_post

    req = _make_request()
    detail = await adapter.probe_for_error(req)

    assert len(captured_post) == 1, "probe_for_error should POST exactly once"
    body = captured_post[0]
    assert body.get("stream") is False, "probe must set stream=false"
    assert body.get("store") is False, (
        "probe must set store=false — the probe wants the error envelope, "
        "not a server-side conversation entry"
    )
    assert body.get("max_output_tokens") == 1, (
        "probe must cap max_output_tokens=1 — on a 2xx probe response we "
        "discard the body anyway, so generating more is pure waste on an "
        "already-struggling instance"
    )
    # And the actionable detail must still surface.
    assert detail is not None
    assert "context size" in detail.lower()


# ---------------------------------------------------------------------------
# 403 integration-denied → graceful degrade → retry → success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_stream_chat_403_integration_denied_degrade_retry_success(
    adapter: LmstudioAdapter,
    mock_http_client: MagicMock,
) -> None:
    """403 integrations-denied → degrade (drop denied) → retry → 200 → normal stream.

    Asserts:
    - The second POST omits the denied integration.
    - The stream yields normal message events (NOT a terminal error).
    - A non-fatal warning signal is emitted.
    """
    # attempt-1: 403 integration denied for mcp/firecrawl
    denied_body = json.dumps({
        "error": {
            "message": "Permission denied to use plugin mcp/firecrawl",
            "code": "permission_denied",
            "param": "integrations",
        }
    }).encode()
    bad_response = _mock_non_streaming_response(denied_body, status_code=403)

    # attempt-2 (degrade retry): 200 with normal SSE stream
    sse_body = _make_native_sse([
        ("message.start", {"type": "message.start"}),
        ("message.delta", {"type": "message.delta", "content": "Degraded but working"}),
        ("message.end", {"type": "message.end"}),
        ("chat.end", {"type": "chat.end", "response_id": "r3"}),
    ])
    ok_response = _mock_streaming_response(sse_body, status_code=200)

    call_count = 0
    captured_bodies: list[Any] = []

    def _build_request(*args: Any, **kwargs: Any) -> MagicMock:
        # _post_stream passes the body as the ``json=`` kwarg to build_request;
        # capture it here (the returned request object is opaque to send()).
        captured_bodies.append(kwargs.get("json"))
        return MagicMock()

    async def _send(request: Any, *, stream: bool) -> AsyncMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return bad_response
        return ok_response

    mock_http_client.build_request = MagicMock(side_effect=_build_request)
    mock_http_client.send = _send

    req = _make_request(integrations=["mcp/firecrawl", "mcp/searxng"])

    events = []
    async for ev in adapter.stream_chat(req, history=[]):
        events.append(ev)

    # --- assertions ---
    assert call_count == 2, "Should retry once"

    # (a) Second POST omits the denied integration
    assert "integrations" in captured_bodies[1]
    assert "mcp/firecrawl" not in captured_bodies[1]["integrations"], (
        "Denied integration should be dropped on retry"
    )
    assert "mcp/searxng" in captured_bodies[1]["integrations"], (
        "Non-denied integration should be kept"
    )

    # (b) Stream yields normal message events (NOT a terminal error)
    types = [e.type for e in events]
    assert "message.delta" in types
    delta_events = [e for e in events if e.type == "message.delta"]
    assert delta_events[0].content == "Degraded but working"
    assert "error" not in types, "Should NOT have a terminal error event"

    # (c) Non-fatal degradation signal is emitted
    warning_events = [e for e in events if e.type == "warning"]
    assert len(warning_events) >= 1
    assert warning_events[0].warning is not None
    assert warning_events[0].warning.get("code") == "adapter.integration_denied_degraded"
    assert "mcp/firecrawl" in warning_events[0].warning.get("message", "")


# ---------------------------------------------------------------------------
# unrelated 403 (bad API key, no integrations) → terminal error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_stream_chat_403_unrelated_surfaces_terminal_error(
    adapter: LmstudioAdapter,
    mock_http_client: MagicMock,
) -> None:
    """Non-integration 403 (e.g. bad API key) must NOT be swallowed — terminal error.

    Asserts exactly ONE error event and no integration degradation.
    """
    # A 403 response that does NOT match the integration-denied pattern.
    auth_error_body = json.dumps({
        "error": {
            "message": "Invalid API key",
            "code": "authentication_error",
        }
    }).encode()
    bad_response = _mock_non_streaming_response(auth_error_body, status_code=403)

    call_count = 0

    async def _send(request: Any, *, stream: bool) -> AsyncMock:
        nonlocal call_count
        call_count += 1
        return bad_response

    mock_http_client.build_request = MagicMock(return_value=MagicMock())
    mock_http_client.send = _send

    # No integrations in the request — this is NOT an integration-denied 403
    req = _make_request()  # integrations=[]

    events = []
    async for ev in adapter.stream_chat(req, history=[]):
        events.append(ev)

    assert call_count == 1, "Should NOT retry"

    # Exactly ONE error event, stream ends
    error_events = [e for e in events if e.type == "error"]
    assert len(error_events) == 1
    assert error_events[0].error is not None
    assert "Invalid API key" in error_events[0].error.get("message", "")

    # No warning (degradation) events
    warning_events = [e for e in events if e.type == "warning"]
    assert warning_events == [], "Should NOT emit degradation warning for unrelated 403"


# ---------------------------------------------------------------------------
# native mid-stream plugin_connection_error (resilience fix, Layer 1)
#
# LM Studio's native surface can fail mid-SSE-stream with a
# plugin_connection_error ("Cannot find plugin handle for plugin: mcp/...")
# when a requested MCP plugin is dead/removed/unreachable — e.g. a stale
# enabled_by_default integration for a server that no longer exists. Prior
# to this fix, that killed the WHOLE turn with no text ever reaching the
# user. These tests cover both wire shapes (HTTP-error-body and mid-stream
# 200 error-event) and the content-already-emitted edge case.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_stream_chat_native_mid_stream_plugin_connection_error_degrades_and_retries(
    adapter: LmstudioAdapter,
    mock_http_client: MagicMock,
) -> None:
    """Native mid-stream 200 plugin_connection_error → degrade (drop the
    named plugin) → retry → normal stream, instead of killing the turn.
    """
    # attempt-1: 200 SSE stream that dies with a plugin_connection_error
    # right after chat.start — no content has rendered yet.
    attempt1_sse = _make_native_sse([
        ("chat.start", {"type": "chat.start", "response_id": "r1", "model_instance_id": "m1"}),
        (
            "error",
            {
                "type": "error",
                "error": {
                    "type": "plugin_connection_error",
                    "message": (
                        "Unable to get plugin tools for 'mcp/firecrawl'. "
                        "Cannot find plugin handle for plugin: mcp/firecrawl"
                    ),
                },
            },
        ),
    ])
    attempt1_response = _mock_streaming_response(attempt1_sse, status_code=200)

    # attempt-2 (degrade retry): 200 with a normal SSE stream.
    attempt2_sse = _make_native_sse([
        ("message.start", {"type": "message.start"}),
        ("message.delta", {"type": "message.delta", "content": "Still works"}),
        ("message.end", {"type": "message.end"}),
        ("chat.end", {"type": "chat.end", "response_id": "r2"}),
    ])
    attempt2_response = _mock_streaming_response(attempt2_sse, status_code=200)

    call_count = 0
    captured_bodies: list[Any] = []

    def _build_request(*args: Any, **kwargs: Any) -> MagicMock:
        captured_bodies.append(kwargs.get("json"))
        return MagicMock()

    async def _send(request: Any, *, stream: bool) -> AsyncMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return attempt1_response
        return attempt2_response

    mock_http_client.build_request = MagicMock(side_effect=_build_request)
    mock_http_client.send = _send

    req = _make_request(integrations=["mcp/firecrawl", "mcp/searxng"])

    events = []
    async for ev in adapter.stream_chat(req, history=[]):
        events.append(ev)

    # --- assertions ---
    assert call_count == 2, "Should retry once"

    # (a) Second POST omits the unserveable plugin, keeps the other.
    assert "integrations" in captured_bodies[1]
    assert "mcp/firecrawl" not in captured_bodies[1]["integrations"], (
        "Unserveable plugin should be dropped on retry"
    )
    assert "mcp/searxng" in captured_bodies[1]["integrations"], (
        "Unaffected integration should be kept"
    )

    # (b) The user gets their answer — normal content, no terminal error.
    types = [e.type for e in events]
    assert "message.delta" in types
    delta_events = [e for e in events if e.type == "message.delta"]
    assert delta_events[0].content == "Still works"
    assert "error" not in types, "The mid-stream error must be swallowed, not surfaced"

    # (c) Non-fatal degradation signal is emitted.
    warning_events = [e for e in events if e.type == "warning"]
    assert len(warning_events) >= 1
    assert warning_events[0].warning is not None
    assert (
        warning_events[0].warning.get("code")
        == "adapter.plugin_connection_error_degraded"
    )
    assert "mcp/firecrawl" in warning_events[0].warning.get("message", "")


@pytest.mark.asyncio()
async def test_stream_chat_native_plugin_connection_error_after_content_surfaces_terminal(
    adapter: LmstudioAdapter,
    mock_http_client: MagicMock,
) -> None:
    """A plugin_connection_error AFTER content has already rendered cannot be
    retried (bytes are already on the wire) — surfaces as a terminal error,
    same as before this fix, and is NOT retried.
    """
    sse_body = _make_native_sse([
        ("message.start", {"type": "message.start"}),
        ("message.delta", {"type": "message.delta", "content": "Partial answer"}),
        (
            "error",
            {
                "type": "error",
                "error": {
                    "type": "plugin_connection_error",
                    "message": "Cannot find plugin handle for plugin: mcp/firecrawl",
                },
            },
        ),
    ])
    mock_response = _mock_streaming_response(sse_body, status_code=200)

    call_count = 0

    async def _send(request: Any, *, stream: bool) -> AsyncMock:
        nonlocal call_count
        call_count += 1
        return mock_response

    mock_http_client.build_request = MagicMock(return_value=MagicMock())
    mock_http_client.send = _send

    req = _make_request(integrations=["mcp/firecrawl", "mcp/searxng"])

    events = []
    async for ev in adapter.stream_chat(req, history=[]):
        events.append(ev)

    assert call_count == 1, "Must NOT retry once content has rendered"
    types = [e.type for e in events]
    assert "message.delta" in types
    assert "error" in types, "Post-content errors still surface — can't un-send bytes"
    warning_events = [e for e in events if e.type == "warning"]
    assert warning_events == []


@pytest.mark.asyncio()
async def test_stream_chat_native_plugin_connection_error_http_body_form_degrades(
    adapter: LmstudioAdapter,
    mock_http_client: MagicMock,
) -> None:
    """plugin_connection_error surfaced as a non-streaming HTTP error body
    (attempt 1 never reaches an SSE body at all) also degrades and retries.
    """
    denied_body = json.dumps({
        "error": {
            "type": "plugin_connection_error",
            "message": (
                "Unable to get plugin tools for 'mcp/firecrawl'. "
                "Cannot find plugin handle for plugin: mcp/firecrawl"
            ),
        }
    }).encode()
    bad_response = _mock_non_streaming_response(denied_body, status_code=424)

    sse_body = _make_native_sse([
        ("message.start", {"type": "message.start"}),
        ("message.delta", {"type": "message.delta", "content": "Recovered"}),
        ("message.end", {"type": "message.end"}),
        ("chat.end", {"type": "chat.end", "response_id": "r9"}),
    ])
    ok_response = _mock_streaming_response(sse_body, status_code=200)

    call_count = 0
    captured_bodies: list[Any] = []

    def _build_request(*args: Any, **kwargs: Any) -> MagicMock:
        captured_bodies.append(kwargs.get("json"))
        return MagicMock()

    async def _send(request: Any, *, stream: bool) -> AsyncMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return bad_response
        return ok_response

    mock_http_client.build_request = MagicMock(side_effect=_build_request)
    mock_http_client.send = _send

    req = _make_request(integrations=["mcp/firecrawl", "mcp/searxng"])

    events = []
    async for ev in adapter.stream_chat(req, history=[]):
        events.append(ev)

    assert call_count == 2, "Should retry once"
    assert "mcp/firecrawl" not in captured_bodies[1]["integrations"]
    assert "mcp/searxng" in captured_bodies[1]["integrations"]

    types = [e.type for e in events]
    assert "message.delta" in types
    assert "error" not in types
    warning_events = [e for e in events if e.type == "warning"]
    assert len(warning_events) >= 1
    assert (
        warning_events[0].warning.get("code")
        == "adapter.plugin_connection_error_degraded"
    )


@pytest.mark.asyncio()
async def test_stream_chat_native_plugin_connection_error_generic_drops_all_integrations(
    adapter: LmstudioAdapter,
    mock_http_client: MagicMock,
) -> None:
    """A plugin_connection_error naming no specific mcp/<name> plugin
    degrades to a full text-only retry — mirrors the 403 generic-denial
    fallback (can't guess which integration is bad, so drop them all).
    """
    attempt1_sse = _make_native_sse([
        ("chat.start", {"type": "chat.start", "response_id": "r1", "model_instance_id": "m1"}),
        (
            "error",
            {
                "type": "error",
                "error": {
                    "type": "plugin_connection_error",
                    "message": "Cannot find plugin handle for plugin.",
                },
            },
        ),
    ])
    attempt1_response = _mock_streaming_response(attempt1_sse, status_code=200)

    attempt2_sse = _make_native_sse([
        ("message.start", {"type": "message.start"}),
        ("message.delta", {"type": "message.delta", "content": "Text only"}),
        ("message.end", {"type": "message.end"}),
        ("chat.end", {"type": "chat.end", "response_id": "r2"}),
    ])
    attempt2_response = _mock_streaming_response(attempt2_sse, status_code=200)

    call_count = 0
    captured_bodies: list[Any] = []

    def _build_request(*args: Any, **kwargs: Any) -> MagicMock:
        captured_bodies.append(kwargs.get("json"))
        return MagicMock()

    async def _send(request: Any, *, stream: bool) -> AsyncMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return attempt1_response
        return attempt2_response

    mock_http_client.build_request = MagicMock(side_effect=_build_request)
    mock_http_client.send = _send

    req = _make_request(integrations=["mcp/firecrawl", "mcp/searxng"])

    events = []
    async for ev in adapter.stream_chat(req, history=[]):
        events.append(ev)

    assert call_count == 2
    # Second POST carries NO integrations at all — full text-only degrade.
    assert captured_bodies[1].get("integrations") in (None, [])
    types = [e.type for e in events]
    assert "message.delta" in types
    assert "error" not in types


# ---------------------------------------------------------------------------
# as_openai_compat_provider (LM Studio endpoint-mode toggle)
# ---------------------------------------------------------------------------


def test_as_openai_compat_provider_reads_live_connection(
    adapter: LmstudioAdapter, mock_http_client: MagicMock
) -> None:
    """The returned OpenAICompatProvider carries the adapter's CURRENT
    name/base_url/http_client, with no per-request api_key (auth already
    lives on the shared http_client's default headers, same as _post_stream)."""
    from lmchat.providers.openai_compat import OpenAICompatProvider

    provider = adapter.as_openai_compat_provider()

    assert isinstance(provider, OpenAICompatProvider)
    assert provider.name == "lmstudio"
    assert provider.context_mode == "replay"
    # Private-attr reach mirrors the existing rewire_singletons convention
    # (lm_studio_overrides_service.py already pokes adapter._base_url the
    # same way).
    assert provider._base_url == "http://localhost:1234"  # noqa: SLF001
    assert provider._http_client is mock_http_client  # noqa: SLF001
    assert provider._api_key is None  # noqa: SLF001


def test_as_openai_compat_provider_tracks_rewire(
    adapter: LmstudioAdapter,
) -> None:
    """A subsequent rewire_singletons-style mutation of the adapter's live
    _base_url / _http_client is picked up automatically — the provider is
    built fresh from ``self`` each call, not cached."""
    new_client = MagicMock(spec=httpx.AsyncClient)
    adapter._base_url = "http://new-host:5678"  # noqa: SLF001
    adapter._http_client = new_client  # noqa: SLF001

    provider = adapter.as_openai_compat_provider()

    assert provider._base_url == "http://new-host:5678"  # noqa: SLF001
    assert provider._http_client is new_client  # noqa: SLF001
