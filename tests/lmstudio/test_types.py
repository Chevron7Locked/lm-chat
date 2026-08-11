# SPDX-License-Identifier: Apache-2.0
"""Tests for lmchat.lmstudio.types — canonical Pydantic shapes.

Type-check tests:
- Round-trip via .model_dump() / .model_validate() for every shape.
- CanonicalEvent type Literal accepts every wire event name.
- CanonicalChatRequest defaults: stream=True, tools=[], integrations=[].
- CanonicalInputBlock has no role field.
- Pydantic raises ValidationError on bad type / unknown field assignment.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from lmchat.lmstudio.types import (
    CanonicalChatRequest,
    CanonicalEvent,
    CanonicalInputBlock,
    CanonicalMessage,
    CanonicalTool,
    CanonicalToolCall,
)

# ---------------------------------------------------------------------------
# CanonicalToolCall
# ---------------------------------------------------------------------------


class TestCanonicalToolCall:
    def test_round_trip(self) -> None:
        """Round-trip CanonicalToolCall via model_dump / model_validate."""
        tc = CanonicalToolCall(id="call_1", name="search_web", arguments={"q": "test"})
        dumped = tc.model_dump()
        restored = CanonicalToolCall.model_validate(dumped)
        assert restored == tc

    def test_fields(self) -> None:
        """All fields serialise correctly."""
        tc = CanonicalToolCall(id="x", name="calc", arguments={"expr": "1+1"})
        d = tc.model_dump()
        assert d["id"] == "x"
        assert d["name"] == "calc"
        assert d["arguments"] == {"expr": "1+1"}

    def test_missing_required_field_raises(self) -> None:
        """ValidationError when required fields are absent."""
        with pytest.raises(ValidationError):
            CanonicalToolCall.model_validate({"id": "x", "name": "y"})  # missing arguments


# ---------------------------------------------------------------------------
# CanonicalTool
# ---------------------------------------------------------------------------


class TestCanonicalTool:
    def test_round_trip(self) -> None:
        """Round-trip CanonicalTool."""
        tool = CanonicalTool(
            name="calculator",
            description="Evaluate a Python expression.",
            parameters={"type": "object", "properties": {"expr": {"type": "string"}}},
        )
        assert CanonicalTool.model_validate(tool.model_dump()) == tool

    def test_parameters_is_dict(self) -> None:
        """parameters field accepts arbitrary dicts (JSON Schema)."""
        tool = CanonicalTool(
            name="t", description="d", parameters={"anyOf": [{"type": "string"}]}
        )
        assert tool.parameters["anyOf"][0]["type"] == "string"


# ---------------------------------------------------------------------------
# CanonicalMessage
# ---------------------------------------------------------------------------


class TestCanonicalMessage:
    def test_round_trip_simple(self) -> None:
        """Round-trip a simple user message."""
        msg = CanonicalMessage(role="user", content="Hello")
        assert CanonicalMessage.model_validate(msg.model_dump()) == msg

    def test_round_trip_assistant_with_tool_calls(self) -> None:
        """Round-trip assistant message with tool_calls list."""
        tc = CanonicalToolCall(id="c1", name="fn", arguments={"k": "v"})
        msg = CanonicalMessage(role="assistant", content=None, tool_calls=[tc])
        restored = CanonicalMessage.model_validate(msg.model_dump())
        assert restored.tool_calls is not None
        assert restored.tool_calls[0].name == "fn"

    def test_tool_role_with_tool_call_id(self) -> None:
        """tool-role message round-trips tool_call_id."""
        msg = CanonicalMessage(role="tool", content="42", tool_call_id="c1")
        restored = CanonicalMessage.model_validate(msg.model_dump())
        assert restored.tool_call_id == "c1"
        assert restored.role == "tool"

    def test_invalid_role_raises(self) -> None:
        """ValidationError on unknown role literal."""
        with pytest.raises(ValidationError):
            CanonicalMessage(role="invalid_role", content="x")  # type: ignore[arg-type]

    def test_reasoning_content_field(self) -> None:
        """reasoning_content field round-trips."""
        msg = CanonicalMessage(role="assistant", reasoning_content="<think>...</think>")
        assert msg.reasoning_content == "<think>...</think>"
        restored = CanonicalMessage.model_validate(msg.model_dump())
        assert restored.reasoning_content == "<think>...</think>"


# ---------------------------------------------------------------------------
# CanonicalInputBlock
# ---------------------------------------------------------------------------


class TestCanonicalInputBlock:
    def test_text_block_round_trip(self) -> None:
        """Text block round-trips."""
        block = CanonicalInputBlock(type="text", content="Hello world")
        restored = CanonicalInputBlock.model_validate(block.model_dump())
        assert restored.content == "Hello world"
        assert restored.type == "text"

    def test_image_block_round_trip(self) -> None:
        """Image block round-trips with data_url."""
        block = CanonicalInputBlock(type="image", data_url="data:image/png;base64,abc123")
        restored = CanonicalInputBlock.model_validate(block.model_dump())
        assert restored.data_url == "data:image/png;base64,abc123"

    def test_no_role_field(self) -> None:
        """CanonicalInputBlock does NOT have a role field (native input is role-less)."""
        block = CanonicalInputBlock(type="text", content="hi")
        dumped = block.model_dump()
        assert "role" not in dumped

    def test_invalid_type_raises(self) -> None:
        """ValidationError on invalid type literal."""
        with pytest.raises(ValidationError):
            CanonicalInputBlock(type="audio", content="x")  # type: ignore[arg-type]

    def test_exclude_none_omits_nulls(self) -> None:
        """model_dump(exclude_none=True) omits None fields (per encode_native usage)."""
        block = CanonicalInputBlock(type="text", content="hi")
        d = block.model_dump(exclude_none=True)
        assert "data_url" not in d
        assert d == {"type": "text", "content": "hi"}


# ---------------------------------------------------------------------------
# CanonicalChatRequest
# ---------------------------------------------------------------------------


class TestCanonicalChatRequest:
    def test_defaults(self) -> None:
        """stream=True, tools=[], integrations=None by default.

        integrations=None means "omitted by the FE — apply admin defaults at
        the route boundary".  Explicit [] means "user chose no tools".
        """
        req = CanonicalChatRequest(model="m", input=[])
        assert req.stream is True
        assert req.tools == []
        assert req.integrations is None

    def test_all_generation_params_accepted(self) -> None:
        """All live-verified generation params are accepted."""
        req = CanonicalChatRequest(
            model="qwen3-8b",
            input=[CanonicalInputBlock(type="text", content="hi")],
            temperature=0.7,
            top_p=0.9,
            top_k=40,
            min_p=0.05,
            repeat_penalty=1.1,
            max_tokens=512,
            max_output_tokens=256,
            reasoning="high",
            store=False,
            stream=True,
        )
        assert req.temperature == 0.7
        assert req.top_k == 40
        assert req.reasoning == "high"
        assert req.store is False

    def test_round_trip(self) -> None:
        """Full round-trip for CanonicalChatRequest."""
        req = CanonicalChatRequest(
            model="m",
            system_prompt="You are helpful.",
            input=[CanonicalInputBlock(type="text", content="Hi")],
            previous_response_id="resp_123",
            integrations=["mcp/searxng"],
            temperature=0.8,
        )
        restored = CanonicalChatRequest.model_validate(req.model_dump())
        assert restored.system_prompt == "You are helpful."
        assert restored.previous_response_id == "resp_123"
        assert restored.integrations == ["mcp/searxng"]

    def test_tools_list(self) -> None:
        """tools list round-trips."""
        tool = CanonicalTool(name="fn", description="d", parameters={})
        req = CanonicalChatRequest(model="m", input=[], tools=[tool])
        restored = CanonicalChatRequest.model_validate(req.model_dump())
        assert len(restored.tools) == 1
        assert restored.tools[0].name == "fn"


# ---------------------------------------------------------------------------
# CanonicalEvent
# ---------------------------------------------------------------------------

# All wire event names.
_ALL_EVENT_TYPES = [
    "chat.start",
    "chat.end",
    "model_load.start",
    "model_load.progress",
    "model_load.end",
    "prompt_processing.start",
    "prompt_processing.progress",
    "prompt_processing.end",
    "message.start",
    "message.delta",
    "message.end",
    "reasoning.start",
    "reasoning.delta",
    "reasoning.end",
    "tool_call.start",
    "tool_call.name",
    "tool_call.arguments",
    "tool_call.success",
    "tool_call.failure",
    "tool_call.repeat_warning",  # synthetic diagnostic event
    "error",
]


@pytest.mark.parametrize("event_type", _ALL_EVENT_TYPES)
def test_canonical_event_accepts_all_wire_types(event_type: str) -> None:
    """CanonicalEvent type Literal accepts every wire event name."""
    event = CanonicalEvent(type=event_type)  # type: ignore[arg-type]
    assert event.type == event_type


def test_canonical_event_rejects_unknown_type() -> None:
    """CanonicalEvent raises ValidationError on unknown event type."""
    with pytest.raises(ValidationError):
        CanonicalEvent(type="tool_call.end")  # type: ignore[arg-type]


def test_canonical_event_rejects_fabricated_event() -> None:
    """CanonicalEvent rejects response.created (never existed in wire stream)."""
    with pytest.raises(ValidationError):
        CanonicalEvent(type="response.created")  # type: ignore[arg-type]


def test_canonical_event_round_trip_delta() -> None:
    """message.delta round-trips with content."""
    evt = CanonicalEvent(type="message.delta", content="Hello")
    restored = CanonicalEvent.model_validate(evt.model_dump())
    assert restored.content == "Hello"
    assert restored.type == "message.delta"


def test_canonical_event_chat_start_fields() -> None:
    """chat.start carries response_id and model_instance_id."""
    evt = CanonicalEvent(
        type="chat.start",
        response_id="resp_abc",
        model_instance_id="inst_1",
    )
    assert evt.response_id == "resp_abc"
    assert evt.model_instance_id == "inst_1"


def test_canonical_event_tool_call_payload() -> None:
    """tool_call.name carries a CanonicalToolCall."""
    tc = CanonicalToolCall(id="", name="search_web", arguments={})
    evt = CanonicalEvent(type="tool_call.name", tool_call=tc)
    assert evt.tool_call is not None
    assert evt.tool_call.name == "search_web"


def test_canonical_event_error_payload() -> None:
    """error event carries error dict."""
    evt = CanonicalEvent(type="error", error={"code": "rate_limited", "message": "too fast"})
    assert evt.error is not None
    assert evt.error["code"] == "rate_limited"


def test_canonical_event_progress_payload() -> None:
    """prompt_processing.progress carries progress float."""
    evt = CanonicalEvent(type="prompt_processing.progress", progress=0.42)
    assert evt.progress == pytest.approx(0.42)


def test_canonical_event_no_extra_fields_on_end() -> None:
    """chat.end carries only response_id (not model_instance_id)."""
    evt = CanonicalEvent(type="chat.end", response_id="resp_xyz")
    # model_instance_id is None by default — not present for chat.end.
    assert evt.model_instance_id is None
    assert evt.response_id == "resp_xyz"
