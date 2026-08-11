# SPDX-License-Identifier: Apache-2.0
"""Native /api/v1/chat encoder and SSE decoder for LM Studio.

Wire-truth rules (authority order):
1. Harness probe output wins over docs or training memory.
2. The native SSE event names ARE the canonical event names — no translation;
   only the per-event payload is mapped into CanonicalEvent fields.
3. system_prompt is a top-level string, NOT a system-role message block.
4. input blocks carry NO role field.
5. tool_call.name wire payload uses `tool_name`; maps to CanonicalToolCall.name.
6. tool_call.end does NOT exist; success/failure are the terminators.

SSE parser: hand-rolled line-reader. httpx-sse is not in the dependency tree.
The hand-rolled parser is simpler (LM Studio SSE is well-formed) and
has no additional transitive dependencies.
"""
from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Final

import httpx

from lmchat.lmstudio.types import (
    CanonicalChatRequest,
    CanonicalEvent,
    CanonicalToolCall,
)
from lmchat.logging import get_logger

log = get_logger(__name__)

# Native params passed through to /api/v1/chat.
# reasoning and integrations are handled separately.
# LM Studio's /api/v1/chat endpoint, as of 2026-06-01:
#   - max_output_tokens IS accepted; max_tokens IS REJECTED ("Unrecognized key").
#     max_output_tokens is the native field name. Sampler-profile injection
#     translates max_tokens → max_output_tokens at the select_profile boundary.
#   - top_k, min_p, repeat_penalty, presence_penalty are ALL accepted by native
#     (checked with a reasoning model, 200 OK).
_NATIVE_SCALAR_PARAMS: Final[tuple[str, ...]] = (
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "repeat_penalty",
    "presence_penalty",
    "max_output_tokens",
    "reasoning",
)


def encode_native(req: CanonicalChatRequest) -> dict:  # type: ignore[type-arg]
    """Map a CanonicalChatRequest to a /api/v1/chat request body.

    Wire-verified rules:
    - system_prompt is a top-level string (not a message block).
    - input is a list of block dicts with NO role field.
    - previous_response_id threads multi-turn server-side state.
    - integrations activates LM Studio's MCP server-side execution.
    - If both integrations and tools are non-empty: integrations wins;
      tools is stripped from the outbound body; a structured-log WARNING
      is emitted with event="adapter.tools_dropped_for_integrations".
    - store=False is forwarded when set; omitted otherwise (LM Studio default is True).
    - Generation params (forwarded when non-None): temperature, top_p, top_k,
      min_p, repeat_penalty, presence_penalty, max_output_tokens, reasoning.

    Args:
        req: The canonical request from the SPA.

    Returns:
        A dict ready to be JSON-serialised and POSTed to /api/v1/chat.
    """
    body: dict = {  # type: ignore[type-arg]
        "model": req.model,
        "input": [block.model_dump(exclude_none=True) for block in req.input],
        "stream": req.stream,
    }

    # Strict-template handling: LM Studio's ``previous_response_id``
    # rehydrates the prior turn's server-side state, which already
    # includes the system_prompt from turn 1. Sending ``system_prompt``
    # AGAIN on the follow-up turn appends a second system message into
    # the rendered conversation, and strict Jinja templates (qwen3.6-35b
    # observed live, line 85: ``raise_exception('System message ...')``)
    # 400 with "Unable to generate parser for this template".
    #
    # Skip ``system_prompt`` whenever ``previous_response_id`` is set —
    # the server-side state already has it. Permissive templates (which
    # ignored duplicates silently) get the same correct behaviour;
    # strict templates stop 400ing.
    #
    # This also means a mid-conversation system_prompt CHANGE (e.g.,
    # admin switches preset on turn 3) does NOT propagate to LM
    # Studio's server-side state. The trade-off is acceptable: the
    # alternative is breaking strict templates entirely. A future
    # "system_prompt change forces a fresh chain" path would clear
    # ``previous_response_id`` when the prompt diffs from the prior
    # turn.
    if req.previous_response_id is not None:
        body["previous_response_id"] = req.previous_response_id
    elif req.system_prompt is not None:
        body["system_prompt"] = req.system_prompt

    # store=False opts out of LM Studio's server-side conversation retention.
    # Critical for sub-sessions: without it LM Studio stores each ephemeral
    # turn in a server-side chain and, on tool-use turns, tries to resume a
    # context_id the next call doesn't supply.  This causes a null-code error
    # event ~78s after the first tool_call.success (direct curl with
    # store:false works; the app without it dies after the first tool call).
    if req.store is not None:
        body["store"] = req.store

    if req.integrations is not None and req.integrations:
        body["integrations"] = req.integrations
        # Native rejects the top-level `tools` field outright
        # ("Unrecognized key(s) in object: 'tools'", as of 2026-05-18).
        # If the caller set both integrations and tools, drop tools and warn.
        if req.tools:
            log.warning(
                "adapter.tools_dropped_for_integrations",
                tools=[t.name for t in req.tools],
                model_id=req.model,
            )
        # tools is intentionally NOT added to body when integrations is set.

    for param in _NATIVE_SCALAR_PARAMS:
        val = getattr(req, param, None)
        if val is not None:
            body[param] = val

    return body


# ---------------------------------------------------------------------------
# SSE parser helpers
# ---------------------------------------------------------------------------





# ---------------------------------------------------------------------------
# Stable error shape from LM Studio error.type
# ---------------------------------------------------------------------------

# Maps LM Studio error.type values to stable canonical codes.
_LM_ERROR_TYPE_TO_CODE: Final[dict[str, str]] = {
    "exceed_context_size_error": "context_window_exceeded",
    "tool_format_generation_error": "tool_format_generation_error",
    "model_not_loaded": "model_not_loaded",
    "cancelled": "stream_cancelled",
}

# Human-readable fallback messages keyed by canonical code when LM Studio
# provides an empty ``message`` field.
_DEFAULT_MESSAGES: Final[dict[str, str]] = {
    "context_window_exceeded": "Prompt exceeds the model's context window.",
    "tool_format_generation_error": "The model produced a malformed tool call.",
    "model_not_loaded": "No model is currently loaded in LM Studio.",
    "stream_cancelled": "The upstream stream was cancelled.",
}


def _normalize_error_event(err_payload: object) -> dict:  # type: ignore[type-arg]
    """Map an LM Studio ``error`` event payload to a stable canonical shape.

    LM Studio ``error`` event payloads vary by surface and firmware version.
    This helper normalises them into ``{code, message, hint?, ...extras}``,
    so every layer above this function sees one consistent shape.

    Args:
        err_payload: The raw ``error`` or root payload from the SSE event JSON.

    Returns:
        A dict with at minimum ``code`` (non-empty) and ``message`` (non-empty).
        Context-window errors also carry ``n_prompt_tokens`` and ``n_ctx``.
    """
    if not isinstance(err_payload, dict):
        # Scalar or None — wrap it.
        return {
            "code": "upstream_error",
            "message": str(err_payload) if err_payload else "Unknown upstream error",
        }

    err_type: str = str(err_payload.get("type") or "")
    raw_message: str = (
        err_payload.get("message")
        or err_payload.get("error")
        or ""
    )

    canonical_code = str(
        _LM_ERROR_TYPE_TO_CODE.get(err_type)
        or err_payload.get("code")
        or err_type
        or "upstream_error"
    )
    message = (
        raw_message
        or _DEFAULT_MESSAGES.get(canonical_code)
        or f"{canonical_code} (no message)"
    )

    out: dict = {"code": canonical_code, "message": message}  # type: ignore[type-arg]

    # Preserve context-window token counts for FE display.
    if canonical_code == "context_window_exceeded":
        if "n_prompt_tokens" in err_payload:
            out["n_prompt_tokens"] = err_payload["n_prompt_tokens"]
        if "n_ctx" in err_payload:
            out["n_ctx"] = err_payload["n_ctx"]

    # Surface optional hint when present.
    hint = err_payload.get("hint")
    if hint:
        out["hint"] = hint

    return out


def _translate_native_event(
    event_name: str,
    data_json: str,
    _tc_state: dict | None = None,
) -> CanonicalEvent | None:
    """Translate one wire SSE event into a CanonicalEvent.

    The native event names ARE the canonical event names; no name translation
    occurs. Only the per-event payload fields are mapped.

    Args:
        event_name: The SSE `event:` field value (e.g. "message.delta").
        data_json:  The SSE `data:` field value (JSON string).

    Returns:
        A CanonicalEvent, or None if the event type is not recognised
        (forward-compatibility: new LM Studio events don't crash the decoder).
    """
    # The full set of recognised canonical event types from CanonicalEvent.type.
    _KNOWN_TYPES: Final[frozenset[str]] = frozenset({
        "chat.start", "chat.end",
        "model_load.start", "model_load.progress", "model_load.end",
        "prompt_processing.start", "prompt_processing.progress", "prompt_processing.end",
        "message.start", "message.delta", "message.end",
        "reasoning.start", "reasoning.delta", "reasoning.end",
        "tool_call.start", "tool_call.name", "tool_call.arguments",
        "tool_call.success", "tool_call.failure",
        "error",
    })

    if event_name not in _KNOWN_TYPES:
        log.warning(
            "native_decoder.unknown_event",
            event_name=event_name,
        )
        return None

    try:
        payload = json.loads(data_json)
    except json.JSONDecodeError:
        log.warning(
            "native_decoder.json_decode_error",
            event_name=event_name,
            data_snippet=data_json[:120],
        )
        return None

    # Build kwargs for CanonicalEvent constructor.
    kwargs: dict = {"type": event_name}  # type: ignore[type-arg]

    if event_name == "chat.start":
        kwargs["response_id"] = payload.get("response_id")
        kwargs["model_instance_id"] = payload.get("model_instance_id")

    elif event_name == "chat.end":
        # LM Studio native /api/v1/chat nests the response_id inside
        # `result.response_id` on the streaming chat.end event (observed
        # against qwen3-vl-8b-instruct, 2026-06-02). The
        # previous decode read `payload["response_id"]` (top-level)
        # which is always absent — so `previous_response_id` was
        # NEVER threaded forward, every turn after the first arrived
        # at LM Studio with no conversation history, and small models
        # produced the "third turn talks about primary colors" failure
        # mode the admin hit. Top-level fallback preserved in case
        # LM Studio changes the shape back.
        result = payload.get("result")
        if isinstance(result, dict) and "response_id" in result:
            kwargs["response_id"] = result.get("response_id")
        else:
            kwargs["response_id"] = payload.get("response_id")
        # Continue-chip closeout: the native wire shape
        # documented in docs/LM_STUDIO_HARNESS.md carries only response_id
        # on chat.end — no stop reason has been live-probed here. Read it
        # forward-compatibly (result-nested first, mirroring response_id;
        # top-level fallback) so a future LM Studio build that emits one
        # flows through without a decoder change. Absent → None → no chip.
        if isinstance(result, dict) and result.get("stop_reason") is not None:
            kwargs["stop_reason"] = result.get("stop_reason")
        else:
            kwargs["stop_reason"] = payload.get("stop_reason")
        # Real token
        # stats from the chat.end `result.stats` block. Absent → None.
        if isinstance(result, dict):
            stats = result.get("stats")
            if isinstance(stats, dict):
                kwargs["total_output_tokens"] = stats.get("total_output_tokens")
                kwargs["tokens_per_second"] = stats.get("tokens_per_second")

    elif event_name in ("message.delta", "reasoning.delta"):
        kwargs["content"] = payload.get("content")

    elif event_name == "prompt_processing.progress":
        kwargs["progress"] = payload.get("progress")

    elif event_name == "model_load.progress":
        kwargs["progress"] = payload.get("progress")

    elif event_name == "tool_call.start":
        # Wire payload: {"type":"tool_call.start"} — LM Studio does NOT emit an
        # id field here.  Synthesise
        # a uuid4 so that _ToolCallAccumulator._id is always populated and
        # finalize() never blocks on a missing id.
        synthesized_id = str(uuid.uuid4())
        # Store in shared state so subsequent events for this tool call inherit it.
        if _tc_state is not None:
            _tc_state["last_id"] = synthesized_id
        kwargs["tool_call"] = CanonicalToolCall(
            id=synthesized_id, name="", arguments={}
        )

    elif event_name == "tool_call.name":
        # Wire payload: {"type":"tool_call.name","tool_name":"...","provider_info":{...}}
        # tool_name → CanonicalToolCall.name (the ONLY rename in the native decoder).
        # Prefer any id LM Studio provides; fall back to the synthesised id from start.
        inherited_id = (
            payload.get("tool_call_id")
            or (_tc_state.get("last_id") if _tc_state is not None else None)
            or ""
        )
        tool_name = payload.get("tool_name", "")
        kwargs["tool_call"] = CanonicalToolCall(id=inherited_id, name=tool_name, arguments={})

    elif event_name == "tool_call.arguments":
        # Wire payload: {"type":"tool_call.arguments","tool":..., "arguments":{...}}
        inherited_id = (
            payload.get("tool_call_id")
            or (_tc_state.get("last_id") if _tc_state is not None else None)
            or ""
        )
        arguments = payload.get("arguments") or {}
        tool = payload.get("tool") or ""
        kwargs["tool_call"] = CanonicalToolCall(id=inherited_id, name=tool, arguments=arguments)

    elif event_name in ("tool_call.success", "tool_call.failure"):
        # Wire payload: {"type":"tool_call.success","tool":..., "arguments":{...}, "output":...}
        inherited_id = (
            payload.get("tool_call_id")
            or (_tc_state.get("last_id") if _tc_state is not None else None)
            or ""
        )
        tool = payload.get("tool") or ""
        arguments = payload.get("arguments") or {}
        # Thread
        # the wire `output` of a SUCCESS onto CanonicalToolCall.result so the
        # FE ToolCallCard can re-render results on reload. Failure keeps
        # result=None — its output rides event.error below.
        _success_output = (
            payload.get("output") if event_name == "tool_call.success" else None
        )
        kwargs["tool_call"] = CanonicalToolCall(
            id=inherited_id,
            name=tool,
            arguments=arguments,
            result=str(_success_output) if _success_output is not None else None,
        )
        if event_name == "tool_call.failure":
            # Canonical error shape {code, tool, message, output}
            raw_output = payload.get("output")
            err_message = (
                str(raw_output)
                if raw_output is not None
                else f"Tool '{tool}' failed"
            )
            kwargs["error"] = {
                "code": "tool_call_failed",
                "tool": tool,
                "message": err_message,
                "output": raw_output,
            }

    elif event_name == "error":
        err_payload = payload.get("error") or payload
        kwargs["error"] = _normalize_error_event(err_payload)

    # For event_name in {chat.end, message.start, message.end, reasoning.start,
    # reasoning.end, model_load.start, model_load.end,
    # prompt_processing.start, prompt_processing.end}: no extra payload fields;
    # the type alone is the signal.
    # NOTE: tool_call.start is handled above (uuid synthesis) and does NOT fall
    # through to this comment's event set.

    return CanonicalEvent(**kwargs)


async def decode_native(stream: httpx.Response) -> AsyncIterator[CanonicalEvent]:
    """Decode an httpx streaming response from /api/v1/chat into CanonicalEvents.

    Iterates SSE events from the upstream response. The native SSE event names
    ARE the canonical event names (1:1 mapping).
    Only the per-event payload is translated into CanonicalEvent fields.

    The one field rename: tool_call.name wire payload uses `tool_name`;
    this is mapped to CanonicalToolCall.name.

    Mid-stream upstream failures (connection close, ReadTimeout, etc.) are
    caught and surfaced as a canonical ``error`` event with
    code="upstream_stream_error" before the iterator terminates. The caller
    sees a clean event-shaped end-of-stream rather than an uncaught
    httpx exception leaking out of the generator.

    Args:
        stream: An httpx.Response with stream=True that has not yet been read.

    Yields:
        CanonicalEvent instances in wire order. Unknown event types are logged
        and skipped (forward-compatibility). On mid-stream failure, a single
        canonical ``error`` event is yielded before the iterator ends.

    Implementation note — why aiter_lines() instead of aiter_bytes():
        The previous implementation called aiter_bytes() and passed each
        chunk to _iter_sse_events(). Because TCP delivers data in arbitrary
        chunk boundaries, an SSE block's "event:" and "data:" lines can land
        in *different* chunks. _iter_sse_events resets its state per call, so
        a split event produced zero output. The fix maintains the SSE state
        machine across all lines using aiter_lines(), which handles line
        reassembly internally (httpx buffers until \\n). This ensures that
        every event: + data: pair is always seen together regardless of how
        the TCP layer delivers bytes. (aiter_bytes yielded 0 events from a
        valid 200 SSE stream.)
    """
    current_event: str | None = None
    current_data: str | None = None
    # Shared mutable state for tool_call uuid propagation:
    # _translate_native_event synthesises a uuid4 on tool_call.start and stores it
    # here so subsequent tool_call.name/.arguments/.success/.failure events for the
    # same tool call inherit the same id (LM Studio does not emit an id field on
    # those follow-on events).
    _tc_state: dict[str, str] = {}

    try:
        async for raw_line in stream.aiter_lines():
            line = raw_line.rstrip("\r")
            if line.startswith("event:"):
                current_event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_part = line[len("data:"):].strip()
                if current_data is None:
                    current_data = data_part
                else:
                    current_data += "\n" + data_part
            elif line == "":
                # Blank line = end of SSE block — dispatch if we have both fields.
                if current_event is not None and current_data is not None:
                    canonical = _translate_native_event(current_event, current_data, _tc_state)
                    if canonical is not None:
                        yield canonical
                current_event = None
                current_data = None
            # Other field prefixes (id:, retry:, comments) are ignored per RFC 8895.

        # Handle a stream that ends without a trailing blank line.
        if current_event is not None and current_data is not None:
            canonical = _translate_native_event(current_event, current_data, _tc_state)
            if canonical is not None:
                yield canonical

    except httpx.HTTPError as exc:
        log.warning(
            "decode_native.upstream_stream_error",
            error_type=type(exc).__name__,
            error=str(exc),
        )
        yield CanonicalEvent(
            type="error",
            error={
                "code": "upstream_stream_error",
                "message": f"{type(exc).__name__}: {exc}",
            },
        )
