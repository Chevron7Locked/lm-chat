# SPDX-License-Identifier: Apache-2.0
"""OpenAI Responses-API /v1/responses encoder and SSE decoder for LM Studio.

This module is used when ``req.tools`` is non-empty and ``req.integrations``
is empty — the surface that /v1/chat/completions previously served.  It
replaces /v1/chat/completions for the tool-use path because /v1/responses
supports both stateful chaining AND client-side tool-use simultaneously.

Key wire differences from /v1/chat/completions (compat)
--------------------------------------------------------
- Uses ``input`` NOT ``messages``.  ``messages`` returns
  ``400 'input' required``.
- ``tools`` is the flat Responses-API shape: ``{type, name, description,
  parameters}`` — NOT the nested Chat Completions shape
  ``{type, function: {name, description, parameters}}``.  The nested shape
  returns ``400 tools.0.type invalid_string``.
- Non-streaming response: a top-level ``output`` array of typed items
  (``reasoning``, ``message``, ``function_call``).
- Streaming SSE: ``response.*`` event family (``response.created``,
  ``response.output_item.added``, ``response.output_text.delta``, etc.).
- ``call_id`` on ``function_call`` output items must round-trip back on
  the next turn's input array.

Capability gaps
---------------
- ``tool_choice: "required"`` is accepted but non-binding (does NOT force
  a function_call).  lm-chat uses ``"auto"`` exclusively.
- Named tool_choice (``{"type": "function", "name": "..."}"``) returns 400.
  lm-chat does not use named tool_choice.

SSE event mapping to canonical events
--------------------------------------
``response.created``            → ``chat.start`` (carries response_id)
``response.in_progress``        → skipped (status heartbeat)
``response.output_item.added``  → skipped (item shell only, no content)
``response.content_part.added`` → skipped (part shell only, no content)
``response.reasoning_text.delta`` → ``reasoning.start`` (first), then
                                   ``reasoning.delta`` (subsequent)
``response.output_text.delta``  → ``message.start`` (first), then
                                   ``message.delta`` (subsequent)
``response.function_call_arguments.delta`` → accumulated in buffer
``response.output_item.done``   → on type="function_call": emit
                                   ``tool_call.start`` + ``tool_call.arguments``
                                   + ``tool_call.success`` with the finalized
                                   call_id; on type="message": emit ``message.end``
``response.completed``          → ``chat.end`` (carries response_id)
any unknown ``response.*``      → logged and skipped (forward-compatibility)

Non-streaming path
------------------
The Responses API can also be used non-streaming (``stream=False``).  The
adapter always sets ``stream=True`` for consistency with the rest of the
codebase.  Non-streaming mode is not implemented here.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Final

import httpx

from lmchat.lmstudio.types import (
    CanonicalChatRequest,
    CanonicalEvent,
    CanonicalInputBlock,
    CanonicalMessage,
    CanonicalTool,
    CanonicalToolCall,
)
from lmchat.logging import get_logger

log = get_logger(__name__)

# Responses-API scalar params passed through.
# reasoning and integrations are handled separately.
# tool_choice is always "auto" (capability gap).
_RESPONSES_SCALAR_PARAMS: Final[tuple[str, ...]] = (
    "temperature",
    "top_p",
    "max_tokens",
    "store",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tool_to_responses(tool: CanonicalTool) -> dict:  # type: ignore[type-arg]
    """Convert a CanonicalTool to the Responses-API flat tool shape.

    Responses-API uses the FLAT shape (name/description/parameters at the
    top level alongside ``type``), NOT the nested Chat Completions shape.
    The nested shape returns 400.

    Args:
        tool: The canonical tool definition.

    Returns:
        A Responses-API tool dict ready for the ``tools`` array.
    """
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.parameters,
    }


def _assemble_responses_input(
    req: CanonicalChatRequest,
    history: list[CanonicalMessage],
) -> list[dict]:  # type: ignore[type-arg]
    """Build the Responses-API ``input`` array from history + current turn.

    The Responses-API ``input`` accepts a list of ``{role, content}`` message
    objects where ``content`` can be a string or a list of content parts.

    For tool-result messages (role="tool"), the Responses-API expects an
    ``output`` field carrying the tool result text and a ``call_id`` field
    echoing the ``call_id`` from the corresponding ``function_call`` output
    item.  When ``CanonicalMessage.tool_call_id`` is present and the message
    carries a corresponding ``CanonicalToolCall.call_id``, we populate both.

    Args:
        req:     The canonical request from the SPA.
        history: Prior turns loaded from the DB.

    Returns:
        A list of Responses-API input message dicts.
    """
    input_items: list[dict] = []  # type: ignore[type-arg]

    for msg in history:
        if msg.role == "system":
            # System-like content is passed via the top-level ``instructions``
            # field on the Responses request, not as a message.  Skip here;
            # encode_responses() handles it.
            continue

        if msg.role == "tool":
            # Tool result turn: use the Responses-API function_call_output shape.
            # call_id comes from msg.tool_call_id (which stores the Responses
            # call_id round-trip value for this surface).
            item: dict = {  # type: ignore[type-arg]
                "role": "tool",
                "content": msg.content or "",
            }
            if msg.tool_call_id:
                item["call_id"] = msg.tool_call_id
            input_items.append(item)
            continue

        if msg.role == "assistant" and msg.tool_calls:
            # Assistant message that issued tool calls.
            # Include both content (if any) and the function_call items.
            entry: dict = {"role": "assistant", "content": []}  # type: ignore[type-arg]
            if msg.content:
                entry["content"].append({"type": "output_text", "text": msg.content})
            for tc in msg.tool_calls:
                fc: dict = {  # type: ignore[type-arg]
                    "type": "function_call",
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments),
                }
                if tc.call_id:
                    fc["call_id"] = tc.call_id
                else:
                    fc["call_id"] = tc.id  # fall back to id for compat
                entry["content"].append(fc)
            input_items.append(entry)
            continue

        # Regular user or assistant message.
        content = msg.content or ""
        input_items.append({"role": msg.role, "content": content})

    # Current turn from req.input blocks.
    current_content: str | list[dict] = _assemble_current_content(req.input)  # type: ignore[type-arg]
    input_items.append({"role": "user", "content": current_content})

    return input_items


def _assemble_current_content(
    blocks: list[CanonicalInputBlock],
) -> str | list[dict]:  # type: ignore[type-arg]
    """Convert current-turn input blocks to a Responses-API content value.

    Pure text → single string.
    Mixed/image → multipart list using Responses-API part types
    (``input_text`` / ``input_image``).

    Args:
        blocks: The CanonicalInputBlock list from CanonicalChatRequest.input.

    Returns:
        A string (text-only) or a list of content-part dicts (mixed/image).
    """
    has_image = any(b.type == "image" for b in blocks)
    if not has_image:
        return "".join(b.content or "" for b in blocks if b.type == "text")

    parts: list[dict] = []  # type: ignore[type-arg]
    for block in blocks:
        if block.type == "text":
            parts.append({"type": "input_text", "text": block.content or ""})
        elif block.type == "image":
            parts.append({
                "type": "input_image",
                "image_url": block.data_url or "",
            })
    return parts


# ---------------------------------------------------------------------------
# Encoder
# ---------------------------------------------------------------------------


def encode_responses(
    req: CanonicalChatRequest,
    history: list[CanonicalMessage],
) -> dict:  # type: ignore[type-arg]
    """Map a CanonicalChatRequest to a /v1/responses request body.

    Differences from encode_compat():
    - Uses ``input`` (list) NOT ``messages``.
    - Tools use the flat Responses-API shape (NOT nested).
    - ``tool_choice`` is always ``"auto"`` (capability gap: "required" is
      non-binding; named tool_choice returns 400).
    - ``instructions`` carries system_prompt (replaces the system role message).
    - Streaming: always ``stream=True`` consistent with codebase convention.

    Args:
        req:     The canonical request from the SPA.
        history: Prior turns for this chat (from DB, via streaming service).

    Returns:
        A dict ready to be JSON-serialised and POSTed to /v1/responses.
    """
    body: dict = {  # type: ignore[type-arg]
        "model": req.model,
        "input": _assemble_responses_input(req, history),
        "stream": req.stream,
        # Hardcoded "auto": /v1/responses
        # accepts "auto" / "none" / "required", but "required" is non-binding
        # (does not force a function_call), and named tool_choice
        # ({"type": "function", "name": "..."}) returns 400 invalid_type.
        # "auto" is the only reliable value.
        "tool_choice": "auto",
    }

    if req.system_prompt is not None:
        body["instructions"] = req.system_prompt

    if req.tools:
        body["tools"] = [_tool_to_responses(t) for t in req.tools]

    for param in _RESPONSES_SCALAR_PARAMS:
        val = getattr(req, param, None)
        if val is not None:
            body[param] = val

    # reasoning and integrations intentionally NOT included:
    # - reasoning: not a recognised top-level field on /v1/responses
    # - integrations: pass-through for MCP is native-surface only; if the
    #   caller needs MCP + tools, they should use native + integrations
    #   (which has no client-side tools), or accept the tool-use path here.

    return body


# ---------------------------------------------------------------------------
# SSE parser helpers
# ---------------------------------------------------------------------------


def _parse_responses_sse_chunk(chunk_bytes: bytes) -> list[tuple[str, str]]:
    """Extract (event_type, data_json) pairs from a Responses-API SSE chunk.

    The Responses-API SSE format uses explicit ``event:`` lines:
        event: response.output_text.delta
        data: {"type": "response.output_text.delta", "delta": "...", ...}
        \\n

    Args:
        chunk_bytes: Raw bytes from the HTTP response.

    Returns:
        List of (event_type, data_json) tuples in wire order.
    """
    events: list[tuple[str, str]] = []
    current_event: str | None = None
    current_data: str | None = None

    for raw_line in chunk_bytes.decode("utf-8", errors="replace").splitlines():
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
            if current_event is not None and current_data is not None:
                events.append((current_event, current_data))
            current_event = None
            current_data = None

    if current_event is not None and current_data is not None:
        events.append((current_event, current_data))

    return events


# ---------------------------------------------------------------------------
# SSE decoder state machine
# ---------------------------------------------------------------------------


class _ResponsesDecoderState:
    """Mutable SSE decoder state for the /v1/responses stream.

    Tracks in-progress output items and their type so we can emit the
    correct boundaries (message.start / message.end, reasoning.start /
    reasoning.end, tool_call.start / tool_call.arguments / tool_call.success).
    """

    def __init__(self) -> None:
        """Initialise all state flags and buffers."""
        self.response_id: str | None = None
        self.in_message: bool = False
        self.in_reasoning: bool = False
        # Per-item-id buffers for in-progress output items.
        # item_id → {"type": str, "name"?: str, "args_buf": str,
        #            "call_id"?: str, "id"?: str}
        self.items: dict[str, dict] = {}  # type: ignore[type-arg]
        # The item_id of the currently active output item.
        self.active_item_id: str | None = None

    def handle_event(
        self, event_type: str, payload: dict  # type: ignore[type-arg]
    ) -> list[CanonicalEvent]:
        """Translate one /v1/responses SSE event into 0-N CanonicalEvents.

        Args:
            event_type: The SSE ``event:`` field value.
            payload:    Parsed JSON from the SSE ``data:`` field.

        Returns:
            A list of CanonicalEvent objects to yield.
        """
        events: list[CanonicalEvent] = []

        # ---- response.created → chat.start ----
        if event_type == "response.created":
            self.response_id = payload.get("id")
            events.append(CanonicalEvent(
                type="chat.start",
                response_id=self.response_id,
            ))

        # ---- response.in_progress → skip (status heartbeat) ----
        elif event_type == "response.in_progress":
            pass

        # ---- response.output_item.added → register item ----
        elif event_type == "response.output_item.added":
            item = payload.get("item") or {}
            item_id = item.get("id", "")
            item_type = item.get("type", "")
            if item_id:
                self.items[item_id] = {
                    "type": item_type,
                    "name": item.get("name", ""),
                    "args_buf": "",
                    "call_id": item.get("call_id"),
                    "id": item_id,
                }
                self.active_item_id = item_id

        # ---- response.content_part.added → skip (part shell, no content) ----
        elif event_type == "response.content_part.added":
            pass

        # ---- response.reasoning_text.delta → reasoning.start/delta ----
        elif event_type == "response.reasoning_text.delta":
            delta = payload.get("delta", "")
            if delta:
                if not self.in_reasoning:
                    self.in_reasoning = True
                    events.append(CanonicalEvent(type="reasoning.start"))
                events.append(CanonicalEvent(type="reasoning.delta", content=delta))

        # ---- response.output_text.delta → message.start/delta ----
        elif event_type == "response.output_text.delta":
            delta = payload.get("delta", "")
            if delta:
                if self.in_reasoning:
                    self.in_reasoning = False
                    events.append(CanonicalEvent(type="reasoning.end"))
                if not self.in_message:
                    self.in_message = True
                    events.append(CanonicalEvent(type="message.start"))
                events.append(CanonicalEvent(type="message.delta", content=delta))

        # ---- response.function_call_arguments.delta → buffer ----
        elif event_type == "response.function_call_arguments.delta":
            item_id = payload.get("item_id", "")
            delta = payload.get("delta", "")
            if item_id and item_id in self.items and delta:
                self.items[item_id]["args_buf"] += delta

        # ---- response.output_item.done → finalize item ----
        elif event_type == "response.output_item.done":
            item = payload.get("item") or {}
            item_type = item.get("type", "")
            item_id = item.get("id", "")

            if item_type == "message":
                if self.in_reasoning:
                    self.in_reasoning = False
                    events.append(CanonicalEvent(type="reasoning.end"))
                if self.in_message:
                    self.in_message = False
                    events.append(CanonicalEvent(type="message.end"))

            elif item_type == "function_call":
                # Emit tool_call.start + tool_call.arguments + tool_call.success.
                name = item.get("name", "")
                call_id: str | None = item.get("call_id")
                fc_id = item_id

                # Get arguments from item (finalized) or fall back to buffer.
                raw_args = item.get("arguments", "")
                if not raw_args and fc_id in self.items:
                    raw_args = self.items[fc_id]["args_buf"]

                try:
                    parsed_args = json.loads(raw_args) if raw_args.strip() else {}
                except json.JSONDecodeError:
                    log.warning(
                        "responses_decoder.function_call_args_parse_error",
                        item_id=fc_id,
                        raw_preview=raw_args[:120],
                    )
                    parsed_args = {}

                tc = CanonicalToolCall(
                    id=fc_id,
                    name=name,
                    arguments=parsed_args,
                    call_id=call_id,
                )
                events.append(CanonicalEvent(type="tool_call.start", tool_call=tc))
                events.append(CanonicalEvent(type="tool_call.arguments", tool_call=tc))
                events.append(CanonicalEvent(type="tool_call.success"))

            # Clean up item state.
            self.items.pop(item_id, None)

        # ---- response.completed → chat.end ----
        elif event_type == "response.completed":
            if self.in_reasoning:
                self.in_reasoning = False
                events.append(CanonicalEvent(type="reasoning.end"))
            if self.in_message:
                self.in_message = False
                events.append(CanonicalEvent(type="message.end"))
            response_id = payload.get("response", {}).get("id") or self.response_id
            # Continue-chip closeout: the Responses API
            # signals truncation via response.incomplete_details.reason ==
            # "max_output_tokens". Map it onto the same OpenAI-compat
            # stop_reason vocabulary the /v1/chat/completions decoder uses
            # ("length" | "stop") so the FE has one contract. Absent
            # incomplete_details → natural completion → "stop".
            _incomplete = payload.get("response", {}).get("incomplete_details")
            _stop_reason = (
                "length"
                if isinstance(_incomplete, dict)
                and _incomplete.get("reason") == "max_output_tokens"
                else "stop"
            )
            events.append(
                CanonicalEvent(
                    type="chat.end",
                    response_id=response_id,
                    stop_reason=_stop_reason,
                )
            )

        # ---- error events ----
        elif event_type == "error" or "error" in payload:
            err = payload.get("error") or payload
            events.append(CanonicalEvent(
                type="error",
                error={
                    "code": (
                        err.get("code", "upstream_error")
                        if isinstance(err, dict)
                        else "upstream_error"
                    ),
                    "message": (
                        err.get("message", str(err))
                        if isinstance(err, dict)
                        else str(err)
                    ),
                },
            ))

        else:
            # Forward-compatibility: unknown response.* events are logged and skipped.
            log.debug(
                "responses_decoder.unknown_event",
                event_type=event_type,
            )

        return events


# ---------------------------------------------------------------------------
# Decoder
# ---------------------------------------------------------------------------


async def decode_responses(stream: httpx.Response) -> AsyncIterator[CanonicalEvent]:
    """Decode an httpx streaming response from /v1/responses into CanonicalEvents.

    Maps the Responses-API SSE event family to canonical events so downstream
    consumers (streaming_service, quality_modes) don't need to know which
    upstream surface was used.

    Event mapping summary (per module docstring):
    - response.created → chat.start
    - response.output_text.delta → message.start (first), message.delta
    - response.reasoning_text.delta → reasoning.start (first), reasoning.delta
    - response.function_call_arguments.delta → buffered
    - response.output_item.done (function_call) → tool_call.start + .arguments + .success
    - response.output_item.done (message) → message.end
    - response.completed → chat.end
    - Unknown events → logged and skipped

    Mid-stream upstream failures (connection close, ReadTimeout, etc.) are
    caught and surfaced as a canonical ``error`` event before the iterator
    terminates.

    Args:
        stream: An httpx.Response with stream=True from /v1/responses.

    Yields:
        CanonicalEvent instances in wire order.
    """
    state = _ResponsesDecoderState()

    try:
        async for chunk in stream.aiter_bytes():
            for event_type, data_json in _parse_responses_sse_chunk(chunk):
                try:
                    payload = json.loads(data_json)
                except json.JSONDecodeError:
                    log.warning(
                        "responses_decoder.json_decode_error",
                        event_type=event_type,
                        data_snippet=data_json[:120],
                    )
                    continue

                for event in state.handle_event(event_type, payload):
                    yield event

    except httpx.HTTPError as exc:
        log.warning(
            "decode_responses.upstream_stream_error",
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
