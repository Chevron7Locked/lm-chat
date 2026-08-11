# SPDX-License-Identifier: Apache-2.0
"""Public non-chat streaming primitive for other callers.

This class is the reuse surface for panel seats and any other caller that
needs raw CanonicalEvent streams without the per-chat lock, draft-row persist
state machine, or memory ingestion gate.  Those layers live exclusively in
streaming_service.py.

Design decision — typed request over raw dict
---------------------------------------------
This module accepts
``CanonicalChatRequest`` instead of a raw dict because:

1. Type safety — the adapter's ``stream_chat`` already expects
   ``CanonicalChatRequest``; accepting a raw dict would require an internal
   ``model_validate`` that duplicates validation at every call site.
2. Consistency — every other surface passes the canonical type.
3. Panel callers already construct ``CanonicalChatRequest`` objects.
"""
from __future__ import annotations

import json
import re
from collections import deque
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, cast

from lmchat.lmstudio.types import (
    CanonicalChatRequest,
    CanonicalEvent,
    CanonicalMessage,
    CanonicalToolCall,
)
from lmchat.logging import get_logger
from lmchat.metrics import (
    TOOL_CALL_FAILURE_STREAKS_TOTAL,
    TOOL_CALL_NAME_WARNINGS_TOTAL,
    TOOL_CALL_REPEATS_TOTAL,
)
from lmchat.providers.base import ChatProvider
from lmchat.services.tool_args import coerce_tool_args

if TYPE_CHECKING:
    from lmchat.services.lmstudio_adapter import LmstudioAdapter

log = get_logger(__name__)

# How many recent tool-call signatures to retain per stream for
# exact-repeat detection.  A deque(maxlen=5) keeps overhead negligible.
REPEAT_LOOKBACK_TOOLCALLS: int = 5

# How many consecutive failures of the same tool trigger a warning.
FAILURE_STREAK_THRESHOLD: int = 3

# Structural tool-name validation.
# Real MCP tool names follow snake_case: lowercase letters, digits,
# underscores; usually 3-64 chars; never spaces or markup characters.
# Source: live-probed wire event `tool_name: "search_web"` in the
# CanonicalEvent docstring (`types.py:130-131`).  Tunable assumption —
# if a future MCP plugin uses a non-conforming naming scheme (e.g.
# CamelCase or kebab-case), relax this regex.
_TOOL_NAME_VALID_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class StreamingClientError(Exception):
    """Base exception for LmstudioStreamingClient failures."""


class MalformedToolCallError(StreamingClientError):
    """Raised when an upstream tool_call accumulates non-JSON arguments.

    A malformed tool call is a model-side bug, not a transient error:
    the right behavior is to surface a structured error and let the
    caller terminate the stream cleanly with a canonical error frame.

    Args:
        tool_name:   Name of the tool that produced the malformed JSON.
        raw_preview: First 120 chars of the accumulated arguments string.
        error:       The json.JSONDecodeError message.
    """

    def __init__(self, *, tool_name: str, raw_preview: str, error: str) -> None:
        self.tool_name = tool_name
        self.raw_preview = raw_preview
        self.error = error
        super().__init__(
            f"malformed tool_call arguments JSON for tool {tool_name!r}: "
            f"{error} (raw preview: {raw_preview!r})"
        )


class StreamingClientUpstreamError(StreamingClientError):
    """Raised when ``raise_on_error=True`` and an ``error`` event is received.

    Wraps the canonical error dict so the caller can inspect ``code``
    and ``message`` without re-parsing.

    Args:
        event: The canonical ``error`` event that triggered the raise.
    """

    def __init__(self, event: CanonicalEvent) -> None:
        """Initialise with the error event.

        Args:
            event: The canonical ``error`` event received from upstream.
        """
        self.event = event
        err = event.error or {}
        super().__init__(f"upstream error: code={err.get('code')!r} message={err.get('message')!r}")


# Sentinel value used by _ToolCallAccumulator to distinguish
# dict-merge mode from raw-string-concat mode in _arguments_buf.
_DICT_CHUNK_SENTINEL = "\x00dict-chunk-mode\x00"


def _deep_merge(base: dict, overlay: dict) -> None:  # type: ignore[type-arg]
    """Merge *overlay* into *base* in-place (recursive for nested dicts).

    Scalar values in *overlay* overwrite those in *base*; list values
    are extended; nested dicts are recursively merged.
    """
    for key, val in overlay.items():
        if key in base and isinstance(base[key], dict) and isinstance(val, dict):
            _deep_merge(base[key], val)
        elif key in base and isinstance(base[key], list) and isinstance(val, list):
            base[key].extend(val)
        else:
            base[key] = val


# ---------------------------------------------------------------------------
# Tool-call accumulator
# ---------------------------------------------------------------------------


class _ToolCallAccumulator:
    """Accumulates streaming tool_call.* events into a complete CanonicalToolCall.

    LM Studio emits tool call data across multiple events::

        tool_call.start      → carries tool id (LM Studio omits id on wire;
                               native.py synthesises a uuid so _id is always
                               populated after fix #4)
        tool_call.name       → carries tool_name
        tool_call.arguments  → carries partial JSON string (may arrive in chunks)
        tool_call.success    → terminal (tool executed; result available)
        tool_call.failure    → terminal (tool error)

    This accumulator collects the data, and ``finalize()`` returns a complete
    ``CanonicalToolCall`` when enough data is available or ``None`` if the call
    cannot be reconstructed (e.g. missing name).
    """

    def __init__(self) -> None:
        """Initialise empty accumulator state."""
        self._id: str | None = None
        self._name: str | None = None
        self._arguments_buf: list[str] = []
        # Noise gate: track whether a tool_call.start
        # was seen since the last reset so finalize() can distinguish a
        # "real partial accumulation" (start seen, name missing) from a
        # spurious finalize on an accumulator that was never armed
        # (tool_call.success/failure arriving without a prior start).
        self._had_start: bool = False

    def ingest(self, event: CanonicalEvent) -> None:
        """Ingest a single tool_call.* event.

        Args:
            event: A ``CanonicalEvent`` with type starting with ``tool_call.``.
        """
        if event.type == "tool_call.start":
            # Reset state for every new tool call so state never bleeds across
            # consecutive calls.
            self.reset()
            self._had_start = True
            if event.tool_call is not None:
                self._id = event.tool_call.id
        elif event.type == "tool_call.name" and event.tool_call is not None:
            self._name = event.tool_call.name
        elif event.type == "tool_call.arguments" and event.tool_call is not None:
            # Noise gate: if no tool_call.start was seen,
            # the arguments arrived out of sequence (model bug or stream
            # replay artefact). Silently ignore rather than warn — there is
            # no accumulation state to corrupt and the warning is noise.
            if not self._had_start:
                log.debug(
                    "streaming_client.accumulator.arguments_without_start",
                    tool_name=self._name,
                )
                return
            # arguments field holds partial JSON text accumulated as a string.
            # Accept empty chunks (log at debug, don't silently drop
            # them — an empty string is a valid no-op chunk, not an error).
            chunk = event.tool_call.arguments
            if chunk is None:
                log.debug(
                    "streaming_client.accumulator.null_args_chunk",
                    tool_name=self._name,
                )
                return
            if isinstance(chunk, str):
                if not chunk:
                    log.debug(
                        "streaming_client.accumulator.empty_args_chunk",
                        tool_name=self._name,
                    )
                self._arguments_buf.append(chunk)
            else:
                # Dict-typed chunk — merge properly instead of
                # json.dumps concat (which would concatenate two serialised
                # dicts and produce invalid JSON).  Accumulate each dict-chunk
                # by deep-merging into a single canonical dict, then serialise
                # once at finalize() time.
                #
                # Strategy: store a sentinel marker as the first element of the
                # buffer to signal "this buf holds merged-dict state, not raw
                # string chunks".  The marker is _DICT_CHUNK_SENTINEL; the
                # second element is the JSON representation of the merged dict.
                if (
                    self._arguments_buf
                    and self._arguments_buf[0] == _DICT_CHUNK_SENTINEL
                ):
                    # Already in dict-merge mode: merge into the accumulated dict.
                    try:
                        merged = json.loads(self._arguments_buf[1])
                    except (json.JSONDecodeError, IndexError):
                        merged = {}
                    _deep_merge(merged, chunk)
                    self._arguments_buf[1] = json.dumps(merged)
                else:
                    # First dict chunk — switch to dict-merge mode.
                    # Any prior string chunks are discarded (edge case: mixed
                    # str+dict chunks within the same call; log at warning).
                    if self._arguments_buf:
                        log.warning(
                            "streaming_client.accumulator.mixed_chunk_types",
                            tool_name=self._name,
                            prior_string_chunks=len(self._arguments_buf),
                        )
                    self._arguments_buf = [_DICT_CHUNK_SENTINEL, json.dumps(chunk)]

    def finalize(self) -> CanonicalToolCall | None:
        """Return a complete tool call or ``None`` if data is insufficient.

        Returns:
            A ``CanonicalToolCall`` with ``id``, ``name``, and ``arguments``
            populated, or ``None`` if the name was never received.

        Raises:
            MalformedToolCallError: If ``name`` is present but the accumulated
                arguments fail to parse as JSON. A malformed tool-call is a
                model-side bug, not transient — surface as a structured error
                so the caller can emit a canonical error event and terminate
                the stream.
        """
        if not self._name:
            # Noise gate: only warn when a tool_call.start
            # was seen (i.e. the accumulator was armed) AND there are
            # accumulated characters (i.e. arguments arrived without a name).
            # A tool_call.success/failure without a prior start is benign
            # (LM Studio native surface route sometimes emits terminal events
            # before any start on server-side tool execution) — suppress the
            # warning to eliminate log noise on successful tool calls.
            accumulated_chars = sum(
                len(s) for s in self._arguments_buf
                if s != _DICT_CHUNK_SENTINEL
            )
            if self._had_start and accumulated_chars > 0:
                log.warning(
                    "streaming_client.accumulator.finalize_missing_data",
                    has_id=self._id is not None,
                    has_name=self._name is not None,
                    accumulated_chars=accumulated_chars,
                    dict_mode=bool(
                        self._arguments_buf
                        and self._arguments_buf[0] == _DICT_CHUNK_SENTINEL
                    ),
                )
            return None
        # Resolve call_id: if native.py didn't synthesise one (shouldn't happen
        # post fix #4, but be defensive), fall back to a generated id rather
        # than blocking the whole call.
        call_id = self._id or f"tc-fallback-{id(self)}"
        if not self._id:
            log.warning(
                "streaming_client.accumulator.missing_id_fallback",
                tool_name=self._name,
                fallback_id=call_id,
            )

        # Dict-merge mode: the buffer already holds the merged JSON string.
        if (
            self._arguments_buf
            and self._arguments_buf[0] == _DICT_CHUNK_SENTINEL
        ):
            raw = self._arguments_buf[1] if len(self._arguments_buf) > 1 else ""
        else:
            raw = "".join(self._arguments_buf)

        if not raw.strip():
            return CanonicalToolCall(id=call_id, name=self._name, arguments={})
        # First try the tolerant coercer — handles code fences, single-quoted
        # keys/values, trailing prose, AND truncation (qwen-code-adapted
        # repair). See
        # :mod:`lmchat.services.tool_args` module docstring for provenance and
        # the ``tool_format_generation_error`` failure mode that
        # motivated this. ``MalformedToolCallError`` still raises only when
        # every repair fails, so genuine structural failures still terminate
        # the stream loudly (malformed = model-side bug,
        # not transient).
        coerced = coerce_tool_args(raw)
        if coerced is None:
            log.error(
                "streaming_client.tool_call_args_malformed_json",
                tool_name=self._name,
                raw_preview=raw[:120],
            )
            raise MalformedToolCallError(
                tool_name=self._name,
                raw_preview=raw[:120],
                error="coerce_tool_args returned None after all repair attempts",
            )
        return CanonicalToolCall(id=call_id, name=self._name, arguments=coerced)

    def reset(self) -> None:
        """Reset accumulator state for the next tool call."""
        self._id = None
        self._name = None
        self._arguments_buf = []
        self._had_start = False


# ---------------------------------------------------------------------------
# Public client
# ---------------------------------------------------------------------------


class LmstudioStreamingClient:
    """Public non-chat streaming primitive for other callers (panel reuse).

    Wraps the same upstream operations ``streaming_service.stream_chat`` uses
    internally (open upstream, parse SSE events, route tool calls) but WITHOUT
    the per-chat lock, draft-row persist state machine, or memory ingestion
    gate.  Callers own their own persistence and side effects.

    Construct once and reuse across requests — the instance holds no
    per-stream state.

    Args:
        adapter: The shared ``LmstudioAdapter`` instance (injected by the
                 FastAPI lifespan at ``app.state.lmstudio_adapter``).
    """

    def __init__(self, *, adapter: ChatProvider) -> None:
        """Initialise the client with a shared adapter.

        Args:
            adapter: Any ``ChatProvider`` implementation (currently always
                     ``LmstudioAdapter``; typed as the protocol seam so a
                     later phase can inject an OAI-compat provider without
                     touching this class).
        """
        self._adapter = adapter

    async def probe_for_error(self, req: CanonicalChatRequest) -> str | None:
        """Re-issue *req* with ``stream=False`` to get LM Studio's actual error.

        See :meth:`LmstudioAdapter.probe_for_error` — this is a thin
        passthrough so callers (StreamingService) don't need to reach
        through ``_adapter``.

        Note: ``probe_for_error`` is an LM-Studio-specific method not on the
        ``ChatProvider`` Protocol.  The cast here is intentional: in the
        current wiring (A1) the only concrete provider is ``LmstudioAdapter``.
        This method will be removed or rerouted in Workstream A4 when provider-
        routing moves to the registry layer.
        """
        lmstudio_adapter = cast("LmstudioAdapter", self._adapter)
        return await lmstudio_adapter.probe_for_error(req)

    async def stream(
        self,
        *,
        request: CanonicalChatRequest,
        history: list[CanonicalMessage] | None = None,
        on_tool_call: Callable[[CanonicalToolCall], None] | None = None,
        raise_on_error: bool = False,
        cumulative_tool_rounds: int = 0,
    ) -> AsyncIterator[CanonicalEvent]:
        """Open an upstream stream and yield canonical events.

        Opens a connection to LM Studio via the adapter (which handles surface
        selection, rejected-param stripping, and the reactive-learn-and-retry
        loop).  Events are yielded in wire order.

        Tool-call accumulation
        ----------------------
        The accumulator runs unconditionally so that ``coerce_tool_args`` repair
        runs on every tool call regardless of whether a callback is wired (fix
        #10).  When ``on_tool_call`` is set the callback is invoked with each
        completed ``CanonicalToolCall``; when it is ``None`` the accumulation
        still runs (for repair) but the callback step is skipped.

        The raw tool_call.* events are still forwarded to the caller — the
        accumulator is additive, not a replacement.

        Error handling
        --------------
        When ``raise_on_error=False`` (default): ``error`` events are yielded
        to the caller like any other event.  The caller decides what to do.

        When ``raise_on_error=True``: upon receiving an ``error`` event, the
        generator raises ``StreamingClientUpstreamError`` (wrapping the event)
        instead of yielding it.  The generator terminates immediately.

        Args:
            request:        Canonical chat request — passed directly to
                            the provider's ``stream_chat``.
            history:        Prior turns for replay-mode providers.  ``None``
                            (default) leaves LM Studio's native chain mode
                            unchanged — the adapter ignores it for the native
                            surface.  A future replay provider reads it to
                            assemble full-turn context.
            on_tool_call:   Optional callback invoked with each completed
                            ``CanonicalToolCall``.  Invoked synchronously in
                            the generator loop (keep it fast).  May be
                            ``None`` (default); tool events still pass through.
            raise_on_error: When ``True``, raise ``StreamingClientUpstreamError``
                            instead of yielding error events.  Default ``False``.

        Yields:
            :class:`~lmchat.lmstudio.types.CanonicalEvent` in wire order.

        Raises:
            StreamingClientUpstreamError: When ``raise_on_error=True`` and an
                ``error`` event is received from upstream.
        """
        log.info(
            "streaming_client.stream.start",
            model_id=request.model,
            has_tool_callback=on_tool_call is not None,
            raise_on_error=raise_on_error,
        )

        # Always create the accumulator so coerce_tool_args repair runs
        # unconditionally, not only when a callback is wired.
        accumulator = _ToolCallAccumulator()
        # Per-stream signature tracker for exact-repeat detection.
        # Signature: (name, sorted-args-JSON, is_success).
        # Failure-then-retry is legitimate and must NOT fire the warning.
        recent_calls: deque[tuple[str, str, bool]] = deque(
            maxlen=REPEAT_LOOKBACK_TOOLCALLS
        )
        # Consecutive failure streak per tool name.
        failure_streaks: dict[str, int] = {}

        event_count = 0
        # type: ignore explanation — the Protocol declares stream_chat as
        # ``async def → AsyncIterator``, which pyright treats as a coroutine
        # (not directly async-iterable).  At runtime, LmstudioAdapter is an
        # async generator, so the ``async for`` is correct.  The Protocol
        # typing is the source of the discrepancy; a Protocol that uses
        # ``AsyncGenerator`` as the return type would satisfy both pyright
        # and runtime, but changing base.py is out of A1 scope.
        async for event in self._adapter.stream_chat(  # type: ignore[misc]
            request, history=history, cumulative_tool_rounds=cumulative_tool_rounds
        ):
            event_count += 1

            # ---- error handling ----
            if event.type == "error":
                log.warning(
                    "streaming_client.stream.error_event",
                    model_id=request.model,
                    error_code=(event.error or {}).get("code"),
                    raise_on_error=raise_on_error,
                )
                # Reset accumulator on top-level error so state doesn't
                # bleed into any subsequent recovery or retry stream.
                accumulator.reset()
                if raise_on_error:
                    raise StreamingClientUpstreamError(event)
                yield event
                return

            # Structural tool-name validation.
            # Fires once per tool_call.name event; doesn't depend on call
            # success/failure or on_tool_call callback presence.  Yields warning
            # BEFORE the original event flows downstream (different timing from
            # the per-stream signature tracker above, which yields AFTER;
            # verified no downstream consumer assumes warning-after).
            if (
                event.type == "tool_call.name"
                and event.tool_call is not None
                and event.tool_call.name
                and not _TOOL_NAME_VALID_RE.match(event.tool_call.name)
            ):
                log.warning(
                    "streaming_client.tool_call_name_malformed",
                    model_id=request.model,
                    attempted_name=event.tool_call.name,
                )
                TOOL_CALL_NAME_WARNINGS_TOTAL.labels(kind="structural").inc()
                yield CanonicalEvent(
                    type="tool_call.name_warning",
                    tool_call=event.tool_call,
                    error={
                        "code": "tool_name_malformed",
                        "attempted": event.tool_call.name,
                        "expected_pattern": "snake_case identifier (3-64 chars)",
                    },
                )

            # ---- tool-call accumulation (always active) ----
            if event.type.startswith("tool_call."):
                accumulator.ingest(event)

                # Invoke callback on terminal tool-call events when the
                # accumulated call is complete.
                if event.type in ("tool_call.success", "tool_call.failure"):
                    try:
                        completed = accumulator.finalize()
                    except MalformedToolCallError as exc:
                        # Malformed JSON in tool_call arguments — surface as a
                        # canonical error event and terminate the stream.
                        # Model-side bug, not transient, no retry.
                        # Uses canonical code "tool_format_generation_error"
                        # (matches LM Studio's own error.type for this failure
                        # class) so callers and the salvage gate have one
                        # consistent code to match against.
                        log.error(
                            "streaming_client.stream.malformed_tool_call",
                            model_id=request.model,
                            tool_name=exc.tool_name,
                            error=exc.error,
                        )
                        yield CanonicalEvent(
                            type="error",
                            error={
                                "code": "tool_format_generation_error",
                                "tool": exc.tool_name,
                                "message": exc.error,
                            },
                        )
                        accumulator.reset()
                        return
                    if completed is not None:
                        log.info(
                            "streaming_client.stream.tool_call_complete",
                            model_id=request.model,
                            tool_name=completed.name,
                            tool_id=completed.id,
                        )
                        # Exact-repeat detection.
                        args_sig = json.dumps(
                            completed.arguments, sort_keys=True, separators=(",", ":")
                        )
                        is_success = event.type == "tool_call.success"
                        sig = (completed.name, args_sig, is_success)

                        # Match only against prior SUCCESSFUL identical calls
                        # (fail → retry with same args is legitimate).
                        is_repeat = any(
                            prior == (completed.name, args_sig, True)
                            for prior in recent_calls
                        )
                        if is_repeat:
                            log.warning(
                                "streaming_client.tool_call_repeat",
                                model_id=request.model,
                                tool_name=completed.name,
                                args_preview=args_sig[:120],
                            )
                            TOOL_CALL_REPEATS_TOTAL.labels(tool_name=completed.name).inc()
                            yield CanonicalEvent(
                                type="tool_call.repeat_warning",
                                tool_call=completed,
                            )
                        recent_calls.append(sig)

                        # Consecutive failure streak detection.
                        if event.type == "tool_call.failure":
                            failure_streaks[completed.name] = (
                                failure_streaks.get(completed.name, 0) + 1
                            )
                            if failure_streaks[completed.name] >= FAILURE_STREAK_THRESHOLD:
                                log.warning(
                                    "streaming_client.tool_call_failure_streak",
                                    model_id=request.model,
                                    tool_name=completed.name,
                                    streak=failure_streaks[completed.name],
                                )
                                TOOL_CALL_FAILURE_STREAKS_TOTAL.labels(
                                    tool_name=completed.name
                                ).inc()
                                yield CanonicalEvent(
                                    type="tool_call.failure_streak_warning",
                                    tool_call=completed,
                                    error={
                                        "code": "tool_failure_streak",
                                        "tool": completed.name,
                                        "streak": failure_streaks[completed.name],
                                    },
                                )
                        else:  # tool_call.success — recover; clear the streak
                            failure_streaks.pop(completed.name, None)

                        if on_tool_call is not None:
                            on_tool_call(completed)
                    accumulator.reset()

            # ---- stream terminal: stop after chat.end ----
            if event.type == "chat.end":
                yield event
                log.info(
                    "streaming_client.stream.complete",
                    model_id=request.model,
                    event_count=event_count,
                )
                return

            yield event

        # Adapter exhausted its generator without a chat.end or error event.
        # This is unusual but not illegal — log and let the caller interpret.
        log.info(
            "streaming_client.stream.generator_exhausted",
            model_id=request.model,
            event_count=event_count,
        )
