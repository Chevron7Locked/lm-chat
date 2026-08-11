# SPDX-License-Identifier: Apache-2.0
"""Tests for the ChatProvider Protocol seam.

Covers:
- Structural isinstance check via @runtime_checkable.
- sanitize_request_for_provider: replay strips LM-Studio-specific + OAI-
  incompatible sampler fields; chain leaves the request untouched.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from lmchat.lmstudio.types import (
    CanonicalChatRequest,
    CanonicalEvent,
    CanonicalInputBlock,
    CanonicalMessage,
    CanonicalTool,
)
from lmchat.providers.base import (
    ChatProvider,
    ContextMode,
    sanitize_request_for_provider,
)

# ---------------------------------------------------------------------------
# Minimal dummy implementation (satisfies the Protocol structurally)
# ---------------------------------------------------------------------------


class _DummyProvider:
    """Minimal concrete class that duck-types ChatProvider."""

    name: str = "dummy"
    context_mode: ContextMode = "replay"

    async def list_models(self):
        return []

    async def stream_chat(
        self,
        request: CanonicalChatRequest,
        *,
        history: list[CanonicalMessage] | None,
        tools: list[CanonicalTool] | None = None,
        cumulative_tool_rounds: int = 0,
    ) -> AsyncIterator[CanonicalEvent]:
        # pragma: no cover
        return
        yield  # make it an async generator  # type: ignore[misc]

    def auth_headers(self) -> dict[str, str]:
        return {}


# ---------------------------------------------------------------------------
# Fixture: a CanonicalChatRequest with all LM-Studio-specific fields set
# ---------------------------------------------------------------------------


@pytest.fixture()
def full_request() -> CanonicalChatRequest:
    """A request with every LM-Studio-specific and sampler field populated."""
    return CanonicalChatRequest(
        model="qwen3.6-35b-a3b",
        system_prompt="You are a test assistant.",
        input=[CanonicalInputBlock(type="text", content="Hello!")],
        previous_response_id="resp-abc123",
        integrations=["mcp/context7"],
        temperature=0.7,
        top_p=0.9,
        top_k=40,
        min_p=0.05,
        repeat_penalty=1.1,
        presence_penalty=0.2,
        max_tokens=512,
        max_output_tokens=256,
        store=True,
        stream=True,
    )


# ---------------------------------------------------------------------------
# Protocol / isinstance checks
# ---------------------------------------------------------------------------


class TestChatProviderProtocol:
    def test_dummy_satisfies_protocol(self):
        """_DummyProvider duck-types ChatProvider (runtime_checkable)."""
        provider = _DummyProvider()
        assert isinstance(provider, ChatProvider)

    def test_plain_object_does_not_satisfy_protocol(self):
        """An object missing the required attributes is not a ChatProvider."""

        class _Incomplete:
            name = "incomplete"
            # missing context_mode, list_models, stream_chat, auth_headers

        assert not isinstance(_Incomplete(), ChatProvider)

    def test_instance_with_all_attrs_satisfies(self):
        """Explicit attribute assignment covers the protocol attributes."""
        provider = _DummyProvider()
        assert provider.name == "dummy"
        assert provider.context_mode == "replay"

    def test_lmstudio_stub_satisfies_protocol(self):
        """A stub mirroring LmstudioAdapter's ChatProvider attrs passes isinstance.

        Constructs a stub (not the real adapter, which requires an http_client)
        to confirm that name="lmstudio" + context_mode="chain" + async stream_chat
        structurally satisfies ChatProvider.
        """

        class _LmstudioStub:
            """Minimal duck-type mirroring LmstudioAdapter's Protocol surface."""

            name: str = "lmstudio"
            context_mode: ContextMode = "chain"

            async def stream_chat(
                self,
                request: CanonicalChatRequest,
                *,
                history: list[CanonicalMessage] | None,
                tools: list[CanonicalTool] | None = None,
                cumulative_tool_rounds: int = 0,
            ) -> AsyncIterator[CanonicalEvent]:
                # pragma: no cover
                return
                yield  # type: ignore[misc]

        stub = _LmstudioStub()
        assert isinstance(stub, ChatProvider)
        assert stub.name == "lmstudio"
        assert stub.context_mode == "chain"


# ---------------------------------------------------------------------------
# sanitize_request_for_provider — replay mode
# ---------------------------------------------------------------------------


class TestSanitizeReplay:
    def test_strip_store(self, full_request: CanonicalChatRequest):
        out = sanitize_request_for_provider(full_request, context_mode="replay")
        assert out.store is None

    def test_strip_integrations(self, full_request: CanonicalChatRequest):
        out = sanitize_request_for_provider(full_request, context_mode="replay")
        assert out.integrations == []

    def test_strip_previous_response_id(self, full_request: CanonicalChatRequest):
        out = sanitize_request_for_provider(full_request, context_mode="replay")
        assert out.previous_response_id is None

    def test_strip_top_k(self, full_request: CanonicalChatRequest):
        out = sanitize_request_for_provider(full_request, context_mode="replay")
        assert out.top_k is None

    def test_strip_min_p(self, full_request: CanonicalChatRequest):
        out = sanitize_request_for_provider(full_request, context_mode="replay")
        assert out.min_p is None

    def test_strip_repeat_penalty(self, full_request: CanonicalChatRequest):
        out = sanitize_request_for_provider(full_request, context_mode="replay")
        assert out.repeat_penalty is None

    def test_keeps_presence_penalty(self, full_request: CanonicalChatRequest):
        """presence_penalty is part of the OAI spec — must NOT be stripped."""
        out = sanitize_request_for_provider(full_request, context_mode="replay")
        assert out.presence_penalty == full_request.presence_penalty

    def test_keeps_model(self, full_request: CanonicalChatRequest):
        out = sanitize_request_for_provider(full_request, context_mode="replay")
        assert out.model == full_request.model

    def test_keeps_temperature(self, full_request: CanonicalChatRequest):
        out = sanitize_request_for_provider(full_request, context_mode="replay")
        assert out.temperature == full_request.temperature

    def test_keeps_top_p(self, full_request: CanonicalChatRequest):
        out = sanitize_request_for_provider(full_request, context_mode="replay")
        assert out.top_p == full_request.top_p

    def test_keeps_system_prompt(self, full_request: CanonicalChatRequest):
        out = sanitize_request_for_provider(full_request, context_mode="replay")
        assert out.system_prompt == full_request.system_prompt

    def test_returns_new_object(self, full_request: CanonicalChatRequest):
        """sanitize must return a copy, not mutate the original (frozen model)."""
        out = sanitize_request_for_provider(full_request, context_mode="replay")
        # The original must still carry its LM-Studio fields.
        assert full_request.store is True
        assert full_request.previous_response_id == "resp-abc123"
        assert full_request.integrations == ["mcp/context7"]
        # And the output must differ.
        assert out is not full_request

    def test_strip_max_output_tokens(self, full_request: CanonicalChatRequest):
        """max_output_tokens is stripped: OpenAI/Groq reject it (they use max_tokens)."""
        out = sanitize_request_for_provider(full_request, context_mode="replay")
        assert out.max_output_tokens is None

    def test_all_stripped_fields_in_one_call(self, full_request: CanonicalChatRequest):
        """Convenience: assert all seven stripped fields at once."""
        out = sanitize_request_for_provider(full_request, context_mode="replay")
        assert out.store is None
        assert out.integrations == []
        assert out.previous_response_id is None
        assert out.top_k is None
        assert out.min_p is None
        assert out.repeat_penalty is None
        assert out.max_output_tokens is None


# ---------------------------------------------------------------------------
# sanitize_request_for_provider — chain mode
# ---------------------------------------------------------------------------


class TestSanitizeChain:
    def test_chain_returns_same_object(self, full_request: CanonicalChatRequest):
        """In chain mode the original request is returned unmodified."""
        out = sanitize_request_for_provider(full_request, context_mode="chain")
        assert out is full_request

    def test_chain_keeps_store(self, full_request: CanonicalChatRequest):
        out = sanitize_request_for_provider(full_request, context_mode="chain")
        assert out.store is True

    def test_chain_keeps_integrations(self, full_request: CanonicalChatRequest):
        out = sanitize_request_for_provider(full_request, context_mode="chain")
        assert out.integrations == ["mcp/context7"]

    def test_chain_keeps_previous_response_id(self, full_request: CanonicalChatRequest):
        out = sanitize_request_for_provider(full_request, context_mode="chain")
        assert out.previous_response_id == "resp-abc123"

    def test_chain_keeps_top_k(self, full_request: CanonicalChatRequest):
        out = sanitize_request_for_provider(full_request, context_mode="chain")
        assert out.top_k == 40

    def test_chain_keeps_min_p(self, full_request: CanonicalChatRequest):
        out = sanitize_request_for_provider(full_request, context_mode="chain")
        assert out.min_p == 0.05

    def test_chain_keeps_repeat_penalty(self, full_request: CanonicalChatRequest):
        out = sanitize_request_for_provider(full_request, context_mode="chain")
        assert out.repeat_penalty == 1.1
