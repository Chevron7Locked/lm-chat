# SPDX-License-Identifier: Apache-2.0
"""OpenAI-compat /v1/chat/completions encoder and SSE decoder for LM Studio.

The compat surface is used when:
- req.tools is non-empty AND req.integrations is empty
  (client-side tool loop driven by streaming_service).

Key design rules:
- reasoning and integrations do NOT translate to compat. Dropped silently.
- The compat decoder synthesises message.start / message.end boundaries
  because OpenAI-style streams don't emit them — only deltas.
- There is NO canonical tool_call.end; tool_call.success on
  finish_reason="tool_calls" is the terminator.
- delta.reasoning_content triggers reasoning.start → reasoning.delta
  transitions; reasoning.end fires when content starts (transition).
- Arguments are buffered until JSON-parseable before emitting tool_call.arguments.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator

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

# Compat params passed through to /v1/chat/completions.
# reasoning and integrations are native-only — explicitly excluded here.
_COMPAT_SCALAR_PARAMS: tuple[str, ...] = (
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "repeat_penalty",
    "max_tokens",
    "max_output_tokens",
    "store",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assemble_current_user_content(blocks: list[CanonicalInputBlock]) -> str | list[dict]:  # type: ignore[type-arg]
    """Convert current-turn input blocks to an OpenAI-compat content value.

    - Pure text turn → single string (most common case).
    - Mixed / image turn → multipart list per OpenAI spec.

    Args:
        blocks: The CanonicalInputBlock list from CanonicalChatRequest.input.

    Returns:
        A string (text-only) or a list of content-part dicts (mixed/image).
    """
    has_image = any(b.type == "image" for b in blocks)

    if not has_image:
        # Fast path: concatenate all text blocks.
        return "".join(b.content or "" for b in blocks if b.type == "text")

    parts: list[dict] = []  # type: ignore[type-arg]
    for block in blocks:
        if block.type == "text":
            parts.append({"type": "text", "text": block.content or ""})
        elif block.type == "image":
            parts.append({
                "type": "image_url",
                "image_url": {"url": block.data_url or ""},
            })
    return parts


def _tool_to_compat(tool: CanonicalTool) -> dict:  # type: ignore[type-arg]
    """Wrap a CanonicalTool in the OpenAI function-call envelope.

    Args:
        tool: The canonical tool definition.

    Returns:
        An OpenAI-compat tool dict with type="function" wrapper.
    """
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


# ---------------------------------------------------------------------------
# Encoder
# ---------------------------------------------------------------------------


def assemble_compat_messages(
    req: CanonicalChatRequest,
    history: list[CanonicalMessage],
) -> list[dict]:  # type: ignore[type-arg]
    """Build the OpenAI-style messages array for the compat surface.

    Prepends system_prompt (if set) as role="system". Appends each history
    message. Appends the current turn assembled from req.input blocks.

    Caller (streaming_service) loads `history` from the DB — the prior
    turns for this chat, ordered oldest-first.

    Args:
        req:     The canonical request carrying system_prompt, input, model, etc.
        history: Prior turns loaded from the DB by the streaming service.

    Returns:
        An OpenAI-compat messages array.
    """
    messages: list[dict] = []  # type: ignore[type-arg]

    if req.system_prompt is not None:
        messages.append({"role": "system", "content": req.system_prompt})

    for msg in history:
        entry: dict = {"role": msg.role}  # type: ignore[type-arg]
        if msg.content is not None:
            entry["content"] = msg.content
        if msg.tool_calls:
            entry["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments),
                    },
                }
                for tc in msg.tool_calls
            ]
        if msg.tool_call_id is not None:
            entry["tool_call_id"] = msg.tool_call_id
        messages.append(entry)

    # Current turn from req.input blocks.
    current_content = _assemble_current_user_content(req.input)
    messages.append({"role": "user", "content": current_content})

    return messages


def encode_compat(
    req: CanonicalChatRequest,
    history: list[CanonicalMessage],
) -> dict:  # type: ignore[type-arg]
    """Map a CanonicalChatRequest to a /v1/chat/completions request body.

    reasoning and integrations are native-only and are NOT included in
    the compat body.

    Args:
        req:     The canonical request from the SPA.
        history: Prior turns for this chat (from DB, via streaming service).

    Returns:
        A dict ready to be JSON-serialised and POSTed to /v1/chat/completions.
    """
    body: dict = {  # type: ignore[type-arg]
        "model": req.model,
        "messages": assemble_compat_messages(req, history),
        "stream": req.stream,
    }

    if req.tools:
        body["tools"] = [_tool_to_compat(t) for t in req.tools]

    for param in _COMPAT_SCALAR_PARAMS:
        val = getattr(req, param, None)
        if val is not None:
            body[param] = val

    # reasoning and integrations intentionally NOT included — compat surface
    # does not support them.

    return body


# ---------------------------------------------------------------------------
# Decoder helpers
# ---------------------------------------------------------------------------


def _parse_sse_chunk(chunk_bytes: bytes) -> list[str]:
    """Extract data payloads from an SSE chunk (OpenAI-style).

    OpenAI-style streams emit `data: <json>\\n\\n` with no `event:` line.
    The terminator is `data: [DONE]\\n\\n`.

    Args:
        chunk_bytes: Raw bytes from the HTTP response.

    Returns:
        List of data values (strings). May include "[DONE]".
    """
    payloads: list[str] = []
    current_data: str | None = None

    for raw_line in chunk_bytes.decode("utf-8", errors="replace").splitlines():
        line = raw_line.rstrip("\r")
        if line.startswith("data:"):
            data_part = line[len("data:"):].strip()
            if current_data is None:
                current_data = data_part
            else:
                current_data += "\n" + data_part
        elif line == "":
            if current_data is not None:
                payloads.append(current_data)
                current_data = None
        # Ignore event:, id:, retry:, comment lines.

    if current_data is not None:
        payloads.append(current_data)

    return payloads


# ---------------------------------------------------------------------------
# Decoder state machine
# ---------------------------------------------------------------------------


class _CompatDecoderState:
    """Mutable SSE decoder state for the compat stream.

    Tracks whether we are in a message, reasoning, or tool_call sequence so
    we can emit .start / .end boundaries that OpenAI-style streams omit.
    """

    def __init__(self) -> None:
        """Initialise all state flags and buffers."""
        self.in_message: bool = False
        self.in_reasoning: bool = False
        self.in_tool_call: bool = False
        # Idempotency guard: some OAI-compat providers (observed: OpenRouter)
        # echo a non-null finish_reason in two consecutive SSE chunks, which
        # would emit chat.end twice. The terminal chat.end must fire at most
        # once per decoded response.
        self.chat_ended: bool = False
        # Per-index argument buffer for tool calls:
        # index → {"id": str, "name": str, "args_buf": str}
        self.tool_call_bufs: dict[int, dict] = {}  # type: ignore[type-arg]

    def handle_delta(self, delta: dict) -> list[CanonicalEvent]:  # type: ignore[type-arg]
        """Translate one OpenAI delta dict into 0-N CanonicalEvents.

        Args:
            delta: The choices[0].delta dict from an OpenAI SSE chunk.

        Returns:
            A list of CanonicalEvent objects to yield to the caller.
        """
        events: list[CanonicalEvent] = []
        content: str | None = delta.get("content")
        reasoning_content: str | None = delta.get("reasoning_content")
        tool_calls: list[dict] | None = delta.get("tool_calls")  # type: ignore[type-arg]

        # --- reasoning_content ---
        if reasoning_content:
            if not self.in_reasoning:
                self.in_reasoning = True
                events.append(CanonicalEvent(type="reasoning.start"))
            events.append(CanonicalEvent(type="reasoning.delta", content=reasoning_content))

        # --- content ---
        if content:
            # Transition: reasoning → message
            if self.in_reasoning:
                self.in_reasoning = False
                events.append(CanonicalEvent(type="reasoning.end"))
            if not self.in_message:
                self.in_message = True
                events.append(CanonicalEvent(type="message.start"))
            events.append(CanonicalEvent(type="message.delta", content=content))

        # --- tool_calls ---
        if tool_calls:
            for tc_delta in tool_calls:
                idx: int = tc_delta.get("index", 0)
                if idx not in self.tool_call_bufs:
                    self.tool_call_bufs[idx] = {"id": "", "name": "", "args_buf": ""}
                    self.in_tool_call = True

                buf = self.tool_call_bufs[idx]
                tc_id: str = tc_delta.get("id") or ""
                if tc_id:
                    buf["id"] = tc_id

                fn: dict | None = tc_delta.get("function")  # type: ignore[type-arg]
                if fn:
                    fn_name: str = fn.get("name") or ""
                    fn_args: str = fn.get("arguments") or ""

                    if fn_name and not buf["name"]:
                        buf["name"] = fn_name
                        # First name delta → tool_call.start
                        events.append(CanonicalEvent(
                            type="tool_call.start",
                            tool_call=CanonicalToolCall(
                                id=buf["id"],
                                name=fn_name,
                                arguments={},
                            ),
                        ))

                    if fn_args:
                        buf["args_buf"] += fn_args
                        # Emit tool_call.arguments once the buffer is valid JSON.
                        try:
                            parsed_args = json.loads(buf["args_buf"])
                        except json.JSONDecodeError:
                            parsed_args = None

                        if parsed_args is not None:
                            events.append(CanonicalEvent(
                                type="tool_call.arguments",
                                tool_call=CanonicalToolCall(
                                    id=buf["id"],
                                    name=buf["name"],
                                    arguments=parsed_args,
                                ),
                            ))
                            # Reset buffer so we don't re-emit on subsequent chunks.
                            buf["args_buf"] = ""

        return events

    def handle_finish(self, finish_reason: str | None) -> list[CanonicalEvent]:
        """Translate a finish_reason into terminal CanonicalEvents.

        Args:
            finish_reason: The choices[0].finish_reason string, or None.

        Returns:
            A list of CanonicalEvent objects to yield.
        """
        events: list[CanonicalEvent] = []

        if finish_reason == "stop" or finish_reason == "length":
            if self.in_reasoning:
                self.in_reasoning = False
                events.append(CanonicalEvent(type="reasoning.end"))
            if self.in_message:
                self.in_message = False
                events.append(CanonicalEvent(type="message.end"))
            # Continue-chip closeout: propagate WHY the
            # stream ended. "length" = max_output_tokens truncation → the
            # FE renders the Continue chip; "stop" = natural end. Before
            # this, both reasons collapsed into a bare chat.end and the
            # documented stop_reason contract in useSSE.ts was comment-only.
            # Guarded so a provider that repeats finish_reason emits chat.end once.
            if not self.chat_ended:
                self.chat_ended = True
                events.append(
                    CanonicalEvent(type="chat.end", stop_reason=finish_reason)
                )

        elif finish_reason == "tool_calls":
            if self.in_reasoning:
                self.in_reasoning = False
                events.append(CanonicalEvent(type="reasoning.end"))
            if self.in_message:
                self.in_message = False
                events.append(CanonicalEvent(type="message.end"))
            # tool_call.success = terminator (no tool_call.end).
            events.append(CanonicalEvent(type="tool_call.success"))
            self.in_tool_call = False
            self.tool_call_bufs.clear()

        return events


# ---------------------------------------------------------------------------
# Decoder
# ---------------------------------------------------------------------------


async def decode_compat(stream: httpx.Response) -> AsyncIterator[CanonicalEvent]:
    """Decode an httpx streaming response from /v1/chat/completions to CanonicalEvents.

    Maps OpenAI-style SSE deltas to canonical events:
    - choices[0].delta.content first chunk → message.start, then message.delta.
    - delta.reasoning_content → reasoning.start / delta / end transitions.
    - delta.tool_calls[].function.name → tool_call.start.
    - delta.tool_calls[].function.arguments (cumulative) → tool_call.arguments
      once JSON-parseable.
    - finish_reason="tool_calls" → tool_call.success.
    - finish_reason="stop"|"length" → message.end + chat.end.
    - [DONE] terminates cleanly; if no finish_reason chunk preceded it,
      a synthetic chat.end(stop_reason="stop") is emitted so the draft
      always finalises.

    There is NO canonical tool_call.end. Success/failure are the terminators.

    Args:
        stream: An httpx.Response with stream=True from /v1/chat/completions.

    Yields:
        CanonicalEvent instances representing the assistant's response.
    Mid-stream upstream failures (connection close, ReadTimeout, etc.) are
    caught and surfaced as a canonical ``error`` event with
    code="upstream_stream_error" before the iterator terminates. The caller
    sees a clean event-shaped end-of-stream rather than an uncaught httpx
    exception leaking out of the generator.
    """
    state = _CompatDecoderState()

    try:
        async for chunk in stream.aiter_bytes():
            for data_value in _parse_sse_chunk(chunk):
                if data_value == "[DONE]":
                    # Synthesise a terminal close if the provider omitted a
                    # finish_reason chunk before [DONE] (observed on some
                    # OAI-compat surfaces). Without this the stream exhausts
                    # without chat.end and the draft is stuck forever.
                    if not state.chat_ended:
                        for event in state.handle_finish("stop"):
                            yield event
                    return

                try:
                    obj = json.loads(data_value)
                except json.JSONDecodeError:
                    log.warning(
                        "compat_decoder.json_decode_error",
                        data_snippet=data_value[:120],
                    )
                    continue

                # LM Studio surfaces context-window overflow mid-stream as
                # an OpenAI-style error object (no choices key). Translate
                # to the canonical context_window_exceeded code so the SPA
                # can render a specific message.
                err_obj = obj.get("error") if isinstance(obj, dict) else None
                if isinstance(err_obj, dict) and err_obj.get("type") == "exceed_context_size_error":
                    yield CanonicalEvent(
                        type="error",
                        error={
                            "code": "context_window_exceeded",
                            "message": err_obj.get("message")
                            or "Prompt exceeds the model's context window.",
                            "n_prompt_tokens": err_obj.get("n_prompt_tokens"),
                            "n_ctx": err_obj.get("n_ctx"),
                        },
                    )
                    return

                choices = obj.get("choices")
                if not choices:
                    continue

                choice = choices[0]
                delta = choice.get("delta") or {}
                finish_reason: str | None = choice.get("finish_reason")

                for event in state.handle_delta(delta):
                    yield event

                if finish_reason:
                    for event in state.handle_finish(finish_reason):
                        yield event

        # The aiter loop exhausted without a [DONE] sentinel and without a
        # finish_reason chunk. Synthesise chat.end so the draft finalises.
        if not state.chat_ended:
            for event in state.handle_finish("stop"):
                yield event

    except httpx.HTTPError as exc:
        log.warning(
            "decode_compat.upstream_stream_error",
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
