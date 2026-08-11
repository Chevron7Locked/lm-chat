# SPDX-License-Identifier: Apache-2.0
"""Canonical Pydantic shapes for the LM Studio wire layer.

These are the SPA-facing shapes the adapter
consumes and emits. Every field here matches LM Studio's
API surface (checked against qwen3-8b, 2026-05-18).

Authority: docs/LM_STUDIO_HARNESS.md beats LM Studio's web docs when they
disagree. All event types are the exact wire names emitted by LM Studio's
native /api/v1/chat SSE stream.

Notable wire facts encoded here:
- system_prompt is a top-level string on the native request (NOT a
  system-role message block; both rejected with "Unrecognized key(s)").
- input blocks have NO role field on the native endpoint.
- tool_call.name IS a distinct wire event (observed 2026-05-18 with
  integrations: ["mcp/searxng"]). Its payload carries `tool_name` (the
  tool name string).
- tool_call.end does NOT exist; tool_call.success / tool_call.failure are
  the natural terminators.
- model_load.* events appear only on cold-start (model not yet loaded).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class CanonicalToolCall(BaseModel):
    """A tool call emitted by the assistant (native MCP or compat tool-use).

    The ``call_id`` field is set when the tool call originates from a
    ``/v1/responses`` output item.  The Responses API assigns a ``call_id``
    on the ``function_call`` output item; the value must be echoed back on
    the next-turn input array as ``call_id`` when feeding the tool result.

    For tool calls from the native ``/api/v1/chat`` surface or the legacy
    ``/v1/chat/completions`` surface, ``call_id`` is ``None``.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    arguments: dict  # type: ignore[type-arg]
    call_id: str | None = None  # Responses-API round-trip key; None on native/compat
    # The wire `output` of a tool_call.success event. None on every other
    # tool_call.* event (failure routes its output through event.error).
    # The FE ToolCallCard reads this to re-render results on reload.
    result: str | None = None


class CanonicalTool(BaseModel):
    """A tool definition sent by the SPA for compat-surface tool use."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    parameters: dict  # type: ignore[type-arg]  # JSON Schema object


class CanonicalMessage(BaseModel):
    """A fully-resolved prior turn (from the DB) used by the compat encoder.

    Not used by the native encoder (native threads history via
    previous_response_id, not by replaying messages).
    """

    model_config = ConfigDict(frozen=True)

    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    reasoning_content: str | None = None
    tool_calls: list[CanonicalToolCall] | None = None
    tool_call_id: str | None = None  # for role="tool"


class CanonicalInputBlock(BaseModel):
    """One content block in the current-turn input.

    No `role` field — native input is current-turn only, role-less.
    """

    model_config = ConfigDict(frozen=True)

    type: Literal["text", "image"]
    content: str | None = None  # for type="text"
    data_url: str | None = None  # for type="image", e.g. "data:image/png;base64,..."


class CanonicalChatRequest(BaseModel):
    """The SPA-facing request shape. LM Studio targets only.

    All generation params are accepted by LM Studio's
    /api/v1/chat as of 2026-05-18.
    """

    model_config = ConfigDict(frozen=True)

    model: str
    # system_prompt is a top-level string in LM Studio native (not a message block).
    system_prompt: str | None = None
    # input carries the CURRENT turn's content blocks (text, image).
    # Native doesn't take multi-turn history here; compat builds a full messages array.
    input: list[CanonicalInputBlock] = []
    previous_response_id: str | None = None  # native multi-turn server-side chain
    tools: list[CanonicalTool] = []
    # e.g. ["mcp/context7", "mcp/filesystem"]; None = apply admin defaults
    integrations: list[str] | None = None
    # Sampling / generation params (all accepted by LM Studio as of 2026-05-18):
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    min_p: float | None = None
    repeat_penalty: float | None = None
    presence_penalty: float | None = None
    max_tokens: int | None = None
    # native /api/v1/chat field name; max_tokens is compat-only
    max_output_tokens: int | None = None
    reasoning: str | None = None  # model-dependent string enum; cache catches rejection
    store: bool | None = None  # opt out of LM Studio stateful conversation retention
    stream: bool = True


class CanonicalEvent(BaseModel):
    """The SPA-facing SSE event shape, mirroring LM Studio's native stream.

    The type Literal enumerates the wire events observed 2026-05-18
    plus synthetic diagnostic events added by the lm-chat pipeline.
    Synthetic diagnostic events are annotated inline.

    Tool-call event excerpt (verbatim):
        event: tool_call.name
        data: {"type":"tool_call.name","tool_name":"search_web",
               "provider_info":{"type":"plugin","plugin_id":"mcp/searxng"}}

    Absent by design:
    - tool_call.end: does NOT exist; success/failure are the terminators.
    - response.created: does NOT exist on the native stream.
    """

    type: Literal[
        # Lifecycle
        "chat.start",
        "chat.end",
        # Cold-start only (model not yet loaded):
        "model_load.start",
        "model_load.progress",
        "model_load.end",
        # Prompt processing:
        "prompt_processing.start",
        "prompt_processing.progress",  # carries progress: 0.0-1.0
        "prompt_processing.end",
        # Generation:
        "message.start",
        "message.delta",
        "message.end",
        "reasoning.start",
        "reasoning.delta",
        "reasoning.end",
        # Tools (observed with MCP integrations; no tool_call.end):
        "tool_call.start",
        "tool_call.name",
        "tool_call.arguments",
        "tool_call.success",
        "tool_call.failure",
        # Synthetic diagnostic events (not wire events from LM Studio):
        "tool_call.repeat_warning",       # emitted by lm-chat pipeline
        "tool_call.failure_streak_warning",  # emitted by lm-chat pipeline
        "tool_call.name_warning",         # emitted by lm-chat pipeline
        # Non-fatal warnings (adapter-emitted, FE-handled via warning SSE frame):
        "warning",
        # Terminal error:
        "error",
    ]
    response_id: str | None = None  # present on chat.start and chat.end
    # Continue-chip closeout: why generation terminated,
    # carried on chat.end only. Values follow the OpenAI-compat
    # finish_reason convention LM Studio emits on /v1/chat/completions:
    # "stop" (natural end) | "length" (max_output_tokens truncation).
    # None on every other event type and on decoders that don't surface a
    # reason (native /api/v1/chat doesn't document one). The FE Continue
    # chip renders when stop_reason == "length".
    stop_reason: str | None = None
    # Real token stats from the native chat.end `result.stats` block.
    # None when the upstream surface doesn't provide them (compat path).
    total_output_tokens: int | None = None
    tokens_per_second: float | None = None
    content: str | None = None  # for *.delta events
    progress: float | None = None  # for prompt_processing.progress
    tool_call: CanonicalToolCall | None = None  # for tool_call.* events
    error: dict | None = None  # type: ignore[type-arg]  # for error event
    warning: dict | None = None  # type: ignore[type-arg]  # for warning event (non-fatal)
    model_instance_id: str | None = None  # for chat.start


# Resolve forward references on CanonicalMessage (uses CanonicalToolCall).
CanonicalMessage.model_rebuild()
