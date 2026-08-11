# SPDX-License-Identifier: Apache-2.0
"""Tests for OpenAICompatProvider — Workstream A2.

Covers:
- stream_chat: happy path (SSE stream → CanonicalEvents including message.delta "hi").
- stream_chat: full-replay body shape (messages array contains prior history + current turn).
- stream_chat: sanitize strips top_k / store / integrations from the POSTed body.
- stream_chat: non-200 response → single error CanonicalEvent.
- stream_chat: tools param accepted without failing.
- Protocol isinstance check.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from lmchat.lmstudio.types import (
    CanonicalChatRequest,
    CanonicalEvent,
    CanonicalInputBlock,
    CanonicalMessage,
    CanonicalTool,
)
from lmchat.providers import OpenAICompatProvider
from lmchat.providers.base import ChatProvider

# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------


def _sse_bytes(*payloads: str) -> bytes:
    """Build an OpenAI-style SSE byte string from data payloads.

    Each payload becomes ``data: <payload>\\n\\n``.  A ``[DONE]`` sentinel is
    always appended at the end.

    Args:
        *payloads: The JSON strings (or ``"[DONE]"``) to embed.

    Returns:
        Raw bytes that mimic what an httpx streaming response yields.
    """
    lines = []
    for p in payloads:
        lines.append(f"data: {p}\n\n")
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode("utf-8")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def history() -> list[CanonicalMessage]:
    """Two prior turns (user + assistant) for history-replay assertions."""
    return [
        CanonicalMessage(role="user", content="Who are you?"),
        CanonicalMessage(role="assistant", content="I am an AI."),
    ]


@pytest.fixture()
def base_request() -> CanonicalChatRequest:
    """Minimal CanonicalChatRequest — no LM-Studio-specific fields set."""
    return CanonicalChatRequest(
        model="gpt-4o-mini",
        input=[CanonicalInputBlock(type="text", content="Hello!")],
        stream=True,
    )


@pytest.fixture()
def dirty_request() -> CanonicalChatRequest:
    """Request with LM-Studio-specific fields that sanitize must strip."""
    return CanonicalChatRequest(
        model="gpt-4o-mini",
        input=[CanonicalInputBlock(type="text", content="Strip me!")],
        stream=True,
        top_k=40,
        store=True,
        integrations=["mcp/context7"],
        previous_response_id="resp-xyz",
        min_p=0.05,
        repeat_penalty=1.1,
    )


def _make_mock_client(*, status_code: int = 200, body_bytes: bytes) -> MagicMock:
    """Build a mock httpx.AsyncClient whose .stream() context manager yields body_bytes.

    The mock captures the POST call arguments so tests can inspect the
    serialised body and headers.

    Args:
        status_code: HTTP status code the mock response will report.
        body_bytes:  Raw bytes the mock response's aiter_bytes() will yield
                     (a single chunk per call).

    Returns:
        A :class:`unittest.mock.MagicMock` that behaves like
        :class:`httpx.AsyncClient` for the purposes of these tests.
    """
    # Build the response mock.
    response_mock = MagicMock(spec=httpx.Response)
    response_mock.status_code = status_code

    # aiter_bytes yields one chunk.
    async def _aiter_bytes():
        yield body_bytes

    response_mock.aiter_bytes = _aiter_bytes

    # aread() returns the full body (for non-200 path).
    response_mock.aread = AsyncMock(return_value=body_bytes)

    # __aenter__ / __aexit__ for use as async context manager.
    cm_mock = AsyncMock()
    cm_mock.__aenter__ = AsyncMock(return_value=response_mock)
    cm_mock.__aexit__ = AsyncMock(return_value=False)

    # The client itself.
    client_mock = MagicMock(spec=httpx.AsyncClient)
    client_mock.stream = MagicMock(return_value=cm_mock)

    return client_mock


# ---------------------------------------------------------------------------
# Protocol / isinstance
# ---------------------------------------------------------------------------


class TestOpenAICompatProviderProtocol:
    def test_satisfies_chat_provider_protocol(self):
        """OpenAICompatProvider duck-types ChatProvider (@runtime_checkable)."""
        provider = OpenAICompatProvider(
            name="openai",
            base_url="https://api.openai.com",
            api_key="sk-test",
            http_client=MagicMock(spec=httpx.AsyncClient),
        )
        assert isinstance(provider, ChatProvider)

    def test_name_and_context_mode(self):
        provider = OpenAICompatProvider(
            name="openrouter",
            base_url="https://openrouter.ai/api",
            api_key=None,
            http_client=MagicMock(spec=httpx.AsyncClient),
        )
        assert provider.name == "openrouter"
        assert provider.context_mode == "replay"

    def test_trailing_slash_stripped_from_base_url(self):
        provider = OpenAICompatProvider(
            name="groq",
            base_url="https://api.groq.com/openai/",
            api_key="gsk_test",
            http_client=MagicMock(spec=httpx.AsyncClient),
        )
        assert not provider._base_url.endswith("/")
        assert provider._base_url == "https://api.groq.com/openai"

    def test_auth_headers_with_key(self):
        provider = OpenAICompatProvider(
            name="openai",
            base_url="https://api.openai.com",
            api_key="sk-abc",
            http_client=MagicMock(spec=httpx.AsyncClient),
        )
        assert provider.auth_headers() == {"Authorization": "Bearer sk-abc"}

    def test_auth_headers_without_key(self):
        provider = OpenAICompatProvider(
            name="local",
            base_url="http://localhost:8000",
            api_key=None,
            http_client=MagicMock(spec=httpx.AsyncClient),
        )
        assert provider.auth_headers() == {}

    def test_trailing_v1_stripped_from_base_url(self):
        """OpenRouter's own docs show a base_url already ending in '/v1';
        every URL-construction call site appends '/v1/...' itself, so this
        must be stripped or requests double up to '.../v1/v1/...' (404)."""
        provider = OpenAICompatProvider(
            name="openrouter",
            base_url="https://openrouter.ai/api/v1",
            api_key="or-key",
            http_client=MagicMock(spec=httpx.AsyncClient),
        )
        assert provider._base_url == "https://openrouter.ai/api"

    def test_no_v1_suffix_base_url_unchanged(self):
        """The equivalent base_url without the /v1 suffix is left as-is —
        both forms must converge on the same effective URL."""
        provider = OpenAICompatProvider(
            name="openrouter",
            base_url="https://openrouter.ai/api",
            api_key="or-key",
            http_client=MagicMock(spec=httpx.AsyncClient),
        )
        assert provider._base_url == "https://openrouter.ai/api"

    def test_trailing_v1_and_slash_both_stripped(self):
        provider = OpenAICompatProvider(
            name="openrouter",
            base_url="https://openrouter.ai/api/v1/",
            api_key="or-key",
            http_client=MagicMock(spec=httpx.AsyncClient),
        )
        assert provider._base_url == "https://openrouter.ai/api"


# ---------------------------------------------------------------------------
# Happy-path streaming
# ---------------------------------------------------------------------------


class TestStreamChatHappyPath:
    """stream_chat with a 200 SSE stream → CanonicalEvents."""

    @pytest.mark.asyncio
    async def test_yields_message_delta(self, base_request, history):
        """A single-chunk SSE stream yields a message.delta carrying 'hi'."""
        chunk = json.dumps({
            "choices": [{"delta": {"content": "hi"}, "finish_reason": None}]
        })
        done_chunk = json.dumps({
            "choices": [{"delta": {}, "finish_reason": "stop"}]
        })
        sse = _sse_bytes(chunk, done_chunk)
        client = _make_mock_client(status_code=200, body_bytes=sse)

        provider = OpenAICompatProvider(
            name="openai",
            base_url="https://api.openai.com",
            api_key="sk-test",
            http_client=client,
        )

        events: list[CanonicalEvent] = []
        async for ev in provider.stream_chat(base_request, history=history):
            events.append(ev)

        types = [e.type for e in events]
        assert "message.delta" in types

        delta_events = [e for e in events if e.type == "message.delta"]
        assert len(delta_events) == 1
        assert delta_events[0].content == "hi"

    @pytest.mark.asyncio
    async def test_emits_message_start_and_end(self, base_request, history):
        """The compat decoder synthesises message.start and message.end."""
        chunk = json.dumps({
            "choices": [{"delta": {"content": "hello"}, "finish_reason": None}]
        })
        stop_chunk = json.dumps({
            "choices": [{"delta": {}, "finish_reason": "stop"}]
        })
        sse = _sse_bytes(chunk, stop_chunk)
        client = _make_mock_client(status_code=200, body_bytes=sse)

        provider = OpenAICompatProvider(
            name="openai",
            base_url="https://api.openai.com",
            api_key="sk-test",
            http_client=client,
        )

        events: list[CanonicalEvent] = []
        async for ev in provider.stream_chat(base_request, history=history):
            events.append(ev)

        types = [e.type for e in events]
        assert "message.start" in types
        assert "message.end" in types
        assert "chat.end" in types

    @pytest.mark.asyncio
    async def test_posts_to_correct_url(self, base_request, history):
        """The POST URL must be {base_url}/v1/chat/completions."""
        sse = _sse_bytes(
            json.dumps({"choices": [{"delta": {"content": "x"}, "finish_reason": None}]}),
            json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        )
        client = _make_mock_client(status_code=200, body_bytes=sse)

        provider = OpenAICompatProvider(
            name="openrouter",
            base_url="https://openrouter.ai/api",
            api_key="or-key",
            http_client=client,
        )

        async for _ in provider.stream_chat(base_request, history=history):
            pass

        call_args = client.stream.call_args
        assert call_args is not None
        method, url = call_args.args
        assert method == "POST"
        assert url == "https://openrouter.ai/api/v1/chat/completions"

    @pytest.mark.asyncio
    async def test_tools_param_accepted(self, base_request, history):
        """Non-empty tools param must not cause an exception or yield an error."""
        sse = _sse_bytes(
            json.dumps({"choices": [{"delta": {"content": "ok"}, "finish_reason": None}]}),
            json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        )
        client = _make_mock_client(status_code=200, body_bytes=sse)

        provider = OpenAICompatProvider(
            name="openai",
            base_url="https://api.openai.com",
            api_key="sk-test",
            http_client=client,
        )

        tool = CanonicalTool(
            name="search",
            description="Search the web",
            parameters={"type": "object", "properties": {}},
        )

        events: list[CanonicalEvent] = []
        async for ev in provider.stream_chat(
            base_request, history=history, tools=[tool]
        ):
            events.append(ev)

        error_events = [e for e in events if e.type == "error"]
        assert error_events == [], f"Unexpected error events: {error_events}"


# ---------------------------------------------------------------------------
# Body / history shape
# ---------------------------------------------------------------------------


class TestStreamChatBodyShape:
    """Verify the JSON body POSTed to the upstream."""

    def _capture_body(self, client: MagicMock) -> dict:  # type: ignore[type-arg]
        """Extract the deserialized JSON body from the captured stream call."""
        call_args = client.stream.call_args
        assert call_args is not None, "client.stream was not called"
        # content= keyword arg (provider passes body as content=json.dumps(...))
        raw: bytes | str = call_args.kwargs.get("content") or b""
        if not raw and len(call_args.args) > 2:
            raw = call_args.args[2]
        return json.loads(raw)

    @pytest.mark.asyncio
    async def test_body_contains_full_history(self, base_request, history):
        """The messages array includes prior history turns + current turn."""
        sse = _sse_bytes(
            json.dumps({"choices": [{"delta": {"content": "x"}, "finish_reason": None}]}),
            json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        )
        client = _make_mock_client(status_code=200, body_bytes=sse)

        provider = OpenAICompatProvider(
            name="openai",
            base_url="https://api.openai.com",
            api_key="sk-test",
            http_client=client,
        )

        async for _ in provider.stream_chat(base_request, history=history):
            pass

        body = self._capture_body(client)
        messages = body["messages"]

        # history has 2 turns; current turn adds 1 more.
        assert len(messages) >= 3, f"Expected ≥3 messages, got {len(messages)}: {messages}"

        roles = [m["role"] for m in messages]
        assert roles[0] == "user"       # first history turn
        assert roles[1] == "assistant"  # second history turn
        assert roles[-1] == "user"      # current turn

    @pytest.mark.asyncio
    async def test_body_has_model_and_stream(self, base_request, history):
        """POSTed body carries model and stream=True."""
        sse = _sse_bytes(
            json.dumps({"choices": [{"delta": {"content": "x"}, "finish_reason": None}]}),
            json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        )
        client = _make_mock_client(status_code=200, body_bytes=sse)

        provider = OpenAICompatProvider(
            name="openai",
            base_url="https://api.openai.com",
            api_key="sk-test",
            http_client=client,
        )

        async for _ in provider.stream_chat(base_request, history=history):
            pass

        body = self._capture_body(client)
        assert body["model"] == "gpt-4o-mini"
        assert body["stream"] is True


# ---------------------------------------------------------------------------
# Sanitization
# ---------------------------------------------------------------------------


class TestSanitization:
    """Verify LM-Studio-specific + OAI-incompatible fields are stripped."""

    def _capture_body(self, client: MagicMock) -> dict:  # type: ignore[type-arg]
        call_args = client.stream.call_args
        assert call_args is not None
        raw: bytes | str = call_args.kwargs.get("content") or b""
        return json.loads(raw)

    @pytest.mark.asyncio
    async def test_sanitize_strips_top_k(self, dirty_request, history):
        """top_k must NOT appear in the POSTed body."""
        sse = _sse_bytes(
            json.dumps({"choices": [{"delta": {"content": "x"}, "finish_reason": None}]}),
            json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        )
        client = _make_mock_client(status_code=200, body_bytes=sse)
        provider = OpenAICompatProvider(
            name="openai",
            base_url="https://api.openai.com",
            api_key="sk-test",
            http_client=client,
        )

        async for _ in provider.stream_chat(dirty_request, history=history):
            pass

        body = self._capture_body(client)
        assert "top_k" not in body

    @pytest.mark.asyncio
    async def test_sanitize_strips_store(self, dirty_request, history):
        """store must NOT appear in the POSTed body."""
        sse = _sse_bytes(
            json.dumps({"choices": [{"delta": {"content": "x"}, "finish_reason": None}]}),
            json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        )
        client = _make_mock_client(status_code=200, body_bytes=sse)
        provider = OpenAICompatProvider(
            name="openai",
            base_url="https://api.openai.com",
            api_key="sk-test",
            http_client=client,
        )

        async for _ in provider.stream_chat(dirty_request, history=history):
            pass

        body = self._capture_body(client)
        assert "store" not in body

    @pytest.mark.asyncio
    async def test_sanitize_strips_integrations(self, dirty_request, history):
        """integrations must NOT appear in the POSTed body (compat encoder omits it)."""
        sse = _sse_bytes(
            json.dumps({"choices": [{"delta": {"content": "x"}, "finish_reason": None}]}),
            json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        )
        client = _make_mock_client(status_code=200, body_bytes=sse)
        provider = OpenAICompatProvider(
            name="openai",
            base_url="https://api.openai.com",
            api_key="sk-test",
            http_client=client,
        )

        async for _ in provider.stream_chat(dirty_request, history=history):
            pass

        body = self._capture_body(client)
        assert "integrations" not in body

    @pytest.mark.asyncio
    async def test_sanitize_strips_min_p_and_repeat_penalty(self, dirty_request, history):
        """min_p and repeat_penalty must NOT appear in the body."""
        sse = _sse_bytes(
            json.dumps({"choices": [{"delta": {"content": "x"}, "finish_reason": None}]}),
            json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        )
        client = _make_mock_client(status_code=200, body_bytes=sse)
        provider = OpenAICompatProvider(
            name="openai",
            base_url="https://api.openai.com",
            api_key="sk-test",
            http_client=client,
        )

        async for _ in provider.stream_chat(dirty_request, history=history):
            pass

        body = self._capture_body(client)
        assert "min_p" not in body
        assert "repeat_penalty" not in body


# ---------------------------------------------------------------------------
# Non-200 error path
# ---------------------------------------------------------------------------


class TestStreamChatNon200:
    """Non-200 responses must yield exactly one error CanonicalEvent."""

    @pytest.mark.asyncio
    async def test_401_yields_error_event(self, base_request, history):
        """A 401 response yields a single error CanonicalEvent."""
        body_bytes = json.dumps({
            "error": {
                "type": "invalid_api_key",
                "message": "Incorrect API key provided.",
            }
        }).encode("utf-8")
        client = _make_mock_client(status_code=401, body_bytes=body_bytes)

        provider = OpenAICompatProvider(
            name="openai",
            base_url="https://api.openai.com",
            api_key="bad-key",
            http_client=client,
        )

        events: list[CanonicalEvent] = []
        async for ev in provider.stream_chat(base_request, history=history):
            events.append(ev)

        assert len(events) == 1
        assert events[0].type == "error"
        assert events[0].error is not None
        assert events[0].error.get("code") == "invalid_api_key"
        assert "Incorrect API key" in events[0].error.get("message", "")

    @pytest.mark.asyncio
    async def test_429_yields_error_event(self, base_request, history):
        """A 429 rate-limit response yields an error event."""
        body_bytes = json.dumps({
            "error": {
                "type": "rate_limit_exceeded",
                "message": "You have exceeded your rate limit.",
            }
        }).encode("utf-8")
        client = _make_mock_client(status_code=429, body_bytes=body_bytes)

        provider = OpenAICompatProvider(
            name="openrouter",
            base_url="https://openrouter.ai/api",
            api_key="or-key",
            http_client=client,
        )

        events: list[CanonicalEvent] = []
        async for ev in provider.stream_chat(base_request, history=history):
            events.append(ev)

        assert len(events) == 1
        assert events[0].type == "error"
        assert events[0].error is not None
        assert events[0].error.get("code") == "rate_limit_exceeded"

    @pytest.mark.asyncio
    async def test_non_json_non_200_yields_error_event(self, base_request, history):
        """A non-200 with non-JSON body still yields one error event."""
        body_bytes = b"Internal Server Error"
        client = _make_mock_client(status_code=500, body_bytes=body_bytes)

        provider = OpenAICompatProvider(
            name="groq",
            base_url="https://api.groq.com/openai",
            api_key="gsk_test",
            http_client=client,
        )

        events: list[CanonicalEvent] = []
        async for ev in provider.stream_chat(base_request, history=history):
            events.append(ev)

        assert len(events) == 1
        assert events[0].type == "error"
        assert events[0].error is not None
        # code falls back to the HTTP status string
        assert events[0].error.get("code") == "500"
        assert "Internal Server Error" in events[0].error.get("message", "")

    @pytest.mark.asyncio
    async def test_no_further_events_after_non_200(self, base_request, history):
        """After a non-200 error event, no further events are yielded."""
        body_bytes = json.dumps({
            "error": {"type": "auth_error", "message": "Unauthorized"}
        }).encode("utf-8")
        client = _make_mock_client(status_code=403, body_bytes=body_bytes)

        provider = OpenAICompatProvider(
            name="openai",
            base_url="https://api.openai.com",
            api_key="sk-test",
            http_client=client,
        )

        events: list[CanonicalEvent] = []
        async for ev in provider.stream_chat(base_request, history=history):
            events.append(ev)

        assert len(events) == 1


# ---------------------------------------------------------------------------
# Auth isolation — a shared http_client may carry ANOTHER provider's default
# Authorization header (e.g. the lifespan-shared client is scoped to LM
# Studio's own bearer key — see app.py).  A provider with no api_key of its
# own must not silently inherit that stray default.
# ---------------------------------------------------------------------------


class TestAuthIsolation:
    """Real httpx.AsyncClient + httpx.MockTransport — exercises actual
    header-merge behavior rather than mocking it away."""

    @pytest.mark.asyncio
    async def test_list_models_does_not_leak_shared_client_default_auth(
        self,
    ) -> None:
        """A provider built with no api_key must NOT send the shared
        client's default Authorization (e.g. a different provider's key)."""
        captured: dict[str, str] = {}

        async def _handler(request: httpx.Request) -> httpx.Response:
            captured["authorization"] = request.headers.get("authorization", "")
            return httpx.Response(200, json={"data": []})

        transport = httpx.MockTransport(_handler)
        shared = httpx.AsyncClient(
            headers={"Authorization": "Bearer LM-STUDIO-KEY"},
            transport=transport,
        )
        try:
            provider = OpenAICompatProvider(
                name="openrouter",
                base_url="https://openrouter.ai/api",
                api_key=None,
                http_client=shared,
            )
            await provider.list_models_detailed()
        finally:
            await shared.aclose()

        assert captured["authorization"] != "Bearer LM-STUDIO-KEY"

    @pytest.mark.asyncio
    async def test_list_models_uses_own_key_over_shared_client_default(
        self,
    ) -> None:
        """A provider with its OWN api_key sends that key, not the shared
        client's default."""
        captured: dict[str, str] = {}

        async def _handler(request: httpx.Request) -> httpx.Response:
            captured["authorization"] = request.headers.get("authorization", "")
            return httpx.Response(200, json={"data": []})

        transport = httpx.MockTransport(_handler)
        shared = httpx.AsyncClient(
            headers={"Authorization": "Bearer LM-STUDIO-KEY"},
            transport=transport,
        )
        try:
            provider = OpenAICompatProvider(
                name="openrouter",
                base_url="https://openrouter.ai/api",
                api_key="ORKEY",
                http_client=shared,
            )
            await provider.list_models_detailed()
        finally:
            await shared.aclose()

        assert captured["authorization"] == "Bearer ORKEY"

    @pytest.mark.asyncio
    async def test_inherit_shared_client_auth_opts_into_the_default(self) -> None:
        """The one legitimate exception: a provider explicitly built with
        inherit_shared_client_auth=True (LmstudioAdapter's own
        openai_compat view of ITS OWN shared connection) keeps using the
        shared client's default Authorization instead of stripping it."""
        captured: dict[str, str] = {}

        async def _handler(request: httpx.Request) -> httpx.Response:
            captured["authorization"] = request.headers.get("authorization", "")
            return httpx.Response(200, json={"data": []})

        transport = httpx.MockTransport(_handler)
        shared = httpx.AsyncClient(
            headers={"Authorization": "Bearer LM-STUDIO-KEY"},
            transport=transport,
        )
        try:
            provider = OpenAICompatProvider(
                name="lmstudio",
                base_url="http://localhost:1234",
                api_key=None,
                http_client=shared,
                inherit_shared_client_auth=True,
            )
            await provider.list_models_detailed()
        finally:
            await shared.aclose()

        assert captured["authorization"] == "Bearer LM-STUDIO-KEY"
