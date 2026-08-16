# SPDX-License-Identifier: Apache-2.0
"""Main streaming service for lm-chat: chat pipeline entry point.

``StreamingService.stream_chat`` acquires a per-chat lock, creates a draft
row, opens the upstream SSE stream, and runs a disconnect-watcher alongside
the persist state machine in an ``asyncio.TaskGroup`` (PEP 654 ``except*``
cancellation scope; normal completion signals via ``_StreamDone``).

State machine: draft -> (message.delta x N) -> pending_finalization -> final,
or -> aborted_by_client on disconnect. ``pending_finalization`` is recovered
by the reaper if the commit fails.

``message.delta`` content coalesces to the DB every 250ms via
``_CoalesceTimer``. Idle-timeout (default 300s) fires only on true silence —
no content-bearing event and no ``prompt_processing.*`` heartbeat.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime
from time import monotonic
from typing import TYPE_CHECKING, Any, Final, NamedTuple

# System local tz for the per-turn context block; astimezone(None) avoids
# importing zoneinfo or hard-coding a zone.
_LOCAL_TZ = datetime.now().astimezone().tzinfo

from pydantic import BaseModel
from sqlalchemy import Table, and_, func, insert, or_, select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.sql.elements import ColumnElement

from lmchat.db.retry import with_write_retry
from lmchat.db.schema import chats, compactions, messages, sub_session_messages, sub_sessions
from lmchat.lmstudio.oob_text import oob_message_text, oob_salvage
from lmchat.lmstudio.types import CanonicalChatRequest, CanonicalEvent, CanonicalToolCall
from lmchat.logging import get_logger
from lmchat.metrics import STREAMS_ACTIVE, STREAMS_COMPLETED, STREAMS_FAILED, STREAMS_SALVAGED
from lmchat.services._active_streams import mark_active, mark_inactive
from lmchat.services._stream_state import (
    PersistState,
    finalize_pending,
    safe_abort_draft,
)
from lmchat.services.app_settings_service import (
    resolve_memory_distillation_enabled as _resolve_memory_distillation_enabled,
)
from lmchat.services.app_settings_service import resolve_repeat_warning_cut_k
from lmchat.services.audit_service import AuditEvent, write_audit_event
from lmchat.services.bg_aux import (
    bg_aux_overloaded,
    bg_aux_pending,
    bg_aux_slot,
)
from lmchat.services.capability_legend import render_capability_legend
from lmchat.services.chat_service import ChatNotFoundError
from lmchat.services.lm_studio_overrides_service import resolve_lm_studio_endpoint_mode
from lmchat.services.lmstudio_streaming_client import LmstudioStreamingClient
from lmchat.services.streaming_errors import (
    StreamInProgressError,
    SubSessionStreamInProgressError,
)
from lmchat.services.system_guide import (
    ensure_section_embeddings_background as _guide_ensure_section_embeddings_background,
)
from lmchat.services.system_guide import (
    get_cached_section_embeddings as _guide_get_cached_section_embeddings,
)
from lmchat.services.system_guide import guide_context_block, is_app_directed_question
from lmchat.services.system_guide import (
    guide_context_block_semantic as _guide_context_block_semantic,
)

if TYPE_CHECKING:
    from fastapi import Request

    from lmchat.lmstudio.types import CanonicalMessage
    from lmchat.mcp.host import McpHost
    from lmchat.services.auth_service import User
    from lmchat.services.quality_modes import QualityModeService

from lmchat.embedding.client import EmbeddingClient
from lmchat.services.memory_service import MemoryService
from lmchat.services.models_service import (
    ModelsService,
    resolve_background_model_id,
)
from lmchat.services.preset_catalog import get_preset_definition, list_adoptable_preset_ids
from lmchat.services.project_summary_service import (
    count_project_messages,
    refresh_project_summary,
)
from lmchat.services.project_summary_service import (
    should_refresh as _should_refresh_project_summary,
)
from lmchat.services.sampler_profiles import profile_for_request
from lmchat.services.substance_fold import _has_real_answer, resolve_terminal_content
from lmchat.utils.lru_counter import LruCappedCounter
from lmchat.utils.task_lifetime import spawn_background_task

log = get_logger(__name__)

# Coalesce flush interval (250ms).
_COALESCE_INTERVAL_SEC: Final[float] = 0.250

# Disconnect poll interval (500ms).
_DISCONNECT_POLL_SEC: Final[float] = 0.500

# Ceiling on the per-turn semantic guide-injection QUERY EMBED only — the
# one-time corpus embed runs in a detached background task with its own
# timeout and never blocks a turn. Any overrun/failure here falls through to
# the deterministic keyword engine; guide injection is never turn-blocking.
_GUIDE_SEMANTIC_TIMEOUT_SEC: Final[float] = 8.0

# Grace period after stall_event before the dead-man `raise _StreamStall`;
# covers the case where the persist body was mid-yield (not blocked on
# anext) when the watcher fired.
_STALL_GRACE_SEC: Final[float] = 2.0

# Bounded wait on the detached auto-memory distillation task before giving up
# on the inline `memory.saved` frame. On timeout the task keeps running and
# still stores — it just shows up later on the Memory page instead of inline.
_MEMORY_SAVED_FRAME_WAIT_SEC: Final[float] = 45.0

# Content-bearing event types that reset the idle-timeout clock.
_CONTENT_BEARING: Final[frozenset[str]] = frozenset(
    {
        "message.delta",
        "message.end",
        "reasoning.delta",
        "reasoning.end",
        "tool_call.start",
        "tool_call.name",
        "tool_call.arguments",
        "tool_call.success",
        "tool_call.failure",
        "chat.end",
    }
)

# Heartbeats carry no content but ARE evidence of active work (e.g. local
# prompt-processing on a large context, which can outlast the idle timeout
# before the first token) — also reset the idle clock so a slow-but-alive
# model isn't aborted mid-processing.
_KEEPALIVE_HEARTBEAT: Final[frozenset[str]] = frozenset(
    {
        "prompt_processing.start",
        "prompt_processing.progress",
        "prompt_processing.end",
    }
)

# Same as _CONTENT_BEARING minus the terminal chat.end; gates the
# grammar-degrade retry — once any of these fire, content is already
# rendering and an upstream error can no longer be swallowed and retried.
_CONTENT_EMITTED_EVENTS: Final[frozenset[str]] = frozenset(
    {
        "message.delta",
        "message.end",
        "reasoning.delta",
        "reasoning.end",
        "tool_call.start",
        "tool_call.name",
        "tool_call.arguments",
        "tool_call.success",
        "tool_call.failure",
    }
)


def _is_grammar_parse_error(message: str) -> bool:
    """True if *message* matches LM Studio's grammar/sampler-parse failure text.

    Intentionally narrow — must not fire on ordinary tool-call failures or
    normal 400s, only the specific grammar/sampler init path.
    """
    low = message.lower()
    return "failed to parse grammar" in low or "failed to initialize samplers" in low


def _grammar_degrade_eligible(
    *,
    is_native_path: bool,
    has_integrations: bool,
    content_emitted: bool,
    already_degraded: bool,
    error_detail: str,
) -> bool:
    """Shared eligibility rule for degrading a grammar/sampler-parse failure
    into a tool-less retry — used by both the main pump
    (``StreamingService._run_persist_and_yield``) and the sub-session pump
    (``routes.chats._sub_session_sse``); only the retry mechanics differ
    per caller.
    """
    return (
        is_native_path
        and has_integrations
        and not content_emitted
        and not already_degraded
        and _is_grammar_parse_error(error_detail)
    )


def _grammar_degrade_warning(integrations: list[str]) -> str:
    """User-facing warning when a bad tool schema forces a tool-less retry.

    Identical text on both SSE surfaces; only the wrapping frame differs per caller.
    """
    names = ", ".join(integrations)
    return (
        "One or more tools couldn't be loaded by LM Studio (bad tool schema). "
        f"Retrying without tools. Active integrations were: {names}."
    )

# Per-turn backstop: local models run the MCP tool loop natively and a
# misbehaving model can re-decide "let me try a more targeted search" forever
# with no answer. Exceeding this cap aborts upstream and finalizes with
# whatever's written so far. Separate from the cross-turn `_tool_round_counts`
# LRU (feeds the MTP-suspect heuristic only). High threshold — a pathological-
# loop backstop, not a research limiter (see `_MAX_IDENTICAL_TOOL_ROUNDS` for
# the earlier-firing real signal). Override via
# `LM_CHAT_MAX_TOOL_ROUNDS_PER_TURN` (<=0 disables).
_MAX_TOOL_ROUNDS_PER_TURN: Final[int] = int(os.getenv("LM_CHAT_MAX_TOOL_ROUNDS_PER_TURN", "256"))

# The real degenerate-loop signal: the model re-issuing the SAME tool call
# with no progress, not making many different calls (legitimate research).
# Cuts after this many CONSECUTIVE identical SUCCESSFUL calls; any different
# call — or any failure — resets/skips the streak (see
# _apply_tool_call_delta): a flaky MCP server / transient network blip makes
# identical-args retries legitimate, mirroring
# lmstudio_streaming_client's own "match only against prior SUCCESSFUL
# identical calls" repeat detector. Override via
# `LM_CHAT_MAX_IDENTICAL_TOOL_ROUNDS` (<=0 disables).
_MAX_IDENTICAL_TOOL_ROUNDS: Final[int] = int(os.getenv("LM_CHAT_MAX_IDENTICAL_TOOL_ROUNDS", "5"))

# Fast-path cut driven by the streaming client's own repeat detector, which
# catches non-consecutive repeats too via a lookback window. K=16 means the
# 17th identical call in the window fires the cut — permissive so heavy
# agentic/research runs aren't cut short. Override via
# `LM_CHAT_REPEAT_WARNING_CUT_K` (<=0 disables).
#
# NOT read in the hot path (see _track_loop_cut_signals's
# repeat_warning_cut_k parameter) — the effective K is now resolved per-turn
# in stream_chat via the per-chat override -> global admin default -> config
# default chain (config.Settings.lm_chat_repeat_warning_cut_k /
# app_settings_service.resolve_repeat_warning_cut_k). Kept only as a
# documented standalone default for anything reading the raw env var
# directly outside a request context.
_REPEAT_WARNING_CUT_K: Final[int] = int(os.getenv("LM_CHAT_REPEAT_WARNING_CUT_K", "16"))


class _LoopCutDecision(NamedTuple):
    """Pure decision for whether/why to cut a runaway tool-call loop.

    Returned by ``_decide_loop_cut``; the generator
    (``StreamingService._run_persist_and_yield``) still owns every side
    effect (logging, metrics, state writes, upstream aclose, finalize, SSE
    yields) and reconstructs them from this decision.

    Attributes:
        should_cut: Whether the loop-cut predicate fired for this event.
        cut_reason: ``"repeat_loop"`` | ``"failure_streak"`` |
            ``"tool_loop_cap"`` when ``should_cut``; else ``None``. The
            fine-grained label for logs/metrics — NOT the ``stop_reason`` /
            warning-frame code, which stays ``"tool_loop_cap"`` for all paths.
        effective_cut: ``cut_reason`` normalised for
            ``resolve_terminal_content``'s ``loop_cut_reason``: relabelled
            ``"repeat_loop"`` when the identical-rounds backstop also
            tripped. ``None`` when ``should_cut`` is ``False``.
    """

    should_cut: bool
    cut_reason: str | None
    effective_cut: str | None


def _decide_loop_cut(
    *,
    early_cut_reason: str | None,
    event_type: str,
    consecutive_identical_rounds: int,
    turn_tool_rounds: int,
) -> _LoopCutDecision:
    """Decide whether to cut the current tool-call loop, and why.

    Pure — reads only its arguments and the module constants
    ``_MAX_IDENTICAL_TOOL_ROUNDS`` / ``_MAX_TOOL_ROUNDS_PER_TURN``.

    Args:
        early_cut_reason: Client-advisory reason (``"repeat_loop"`` |
            ``"failure_streak"`` | ``None``); fires the cut on any event.
        event_type: The current event type — the service-local backstops
            only evaluate on ``tool_call.success`` / ``tool_call.failure``.
        consecutive_identical_rounds: Count of consecutive identical
            (name + args) tool calls observed this turn.
        turn_tool_rounds: Count of tool rounds observed this turn.
    """
    should_cut = early_cut_reason is not None
    if not should_cut:
        should_cut = event_type in (
            "tool_call.success",
            "tool_call.failure",
        ) and (
            (
                _MAX_IDENTICAL_TOOL_ROUNDS > 0
                and consecutive_identical_rounds >= _MAX_IDENTICAL_TOOL_ROUNDS
            )
            or (
                _MAX_TOOL_ROUNDS_PER_TURN > 0
                and turn_tool_rounds > _MAX_TOOL_ROUNDS_PER_TURN
            )
        )

    if not should_cut:
        return _LoopCutDecision(should_cut=False, cut_reason=None, effective_cut=None)

    if early_cut_reason == "repeat_loop":
        cut_reason = "repeat_loop"
    elif early_cut_reason == "failure_streak":
        cut_reason = "failure_streak"
    else:
        cut_reason = "tool_loop_cap"

    effective_cut = cut_reason
    if effective_cut not in ("repeat_loop", "failure_streak") and (
        _MAX_IDENTICAL_TOOL_ROUNDS > 0
        and consecutive_identical_rounds >= _MAX_IDENTICAL_TOOL_ROUNDS
    ):
        effective_cut = "repeat_loop"

    return _LoopCutDecision(
        should_cut=True, cut_reason=cut_reason, effective_cut=effective_cut
    )


# Request model


class ChatStreamRequest(BaseModel):
    """SPA-facing request for POST /api/chat/stream.

    Args:
        chat_id: PK of the chat to stream into.
        payload: The canonical LM Studio chat request.
    """

    chat_id: int
    payload: CanonicalChatRequest


# Internal sentinel


class _StreamDone(Exception):
    """Sentinel raised to exit ``asyncio.TaskGroup`` on normal completion.

    Per the TaskGroup pattern, raising ``_StreamDone`` from inside a
    TaskGroup causes the group to cancel pending tasks and exit; catching it
    via ``except*`` lets real exceptions still propagate.
    """


class _StreamStall(Exception):
    """Sentinel raised by the disconnect watcher on idle-timeout detection.

    Raised inside the TaskGroup to cancel the persist iterator and signal
    that an ``upstream_stall`` error frame must be emitted.
    """

    def __init__(self, idle_s: float) -> None:
        self.idle_s = idle_s
        super().__init__(f"upstream stall: no content-bearing event for {idle_s:.1f}s")


class _ProviderResolution(NamedTuple):
    """Result of resolving the provider / context-mode for a turn.

    Returned by ``StreamingService._resolve_provider_and_context_mode``. On
    success, ``context_mode`` / ``dispatch_provider`` are what ``stream_chat``
    binds. On an unknown provider, ``error_code`` / ``error_detail`` carry
    what ``stream_chat`` passes to ``_format_error_frame`` — the resolver
    itself can't yield into the caller's SSE stream, so ``stream_chat`` owns
    the actual yield, the ``STREAMS_FAILED`` increment, and the early return.

    ``builtin_web_search`` is set ONLY by the lmstudio/openai_compat branch —
    the single signal that gates the app-executed web_search tool. False for
    the native chain path, the store-integration replay reroute, and every
    cloud provider.
    """

    context_mode: str
    dispatch_provider: Any
    error_code: str | None = None
    error_detail: str | None = None
    builtin_web_search: bool = False


class _CapabilityGateDecision(NamedTuple):
    """Result of resolving the wire model id + integrations gate for a turn.

    Returned by ``StreamingService._resolve_model_and_integrations_gate``,
    a pure decision computation: the reprobe/wire-id lookup, the
    substituted/explicit-pick check, the Layer-1 non-tool-model capability
    check, and the Layer-2 context-budget trim decision. It never logs,
    yields into the SSE stream, increments ``STREAMS_FAILED``, or returns
    from the turn — ``stream_chat`` owns every log event, SSE yield, metric
    increment, and early return, reconstructed from this decision.

    Attributes:
        resolution: ``"no_model_loaded"`` (nothing loaded — yield
            ``upstream_unavailable``, return), ``"requested_model_unloaded"``
            (in-catalog but unloaded, explicit pick or no fallback — same
            yield+return), ``"implicit_fallback"`` (substituted for an
            implicit default — log, substitute ``wire_model_id``, continue),
            or ``"resolved"`` (no substitution needed).
        wire_model_id: The resolved ``loaded_instance_id`` to send upstream;
            ``None`` only when ``resolution == "no_model_loaded"``.
        trimmed_kept: Integrations to keep; populated only when
            ``integrations_action == "trim"``.
        trimmed_dropped: Integrations Layer-2 dropped; populated alongside
            ``trimmed_kept``. Unused when ``context_budget_terminate``.
        fallback_key: The substituted model's catalog key; set for
            ``"requested_model_unloaded"`` / ``"implicit_fallback"`` only.
        integrations_action: ``"keep"``, ``"drop_all"`` (Layer-1: model isn't
            trained for tool use), or ``"trim"`` (Layer-2: fit context budget).
        context_budget_terminate: ``True`` when Layer-2 found the request
            unsalvageable even with every integration dropped — log,
            increment ``STREAMS_FAILED`` BEFORE yielding
            ``context_budget_exceeded``, and return.
        budget_estimated_total: ``ContextBudget.estimated_total``.
        budget_max_with_headroom: ``ContextBudget.max_with_headroom``.
    """

    resolution: str
    wire_model_id: str | None
    trimmed_kept: list[str]
    trimmed_dropped: list[str]
    fallback_key: str | None = None
    integrations_action: str = "keep"
    context_budget_terminate: bool = False
    budget_estimated_total: int = 0
    budget_max_with_headroom: int = 0


# Coalesce timer


class _CoalesceTimer:
    """Accumulates ``message.delta`` content and flushes every _COALESCE_INTERVAL_SEC.

    Callers call ``add(text)`` on each delta and ``should_flush()`` /
    ``flush()`` on the timer tick.

    Parameterized on ``table`` (default ``messages``) so the SAME
    coalesce-and-touch-activity discipline drives durable sub-sessions
    (``sub_session_messages``, migration 0045) — without periodic
    ``last_activity_at`` bumps the reaper's extended sub-session sweep
    would force-finalize a healthy multi-minute ``/research`` run at the
    5-minute inactivity mark.

    2026-08-15: also coalesces ``reasoning_content`` and ``tool_calls`` onto
    the draft row, closing the gap where a process kill (no teardown, no
    salvage) left a reaper-finalized draft with content but neither —
    everything the model thought and every tool it ran was gone even
    though the answer text survived. ``state`` (optional; the SAME
    ``_state``/``_pstate`` dict the caller already mirrors
    ``acc_reasoning``/``acc_tool_calls`` into) is read directly rather than
    handed a duplicate accumulator — this class never buffers reasoning or
    tool_calls itself, only tracks what it last WROTE, so a flush triggered
    by content alone doesn't needlessly rewrite an unchanged reasoning/
    tool_calls blob once either has stabilized. ``state=None`` (the old
    call shape, still used by the two hygiene tests that only exercise
    ``touch_activity()``) keeps this exactly as before.
    """

    def __init__(
        self,
        *,
        engine: AsyncEngine,
        message_id: int,
        table: Table = messages,
        state: dict[str, object] | None = None,
    ) -> None:
        self._engine = engine
        self._message_id = message_id
        self._table = table
        self._state = state
        self._buf: list[str] = []
        self._last_flush = monotonic()
        # Throttled separately from flush to avoid one DB transaction per
        # tool_call.arguments chunk (it streams like content deltas).
        self._last_touch = monotonic()
        # What was last WRITTEN (not just accumulated) — see the class
        # docstring. A fresh row starts with neither column populated, so 0 /
        # None correctly treat the first non-empty value as "changed".
        self._last_reasoning_len_written = 0
        self._last_tool_calls_written: list[dict[str, object]] | None = None

    def add(self, text: str) -> None:
        """Append *text* to the accumulation buffer."""
        self._buf.append(text)

    def _pending_reasoning_and_tool_calls(
        self,
    ) -> tuple[str | None, list[dict[str, object]] | None]:
        """Return whichever of (reasoning, tool_calls) has changed since the
        last write to this row — the other is ``None``, meaning "nothing new,
        don't touch that column". ``(None, None)`` when ``state`` is unset or
        neither has changed.
        """
        if self._state is None:
            return None, None
        reasoning: str | None = None
        _r = self._state.get("acc_reasoning")
        if isinstance(_r, str) and _r and len(_r) > self._last_reasoning_len_written:
            reasoning = _r
        tool_calls: list[dict[str, object]] | None = None
        _t = self._state.get("acc_tool_calls")
        if isinstance(_t, list) and _t and _t != self._last_tool_calls_written:
            tool_calls = _t
        return reasoning, tool_calls

    def should_flush(self) -> bool:
        """Return True if the interval elapsed AND there's something new to
        write — buffered content, or reasoning that has grown since the last
        write. This is the ONLY periodic touchpoint during a pure-reasoning
        phase (no content yet): ``add()`` is never called for reasoning
        deltas, so without this branch a turn killed before its first
        content token would flush nothing, ever, regardless of how much
        reasoning had accumulated. tool_calls-only growth is covered by
        ``touch_activity()``'s own tool_call.* cadence, not this trigger.
        """
        if (monotonic() - self._last_flush) < _COALESCE_INTERVAL_SEC:
            return False
        if self._buf:
            return True
        reasoning, _ = self._pending_reasoning_and_tool_calls()
        return reasoning is not None

    async def flush(self) -> None:
        """Persist accumulated content (+ reasoning/tool_calls if either has
        changed) to the draft row and reset the buffer.

        No-op if there's nothing new anywhere. The buffer is cleared and
        ``_last_flush``/the last-written trackers are advanced ONLY after the
        DB write succeeds, so a transient non-lock DB error leaves everything
        intact for the next flush attempt to retry — otherwise it would
        silently lose every delta in the in-flight buffer.
        """
        reasoning, tool_calls = self._pending_reasoning_and_tool_calls()
        if not self._buf and reasoning is None and tool_calls is None:
            return
        content_to_append = "".join(self._buf)

        message_id = self._message_id
        engine = self._engine
        table = self._table

        async def _update() -> None:
            async with engine.begin() as conn:
                # state == 'draft' guard: a coalesce flush must only touch the
                # LIVE draft row. Without it, a delta flushed AFTER the row left
                # draft (a disconnect aborted it to aborted_by_client, or a
                # concurrent finalize moved it to pending/final) would overwrite
                # that row's content by PK — corrupting an aborted/finalized row
                # (found by the strong seat, P2 review 2026-08-12). A non-draft
                # row → row is None here → no write; the buffer is then cleared
                # (the row is done/aborting, nothing left to persist).
                row = (
                    await conn.execute(
                        select(table.c.content).where(
                            table.c.id == message_id,
                            table.c.state == PersistState.DRAFT.value,
                        )
                    )
                ).fetchone()
                if row is None:
                    return
                # Touch last_activity_at in the SAME UPDATE (hot path — do
                # NOT add a second statement).
                from datetime import UTC  # noqa: PLC0415
                from datetime import datetime as _dt

                values: dict[str, object] = {"last_activity_at": _dt.now(UTC)}
                if content_to_append:
                    values["content"] = (row[0] or "") + content_to_append
                if reasoning is not None:
                    values["reasoning_content"] = reasoning
                if tool_calls is not None:
                    values["tool_calls"] = tool_calls

                await conn.execute(
                    table.update()
                    .where(
                        table.c.id == message_id,
                        table.c.state == PersistState.DRAFT.value,
                    )
                    .values(**values)
                )

        try:
            await with_write_retry(_update)
        except Exception as exc:
            # with_write_retry already handles "database is locked"; this
            # covers other transient errors (disk full, integrity, etc).
            # Buffer stays intact so the next flush retries the same content.
            log.warning(
                "stream.coalesce_flush_failed",
                message_id=message_id,
                error=str(exc),
                buf_size=len(content_to_append),
            )
            return

        self._buf = []
        self._last_flush = monotonic()
        if reasoning is not None:
            self._last_reasoning_len_written = len(reasoning)
        if tool_calls is not None:
            self._last_tool_calls_written = tool_calls

    async def touch_activity(self) -> None:
        """Touch ``last_activity_at`` (+ reasoning/tool_calls if either has
        changed) without flushing buffered content.

        ``tool_call.*`` events never call ``add()``, so ``flush()`` is a
        no-op for them; without this, a long content-free tool chain never
        bumps ``last_activity_at`` and the reaper finalizes the row mid-
        stream at the 5-min threshold. Throttled to
        ``_COALESCE_INTERVAL_SEC`` since ``tool_call.arguments`` streams in
        chunks like content deltas. This is also the periodic touchpoint for
        a pure tool-call burst with no content or growing reasoning yet.

        Now guarded ``WHERE state='draft'`` — unguarded before 2026-08-15,
        harmless when the only column touched was ``last_activity_at``, but
        not once reasoning_content/tool_calls share the same UPDATE (the
        same corruption ``flush()``'s guard exists to prevent).
        """
        now = monotonic()
        if now - self._last_touch < _COALESCE_INTERVAL_SEC:
            return
        self._last_touch = now

        message_id = self._message_id
        engine = self._engine
        table = self._table
        reasoning, tool_calls = self._pending_reasoning_and_tool_calls()

        from datetime import UTC  # noqa: PLC0415
        from datetime import datetime as _dt

        async def _touch() -> None:
            values: dict[str, object] = {"last_activity_at": _dt.now(UTC)}
            if reasoning is not None:
                values["reasoning_content"] = reasoning
            if tool_calls is not None:
                values["tool_calls"] = tool_calls
            async with engine.begin() as conn:
                await conn.execute(
                    table.update()
                    .where(
                        table.c.id == message_id,
                        table.c.state == PersistState.DRAFT.value,
                    )
                    .values(**values)
                )

        try:
            await with_write_retry(_touch)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "stream.coalesce_touch_activity_failed",
                message_id=message_id,
                error=str(exc),
            )
            return
        if reasoning is not None:
            self._last_reasoning_len_written = len(reasoning)
        if tool_calls is not None:
            self._last_tool_calls_written = tool_calls


# ---------------------------------------------------------------------------
# Draft finalize / salvage — table-parameterized (durable sub-sessions,
# migration 0045).
#
# Extracted as module-level functions (not bound to ``self``) so the
# route layer (``routes/chats.py::_sub_session_sse``) can drive the exact
# SAME finalize/salvage logic against ``sub_session_messages`` with only an
# ``AsyncEngine`` in hand — no ``StreamingService`` instance required.
# ``StreamingService._finalize_message`` / ``_release_stuck_draft`` below
# are thin wrappers over these for the main-chat call sites, unchanged.
# ---------------------------------------------------------------------------


def _remembered_turn_state(table: Table = messages) -> ColumnElement[bool]:
    """SQLAlchemy predicate: does this row belong in the model's OWN memory
    of the conversation?

    A gracefully ``FINAL`` row always qualifies. An ``aborted_by_client``
    row qualifies too, but ONLY when it carries real ``content`` — the
    user already sees every non-draft row in the transcript
    (``message_service.py``'s FE-facing query filters only ``!= draft``),
    so the model's own context should not silently disagree with what is
    on screen: a turn the user watched happen and can see above the input
    box is real conversational context, not noise, even when a disconnect
    cut it short. ``content != ''`` excludes the one case where inclusion
    would be worse than omission — a disconnect that landed before any
    answer text existed leaves a blank assistant turn, and feeding that
    into history reads as the model having said nothing on purpose.

    Deliberately NOT used by auto-title (``chat_service.py``'s
    ``_maybe_generate_title`` history query) or compaction
    (``ChatService.compact``) — those ask "is this turn SETTLED enough to
    summarize/name the chat from," a different question than "did the
    model produce this." 951744c's own reasoning for why compaction stays
    ``FINAL``-only still holds: an aborted row "isn't settled content" for
    a *permanent* summary, even though it is legitimate turn-by-turn
    memory. Conflating the two was the actual bug — a truncated answer
    silently becoming part of a chat's permanent compacted history when
    a race happened to land it FINAL; keeping the two checks apart (this
    predicate for live conversation memory, plain ``state == FINAL`` for
    anything that gets treated as settled) is what avoids reintroducing
    that failure mode while still fixing the model-forgets-it gap.

    Args:
        table: ``messages`` (default) or ``sub_session_messages`` — same
               ``state``/``content`` column shape, so this is a genuine
               table swap like the other state-machine helpers in this
               module.
    """
    return or_(
        table.c.state == PersistState.FINAL.value,
        and_(
            table.c.state == PersistState.ABORTED_BY_CLIENT.value,
            table.c.content != "",
        ),
    )


async def _finalize_message_impl(
    engine: AsyncEngine,
    *,
    msg_id: int,
    response_id: str | None,
    final_content: str,
    final_reasoning: str,
    stop_reason: str | None = None,
    tool_calls: list[dict[str, object]] | None = None,
    table: Table = messages,
) -> bool:
    """Flush final content + reasoning + response_id into *table*.

    Transitions draft -> pending_finalization -> final. Returns False if
    the draft -> pending transition lost the race (the reaper recovers)
    or the second transition fails; True once ``final`` is reached.

    See :meth:`StreamingService._finalize_message` for the main-chat
    wrapper's full docstring — behavior is identical, just table-agnostic.
    """
    step1_holder: list[int] = []

    async def _update_to_pending() -> None:
        async with engine.begin() as conn:
            result = await conn.execute(
                table.update()
                .where(
                    table.c.id == msg_id,
                    table.c.state == PersistState.DRAFT.value,
                )
                .values(
                    content=final_content,
                    reasoning_content=final_reasoning or None,  # NULL when empty
                    response_id=response_id,
                    stop_reason=stop_reason,
                    tool_calls=tool_calls,
                    state=PersistState.PENDING_FINALIZATION.value,
                )
            )
            step1_holder.append(result.rowcount)

    try:
        await with_write_retry(_update_to_pending)
    except Exception as exc:
        log.error(
            "stream.pending_transition_failed",
            msg_id=msg_id,
            table=table.name,
            error=str(exc),
        )
        STREAMS_FAILED.labels(reason="db_commit_failed").inc()
        return False

    if not step1_holder or step1_holder[0] == 0:
        log.warning(
            "stream.pending_transition_race_lost",
            msg_id=msg_id,
            table=table.name,
            note="Row was already moved by disconnect handler or reaper.",
        )
        return False

    try:
        won = await finalize_pending(engine=engine, message_id=msg_id, table=table)
    except Exception as exc:
        log.error(
            "stream.finalize_failed",
            msg_id=msg_id,
            table=table.name,
            error=str(exc),
        )
        STREAMS_FAILED.labels(reason="db_commit_failed").inc()
        return False

    if won:
        log.info("stream.finalized", msg_id=msg_id, table=table.name)
        STREAMS_COMPLETED.inc()
        return True
    else:
        log.info(
            "stream.finalize_race_lost",
            msg_id=msg_id,
            table=table.name,
            note="Reaper concurrently finalized this row.",
        )
        return False


async def _release_stuck_draft_impl(
    engine: AsyncEngine,
    msg_id: int,
    chat_id: int,
    reason: str,
    *,
    table: Table = messages,
    salvage_content: str | None = None,
    salvage_reasoning: str | None = None,
    salvage_tool_calls: list[dict[str, object]] | None = None,
    had_tool_calls: bool = False,
    tool_rounds: int = 0,
) -> None:
    """Force-transition a draft row in *table* to FINAL on a terminal error.

    See :meth:`StreamingService._release_stuck_draft` for the main-chat
    wrapper's full docstring — behavior is identical, just table-agnostic.
    ``chat_id`` carries the sub_session_id when *table* is
    ``sub_session_messages`` — it is log-only, never a query predicate.
    """
    values: dict[str, object] = {"state": PersistState.FINAL.value}
    salvaged_kind: str | None = None
    if salvage_content is not None or salvage_reasoning is not None:
        terminal = resolve_terminal_content(
            salvage_content or "",
            salvage_reasoning or "",
            had_tool_calls=had_tool_calls,
            tool_rounds=tool_rounds,
        )
        # Only write recovered substance — never blank out content already
        # coalesced onto the row.
        if terminal.content:
            values["content"] = terminal.content
        if terminal.reasoning:
            values["reasoning_content"] = terminal.reasoning
        if salvage_tool_calls:
            values["tool_calls"] = salvage_tool_calls
        if "content" in values or "reasoning_content" in values:
            values["stop_reason"] = reason
            salvaged_kind = terminal.kind
    try:
        async with engine.begin() as conn:
            upd = await conn.execute(
                sa_update(table)
                .where(
                    table.c.id == msg_id,
                    table.c.state == PersistState.DRAFT.value,
                )
                .values(**values)
            )
        if upd.rowcount and upd.rowcount > 0:
            log.warning(
                "stream.stuck_draft_released",
                msg_id=msg_id,
                chat_id=chat_id,
                table=table.name,
                reason=reason,
                salvaged_kind=salvaged_kind,
            )
    except Exception as exc:  # noqa: BLE001
        # The reaper is the safety net — log and continue rather than
        # let the cleanup itself throw and obscure the original error.
        log.error(
            "stream.stuck_draft_release_failed",
            msg_id=msg_id,
            chat_id=chat_id,
            table=table.name,
            reason=reason,
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Durable sub-sessions (migration 0045) — session-row + draft creation,
# in-progress check, and session-status transition. Free functions (engine-
# only, no StreamingService instance) so ``routes/chats.py`` can drive them
# with just the request's AsyncEngine.
# ---------------------------------------------------------------------------


async def _assert_no_sub_session_stream_in_progress(
    engine: AsyncEngine, *, chat_id: int
) -> None:
    """Raise :class:`SubSessionStreamInProgressError` if *chat_id* has a
    sub-session row in the ``draft`` (actively streaming) state.

    Blocks ONLY on ``draft`` — a genuinely in-flight stream — NOT on
    ``pending_finalization``. A ``pending_finalization`` row is a turn whose
    answer already completed (finalize step 1 ran); only the reaper's step-2
    commit to ``final`` is pending. It is not an active stream, so it must not
    block a follow-up operation on the same chat — most importantly the
    ``/sub-session/finalize`` summary, which the FE fires the instant the
    research turn's SSE closes (row = ``pending_finalization``): counting it as
    "in progress" 409'd finalize against its own just-finished turn and hung the
    panel on "Generating summary…". This mirrors the FE's D9 liveness rule
    (``lib/subSession.ts``: only ``draft`` is "live"). The D4 "one active
    sub-session stream per chat" invariant is still enforced — a second stream
    can't start while the first is ``draft``.

    Must be called INSIDE the per-chat sub-session lock (D4) so the check
    and the subsequent draft insertion are atomic — mirrors why
    ``StreamingService._assert_no_in_progress_stream`` must hold the main
    chat lock. Scoped to sub-sessions only: independent of, and never
    blocked by, an in-progress MAIN-chat stream on the same chat_id.

    Known tradeoff (2026-08-15, backend review ahead of v1.0.3): the
    per-chat lock above is released once the in-progress check + draft
    insertion complete — it is NOT held for the duration of the stream,
    and NOT re-acquired for finalize. ``_finalize_message_impl``'s
    draft -> pending_finalization -> final transition is two SEPARATE
    awaited DB round trips; a second request for the SAME chat_id can
    land its own check + draft-creation in the (small, DB-retry-widenable)
    gap between them, since by then the row already reads
    ``pending_finalization``, not ``draft``. That is this function's
    intended door for the ``/sub-session/finalize`` follow-up described
    above — but the check has no way to tell that caller apart from a
    genuinely new stream, so a second real stream can (rarely) start
    on the same chat before the first one's row has actually settled to
    ``final``. Bounded, not corrupting: ``_active_streams.py`` is already
    refcounted for overlapping streams per chat_id, and the two turns
    write to different row IDs — so the cost is possible contention on
    the same local model, not data loss or a wrong write. Re-narrowing
    back to ``draft | pending_finalization`` reintroduces the 409-against-
    its-own-turn hang this function exists to fix. Closing the gap for
    real needs the caller to say WHICH kind of follow-up it is (e.g. an
    explicit "finalize this specific sub_session_id" intent that bypasses
    the general in-progress check entirely, rather than this function
    trying to infer intent from timing) — not a narrower/wider state list.
    """
    async with engine.connect() as conn:
        result = await conn.execute(
            select(sub_session_messages.c.id)
            .select_from(sub_session_messages.join(sub_sessions))
            .where(
                sub_sessions.c.chat_id == chat_id,
                sub_session_messages.c.state == PersistState.DRAFT.value,
            )
        )
        row = result.fetchone()
    if row is not None:
        raise SubSessionStreamInProgressError(chat_id)


async def _create_sub_session_with_draft(
    engine: AsyncEngine,
    *,
    chat_id: int,
    preset_id: str,
    title: str | None,
    model_id: str,
    user_text: str,
) -> tuple[int, int]:
    """Atomically create a ``sub_sessions`` row + its opening user/draft pair.

    Mirrors :meth:`StreamingService._create_draft`'s pattern for the main
    chat: three inserts in ONE transaction — the ``sub_sessions`` parent
    row (``status='active'``), the user turn (``state='final'``), and the
    assistant draft (``state='draft'``) — so a process crash between
    commits never leaves an orphaned parent row or a draft with no
    matching user turn. Uses ``sub_session_id`` (not ``chat_id``) as the
    FK on ``sub_session_messages`` — the column differs from ``messages``,
    so this is a dedicated sibling rather than a table-parameterized
    ``_create_draft``.

    Returns:
        ``(sub_session_id, draft_msg_id)``.
    """
    ids: list[int] = []

    async def _insert() -> None:
        async with engine.begin() as conn:
            sess_result = await conn.execute(
                insert(sub_sessions).values(
                    chat_id=chat_id,
                    preset_id=preset_id,
                    title=title,
                    status="active",
                    model_id=model_id,
                )
            )
            sess_pk = sess_result.inserted_primary_key
            if sess_pk is None:
                raise RuntimeError("INSERT into sub_sessions returned no PK")
            sub_session_id = int(sess_pk[0])

            await conn.execute(
                insert(sub_session_messages).values(
                    sub_session_id=sub_session_id,
                    role="user",
                    content=user_text,
                    state=PersistState.FINAL.value,
                )
            )
            draft_result = await conn.execute(
                insert(sub_session_messages).values(
                    sub_session_id=sub_session_id,
                    role="assistant",
                    content="",
                    state=PersistState.DRAFT.value,
                    model_id=model_id,
                )
            )
            draft_pk = draft_result.inserted_primary_key
            if draft_pk is None:
                raise RuntimeError("INSERT into sub_session_messages (draft) returned no PK")
            ids.append(sub_session_id)
            ids.append(int(draft_pk[0]))

    await with_write_retry(_insert)
    sub_session_id, msg_id = ids[0], ids[1]
    log.info(
        "sub_session.draft_created",
        chat_id=chat_id,
        sub_session_id=sub_session_id,
        msg_id=msg_id,
        preset_id=preset_id,
        model_id=model_id,
    )
    return sub_session_id, msg_id


async def _append_turn_to_sub_session(
    engine: AsyncEngine,
    *,
    sub_session_id: int,
    model_id: str,
    user_text: str,
) -> tuple[int, int]:
    """Atomically append a new turn onto an EXISTING ``sub_sessions`` row.

    Reopen + continue (P4). The caller has already validated that
    *sub_session_id* belongs to the requesting chat + user — this helper
    does not re-check ownership. Mirrors
    :func:`_create_sub_session_with_draft`'s single-transaction discipline
    but does NOT create a new ``sub_sessions`` parent row: two inserts
    (the new user turn, ``state='final'``; the new assistant draft,
    ``state='draft'``) plus a ``sub_sessions`` update in ONE transaction —
    ``status`` flips back to ``'active'`` (a reopened ``final``/``aborted``
    session resumes streaming) and ``updated_at`` bumps so the session
    re-sorts to the top of the per-chat history list.

    Returns:
        ``(sub_session_id, draft_msg_id)`` — same shape as
        :func:`_create_sub_session_with_draft` so both call sites in
        ``_sub_session_sse`` are symmetric.
    """
    ids: list[int] = []

    async def _insert() -> None:
        async with engine.begin() as conn:
            await conn.execute(
                insert(sub_session_messages).values(
                    sub_session_id=sub_session_id,
                    role="user",
                    content=user_text,
                    state=PersistState.FINAL.value,
                )
            )
            draft_result = await conn.execute(
                insert(sub_session_messages).values(
                    sub_session_id=sub_session_id,
                    role="assistant",
                    content="",
                    state=PersistState.DRAFT.value,
                    model_id=model_id,
                )
            )
            draft_pk = draft_result.inserted_primary_key
            if draft_pk is None:
                raise RuntimeError("INSERT into sub_session_messages (draft) returned no PK")
            await conn.execute(
                sub_sessions.update()
                .where(sub_sessions.c.id == sub_session_id)
                .values(status="active", updated_at=func.now())
            )
            ids.append(int(draft_pk[0]))

    await with_write_retry(_insert)
    msg_id = ids[0]
    log.info(
        "sub_session.turn_appended",
        sub_session_id=sub_session_id,
        msg_id=msg_id,
        model_id=model_id,
    )
    return sub_session_id, msg_id


async def _salvage_aborted_row(
    engine: AsyncEngine,
    msg_id: int,
    *,
    content: str | None,
    reasoning: str | None,
    tool_calls: list[dict[str, object]] | None,
    table: Table = messages,
) -> None:
    """Persist the full accumulated turn state onto a disconnect-aborted row.

    On client disconnect ``safe_abort_draft`` moves the row
    draft -> aborted_by_client WITHOUT writing the accumulated
    reasoning/tool_calls, and ``_release_stuck_draft_impl`` only touches
    ``draft`` rows — so whichever of those two writers wins the race, a
    reopened disconnected turn would otherwise show only whatever the
    (state-guarded) coalesce flush persisted before the abort, losing
    reasoning_content + tool_calls (and any content streamed after the last
    flush but before the core tore down — up to one ``_COALESCE_INTERVAL_SEC``
    window). Write the full accumulated state here, KEEPING the
    ``aborted_by_client`` state — the turn was interrupted, this is NOT a
    finalize. ``WHERE state='aborted_by_client'`` so a graceful/final row is
    never clobbered.

    ``table`` (default ``messages``) parameterizes this over the main chat
    and durable sub-sessions (``sub_session_messages``), mirroring
    :func:`_release_stuck_draft_impl` / :func:`_finalize_message_impl`.

    Deliberately does NOT run ``resolve_terminal_content``'s empty-answer
    fold (substance_fold.py) the way ``_release_stuck_draft_impl`` does:
    content/reasoning/tool_calls are written as three independent columns,
    so a reasoning-only turn (model parked everything in reasoning_content,
    never emitted answer text) that then disconnects keeps `content` empty
    here. Checked, not assumed (2026-08-14): this does NOT produce a blank
    bubble. ChatMessage.tsx's phantom-row guard only suppresses rendering
    when BOTH content AND reasoning_content are empty; ProcessStream.tsx
    renders a "Reasoning" toggle (collapsed, expandable) off `hasReasoning`
    alone, independent of `hasAnswer` — so the user sees the reasoning
    trace via that affordance instead of an empty message. Folding it into
    `content` here would duplicate substance_fold's already-corrected
    empty-only semantics in a second place instead of reusing them, for a
    case the FE already renders correctly without it. If this ever needs to
    change, reuse ``resolve_terminal_content``/``substance_fold`` rather
    than writing a second folding rule.

    Safe against shrinking an already-persisted row: every value here comes
    from the SAME in-memory accumulator the coalesce flush itself draws
    from (mirrored into the caller's ``_state``/``_pstate`` dict on every
    delta), so it is always a superset of — never shorter than — whatever
    the last flush wrote; and each field is only included in the UPDATE when
    truthy (``tool_calls``) or non-None (``content``/``reasoning``), so a
    field the caller never captured is left untouched rather than blanked.
    (strong seat, P2 review 2026-08-12.)
    """
    values: dict[str, object] = {}
    if content is not None:
        values["content"] = content
    if reasoning is not None:
        values["reasoning_content"] = reasoning
    if tool_calls:
        values["tool_calls"] = tool_calls
    if not values:
        return

    async def _do_update() -> None:
        async with engine.begin() as conn:
            await conn.execute(
                table.update()
                .where(
                    table.c.id == msg_id,
                    table.c.state == PersistState.ABORTED_BY_CLIENT.value,
                )
                .values(**values)
            )

    try:
        await with_write_retry(_do_update)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "stream.aborted_salvage_failed",
            msg_id=msg_id,
            table=table.name,
            error=str(exc),
        )


async def _transition_sub_session_status(
    engine: AsyncEngine, *, sub_session_id: int, to_status: str
) -> bool:
    """Atomically move ``sub_sessions.status`` from ``active`` to *to_status*.

    D9: ``active`` -> ``final`` on graceful completion, ``active`` ->
    ``aborted`` on disconnect/error/reaper. A single conditional
    ``UPDATE ... WHERE status='active'`` — whichever caller (the stream's
    own teardown, the disconnect watcher, or the reaper) gets there first
    wins; later callers are clean no-ops, so this is safe to call from
    more than one teardown path without an explicit lock.

    Returns:
        ``True`` if this call performed the transition, ``False`` if the
        row was already out of ``active`` (or absent).
    """
    result_holder: list[int] = []

    async def _do_update() -> None:
        async with engine.begin() as conn:
            result = await conn.execute(
                sub_sessions.update()
                .where(
                    sub_sessions.c.id == sub_session_id,
                    sub_sessions.c.status == "active",
                )
                .values(status=to_status)
            )
            result_holder.append(result.rowcount)

    try:
        await with_write_retry(_do_update)
    except Exception as exc:  # noqa: BLE001
        log.error(
            "sub_session.status_transition_failed",
            sub_session_id=sub_session_id,
            to_status=to_status,
            error=str(exc),
        )
        return False

    won = bool(result_holder) and result_holder[0] == 1
    log.info(
        "sub_session.status_transition",
        sub_session_id=sub_session_id,
        to_status=to_status,
        won=won,
    )
    return won


# SSE frame formatting


def _format_sse_frame(event: CanonicalEvent, *, msg_id: int) -> bytes:
    """Serialize *event* as an SSE frame (``event: <type>\\ndata: <json>\\n\\n``).

    ``msg_id`` is injected into every data payload so the frontend's
    ``useSSE`` reconciliation hook can read a stable identifier from any
    event, including frame #1 (``chat.start``).
    """
    data: dict = {"type": event.type, "msg_id": msg_id}

    if event.content is not None:
        data["content"] = event.content
    if event.response_id is not None:
        data["response_id"] = event.response_id
    if event.progress is not None:
        data["progress"] = event.progress
    if event.tool_call is not None:
        data["tool_call"] = event.tool_call.model_dump()
    if event.error is not None:
        data["error"] = event.error
    if event.warning is not None:
        data["warning"] = event.warning
    if event.model_instance_id is not None:
        data["model_instance_id"] = event.model_instance_id
    # Carried on chat.end only; "length" drives the FE Continue chip.
    if event.stop_reason is not None:
        data["stop_reason"] = event.stop_reason
    # Real LM Studio token stats from
    # the native chat.end `result.stats` block. Sparse like stop_reason —
    # omitted when the upstream surface doesn't report them, so the FE can
    # fall back to its local chunk-count approximation.
    if event.total_output_tokens is not None:
        data["total_output_tokens"] = event.total_output_tokens
    if event.tokens_per_second is not None:
        data["tokens_per_second"] = event.tokens_per_second

    frame = f"event: {event.type}\ndata: {json.dumps(data)}\n\n"
    return frame.encode("utf-8")


# The five tool_call.* event types that carry a CanonicalToolCall payload and
# participate in the persisted messages.tool_calls list. The *_warning
# variants are pipeline advisories, not call lifecycle events — excluded.
_TOOL_CALL_LIFECYCLE: Final[frozenset[str]] = frozenset(
    {
        "tool_call.start",
        "tool_call.name",
        "tool_call.arguments",
        "tool_call.success",
        "tool_call.failure",
    }
)


def _accumulate_tool_call(
    calls: list[dict[str, object]],
    event_type: str,
    tc: CanonicalToolCall,
) -> None:
    """Fold one ``tool_call.*`` event into the persistable tool-call list.

    Mirrors the FE's live accumulator in ``useSSE.ts`` so the list written to
    ``messages.tool_calls`` round-trips into ``ChatMessageData.toolCalls``
    without translation. Entry shape (= FE ``ToolCall``):

        {"id": str, "name": str, "arguments": str (JSON object text),
         "status": "pending" | "success" | "failure", "result"?: str}

    Mutates *calls* in place; entries are keyed by the CanonicalToolCall id.
    Non-lifecycle event types (anything not in ``_TOOL_CALL_LIFECYCLE``) are
    a no-op.
    """
    if event_type not in _TOOL_CALL_LIFECYCLE:
        return

    entry = next((c for c in calls if c["id"] == tc.id), None)

    if event_type == "tool_call.start":
        if entry is None:
            calls.append(
                {
                    "id": tc.id,
                    "name": tc.name or "",
                    "arguments": "{}",
                    "status": "pending",
                }
            )
        return

    if entry is None:
        # Start frame lost (decoder id drift / mid-call reconnect) —
        # synthesise the entry so name/args/result still persist.
        synthesised: dict[str, object] = {
            "id": tc.id,
            "name": "",
            "arguments": "{}",
            "status": "pending",
        }
        entry = synthesised
        calls.append(entry)

    if tc.name:
        entry["name"] = tc.name

    if event_type == "tool_call.arguments":
        entry["arguments"] = json.dumps(tc.arguments)
    elif event_type == "tool_call.success":
        if tc.arguments:
            entry["arguments"] = json.dumps(tc.arguments)
        entry["status"] = "success"
        if tc.result is not None:
            entry["result"] = tc.result
    elif event_type == "tool_call.failure":
        if tc.arguments:
            entry["arguments"] = json.dumps(tc.arguments)
        entry["status"] = "failure"


def _format_error_frame(*, code: str, detail: str, msg_id: int) -> bytes:
    """Build a terminal SSE ``error`` frame."""
    data = {"type": "error", "msg_id": msg_id, "error": {"code": code, "message": detail}}
    frame = f"event: error\ndata: {json.dumps(data)}\n\n"
    return frame.encode("utf-8")


def _format_warning_frame(*, code: str, detail: str, msg_id: int) -> bytes:
    """Build a non-terminal SSE ``warning`` frame.

    Distinct from ``error`` so the FE doesn't terminate the stream — e.g.
    used by the pre-flight budget gate when integrations were trimmed but
    the stream otherwise proceeds normally.
    """
    data = {
        "type": "warning",
        "msg_id": msg_id,
        "warning": {"code": code, "message": detail},
    }
    frame = f"event: warning\ndata: {json.dumps(data)}\n\n"
    return frame.encode("utf-8")


def _format_followups_frame(*, followups: list[str], msg_id: int) -> bytes:
    """Build a synthetic SSE ``followups`` frame.

    Emitted AFTER ``chat.end`` once the out-of-band followups call
    completes, so the answer renders immediately and chips appear a moment
    later without blocking the stream.
    """
    data = {"type": "followups", "msg_id": msg_id, "followups": followups}
    frame = f"event: followups\ndata: {json.dumps(data)}\n\n"
    return frame.encode("utf-8")


def _format_mode_adopt_frame(*, preset_id: str | None, msg_id: int) -> bytes:
    """Build a synthetic SSE ``mode_adopt`` frame.

    Emitted AFTER ``chat.end`` (and after ``followups``, when both fire)
    once the out-of-band C3 mode-adoption call (:func:`_infer_mode_oob`)
    completes, so the answer renders immediately and the persona switch —
    if any — applies a moment later without blocking the stream.
    ``preset_id`` is ``None`` when the OOB call found no confident match
    this turn (the common case — the classifier is deliberately biased
    toward "no change") or failed outright; the FE treats ``None`` as
    "leave the chat's current mode alone."
    """
    data = {"type": "mode_adopt", "msg_id": msg_id, "preset_id": preset_id}
    frame = f"event: mode_adopt\ndata: {json.dumps(data)}\n\n"
    return frame.encode("utf-8")


def _format_memory_saved_frame(*, count: int, msg_id: int) -> bytes:
    """Build a synthetic SSE ``memory.saved`` frame.

    Emitted after ``chat.end`` (and after ``followups``, when present) once
    the detached auto-memory distillation task resolves within the bounded
    wait (``_MEMORY_SAVED_FRAME_WAIT_SEC``). Only emitted when at least one
    new fact was stored.
    """
    data = {"type": "memory.saved", "msg_id": msg_id, "count": count}
    return f"event: memory.saved\ndata: {json.dumps(data)}\n\n".encode()


async def _generate_followups_oob(
    *,
    lm_client: LmstudioStreamingClient,
    model: str,
    conversation_messages: list[dict],  # type: ignore[type-arg]
    assistant_answer: str,
    # Reasoning background-models can spend real time before the array lands
    # in `content`; budget generously (matches the distill/title OOB calls) so
    # it doesn't time out into empty content -> no chips. Every caller passes
    # ``self._aux_model_timeout_sec`` (1800 s); this fallback tracks it so a
    # directly-constructed call can't silently reintroduce a short cap.
    timeout_sec: float = 1800.0,
) -> list[str]:
    """Make a separate lightweight call to generate follow-up question chips.

    Calls the LM Studio compat endpoint with ``stream=False``, thinking
    disabled, and a tight directive. Never raises — all failures return
    ``[]`` so the caller can yield an empty followups frame without
    breaking the turn.

    Returns:
        List of up to 3 follow-up question strings, or ``[]`` on any failure.
    """
    from lmchat.services.lmstudio_adapter import LmstudioAdapter  # noqa: PLC0415

    try:
        adapter = lm_client._adapter  # type: ignore[attr-defined]
        if not isinstance(adapter, LmstudioAdapter):
            # Replay / cloud provider path — skip OOB followups for now.
            return []

        http_client = adapter._http_client  # type: ignore[attr-defined]
        base_url = adapter._base_url  # type: ignore[attr-defined]
        url = f"{base_url}/v1/chat/completions"

        # Ending the message list on an `assistant` turn makes the model
        # CONTINUE that turn (echoes/extends the answer, ignores the "output
        # JSON" directive) instead of answering it — frame the whole thing as
        # a user request for JSON instead.
        def _role_label(role: str) -> str:
            return "User" if role == "user" else "Reply"

        convo_lines = [
            f"{_role_label(str(m.get('role', 'user')))}: {str(m.get('content', '')).strip()}"
            for m in conversation_messages[-6:]
            if str(m.get("content", "")).strip()
        ]
        convo_lines.append(f"Reply: {assistant_answer.strip()}")
        convo_text = "\n".join(convo_lines)

        _FOLLOWUPS_SYSTEM = (
            "You generate follow-up questions an end-user might ask next in a "
            "chat. Output ONLY a JSON array of 2-3 short question strings — no "
            "prose, no markdown, no code fences, no comment wrapper."
        )
        _user_instruction = (
            f"Here is the conversation so far:\n\n{convo_text}\n\n"
            "Write 2-3 natural follow-up questions I (the user) might ask next, "
            "in my own first-person voice (start with How/Why/Can you/What), each "
            "grounded in a specific concept from the last reply. Output ONLY a "
            "JSON array of strings."
        )
        messages: list[dict] = [  # type: ignore[type-arg]
            {"role": "system", "content": _FOLLOWUPS_SYSTEM},
            {"role": "user", "content": _user_instruction},
        ]

        body = {
            "model": model,
            "messages": messages,
            "stream": False,
            # Reasoning models ignore every thinking-disable hint and spend
            # ~1-2k tokens deliberating before emitting the array;
            # max_tokens=120 produces empty content every time, 2048 doesn't.
            "max_tokens": 2048,
            "temperature": 0.3,
        }
        body["thinking"] = {"type": "disabled"}  # best-effort; harmless if ignored

        # url derives from the admin-configured adapter _base_url (never
        # user-controlled) — the SSRF outbound-path guard exempts this.
        # bg_aux_slot serializes against other background aux calls sharing
        # the single background model.
        async with bg_aux_slot():
            resp = await http_client.post(url, json=body, timeout=timeout_sec)
        resp.raise_for_status()
        result = resp.json()
        message = result.get("choices", [{}])[0].get("message", {})
        return _oob_json_array_with_reasoning_salvage(message)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "stream.followups_oob_failed", error=str(exc), error_type=type(exc).__name__
        )
        return []


# Sentinel the mode-adoption classifier is instructed to answer with when no
# persona clearly fits the next turn. Never a valid catalog id (list_preset_ids()
# never contains "none" — RAW_PRESET_ID on the FE is a disjoint sentinel with a
# different meaning, "no system prompt at all", and isn't in this catalog either).
_MODE_ADOPT_NONE = "none"

# Prefix for the classifier's WIRE vocabulary (see _infer_mode_oob /
# _last_valid_mode_id). The classifier is instructed to answer with
# `mode_<id>` tokens (e.g. "mode_research"), never the bare preset id —
# `general` in particular is an ordinary English word, and the salvage
# scan (which deliberately looks anywhere in a reasoning trace, not just
# the final token) would false-match prose like "...but in general, ..."
# and silently drop the user out of an adopted mode back to default. The
# prefix is purely an internal protocol between this prompt and its own
# parser; the function's return contract stays the bare preset id.
_MODE_TOKEN_PREFIX = "mode_"


def _last_valid_mode_id(text: str, valid_ids: list[str]) -> str | None:
    """Recover the LAST catalog id (or the "none" sentinel) mentioned in ``text``.

    Mirrors :func:`_last_json_array_of_strings`'s "take the last, not the
    first" shape, for the same reason: a reasoning model's salvaged
    ``reasoning_content`` can run thousands of characters of deliberation —
    weighing candidates, second-guessing, drafting — before stating its
    actual conclusion, and the conclusion is what's near the END of the
    trace, not scattered earlier mentions.

    Matching is against the ``mode_``-PREFIXED wire tokens
    (:data:`_MODE_TOKEN_PREFIX`), never the bare ids — the classifier
    prompt is instructed to answer with those tokens exclusively (see
    :func:`_infer_mode_oob`). Bare ids are ordinary or near-ordinary
    English words (``general`` above all: an id AND the default persona,
    so a false hit on the word actively wipes an adopted mode back to
    default) and would false-match unrelated prose; a distinctive token
    like ``mode_research`` essentially never occurs by accident. Matching
    is still word-boundary and case-insensitive, so ``mode_researcher``
    never matches ``mode_research`` as a substring. The prefix is stripped
    before returning, so this function's return value is always a bare
    id/``"none"`` — the token vocabulary never leaks past this function.

    ``"none"`` is returned verbatim (not swallowed here) when
    ``mode_none`` is the last matched token, INCLUDING when earlier text
    mentions real mode tokens while deliberating — the caller treats a
    returned ``"none"`` the same as "no match" (see :func:`_infer_mode_oob`).
    This function itself never applies the none/valid-id distinction; it
    only answers "what was the last token that looked like a decision."

    Args:
        text: The candidate reply text (bare token OR long reasoning prose).
        valid_ids: The ids this scan should accept — for C3 mode adoption,
                   :func:`~lmchat.services.preset_catalog.list_adoptable_preset_ids`
                   (NOT the full :func:`~lmchat.services.preset_catalog.list_preset_ids`
                   — the default persona is deliberately excluded from what
                   this function can ever match; see that function's
                   docstring for why).

    Returns:
        The last matched id/``"none"`` (lowercased, prefix stripped), or
        ``None`` when nothing in ``text`` matches any wire token.
    """
    import re as _re  # noqa: PLC0415

    if not text:
        return None
    tokens = [f"{_MODE_TOKEN_PREFIX}{i}" for i in [*valid_ids, _MODE_ADOPT_NONE]]
    alternation = "|".join(_re.escape(t) for t in tokens)
    matches = _re.findall(rf"\b(?:{alternation})\b", text, flags=_re.IGNORECASE)
    if not matches:
        return None
    return matches[-1].lower().removeprefix(_MODE_TOKEN_PREFIX)


async def _infer_mode_oob(
    *,
    lm_client: LmstudioStreamingClient,
    model: str,
    conversation_messages: list[dict],  # type: ignore[type-arg]
    assistant_answer: str,
    # Same generous budget as _generate_followups_oob — a reasoning
    # background model can spend real time deliberating even over a
    # one-word answer. Callers pass ``self._aux_model_timeout_sec`` (1800 s);
    # this fallback tracks it.
    timeout_sec: float = 1800.0,
) -> str | None:
    """C3 — ask a separate lightweight call which role preset the NEXT turn should run under.

    Mirrors :func:`_generate_followups_oob` exactly: a separate
    ``stream=False``, thinking-disabled call made AFTER the main answer has
    already streamed to the client. This MUST stay fully out-of-band — an
    earlier incident measured an injected followups directive riding the
    MAIN answer's system prompt inflating local-model reasoning ~30x (193
    -> 5867 chars, ~1470 wasted tokens/turn; see the OOB-followups
    decoupling this mirrors). A mode-selection directive on the main
    prompt would carry the identical risk, which is exactly why this
    function exists as its own call instead.

    The classifier is deliberately biased toward answering "none" (no
    adoption): most turns are ordinary conversation, and churning the
    chat's persona on every single turn would be worse for the user than
    never adopting one automatically. Only a real ADOPTABLE catalog id
    (:func:`~lmchat.services.preset_catalog.list_adoptable_preset_ids` —
    NEVER the default persona; see that function's docstring for the live
    defect that motivated excluding it) recovered by
    :func:`_last_valid_mode_id` is ever trusted — a hallucinated id, a
    reply with no id anywhere, an empty response, or an upstream failure
    all resolve to ``None``. Never raises.

    Returns:
        A valid preset id, or ``None`` when no mode change is warranted or
        the call failed in any way (never trust free text past this gate).
    """
    from lmchat.services.lmstudio_adapter import LmstudioAdapter  # noqa: PLC0415

    try:
        adapter = lm_client._adapter  # type: ignore[attr-defined]
        if not isinstance(adapter, LmstudioAdapter):
            # Replay / cloud provider path — skip OOB mode adoption for now.
            return None

        http_client = adapter._http_client  # type: ignore[attr-defined]
        base_url = adapter._base_url  # type: ignore[attr-defined]
        url = f"{base_url}/v1/chat/completions"

        # Same framing fix as _generate_followups_oob: ending on an
        # `assistant` turn makes some models CONTINUE it instead of
        # answering the classification question.
        def _role_label(role: str) -> str:
            return "User" if role == "user" else "Reply"

        convo_lines = [
            f"{_role_label(str(m.get('role', 'user')))}: {str(m.get('content', '')).strip()}"
            for m in conversation_messages[-6:]
            if str(m.get("content", "")).strip()
        ]
        convo_lines.append(f"Reply: {assistant_answer.strip()}")
        convo_text = "\n".join(convo_lines)

        # ADOPTABLE ids only — never the default persona. Live probing
        # (2026-08-14) found a local model choosing "general" (the
        # default) deterministically for a clear /research-shaped
        # exchange: the catalog's own "general" entry reads as
        # "general-purpose CONVERSATION", semantically adjacent to this
        # prompt's own "reply none for general/casual conversation" line,
        # and the model reached for that token instead of mode_none — same
        # class of collision the classifier is supposed to resolve AS
        # none, not as a wrong adoption. See
        # preset_catalog.list_adoptable_preset_ids's docstring.
        valid_ids = list_adoptable_preset_ids()
        # Wire vocabulary: `mode_<id>` tokens, derived from
        # list_adoptable_preset_ids() (never a hand-maintained second list
        # — see _MODE_TOKEN_PREFIX's doc comment for why the prefix exists
        # at all).
        none_token = f"{_MODE_TOKEN_PREFIX}{_MODE_ADOPT_NONE}"
        catalog_lines: list[str] = []
        mode_tokens: list[str] = []
        for preset_id in valid_ids:
            preset = get_preset_definition(preset_id)
            assert preset is not None, f"catalog id without a definition: {preset_id}"
            token = f"{_MODE_TOKEN_PREFIX}{preset.id}"
            mode_tokens.append(token)
            catalog_lines.append(f"- {token}: {preset.short_description}")
        catalog_text = "\n".join(catalog_lines)

        _MODE_ADOPT_SYSTEM = (
            "You classify a just-finished chat exchange against a fixed "
            "set of assistant personas, to decide which persona the NEXT "
            "turn should run under. Output ONLY the bare persona TOKEN — a "
            "single lowercase token exactly as listed below (including its "
            "`mode_` prefix), no punctuation, no quotes, no markdown, no "
            "explanation — nothing else."
        )
        _user_instruction = (
            f"Personas:\n{catalog_text}\n\n"
            f"Exchange so far:\n\n{convo_text}\n\n"
            "Which persona best fits the NEXT turn, based on what this "
            "exchange was actually about? Only pick a specific persona "
            "when the conversation clearly and predominantly calls for "
            "it. If it's casual conversation, ambiguous, mixed across "
            "several personas, or doesn't clearly call for a specialized "
            f'one, reply exactly "{none_token}". Reply with EXACTLY one '
            f'token: one of {mode_tokens} or "{none_token}".'
        )
        messages: list[dict] = [  # type: ignore[type-arg]
            {"role": "system", "content": _MODE_ADOPT_SYSTEM},
            {"role": "user", "content": _user_instruction},
        ]

        body = {
            "model": model,
            "messages": messages,
            "stream": False,
            # A bare-word answer needs little budget, but a reasoning model
            # still spends real tokens deliberating even over one word —
            # same cap as the followups/distill OOB calls so it doesn't
            # truncate into an empty response.
            "max_tokens": 2048,
            # Deterministic classification, not creative generation.
            "temperature": 0.0,
        }
        body["thinking"] = {"type": "disabled"}  # best-effort; harmless if ignored

        async with bg_aux_slot():
            resp = await http_client.post(url, json=body, timeout=timeout_sec)
        resp.raise_for_status()
        result = resp.json()
        message = result.get("choices", [{}])[0].get("message", {})
        # Shared content -> reasoning_content salvage primitive (see
        # lmstudio/oob_text.py's module docstring). Tries to recover the
        # LAST valid `mode_<id>` token (or `mode_none`) from `content`
        # FIRST; only if that EXTRACTION comes up empty does it try
        # `reasoning_content` — not merely if `content` is empty. That
        # distinction is load-bearing: an earlier version of this function
        # used oob_message_text's weaker "field empty" rule directly, so a
        # non-empty-but-tokenless `content` (e.g. "I'm not sure") never
        # even looked at `reasoning_content`, silently dropping a mode the
        # model had actually decided on there. See _last_valid_mode_id for
        # the token scan itself (word-boundary, case-insensitive, last
        # match wins — a reasoning trace can run thousands of characters
        # before stating its conclusion).
        candidate = oob_salvage(message, lambda t: _last_valid_mode_id(t, valid_ids))
        if candidate is None or candidate == _MODE_ADOPT_NONE:
            return None
        return candidate
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "stream.mode_adopt_oob_failed", error=str(exc), error_type=type(exc).__name__
        )
        return None


def _last_json_array_of_strings(raw: str) -> list[str]:
    """Extract the LAST top-level ``[...]`` array of strings from prose.

    Reasoning models emit their final JSON answer after one or more DRAFT
    arrays, in either ``content`` or ``reasoning_content`` (see
    :func:`_oob_json_array_with_reasoning_salvage`, which uses this as its
    extractor for both fields via the shared
    :func:`~lmchat.lmstudio.oob_text.oob_salvage` primitive) — an earlier
    draft array is usually NOT the real answer, so scanning for the FIRST
    match instead would often pick the draft. Returns ``[]`` on no match.
    Caps at 3 items.
    """
    import json as _json  # noqa: PLC0415

    result: list[str] = []
    depth = 0
    start = -1
    for i, ch in enumerate(raw):
        if ch == "[":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0 and start != -1:
                candidate = raw[start : i + 1]
                try:
                    parsed = _json.loads(candidate)
                except (_json.JSONDecodeError, ValueError):
                    start = -1
                    continue
                if isinstance(parsed, list):
                    strings = [
                        s.strip()
                        for s in parsed
                        if isinstance(s, str) and s.strip()
                    ]
                    if strings:
                        result = strings[:3]  # keep the LAST valid array
                start = -1
    return result


def _oob_json_array_with_reasoning_salvage(message: dict) -> list[str]:  # type: ignore[type-arg]
    """Parse a JSON-array OOB result, salvaging from ``reasoning_content``.

    A reasoning model asked for a JSON array normally emits it in
    ``content``, but when reasoning runs long the array lands in
    ``reasoning_content`` instead and ``content`` comes back empty — reading
    ``content`` alone would then silently drop the result.

    Goes through the shared :func:`~lmchat.lmstudio.oob_text.oob_salvage`
    primitive with :func:`_last_json_array_of_strings` as the extractor for
    BOTH fields: try to recover the last valid array from ``content``
    first; if that comes up empty, try ``reasoning_content``. "Last, not
    first" matters on the reasoning side (a trace can carry draft arrays
    before the real one) and is harmless on the content side (a clean
    direct reply essentially never carries more than one candidate array,
    so first-vs-last make no practical difference there). Never raises.
    """
    return oob_salvage(message, _last_json_array_of_strings)


# Distillation system prompt — extract durable USER facts, heavily biased
# toward saving NOTHING. Kept module-level so tests can assert against it.
_DISTILL_SYSTEM = (
    "You maintain a long-term memory of durable facts about a single user. "
    "The transcript is labelled by speaker: a line beginning 'User:' is what "
    "the USER said; a line beginning 'Assistant:' is the AI's reply.\n\n"
    "Extract a durable fact ONLY when the USER stated it about themselves on a "
    "'User:' line. The 'Assistant:' lines are CONTEXT ONLY: never treat "
    "anything the assistant said, suggested, asked, offered, or researched as "
    "a fact about the user — not even when the assistant phrases it as though "
    "it were about them (e.g. 'you might enjoy X', 'given your interest in Y'). "
    "If a candidate fact is not directly supported by something the user "
    "themselves said, do not save it.\n\n"
    "SAVE only stable facts about the USER: their identity (name, role, "
    "location), lasting preferences (how they like answers, tools they use), "
    "and ongoing interests or projects. Write each as a short third-person "
    'statement, e.g. "Name is Kevin", "Into astrophysics and dark energy", '
    '"Prefers concise answers" (these illustrate the FORMAT only — never save '
    "the example text itself unless the user actually stated it).\n\n"
    "Do NOT save: one-off questions, the topic of a single query, transient "
    "task details, anything only the assistant said, world facts, or anything "
    "you are not confident is a lasting trait of THIS user. Most exchanges "
    "reveal nothing worth saving — when in doubt, save nothing.\n\n"
    "Output ONLY a JSON array of 0-3 short strings. Output [] when nothing is "
    "worth saving. No prose, no markdown, no code fences."
)


# Human-readable labels for `_qm_mode` ("cove" / "self_consistency", the
# internal identifiers) in the empty-answer fallback message below — that
# text is user-facing and must read naturally.
_QM_MODE_LABELS: Final[dict[str, str]] = {
    "cove": "Chain-of-Verification",
    "self_consistency": "Self-Consistency",
}

# Shown instead of an empty stored answer when a quality mode (CoVe /
# Self-Consistency) finishes without producing any answerable text — e.g. a
# reasoning-heavy model parks its whole answer in reasoning_content on every
# internal generation quality_modes.py fires. Persisting "" would silently
# drop the turn as an empty bubble with no way for the user to know why.
_QM_EMPTY_ANSWER_FALLBACK: Final[str] = (
    "The {mode} quality pass finished but did not produce any answerable "
    "text. Try rephrasing your question, or turn off quality mode for this "
    "chat."
)


async def _distill_memory_oob(
    *,
    lm_client: LmstudioStreamingClient,
    model: str,
    conversation_messages: list[dict],  # type: ignore[type-arg]
    assistant_answer: str,
    # Fire-and-forget, so a generous ceiling is genuinely free — which is why
    # it is 1800 s and not a number tuned to a measured worst case. Callers
    # pass ``self._aux_model_timeout_sec``; this fallback tracks it.
    timeout_sec: float = 1800.0,
) -> list[str]:
    """Out-of-band durable-fact extraction for the auto-memory feature.

    Mirrors :func:`_generate_followups_oob`: a separate non-streaming call,
    isolated from the turn — every failure returns ``[]``. Asks the chat's
    own model to return a JSON array of 0-3 short third-person durable facts
    about the user (or ``[]`` — the common case).

    Returns:
        Up to 3 durable-fact strings, or ``[]`` on any failure / when the
        model judged nothing worth saving.
    """
    from lmchat.services.lmstudio_adapter import LmstudioAdapter  # noqa: PLC0415

    try:
        adapter = lm_client._adapter  # type: ignore[attr-defined]
        if not isinstance(adapter, LmstudioAdapter):
            # Replay / cloud provider path — skip OOB distillation for now.
            return []

        http_client = adapter._http_client  # type: ignore[attr-defined]
        base_url = adapter._base_url  # type: ignore[attr-defined]
        url = f"{base_url}/v1/chat/completions"

        # Contract guard: a durable USER fact can only come from a user turn.
        # Without one the transcript is assistant-only and any extracted
        # "fact" would be misattributed assistant content — refuse rather
        # than lean solely on the prompt scoping.
        if not any(
            str(m.get("role")) == "user" and str(m.get("content", "")).strip()
            for m in conversation_messages
        ):
            return []

        # The label must be unambiguous: a generic "Reply:" reads as either
        # party's turn, and the model mined the assistant's own suggestions
        # as user facts (e.g. "given your interest, you might enjoy academic
        # research" -> stored "Conducts academic research"). "Assistant:"
        # makes the system prompt's exclusion rule enforceable.
        def _role_label(role: str) -> str:
            return "User" if role == "user" else "Assistant"

        convo_lines = [
            f"{_role_label(str(m.get('role', 'user')))}: {str(m.get('content', '')).strip()}"
            for m in conversation_messages[-6:]
            if str(m.get("content", "")).strip()
        ]
        convo_lines.append(f"Assistant: {assistant_answer.strip()}")
        convo_text = "\n".join(convo_lines)

        _user_instruction = (
            f"Here is the latest exchange:\n\n{convo_text}\n\n"
            "Extract any NEW durable facts the USER stated about themselves "
            "that are worth remembering long-term. Output ONLY a JSON array of "
            "0-3 short strings (use [] if nothing qualifies)."
        )
        messages: list[dict] = [  # type: ignore[type-arg]
            {"role": "system", "content": _DISTILL_SYSTEM},
            {"role": "user", "content": _user_instruction},
        ]

        body = {
            "model": model,
            "messages": messages,
            "stream": False,
            "max_tokens": 2048,  # reasoning models ignore thinking-disable hints
            "temperature": 0.1,  # stable, conservative extraction — not creative
        }
        body["thinking"] = {"type": "disabled"}

        # Same SSRF-exempt outbound path + bg_aux serialization as the
        # followups OOB call.
        async with bg_aux_slot():
            resp = await http_client.post(url, json=body, timeout=timeout_sec)
        resp.raise_for_status()
        message = resp.json().get("choices", [{}])[0].get("message", {})
        return _oob_json_array_with_reasoning_salvage(message)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "stream.distill_oob_failed", error=str(exc), error_type=type(exc).__name__
        )
        return []


async def _has_store_integration(
    *,
    request: Request,
    integrations: list[str] | None,
) -> bool:
    """True if any requested ``mcp/<slug>`` integration is Store-sourced.

    Mirrors the merge rule in ``routes/integrations.py``'s composer listing:
    Store-sourced means *slug* is installed, enabled, and consented in
    ``request.app.state.mcp_server_store``. A curated (LM Studio
    ``mcp.json``) integration of the same name is a different runtime and
    doesn't count — this only answers whether the turn needs the
    client-side MCP-Store agentic host instead of LM Studio's own
    server-side dispatch.

    Degrades to ``False`` (never raises) when there are no ``mcp/``
    integrations, the store isn't wired up, or the store read fails.
    """
    if not integrations:
        return False

    from lmchat.mcp.agentic import _integrations_to_server_ids  # noqa: PLC0415

    requested_slugs = set(_integrations_to_server_ids(integrations))
    if not requested_slugs:
        return False

    store = getattr(request.app.state, "mcp_server_store", None)
    if store is None:
        return False

    try:
        store_servers = await store.list_all()
    except Exception:  # noqa: BLE001
        log.warning("stream.store_integration_lookup_failed")
        return False

    return any(
        s.slug in requested_slugs and s.enabled and getattr(s, "consented", True)
        for s in store_servers
    )


# Service


class StreamingService:
    """Streaming chat service — the load-bearing moat of lm-chat v1.

    Implements per-chat lock semantics, the three-stage persist state
    machine, disconnect handling, idle-timeout detection, and memory
    ingestion gating. Constructed once at lifespan start and attached to
    ``app.state.streaming_service``.
    """

    def __init__(
        self,
        *,
        engine: AsyncEngine,
        lm_client: LmstudioStreamingClient,
        memory_service: MemoryService,
        chat_locks: dict[int, asyncio.Lock],
        idle_timeout_sec: int = 1800,
        aux_model_timeout_sec: float = 1800.0,
        embedding_client: EmbeddingClient | None = None,
        models_service: ModelsService | None = None,
        projects_service: object | None = None,
        provider_registry: object | None = None,
        quality_mode_service: QualityModeService | None = None,
    ) -> None:
        """Initialise the streaming service.

        Args:
            projects_service: Optional; when None the project_prompt
                injection path is skipped. Typed ``object`` to avoid a
                circular import with the routes layer.
            provider_registry: Optional; when None all chats use the default
                chain/lmstudio path, else ``chat.settings.provider`` resolves
                to a ``ChatProvider`` whose ``context_mode`` ("chain" |
                "replay") governs which streaming path runs.
            quality_mode_service: Optional; when set and the chat enables
                self-consistency or chain-of-verification on an LM Studio
                chat, the turn is answered via the chosen quality mode
                instead of a single generation.
        """
        self._engine = engine
        self._lm_client = lm_client
        self._memory_service = memory_service
        self._chat_locks = chat_locks
        self._idle_timeout_sec = idle_timeout_sec
        self._aux_model_timeout_sec = aux_model_timeout_sec
        self._embedding_client = embedding_client
        self._models_service = models_service
        self._projects_service = projects_service
        self._provider_registry = provider_registry
        self._quality_mode_service = quality_mode_service
        self._tool_round_counts = LruCappedCounter(self._TOOL_ROUND_LRU_CAP)

    # LRU cap for `_tool_round_counts`; evicts the least-recently-INCREMENTED
    # chat by design — `.get(...)` in `stream_chat` does not bump recency.
    _TOOL_ROUND_LRU_CAP: Final[int] = 1024

    def reset_counter(self, chat_id: int) -> None:
        """Reset the tool round counter for a chat. Idempotent."""
        self._tool_round_counts.reset(chat_id)

    def _increment_tool_round(self, chat_id: int) -> None:
        """Increment the tool-round counter for *chat_id* and apply LRU eviction."""
        self._tool_round_counts.increment(chat_id)

    # Internal helpers

    async def _assert_no_in_progress_stream(self, chat_id: int) -> None:
        """Raise StreamInProgressError if a draft/pending row exists for *chat_id*.

        Must be called INSIDE the per-chat lock to make the check + draft
        insertion atomic.
        """
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(messages.c.id).where(
                    messages.c.chat_id == chat_id,
                    messages.c.state.in_(
                        [PersistState.DRAFT.value, PersistState.PENDING_FINALIZATION.value]
                    ),
                )
            )
            row = result.fetchone()
        if row is not None:
            raise StreamInProgressError(chat_id)

    async def _create_draft(
        self, *, chat_id: int, user_id: int, model_id: str, user_text: str
    ) -> int:
        """Insert a user message row and a draft assistant row atomically.

        Both rows go in a single transaction: ``role='user'``/``state='final'``
        followed by ``role='assistant'``/``state='draft'`` — guarantees the
        conversation never has an assistant reply without its paired user
        turn, even if the process crashes after commit but before the stream
        completes.

        Stream-lifecycle gauge/marker ownership lives in the caller
        (``stream_chat``'s single ``finally``); this method only inserts
        the rows.

        Returns:
            The inserted assistant draft row's ``id``.
        """
        msg_id_holder: list[int] = []

        async def _insert() -> None:
            async with self._engine.begin() as conn:
                await conn.execute(
                    insert(messages).values(
                        chat_id=chat_id,
                        role="user",
                        content=user_text,
                        state=PersistState.FINAL.value,
                    )
                )
                result = await conn.execute(
                    insert(messages).values(
                        chat_id=chat_id,
                        role="assistant",
                        content="",
                        state=PersistState.DRAFT.value,
                        model_id=model_id,
                    )
                )
                pk = result.inserted_primary_key
                if pk is None:
                    raise RuntimeError("INSERT into messages (draft) returned no PK")
                msg_id_holder.append(int(pk[0]))

        await with_write_retry(_insert)
        msg_id = msg_id_holder[0]
        log.info(
            "stream.draft_created",
            chat_id=chat_id,
            user_id=user_id,
            msg_id=msg_id,
            model_id=model_id,
        )
        return msg_id

    async def _write_audit(
        self,
        *,
        event: AuditEvent,
        user_id: int,
        detail: dict,  # type: ignore[type-arg]
    ) -> None:
        """Write an audit event, swallowing failures so the stream is not killed."""
        try:
            await write_audit_event(
                user_id=user_id,
                event=event,
                ip=None,
                user_agent=None,
                detail=detail,
                engine=self._engine,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "stream.audit_write_failed",
                audit_event=event,
                user_id=user_id,
                error=str(exc),
            )

    async def _safe_consume_tokens(
        self,
        *,
        user_id: int,
        content: str,
    ) -> None:
        """Fire-and-forget wrapper around ``quota_service.consume_tokens``.

        Uses the UTF-8 byte-based :func:`approx_token_count` heuristic rather
        than LM Studio's exact stats — it works uniformly across surfaces
        that don't report stats and error/abort paths with no chat.end.
        Delegates to the shared :func:`quota_service.safe_consume_tokens`
        helper, which catches every exception: a quota write failure must
        not kill the stream — the response already completed.
        """
        from lmchat.services.quota_service import safe_consume_tokens

        await safe_consume_tokens(
            self._engine,
            user_id,
            content,
            log_prefix="stream",
        )

    async def _safe_index_message(
        self,
        *,
        msg_id: int,
        chat_id: int,
    ) -> None:
        """Fire-and-forget wrapper around ``memory_service.index_message``.

        Catches every exception and logs at ERROR — the chat already
        streamed successfully, so an indexing failure must not propagate to
        the client. Also records the failure on the memory service so
        repeated failures are visible via ``embedding_status()``.
        """
        try:
            await self._memory_service.index_message(msg_id)
        except Exception as exc:  # noqa: BLE001
            log.error(
                "stream.memory_index_failed",
                msg_id=msg_id,
                chat_id=chat_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            self._memory_service.record_index_write_failure(error=str(exc))

    async def _safe_distill_memory(
        self,
        *,
        user_id: int,
        chat_id: int,
        model_id: str,
        user_text: str,
        assistant_answer: str,
        project_id: int | None,
    ) -> int:
        """Fire-and-forget auto-memory distillation for a completed turn.

        Runs only for MAIN, NON-incognito chats. Skips when the feature flag
        is off, the chat is incognito, or the answer is empty. Calls the OOB
        extractor, then persists 0-3 durable user facts as AUTO insights via
        ``memory_service.distill_and_store``. Catches every exception — a
        distillation failure must never propagate to the client.

        Sub-sessions never reach ``stream_chat``, so this method is
        main-chat-only by construction.

        Returns:
            Count of newly-stored AUTO insights; always 0 on any skip/fail
            path. Never raises.
        """
        try:
            _distill_enabled = await _resolve_memory_distillation_enabled(self._engine)
            if not _distill_enabled:
                return 0
            if not assistant_answer.strip():
                return 0
            # A durable USER fact can only come from something the USER
            # said; an empty user turn (regenerate/retry/attachment-only)
            # would risk misattributing the assistant's own reply.
            if not user_text.strip():
                log.info(
                    "stream.distill.skipped_no_user_text",
                    chat_id=chat_id,
                    user_id=user_id,
                )
                return 0
            # Defence-in-depth: re-check incognito even if the streaming-path
            # guard was somehow bypassed.
            if await self._memory_service._chat_is_incognito(chat_id):
                log.info(
                    "stream.distill.skipped_incognito",
                    chat_id=chat_id,
                    user_id=user_id,
                )
                return 0

            conv: list[dict] = [  # type: ignore[type-arg]
                {"role": "user", "content": user_text.strip()}
            ]

            background_model_key = await resolve_background_model_id(
                engine=self._engine,
                models_service=self._models_service,
                chat_model_id=model_id,
            )
            # resolve_background_model_id returns a catalog key, not a
            # wire-id; LM Studio 400s on the bare catalog key. Resolve to the
            # live wire-id, or skip distillation if nothing is loaded.
            if self._models_service is not None:
                _bg_res = await self._models_service.resolve_to_loaded_or_fallback(
                    background_model_key
                )
                if _bg_res.wire_id is None:
                    log.info(
                        "stream.distill.skipped_no_loaded_model",
                        chat_id=chat_id,
                        user_id=user_id,
                        background_model_key=background_model_key,
                    )
                    return 0
                background_model_wire_id = _bg_res.wire_id
            else:
                # No models_service (legacy/test paths) — pass the key
                # as-is; _distill_memory_oob's adapter guard short-circuits
                # before reaching LM Studio.
                background_model_wire_id = background_model_key
            facts = await _distill_memory_oob(
                lm_client=self._lm_client,
                model=background_model_wire_id,
                conversation_messages=conv,
                assistant_answer=assistant_answer,
                timeout_sec=self._aux_model_timeout_sec,
            )
            if not facts:
                return 0
            stored = await self._memory_service.distill_and_store(
                user_id=user_id,
                facts=facts,
                project_id=project_id,
            )
            log.info(
                "stream.distill.done",
                chat_id=chat_id,
                user_id=user_id,
                candidate_count=len(facts),
                stored_count=len(stored),
            )
            return len(stored)
        except Exception as exc:  # noqa: BLE001
            log.error(
                "stream.distill_failed",
                chat_id=chat_id,
                user_id=user_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return 0

    async def _safe_refresh_project_summary(
        self,
        *,
        user_id: int,
        project_id: int,
        model_id: str,
    ) -> None:
        """Fire-and-forget throttled rolling project-summary refresh.

        Runs after a completed PROJECT-chat turn only. Honors the same
        enable flag as auto-memory distillation, then only regenerates when
        :func:`project_summary_service.should_refresh` says the project
        needs it — so a fast back-and-forth doesn't call the OOB summarizer
        every turn. Catches every exception; a failure must never propagate
        to the client (same defence-in-depth pattern as
        ``_safe_distill_memory``).
        """
        try:
            if self._projects_service is None:
                return
            if not await _resolve_memory_distillation_enabled(self._engine):
                return
            project = await self._projects_service.get(  # type: ignore[attr-defined]
                user_id=user_id, project_id=project_id
            )
            if project is None:
                return
            current_count = await count_project_messages(
                self._engine, user_id=user_id, project_id=project_id
            )
            if not _should_refresh_project_summary(project, current_count):
                return
            await refresh_project_summary(
                engine=self._engine,
                projects_service=self._projects_service,  # type: ignore[arg-type]
                lm_client=self._lm_client,
                models_service=self._models_service,
                user_id=user_id,
                project_id=project_id,
                hint_model_id=model_id,
            )
        except Exception as exc:  # noqa: BLE001
            log.error(
                "stream.project_summary_refresh_failed",
                project_id=project_id,
                user_id=user_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )

    def _fire_post_finalize_background(
        self,
        *,
        msg_id: int,
        chat_id: int,
        user_id: int,
        model_id: str,
        content: str,
        user_text: str,
        project_id: int | None,
        with_distill_and_summary: bool,
    ) -> asyncio.Task[int] | None:
        """Launch the fire-and-forget post-finalize background tasks.

        Always launches memory indexing + quota token consumption. When
        ``with_distill_and_summary`` is True (natural chat.end only, not the
        tool_loop_cap terminal) also launches auto-memory distillation and,
        for project chats, the rolling project-summary refresh. Emits
        nothing to the client — the OOB followups frame yields into the
        stream and deliberately stays in the caller.

        Returns:
            The distillation task handle (still running, detached — the
            caller may optionally bounded-wait on it for an inline
            ``memory.saved`` frame, without owning its lifecycle). ``None``
            when distillation was skipped (aux backlog bound, or
            ``with_distill_and_summary=False``).
        """
        # spawn_background_task holds a strong ref so these can't be GC'd
        # mid-flight (bare create_task() is only weakly referenced by the
        # loop) — neither result is observed by the caller.
        spawn_background_task(
            self._safe_index_message(msg_id=msg_id, chat_id=chat_id),
            name=f"memory_index_{msg_id}",
        )
        spawn_background_task(
            self._safe_consume_tokens(user_id=user_id, content=content),
            name=f"quota_tokens_{msg_id}",
        )
        if with_distill_and_summary and bg_aux_overloaded():
            # Bound the fire-and-forget backlog: distillation serializes
            # behind the single bg_aux slot, so a deep backlog would queue
            # unboundedly — skip this turn's distillation instead.
            log.warning(
                "stream.distill_skipped_aux_backlog",
                msg_id=msg_id,
                chat_id=chat_id,
                pending=bg_aux_pending(),
            )
            return None
        elif with_distill_and_summary:
            # spawn_background_task holds a strong ref so this can't be GC'd if
            # it outlives the caller's bounded shield/wait_for below — it still
            # returns a Task, so the optional inline memory.saved frame wait is
            # unchanged.
            distill_task = spawn_background_task(
                self._safe_distill_memory(
                    user_id=user_id,
                    chat_id=chat_id,
                    model_id=model_id,
                    user_text=user_text,
                    assistant_answer=content,
                    project_id=project_id,
                ),
                name=f"memory_distill_{msg_id}",
            )
            if project_id is not None:
                # spawn_background_task holds a strong ref — its result is
                # not observed by the caller (see comment above).
                spawn_background_task(
                    self._safe_refresh_project_summary(
                        user_id=user_id,
                        project_id=project_id,
                        model_id=model_id,
                    ),
                    name=f"project_summary_{msg_id}",
                )
            return distill_task
        return None

    async def _finalize_message(
        self,
        *,
        msg_id: int,
        response_id: str | None,
        final_content: str,
        final_reasoning: str,  # reasoning persistence
        stop_reason: str | None = None,  # drives the FE Continue chip
        tool_calls: list[dict[str, object]] | None = None,  # accumulated tool-call list
        table: Table = messages,
    ) -> bool:
        """Flush final content + reasoning + response_id.

        Transitions draft -> pending_finalization -> final. Returns False if
        the draft -> pending transition lost the race (the reaper recovers)
        or the second transition fails; True once ``final`` is reached.

        State-only: does not touch ``STREAMS_ACTIVE`` or the
        ``mark_active``/``mark_inactive`` registry — that lifecycle is owned
        by ``stream_chat``'s single try/finally.

        ``stop_reason`` ("stop" | "length" | None) drives the FE Continue
        chip on reload; ``tool_calls`` (FE ToolCall shape, or None) is what
        ToolCallCards re-render from. ``table`` (default ``messages``) is a
        thin passthrough to :func:`_finalize_message_impl` — the durable
        sub-session path (``routes/chats.py``) calls the free function
        directly with ``table=sub_session_messages`` instead, since it does
        not have a ``StreamingService`` instance in hand.
        """
        return await _finalize_message_impl(
            self._engine,
            msg_id=msg_id,
            response_id=response_id,
            final_content=final_content,
            final_reasoning=final_reasoning,
            stop_reason=stop_reason,
            tool_calls=tool_calls,
            table=table,
        )

    async def _release_stuck_draft(
        self,
        msg_id: int,
        chat_id: int,
        reason: str,
        *,
        table: Table = messages,
        salvage_content: str | None = None,
        salvage_reasoning: str | None = None,
        salvage_tool_calls: list[dict[str, object]] | None = None,
        had_tool_calls: bool = False,
        tool_rounds: int = 0,
    ) -> None:
        """Force-transition a draft assistant row to FINAL on a terminal error.

        Without this, an upstream error/stall/exhaustion leaves the draft row
        stuck in ``state='draft'`` — the next stream attempt then 409s via
        :func:`_assert_no_in_progress_stream`, and the reaper's cleanup
        window is far too long for a chat session.

        Partial-answer salvage: the single teardown every NON-graceful
        terminal converges on via ``stream_chat``'s finally (graceful
        terminals already persist via ``_finalize_message``). When
        ``salvage_content`` / ``salvage_reasoning`` are supplied, they run
        through the same ``resolve_terminal_content`` policy so the partial
        answer (with parked reasoning folded to visible content) survives
        instead of reloading as an empty bubble.

        Defensive: only updates rows currently in DRAFT for *msg_id*, so a
        concurrent ``_finalize_message`` win is safe (rowcount=0). Content is
        only overwritten when the salvage actually recovered something.

        ``table`` (default ``messages``) is a thin passthrough to
        :func:`_release_stuck_draft_impl`; ``chat_id`` carries the
        sub_session_id when a caller passes ``table=sub_session_messages``
        (log-only, never a query predicate — see the free function's
        docstring).
        """
        await _release_stuck_draft_impl(
            self._engine,
            msg_id,
            chat_id,
            reason,
            table=table,
            salvage_content=salvage_content,
            salvage_reasoning=salvage_reasoning,
            salvage_tool_calls=salvage_tool_calls,
            had_tool_calls=had_tool_calls,
            tool_rounds=tool_rounds,
        )

    async def _load_replay_history(
        self,
        *,
        chat_id: int,
        msg_id: int,
        wire_payload: CanonicalChatRequest,
        context_mode: str,
    ) -> tuple[CanonicalChatRequest, list[CanonicalMessage] | None]:
        """Load prior-turn history for replay mode / chain tool-turn resume.

        Replay mode loads the FULL message history since the provider has no
        server-side chain; chain mode passes ``history=None`` (LM Studio
        manages context via ``previous_response_id``). Also loads history in
        chain mode when ``previous_response_id`` is None (first turn, or a
        tool turn whose chain was just cleared), composing it into
        ``wire_payload.system_prompt`` since ``encode_native`` only sends
        ``system_prompt`` when ``previous_response_id`` is None. Mirrors the
        sub-session approach in ``chats.py _sub_session_sse``.

        Returns the (possibly system_prompt-composed) ``wire_payload`` and
        the loaded history (``None`` when neither gate fires).
        """
        _replay_history: list | None = None
        if context_mode == "replay" or (
            context_mode == "chain" and wire_payload.previous_response_id is None
        ):
            from lmchat.lmstudio.types import (
                CanonicalMessage as _CanonicalMessage,  # noqa: PLC0415
            )

            _replay_msgs: list[_CanonicalMessage] = []
            try:
                # Large limit (not the default 200) so the provider gets the
                # complete conversation; per-model token budgeting happens
                # downstream in sanitize_request_for_provider/encode_compat.
                async with self._engine.connect() as _hist_conn:
                    _hist_rows = (
                        await _hist_conn.execute(
                            select(
                                messages.c.id,
                                messages.c.role,
                                messages.c.content,
                                messages.c.reasoning_content,
                                messages.c.tool_calls,
                                messages.c.tool_call_id,
                            )
                            .where(
                                messages.c.chat_id == chat_id,
                                # FINAL, or a disconnect-aborted turn with
                                # real content — see _remembered_turn_state:
                                # the model's own memory of the conversation
                                # should match what the user already sees in
                                # the transcript (message_service.py filters
                                # only != draft there).
                                _remembered_turn_state(),
                                # Exclude the current user message (msg_id is
                                # the draft just created; user row is msg_id-1).
                                messages.c.id < msg_id,
                                # Archived rows are NEVER sent to the model —
                                # also load-bearing for the chain-reset
                                # backstop path, which must re-send summary +
                                # active only.
                                messages.c.compaction_id.is_(None),
                            )
                            .order_by(messages.c.id.asc())
                            .limit(10_000)
                        )
                    ).fetchall()
                    # Every compaction span, oldest-first by anchor_msg_id, so
                    # each summary merges into history at its chronological
                    # position.
                    _compaction_rows = (
                        await _hist_conn.execute(
                            select(
                                compactions.c.summary,
                                compactions.c.anchor_msg_id,
                            )
                            .where(compactions.c.chat_id == chat_id)
                            .order_by(compactions.c.anchor_msg_id.asc())
                        )
                    ).fetchall()
                # Merge cursor into _compaction_rows (already anchor_msg_id-ordered).
                _comp_idx = 0

                def _summary_message(_summary: str) -> _CanonicalMessage:
                    return _CanonicalMessage(
                        role="system",
                        content=(
                            "[Compacted summary of earlier conversation]\n"
                            f"{_summary}"
                        ),
                    )

                for _hr in _hist_rows:
                    _row_id = _hr[0]
                    _role = _hr[1]
                    # Flush every compaction whose anchor_msg_id is
                    # at-or-before this row before appending it, so summaries
                    # land in chronological order relative to surviving messages.
                    while (
                        _comp_idx < len(_compaction_rows)
                        and _compaction_rows[_comp_idx][1] <= _row_id
                    ):
                        _replay_msgs.append(
                            _summary_message(_compaction_rows[_comp_idx][0])
                        )
                        _comp_idx += 1
                    # "system" excluded: assemble_compat_messages prepends
                    # req.system_prompt as the authoritative system block.
                    if _role not in ("user", "assistant", "tool"):
                        continue
                    _tc_list = None
                    if _hr[4]:
                        from lmchat.lmstudio.types import (
                            CanonicalToolCall as _CTC,  # noqa: PLC0415
                        )

                        try:
                            _raw_tcs = _hr[4] if isinstance(_hr[4], list) else []
                            _tc_list = [
                                _CTC(
                                    id=str(tc.get("id", "")),
                                    name=str(tc.get("name", "")),
                                    arguments=tc.get("arguments") or {},
                                    result=tc.get("result"),
                                )
                                for tc in _raw_tcs
                                if isinstance(tc, dict)
                            ] or None
                        except Exception:  # noqa: BLE001
                            _tc_list = None
                    _replay_msgs.append(
                        _CanonicalMessage(
                            role=_role,
                            content=_hr[2] or None,
                            reasoning_content=_hr[3] or None,
                            tool_calls=_tc_list,
                            tool_call_id=_hr[5] or None,
                        )
                    )
                # Any compaction spans not flushed above (e.g. fully archived
                # chat with no active row after them) go at the very end.
                while _comp_idx < len(_compaction_rows):
                    _replay_msgs.append(
                        _summary_message(_compaction_rows[_comp_idx][0])
                    )
                    _comp_idx += 1
                # The current user turn is carried separately in req.input and
                # appended by encode_compat as the final message; drop the
                # just-loaded copy (the most recent FINAL row) so it isn't
                # sent twice — history must be PRIOR turns only.
                if _replay_msgs and _replay_msgs[-1].role == "user":
                    _replay_msgs = _replay_msgs[:-1]
                _replay_history = _replay_msgs
                log.info(
                    "stream.replay_history_loaded",
                    chat_id=chat_id,
                    msg_id=msg_id,
                    history_len=len(_replay_msgs),
                )
            except Exception as _hist_exc:  # noqa: BLE001
                # Must not crash the stream — fall back to no prior context.
                log.warning(
                    "stream.replay_history_load_failed",
                    chat_id=chat_id,
                    msg_id=msg_id,
                    error=str(_hist_exc),
                )
                _replay_history = []

            # Chain mode with no previous_response_id: compose history into
            # system_prompt so encode_native carries full prior context.
            # Replay mode uses _replay_history via the compat encoder instead.
            if context_mode == "chain" and _replay_history:
                from lmchat.services.prompt_assembly import (  # noqa: PLC0415
                    serialize_prior_turns,
                )

                _existing_wp_sys = wire_payload.system_prompt or ""
                _composed_sys = _existing_wp_sys + serialize_prior_turns(
                    [(_hm.role or "user", _hm.content or "") for _hm in _replay_history]
                )
                wire_payload = wire_payload.model_copy(
                    update={"system_prompt": _composed_sys}
                )
                log.info(
                    "stream.chain_tool_history_composed",
                    chat_id=chat_id,
                    msg_id=msg_id,
                    history_turns=len(_replay_history),
                )

        return wire_payload, _replay_history

    async def _resolve_provider_and_context_mode(
        self,
        *,
        chat_id: int,
        msg_id: int,
        provider_name: str,
    ) -> _ProviderResolution:
        """Resolve context_mode + dispatch_provider for *provider_name*.

        The unknown-provider case can't be yielded here — a plain method
        can't yield into the caller's SSE stream — so it's surfaced via the
        returned ``error_code``/``error_detail``; ``stream_chat`` owns the
        actual error-frame yield, the ``STREAMS_FAILED`` increment, and the
        early return.
        """
        _context_mode: str = "chain"  # safe default — lmstudio path
        _dispatch_provider = None  # resolved cloud provider for replay dispatch
        _builtin_web_search = False  # set ONLY by the openai_compat branch below
        if provider_name == "lmstudio":
            # native (default) leaves this branch a no-op. openai_compat
            # re-presents the same live adapter as an OpenAICompatProvider
            # and routes through the replay + agentic-MCP dispatch below —
            # the MCP tool system follows automatically: native uses LM
            # Studio's own server-side mcp.json host, openai_compat uses LM
            # Chat's client-side MCP Store (AgenticMcpProvider).
            _lmstudio_endpoint_mode = await resolve_lm_studio_endpoint_mode(
                engine=self._engine
            )
            if (
                _lmstudio_endpoint_mode == "openai_compat"
                and self._provider_registry is not None
            ):
                _lmstudio_native = self._provider_registry.get(  # type: ignore[attr-defined]
                    "lmstudio"
                )
                if _lmstudio_native is not None:
                    _context_mode = "replay"
                    _dispatch_provider = (
                        _lmstudio_native.as_openai_compat_provider()  # type: ignore[attr-defined]
                    )
                    # openai_compat is the ONLY branch that enables the
                    # app-executed web_search tool — native endpoint mode and
                    # the store-integration replay reroute below stay False.
                    _builtin_web_search = True
                    log.info(
                        "stream.lmstudio_openai_compat_dispatch",
                        chat_id=chat_id,
                        msg_id=msg_id,
                    )
        elif self._provider_registry is not None:
            _resolved_provider = self._provider_registry.get(  # type: ignore[attr-defined]
                provider_name
            )
            if _resolved_provider is None:
                log.warning(
                    "stream.unknown_provider_fallback_to_chain",
                    chat_id=chat_id,
                    msg_id=msg_id,
                    provider_name=provider_name,
                )
                return _ProviderResolution(
                    context_mode=_context_mode,
                    dispatch_provider=_dispatch_provider,
                    error_code="unknown_provider",
                    error_detail=(
                        f"Provider '{provider_name}' is not configured. "
                        f"Add it in Settings → Providers before sending a message."
                    ),
                )
            else:
                _context_mode = getattr(_resolved_provider, "context_mode", "chain")
                if _context_mode == "replay":
                    _dispatch_provider = _resolved_provider
                log.info(
                    "stream.provider_resolved",
                    chat_id=chat_id,
                    msg_id=msg_id,
                    provider_name=provider_name,
                    context_mode=_context_mode,
                )
        return _ProviderResolution(
            context_mode=_context_mode,
            dispatch_provider=_dispatch_provider,
            builtin_web_search=_builtin_web_search,
        )

    async def _assemble_system_prompt(
        self,
        *,
        chat_id: int,
        msg_id: int,
        user_id: int,
        model_id: str,
        payload: ChatStreamRequest,
        _context_mode: str,
        _existing_sys: str,
        _chat_settings: dict[str, Any],
        builtin_web_search: bool = False,
    ) -> CanonicalChatRequest:
        """Assemble the wire-bound system prompt: context block, RAG, per-turn
        layer relocation, reasoning-effort, and sampler-profile mutations.

        Runs after ``_resolve_provider_and_context_mode`` and before the
        model-capability-resolution + integrations gate, which stays INLINE
        in ``stream_chat`` (that section's yields are interleaved with
        ``wire_payload`` mutations across several branches with inconsistent
        metric-increment ordering — extracting it risked silently
        normalising that per-site ordering). This method's body has no
        yields, so it's a plain async method, mirroring
        ``_load_replay_history`` / ``_resolve_provider_and_context_mode``.

        Covers, in order: [Context] date/persona injection; capability
        legend; guide-context injection; composing ``_followups_payload``
        (directive moved out-of-band); RAG augmentation + window-aware trim;
        per-turn layer relocation + orphaned-injected-message detection
        (chain mode only); reasoning-effort wiring; sampler-profile wiring.

        ``builtin_web_search`` folds the app-executed web_search tool into
        the capability legend's DO section when the openai_compat branch of
        ``_resolve_provider_and_context_mode`` set it.

        Returns:
            The fully-assembled ``CanonicalChatRequest`` that ``stream_chat``
            binds to ``reasoning_payload`` before continuing into the
            (still-inline) capability-resolution + integrations gate.
        """
        # Every preset suffered a "tell me today's date" gap with nothing
        # prepending a runtime context block; prepend FIRST so it's the
        # model's earliest signal. Baked into turn 1 for chains that reuse
        # previous_response_id — the date doesn't change mid-chat.
        #
        # The "running locally via LM Studio" attribution is gated behind
        # chain mode — a replay-mode cloud provider sees the system prompt
        # every turn, so re-sending an LM-Studio claim would mislead it.
        # Declared here (not inside the `if` below) so per_turn_date further
        # down — gated by the same condition in a separate `if` block pyright
        # can't correlate — has a definite binding.
        _ctx_dt: str | None = None

        # Computed once, appended inside both branches below so it inherits
        # whatever upstream raw/none-preset suppression already shaped
        # `_existing_sys`.
        _enabled_tools = list(payload.payload.integrations or [])
        if builtin_web_search:
            _enabled_tools.append("web_search")
        _capability_legend = render_capability_legend(enabled_tools=_enabled_tools)

        # Guide-context injection: mode-independent (native chain never gets
        # a tool-call loop, so this is native users' only guide-lookup path).
        # Scored against the same latest-user-message text RAG queries with,
        # extracted early since RAG's own extraction only runs when enabled.
        #
        # `is_app_directed_question` is the primary gate — a declarative
        # message never reaches either retrieval engine, which keeps the app
        # from embedding the user's message every turn. When it passes and an
        # embedding client + models service are wired, the SEMANTIC engine
        # (the same LM Studio embed model document/memory RAG uses) serves
        # the turn IF its corpus embedding matrix is already cached
        # (non-blocking). The one-time corpus embed never runs inline here —
        # a cold cache kicks it off as a detached background task
        # (fire-and-forget, its own timeout) and THIS turn falls straight to
        # keyword; a later turn with a warm cache is what actually gets the
        # semantic engine. Any failure along the semantic path (no embed
        # model, corpus not cached, timeout, network error, nothing clears
        # the floor) falls through to the deterministic keyword engine,
        # exactly as if no embedder were configured at all.
        _latest_user_text = " ".join(
            block.content or ""
            for block in payload.payload.input
            if block.type == "text" and block.content
        )
        _guide_context_block: str | None = None
        if is_app_directed_question(_latest_user_text):
            _guide_embedding_client = self._embedding_client
            if _guide_embedding_client is not None and self._models_service is not None:
                _guide_embed_key: str | None = None
                try:
                    from lmchat.services.memory_service import (  # noqa: PLC0415
                        resolve_active_embedding_model_key,
                    )

                    _guide_embed_key = await resolve_active_embedding_model_key(
                        engine=self._engine,
                        models_service=self._models_service,
                        persist_default=False,
                    )
                except Exception:  # noqa: BLE001 -- guide embed-model resolution must never block/break a turn
                    log.debug(
                        "stream.guide_semantic_unavailable",
                        chat_id=chat_id,
                        msg_id=msg_id,
                        exc_info=True,
                    )
                    _guide_embed_key = None

                if _guide_embed_key is not None:
                    _guide_resolved_key: str = _guide_embed_key
                    _guide_cached_embeddings = _guide_get_cached_section_embeddings(
                        _guide_resolved_key
                    )
                    if _guide_cached_embeddings is not None:
                        # Fast path: corpus already embedded, only a single
                        # query embed remains.
                        async def _guide_embed_query(text: str) -> list[float]:
                            return await _guide_embedding_client.embed_one(
                                text=text, model_id=_guide_resolved_key
                            )

                        try:
                            async with asyncio.timeout(_GUIDE_SEMANTIC_TIMEOUT_SEC):
                                _guide_context_block = await _guide_context_block_semantic(
                                    _latest_user_text,
                                    embed_one=_guide_embed_query,
                                    section_texts_and_meta=_guide_cached_embeddings,
                                )
                        except Exception:  # noqa: BLE001 -- semantic guide retrieval must never block/break a turn
                            log.debug(
                                "stream.guide_semantic_query_failed",
                                chat_id=chat_id,
                                msg_id=msg_id,
                                exc_info=True,
                            )
                            _guide_context_block = None
                    else:
                        # Cold cache: kick off the corpus embed in the
                        # background and serve THIS turn from keyword.
                        _guide_ensure_section_embeddings_background(
                            embed_batch=_guide_embedding_client.embed_batch,
                            model_key=_guide_resolved_key,
                        )
            if _guide_context_block is None:
                _guide_context_block = guide_context_block(_latest_user_text)

        if _context_mode == "chain":
            try:
                _now = datetime.now(_LOCAL_TZ)
                # Human-readable: "Thursday, June 12, 2026 at 09:42 CST (UTC-06:00)"
                _ctx_dt = _now.strftime("%A, %B %-d, %Y at %H:%M %Z")
                _utc_offset_seconds = _now.utcoffset()
                if _utc_offset_seconds is not None:
                    _offset_hours = _utc_offset_seconds.total_seconds() / 3600
                    _ctx_dt = f"{_ctx_dt} (UTC{_offset_hours:+03.0f}:00)"
            except Exception:  # noqa: BLE001
                # Time-keeping fallback — never fail a stream because of a
                # localisation issue.
                _ctx_dt = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
            _CONTEXT_BLOCK = (
                f"[Context]\n"
                f"- Right now: {_ctx_dt}. Treat this as ground truth — answer "
                f"date/time questions from it directly and confidently. Don't "
                f"say you lack real-time access, can't know the date, or that "
                f"this is user-supplied; it is current and correct.\n"
                f"- You're running locally on the user's own machine via LM "
                f"Studio. They're the only person here — skip requests for "
                f"context (timezone, environment) this block already gives you.\n"
            )
            _existing_sys = (
                f"{_CONTEXT_BLOCK}\n{_existing_sys}" if _existing_sys else _CONTEXT_BLOCK
            )
            _existing_sys = f"{_existing_sys}\n\n{_capability_legend}"
            if _guide_context_block:
                _existing_sys = f"{_existing_sys}\n\n{_guide_context_block}"
        else:
            # Replay mode: system prompt is re-sent every turn, so omit the
            # LM-Studio attribution but still inject a temporal anchor so the
            # model has a reliable "now" instead of giving philosophical
            # non-answers to time-sensitive questions.
            try:
                _now = datetime.now(_LOCAL_TZ)
                _ctx_dt_replay = _now.strftime("%A, %B %-d, %Y at %H:%M %Z")
                _utc_offset_seconds = _now.utcoffset()
                if _utc_offset_seconds is not None:
                    _offset_hours = _utc_offset_seconds.total_seconds() / 3600
                    _ctx_dt_replay = f"{_ctx_dt_replay} (UTC{_offset_hours:+03.0f}:00)"
            except Exception:  # noqa: BLE001
                _ctx_dt_replay = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
            _TEMPORAL_ANCHOR = (
                f"[Current date and time: {_ctx_dt_replay}. "
                f"Treat this as authoritative for \"today\"/\"now\"/\"current\". "
                f"Your training data may predate it — for anything that could have "
                f"changed since your knowledge cutoff, rely on tools/search rather "
                f"than guessing, and do not dispute the date.]"
            )
            _existing_sys = (
                f"{_TEMPORAL_ANCHOR}\n\n{_existing_sys}"
                if _existing_sys
                else _TEMPORAL_ANCHOR
            )
            _existing_sys = f"{_existing_sys}\n\n{_capability_legend}"
            if _guide_context_block:
                _existing_sys = f"{_existing_sys}\n\n{_guide_context_block}"

        # Followups generate OUT-OF-BAND after the main answer completes (see
        # chat.end below) — the old inline directive caused reasoning models
        # to obsess over the fussy HTML-comment constraint (measured 30x
        # blowup on simple turns). _followups_payload carries the composed
        # system_prompt without any directive.
        _followups_payload = payload.payload.model_copy(update={"system_prompt": _existing_sys})

        # RAG augmentation (behind rag_enabled). Modifies the system_prompt
        # on the CanonicalChatRequest copy only — the original payload is
        # never mutated.
        rag_payload = _followups_payload
        # Retain the exact RAG-block string so relocate_per_turn_layers below
        # can move it into input[0] on follow-up turns without re-retrieving.
        _rag_block: str | None = None
        if self._embedding_client is not None and self._models_service is not None:
            from lmchat.services.rag_service import augment_prompt as _rag_augment

            try:
                current_message_text = " ".join(
                    block.content or ""
                    for block in payload.payload.input
                    if block.type == "text" and block.content
                )
                augmented = await _rag_augment(
                    chat_id=chat_id,
                    user_id=user_id,
                    current_message=current_message_text,
                    engine=self._engine,
                    embedding_client=self._embedding_client,
                    models_service=self._models_service,
                    memory_service=self._memory_service,
                )
                if augmented.context_block:
                    # Window-aware trim caps the retrieval block to a
                    # fraction of the model's context window so a small
                    # local seat can't be overflowed before the user
                    # message even arrives.
                    from lmchat.services.rag_service import (
                        trim_rag_context_for_model,
                    )

                    trimmed_block, original_chars, trim_fired = trim_rag_context_for_model(
                        augmented.context_block,
                        # Reuse the window `augment_prompt` already probed
                        # LIVE (via _resolve_chat_ctx_window /
                        # ModelsService.get_max_context_length — provider-
                        # agnostic, no model-name table) for
                        # rag_inject_budget, instead of re-deriving a
                        # separate guess and trimming a correctly-budgeted
                        # block back down. 0 (RAG disabled this turn /
                        # probe failed) falls back to the fixed
                        # "window unknown" floor inside
                        # trim_rag_context_for_model.
                        ctx_window=augmented.ctx_window,
                    )
                    if trim_fired:
                        log.warning(
                            "stream.rag_context_trimmed",
                            chat_id=chat_id,
                            msg_id=msg_id,
                            model=payload.payload.model,
                            original_chars=original_chars,
                            trimmed_chars=len(trimmed_block),
                        )
                    # Read from _followups_payload (not payload.payload) so
                    # the RAG context sits above the followups directive.
                    existing_sys = _followups_payload.system_prompt or ""
                    # Follow-up chain turns wrap the block in RAG sentinel
                    # markers so relocate_per_turn_layers below can slice it
                    # back out by marker boundary rather than matching raw
                    # text. Turn 1 and replay mode never route through the
                    # strip, so their prepend format is left untouched.
                    _rag_prepend_block = trimmed_block
                    if (
                        _context_mode == "chain"
                        and payload.payload.previous_response_id is not None
                    ):
                        from lmchat.services.prompt_assembly import (  # noqa: PLC0415
                            RAG_CLOSE_MARKER as _rag_close_marker,
                        )
                        from lmchat.services.prompt_assembly import (
                            RAG_OPEN_MARKER as _rag_open_marker,
                        )

                        _rag_prepend_block = (
                            f"{_rag_open_marker}\n{trimmed_block}\n{_rag_close_marker}"
                        )
                    new_sys = (
                        _rag_prepend_block
                        + ("\n\n" if existing_sys else "")
                        + existing_sys
                    )
                    rag_payload = _followups_payload.model_copy(
                        update={"system_prompt": new_sys}
                    )
                    _rag_block = trimmed_block
                    log.info(
                        "stream.rag_augmented",
                        chat_id=chat_id,
                        msg_id=msg_id,
                        memory_hits=augmented.memory_hits,
                        doc_hits=augmented.doc_hits,
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "stream.rag_augmentation_failed",
                    chat_id=chat_id,
                    msg_id=msg_id,
                    error=str(exc),
                )

        # Per-turn layer relocation. encode_native drops system_prompt
        # whenever previous_response_id is set, so per-turn layers (fresh RAG
        # retrieval, a corrective note when tools appeared mid-chat) must
        # travel in `input` on follow-up turns instead; chain-persistent
        # layers stay in system_prompt since LM Studio's chain state carries
        # them from turn 1. Runs before the budget gate so its _input_text
        # aggregation counts the relocated block automatically.
        #
        # Replay mode skips this entirely — the provider gets the full
        # system_prompt every turn and there's no server-side chain to
        # compensate for.
        if _context_mode == "chain":
            # Detect orphaned injected assistant messages (response_id IS
            # NULL, appended via inject_message) sitting after the last
            # LM-Studio-chained turn — invisible to the model on follow-ups
            # otherwise. Runs on every turn, not just chained follow-ups.
            _orphaned_injected: list[str] = []
            try:
                async with self._engine.connect() as _conn:
                    # Deliberately still plain `state == FINAL`, not
                    # _remembered_turn_state(): response_id is only ever
                    # written by _finalize_message_impl's step 1, which
                    # requires the row to still be `draft` at that moment —
                    # the same row can therefore never carry BOTH a
                    # response_id AND end up `aborted_by_client` (that
                    # transition also requires `draft`, so step 1 landing
                    # first forecloses it). Broadening this predicate would
                    # be a no-op; keeping it plain says so.
                    _last_chained = await _conn.execute(
                        select(messages.c.id)
                        .where(
                            messages.c.chat_id == chat_id,
                            messages.c.role == "assistant",
                            messages.c.response_id.isnot(None),
                            messages.c.state == PersistState.FINAL.value,
                        )
                        .order_by(messages.c.id.desc())
                        .limit(1)
                    )
                    _lc_row = _last_chained.fetchone()
                    # No chained turn yet -> floor of 0 surfaces EVERY
                    # injected message; else only those after the last
                    # chained turn.
                    _floor_id = _lc_row[0] if _lc_row is not None else 0
                    _orphaned_rows = await _conn.execute(
                        select(messages.c.content)
                        .where(
                            messages.c.chat_id == chat_id,
                            messages.c.role == "assistant",
                            messages.c.response_id.is_(None),
                            messages.c.id > _floor_id,
                            # FINAL (an injected message, or a gracefully-
                            # finalized turn with no response_id — e.g. a
                            # replay-mode provider), or a disconnect-aborted
                            # turn with real content. Chain mode has no
                            # OTHER mechanism to surface a salvaged-but-
                            # aborted turn on a follow-up turn (the replay
                            # history query above is skipped whenever
                            # previous_response_id is set) — this relocation
                            # IS the chain-mode equivalent of that fix. See
                            # _remembered_turn_state.
                            _remembered_turn_state(),
                            # An archived injected message must never be
                            # relocated back into input[0] — would leak into
                            # the model's context and defeat compaction.
                            messages.c.compaction_id.is_(None),
                        )
                        .order_by(messages.c.id.asc())
                    )
                    _orphaned_injected = [row[0] for row in _orphaned_rows.fetchall() if row[0]]
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "stream.orphaned_injected_query_failed",
                    chat_id=chat_id,
                    msg_id=msg_id,
                    error=str(exc),
                )

            from lmchat.services.prompt_assembly import (  # noqa: PLC0415
                relocate_per_turn_layers,
            )

            rag_payload = relocate_per_turn_layers(
                rag_payload,
                rag_block=_rag_block,
                tools_now_available=bool(
                    payload.payload.previous_response_id is not None
                    and payload.payload.integrations
                ),
                injected_messages=_orphaned_injected or None,
                # On follow-up turns the [Context] block's date never
                # reaches the wire (encode_native drops system_prompt and
                # relocate_per_turn_layers doesn't re-emit chain-persistent
                # layers) — route _ctx_dt into input[0] instead so a
                # long-lived chat doesn't report turn 1's date forever. None
                # on turn 1, where [Context] is already in system_prompt.
                per_turn_date=(
                    _ctx_dt
                    if payload.payload.previous_response_id is not None
                    else None
                ),
            )

        # Reasoning-effort wiring: the SPA persists a per-chat
        # reasoning_effort setting; populate CanonicalChatRequest.reasoning so
        # it reaches the native wire body. Gated by the model's
        # capabilities.reasoning.allowed_options — an unsupported/disallowed
        # level is suppressed (LM Studio would 400, and the rejected-param
        # cache then disables it permanently for the model).
        reasoning_payload = rag_payload
        _reasoning_effort: str | None = None
        _caps = None
        try:
            _reasoning_effort = _chat_settings.get("reasoning_effort")
            # Capabilities are fetched whenever models_service is available,
            # not only when reasoning_effort is set — the sampler-profile
            # wiring below needs the model's real reasoning capability on
            # every turn, including the default (thinking-mode) new-chat
            # case, or it silently picks the INSTRUCT profile instead.
            if self._models_service is not None:
                try:
                    _caps = await self._models_service.get_capabilities(model_id)
                except KeyError:
                    _caps = None
            if _reasoning_effort:
                _allowed: list[str] = []
                if _caps is not None and _caps.reasoning is not None:
                    _allowed = list(_caps.reasoning.allowed_options)
                if _reasoning_effort in _allowed:
                    reasoning_payload = rag_payload.model_copy(
                        update={"reasoning": _reasoning_effort}
                    )
                    log.info(
                        "stream.reasoning_effort_applied",
                        chat_id=chat_id,
                        msg_id=msg_id,
                        reasoning_effort=_reasoning_effort,
                    )
                else:
                    log.info(
                        "stream.reasoning_effort_suppressed",
                        chat_id=chat_id,
                        msg_id=msg_id,
                        reasoning_effort=_reasoning_effort,
                        allowed_options=_allowed,
                    )
        except Exception as exc:  # noqa: BLE001
            # Reasoning-wiring failure must not break the stream. Log + continue.
            log.warning(
                "stream.reasoning_effort_wiring_failed",
                chat_id=chat_id,
                msg_id=msg_id,
                error=str(exc),
            )

        # Sampler profile wiring: apply vendor-recommended settings for
        # profiled models.
        _sampler_disabled = os.getenv("LM_CHAT_DISABLE_SAMPLER_PROFILES") == "1"
        if not _sampler_disabled:
            _profile = profile_for_request(
                model_id=model_id,
                reasoning_effort=_reasoning_effort,
                supports_reasoning=_caps.reasoning is not None if _caps else False,
            )
            if _profile is not None:
                # Fill only the fields the caller left unset. Every sampler
                # field on the payload is ``X | None = None``, so a non-None
                # value is a CHOICE — from the per-chat numeric rail, the
                # active preset, or whatever the operator configured — and a
                # vendor default must not silently overwrite it. Before this,
                # model_copy(update=_profile) clobbered the lot: set
                # temperature to 0.2 deliberately and a profiled model
                # replaced it, with nothing but a server-side log to show for
                # it. The escape hatches were renaming the model to end in
                # "-scar" or killing profiles process-wide with
                # LM_CHAT_DISABLE_SAMPLER_PROFILES — neither of which is a
                # per-chat opt-out.
                _applied = {
                    _k: _v
                    for _k, _v in _profile.items()
                    if getattr(reasoning_payload, _k, None) is None
                }
                _respected = sorted(set(_profile) - set(_applied))
                if _applied:
                    reasoning_payload = reasoning_payload.model_copy(update=_applied)
                log.info(
                    "sampler_profile_applied",
                    chat_id=chat_id,
                    msg_id=msg_id,
                    model_id=model_id,
                    profile_keys=list(_applied.keys()),
                    respected_caller_keys=_respected,
                )

        return reasoning_payload

    async def _resolve_model_and_integrations_gate(
        self,
        *,
        model_id: str,
        chat_stored_model_id: str | None,
        wire_payload: CanonicalChatRequest,
        mcp_host: McpHost | None = None,
    ) -> _CapabilityGateDecision:
        """Resolve the wire model id and run the two-layer integrations gate.

        Pure decision computation for the chain-mode pre-flight gate: the
        reprobe/wire-id lookup (with auth-failed reprobe retry), the
        substituted/explicit-pick check, the Layer-1 non-tool-model
        capability check, and the Layer-2 context-budget trim decision. Never
        logs, yields, or increments metrics — ``stream_chat`` owns every side
        effect, reconstructed from the returned ``_CapabilityGateDecision``
        (see that class for the per-site mapping).

        ``mcp_host`` is used only to probe each requested integration's real
        advertised tool-schema size (for an already-connected server) for the
        Layer-2 budget gate; unconnected servers fall back to the flat
        ``MCP_INTEGRATION_SCHEMA_TOKENS`` guess.
        """
        # Caller only invokes this under its own models_service-is-not-None
        # guard; narrow the type so calls below skip repeated None-checks.
        assert self._models_service is not None

        # A 401 likely left the model cache empty — force one storm-guarded
        # reprobe before declaring "not loaded" (recovers a cold start where
        # the LM Studio key was fixed after boot, without the 60s backoff).
        _res = await self._models_service.resolve_to_loaded_or_fallback(model_id)
        if _res.wire_id is None and self._models_service.auth_failed:
            if await self._models_service.force_refresh():
                _res = await self._models_service.resolve_to_loaded_or_fallback(model_id)
        if _res.wire_id is None:
            return _CapabilityGateDecision(
                resolution="no_model_loaded",
                wire_model_id=None,
                trimmed_kept=[],
                trimmed_dropped=[],
            )

        _fallback_key: str | None = None
        if _res.substituted:
            # The requested model is in-catalog but has no loaded instance.
            # Implicit default (no explicit per-chat model_id): fall back
            # silently — the stale model came from the global default, not a
            # deliberate pick. Explicit pick (chat.model_id non-NULL): honour
            # the user's choice via an actionable error instead.
            if not chat_stored_model_id and _res.wire_id is not None:
                # DELIBERATELY do not persist the fallback catalog key as
                # chats.model_id: chat_stored_model_id is the ONLY signal
                # that decides implicit-vs-explicit on the NEXT turn. Since
                # the FE never invalidates its chats-list cache after a
                # stream, writing it here would make the next implicit-
                # default turn look like an explicit pick and hard-error on
                # the very model this fallback just substituted away from.
                # Leaving it NULL keeps every implicit-default turn on the
                # silent-fallback path until a REAL explicit pick is made
                # (PATCH /api/chats/{id} -> chat_service.set_model_id).
                _resolution = "implicit_fallback"
                _fallback_key = _res.fallback_key
                _wire_model_id = _res.wire_id
            else:
                # Explicit pick OR no usable fallback wire_id: caller
                # surfaces the actionable error so the user can load their
                # chosen model or switch.
                return _CapabilityGateDecision(
                    resolution="requested_model_unloaded",
                    wire_model_id=_res.wire_id,
                    trimmed_kept=[],
                    trimmed_dropped=[],
                    fallback_key=_res.fallback_key,
                )
        else:
            # Not substituted: wire_id is a live loaded instance.
            _resolution = "resolved"
            _wire_model_id = _res.wire_id

        # Pre-flight integrations gate, two layers:
        # 1. Non-tool-trained model + any integrations -> drop all (LM
        #    Studio would expand the schemas anyway and waste prompt tokens).
        # 2. Tool-trained model + integrations that would overflow the
        #    loaded context window -> trim from the back. Layer 1 alone only
        #    protects the first case; the second used to sail straight into
        #    a silent stream death (context overflow -> LM Studio returns
        #    200 + chat.start, then closes ~20s later with no error).
        _caps = None
        try:
            # Needs the catalog KEY, not the instance id — model_id is the
            # stored key; resolve_to_loaded_or_fallback ran above.
            _caps = await self._models_service.get_capabilities(model_id)
        except Exception:  # noqa: BLE001
            pass

        _integrations = list(wire_payload.integrations or [])
        _action = "keep"

        # ---- Layer 1: non-tool model → drop all integrations ----
        if _caps is not None and not _caps.trained_for_tool_use and _integrations:
            _action = "drop_all"
            _integrations = []

        # ---- Layer 2: context-budget gate ----
        if _integrations:
            from lmchat.services._token_budget import (  # noqa: PLC0415
                approx_token_count,
                estimate_context_budget,
            )

            try:
                _max_ctx = await self._models_service.get_max_context_length(model_id)
            except Exception:  # noqa: BLE001
                _max_ctx = 0
            # Images contribute ~1700 tokens each but the FE doesn't send
            # them yet; add image counts here when it does.
            _input_text = " ".join(blk.content or "" for blk in wire_payload.input)

            # Probe each CONNECTED server's real advertised tool-schema JSON
            # size instead of the flat MCP_INTEGRATION_SCHEMA_TOKENS guess.
            # A server with nothing cached to probe (or any lookup failure)
            # is simply left out of this map and falls back to the flat guess.
            _integration_token_costs: dict[str, int] = {}
            if mcp_host is not None:
                try:
                    _connected = set(mcp_host.connected_server_ids)
                except Exception:  # noqa: BLE001
                    _connected = set()
                for _integration in _integrations:
                    _server_id = (
                        _integration[len("mcp/") :]
                        if _integration.startswith("mcp/")
                        else ""
                    )
                    if not _server_id or _server_id not in _connected:
                        continue
                    try:
                        _tools = mcp_host.list_tools([_server_id])
                        _schema_json = json.dumps([
                            {
                                "type": "function",
                                "function": {
                                    "name": _tool.name,
                                    "description": _tool.description,
                                    "parameters": _tool.parameters,
                                },
                            }
                            for _tool in _tools
                        ])
                    except Exception:  # noqa: BLE001
                        continue
                    _integration_token_costs[_integration] = approx_token_count(
                        _schema_json
                    )

            budget = estimate_context_budget(
                # encode_native drops system_prompt on follow-up turns — don't
                # count a prompt that never reaches the wire; the relocated
                # per-turn block is already in _input_text.
                system_prompt=(
                    None
                    if wire_payload.previous_response_id is not None
                    else wire_payload.system_prompt
                ),
                input_text=_input_text,
                integrations=_integrations,
                max_context_length=_max_ctx,
                integration_token_costs=_integration_token_costs,
            )
            if budget.would_overflow:
                # Overflow even after trimming every integration — prompt +
                # system alone exceeds the budget. Caller fails fast BEFORE
                # opening the upstream stream, instead of the old silent
                # death caught only after 20+ seconds by the stall path.
                return _CapabilityGateDecision(
                    resolution=_resolution,
                    wire_model_id=_wire_model_id,
                    trimmed_kept=[],
                    trimmed_dropped=[],
                    fallback_key=_fallback_key,
                    integrations_action=_action,
                    context_budget_terminate=True,
                    budget_estimated_total=budget.estimated_total,
                    budget_max_with_headroom=budget.max_with_headroom,
                )
            if budget.dropped:
                # Trim — the stream proceeds with the kept set; the caller's
                # warning frame lets the FE surface the change.
                _kept = [i for i in _integrations if i not in budget.dropped]
                return _CapabilityGateDecision(
                    resolution=_resolution,
                    wire_model_id=_wire_model_id,
                    trimmed_kept=_kept,
                    trimmed_dropped=budget.dropped,
                    fallback_key=_fallback_key,
                    integrations_action="trim",
                    budget_estimated_total=budget.estimated_total,
                    budget_max_with_headroom=budget.max_with_headroom,
                )

        return _CapabilityGateDecision(
            resolution=_resolution,
            wire_model_id=_wire_model_id,
            trimmed_kept=[],
            trimmed_dropped=[],
            fallback_key=_fallback_key,
            integrations_action=_action,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _apply_tool_call_delta(
        self,
        *,
        event: CanonicalEvent,
        chat_id: int,
        state: dict[str, object],
        accumulated_tool_calls: list[dict[str, object]],
        turn_tool_rounds: int,
        last_tool_sig: str | None,
        consecutive_identical_rounds: int,
    ) -> tuple[int, str | None, int]:
        """Fold one event's tool-call delta into the turn's accumulators.

        On any event carrying a ``tool_call``, folds it into
        *accumulated_tool_calls* (mutated in place, mirrored into ``state``
        for the teardown salvage path). On a completed round
        (``tool_call.success``/``tool_call.failure``), bumps the cross-turn
        round counter and this turn's round count, and — on SUCCESS only —
        tracks whether the call signature (name + args) repeats the
        previous successful round: the consecutive-identical-rounds
        backstop for the loop-cut decision.

        A failed call neither extends nor resets the streak: it's left
        untouched, exactly mirroring ``lmstudio_streaming_client``'s own
        repeat detector, which matches "only against prior SUCCESSFUL
        identical calls (fail -> retry with same args is legitimate)". Model
        hits a flaky MCP server / transient network blip and retries the
        same valid call — that must not count toward the same cut a
        hallucinating success-loop trips. The per-turn round cap
        (``_MAX_TOOL_ROUNDS_PER_TURN``) and the client's own
        ``failure_streak`` detector still bound a genuine failure loop.

        Returns the updated ``(turn_tool_rounds, last_tool_sig,
        consecutive_identical_rounds)`` since those are plain locals in the
        caller's generator frame.
        """
        if event.tool_call is not None:
            _accumulate_tool_call(accumulated_tool_calls, event.type, event.tool_call)
            # Expose the tool-call buffer to the teardown salvage so a stall
            # mid-tool-loop keeps its tool cards on reload.
            state["acc_tool_calls"] = accumulated_tool_calls

        if event.type in ("tool_call.success", "tool_call.failure"):
            self._increment_tool_round(chat_id)
            # Bumped here so the cap check below sees the round that just
            # completed.
            turn_tool_rounds += 1
            state["acc_tool_rounds"] = turn_tool_rounds
            _tc = event.tool_call
            if _tc is not None and event.type == "tool_call.success":
                try:
                    _sig = f"{_tc.name}\x00" + json.dumps(
                        _tc.arguments,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                except (TypeError, ValueError):
                    _sig = f"{_tc.name}\x00{_tc.arguments!r}"
                if _sig == last_tool_sig:
                    consecutive_identical_rounds += 1
                else:
                    last_tool_sig = _sig
                    consecutive_identical_rounds = 0

        return turn_tool_rounds, last_tool_sig, consecutive_identical_rounds

    def _track_loop_cut_signals(
        self,
        *,
        event: CanonicalEvent,
        repeat_warn_counts: dict[tuple[str, str], int],
        early_cut_reason: str | None,
        msg_id: int,
        repeat_warning_cut_k: int,
    ) -> str | None:
        """Track the two client-advisory early loop-cut signals.

        Acted on (by the caller) after the SSE frame is yielded, so the FE
        sees the warning before we abort.

        1. ``tool_call.repeat_warning``: the client saw a prior SUCCESSFUL
           call with the same (name, args) via a lookback deque (catches
           non-consecutive repeats too); cut after K=``repeat_warning_cut_k``
           warnings for the signature. Effective K is resolved by the caller
           (per-chat override -> global admin default -> config default; see
           ``stream_chat``'s ``_repeat_warning_cut_k`` computation).
        2. ``tool_call.failure_streak_warning``: the client detected
           FAILURE_STREAK_THRESHOLD consecutive failures for the same tool
           — cut immediately.

        Mutates *repeat_warn_counts* in place; returns the (possibly
        unchanged) ``early_cut_reason``.
        """
        if (
            event.type == "tool_call.repeat_warning"
            and repeat_warning_cut_k > 0
            and early_cut_reason is None
            and event.tool_call is not None
        ):
            try:
                _rw_sig = (
                    event.tool_call.name,
                    json.dumps(
                        event.tool_call.arguments,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
            except (TypeError, ValueError):
                _rw_sig = (
                    event.tool_call.name,
                    repr(event.tool_call.arguments),
                )
            repeat_warn_counts[_rw_sig] = repeat_warn_counts.get(_rw_sig, 0) + 1
            if repeat_warn_counts[_rw_sig] >= repeat_warning_cut_k:
                early_cut_reason = "repeat_loop"
                log.warning(
                    "stream.repeat_loop_cut_armed",
                    msg_id=msg_id,
                    tool_name=event.tool_call.name,
                    repeat_count=repeat_warn_counts[_rw_sig],
                    threshold=repeat_warning_cut_k,
                )

        if event.type == "tool_call.failure_streak_warning" and early_cut_reason is None:
            early_cut_reason = "failure_streak"
            log.warning(
                "stream.failure_streak_cut_armed",
                msg_id=msg_id,
                tool_name=(
                    event.tool_call.name if event.tool_call is not None else "unknown"
                ),
                streak=(event.error or {}).get("streak"),
            )

        return early_cut_reason

    async def _finalize_tool_loop_cut(
        self,
        *,
        loop_cut_decision: _LoopCutDecision,
        msg_id: int,
        chat_id: int,
        model_id: str,
        user_id: int,
        user_text: str,
        chat_project_id: int | None,
        turn_tool_rounds: int,
        accumulated_content: str,
        accumulated_reasoning: str,
        accumulated_tool_calls: list[dict[str, object]],
        aiter_iter: AsyncIterator[CanonicalEvent],
        coalesce: _CoalesceTimer,
        state: dict[str, object],
    ) -> AsyncIterator[bytes]:
        """Close out a turn whose tool-call loop was cut (cap/repeat/failure-streak).

        Per-turn tool-loop cap: LM Studio drives the MCP loop natively for
        local models and never stops on its own if the model keeps
        re-deciding to call tools. Once rounds exceed the cap, abort
        upstream and finalize with whatever's written — never a silent
        empty death. A tools-off re-synthesis is NOT attempted here (unlike
        the cloud agentic path, which owns working_history): the gathered
        tool results live in LM Studio's server-side state, which only
        becomes a chainable response_id once THIS turn completes with a
        chat.end that never arrived, so there's no usable anchor to
        resynthesize against.

        Mirrors the ``chat.end`` finalize path but with ``stop_reason``
        hard-pinned to ``"tool_loop_cap"`` (the FE contract) and no OOB
        followups/memory-distill wait — the caller always ``return``s
        immediately after consuming this generator, ending the turn.
        """
        # _decide_loop_cut guarantees both are non-None whenever should_cut
        # is True.
        assert loop_cut_decision.cut_reason is not None
        assert loop_cut_decision.effective_cut is not None
        # stop_reason / warning-frame code stay "tool_loop_cap" for all
        # paths (unchanged FE contract); cut_reason is only the
        # fine-grained log/metric label below.
        cut_reason: str = loop_cut_decision.cut_reason
        effective_cut: str = loop_cut_decision.effective_cut
        log.warning(
            "stream.tool_loop_cap_hit",
            msg_id=msg_id,
            chat_id=chat_id,
            model_id=model_id,
            turn_tool_rounds=turn_tool_rounds,
            cap=_MAX_TOOL_ROUNDS_PER_TURN,
            cut_reason=cut_reason,
            has_partial_content=bool(accumulated_content.strip()),
        )
        STREAMS_SALVAGED.labels(reason=cut_reason).inc()

        # Silence the watcher BEFORE the (potentially slow) upstream
        # teardown: aclose() + finalize DB writes could exceed
        # _idle_timeout_sec + _STALL_GRACE_SEC and trigger a spurious stall
        # on a turn we're cleanly finishing (mirrors the in-body stall
        # handler's stall_handled flag).
        state["done"] = True
        state["stall_handled"] = True

        # Closing the async generator propagates GeneratorExit into the
        # adapter's stream_chat, whose finally tears down the httpx stream.
        with suppress(Exception):
            await aiter_iter.aclose()  # type: ignore[attr-defined]

        await coalesce.flush()

        # Same policy as chat.end. effective_cut was already normalised to
        # "repeat_loop" above when the identical-rounds backstop also
        # tripped.
        _terminal = resolve_terminal_content(
            accumulated_content,
            accumulated_reasoning,
            had_tool_calls=True,
            tool_rounds=turn_tool_rounds,
            loop_cut_reason=effective_cut,
        )
        final_loop_content = _terminal.content
        final_reasoning = _terminal.reasoning or ""

        _resp_id = state["response_id"]
        assert _resp_id is None or isinstance(_resp_id, str), (
            "response_id must be str | None"
        )
        final_ok = await self._finalize_message(
            msg_id=msg_id,
            response_id=_resp_id,
            final_content=final_loop_content,
            final_reasoning=final_reasoning,
            stop_reason="tool_loop_cap",
            tool_calls=accumulated_tool_calls or None,
        )
        # _finalize_message only transitions message state now (gauge/marker
        # teardown is owned by stream_chat's single finally); no latching
        # needed here.
        # Tell the FE the loop was CUT (not a natural finish), so the user
        # understands why the answer is truncated.
        yield _format_warning_frame(
            code="tool_loop_cap",
            detail=(
                "Stopped a runaway tool-call loop after "
                f"{turn_tool_rounds} rounds. The model kept "
                "calling tools without answering."
            ),
            msg_id=msg_id,
        )
        # Synthetic chat.end so the FE closes the stream cleanly (a finished
        # turn, not an error).
        yield _format_sse_frame(
            CanonicalEvent(type="chat.end", stop_reason="tool_loop_cap"),
            msg_id=msg_id,
        )
        if final_ok:
            # with_distill_and_summary=False -> always None; captured only
            # for parity with the natural chat.end site (not awaited here).
            _distill_task = self._fire_post_finalize_background(
                msg_id=msg_id,
                chat_id=chat_id,
                user_id=user_id,
                model_id=model_id,
                content=final_loop_content,
                user_text=user_text,
                project_id=chat_project_id,
                with_distill_and_summary=False,
            )
            log.info(
                "stream.completed_tool_loop_capped",
                msg_id=msg_id,
                chat_id=chat_id,
            )
            await self._write_audit(
                event="stream.tool_loop_capped",
                user_id=user_id,
                detail={
                    "msg_id": msg_id,
                    "chat_id": chat_id,
                    "turn_tool_rounds": turn_tool_rounds,
                    "cap": _MAX_TOOL_ROUNDS_PER_TURN,
                },
            )

    async def _emit_grammar_degrade_warning(
        self,
        *,
        original_integrations: list[str],
        detail: str,
        msg_id: int,
        chat_id: int,
        model_id: str,
    ) -> AsyncIterator[bytes]:
        """Log, count, and yield the grammar-degrade warning frame.

        Pure emission — the caller sets the degrade-once flag *before*
        calling this (so the flag already reads correctly if anything
        inspects it mid-yield) and rebinds the upstream iterator to the
        tool-less retry *after* this generator is exhausted, exactly
        mirroring the original nested-closure ordering.
        """
        log.warning(
            "stream.grammar_degrade.triggered",
            msg_id=msg_id,
            chat_id=chat_id,
            model_id=model_id,
            integrations=original_integrations,
            error_message=detail[:200],
        )
        STREAMS_SALVAGED.labels(reason="grammar_degrade").inc()
        yield _format_warning_frame(
            code="tool_schema_parse_failed",
            detail=_grammar_degrade_warning(original_integrations),
            msg_id=msg_id,
        )

    async def stream_chat(
        self,
        *,
        chat_id: int,
        user: User,
        payload: ChatStreamRequest,
        request: Request,
    ) -> AsyncIterator[bytes]:
        """Stream an assistant response for *chat_id*.

        Acquires the per-chat lock, enforces the single-stream invariant,
        creates a draft row, opens the upstream connection, runs the
        disconnect watcher + persist state machine in an
        ``asyncio.TaskGroup``, yields raw SSE bytes, and performs
        terminal-state cleanup.

        Raises:
            StreamInProgressError: If a draft/pending row already exists for
                the chat (caught by the route -> HTTP 409).
        """
        user_id: int = user.id  # type: ignore[attr-defined]
        model_id: str = payload.payload.model

        chat_lock = self._chat_locks.setdefault(chat_id, asyncio.Lock())

        async with chat_lock:
            # Ownership check (404 if not this user's chat) + pull
            # project_id (for the project system_prompt injection below) and
            # model_id (non-NULL only for an EXPLICIT per-chat pick — the
            # substituted-model early-error path uses this distinction:
            # implicit default falls back silently, explicit pick keeps the
            # error).
            async with self._engine.connect() as _conn:
                _row = (
                    await _conn.execute(
                        select(chats.c.id, chats.c.project_id, chats.c.model_id).where(
                            chats.c.id == chat_id,
                            chats.c.user_id == user_id,
                        )
                    )
                ).fetchone()
            if _row is None:
                raise ChatNotFoundError(chat_id)
            chat_project_id: int | None = _row.project_id
            # NULL / empty = this chat has no explicit per-chat model; the
            # model in the request came from the global/override default.
            chat_stored_model_id: str | None = _row.model_id or None

            await self._assert_no_in_progress_stream(chat_id)

            # Inside the lock, before _create_draft, so the user row and
            # assistant draft are persisted atomically in one transaction.
            user_text = " ".join(
                block.content or ""
                for block in payload.payload.input
                if block.type == "text" and block.content
            )

            msg_id = await self._create_draft(
                chat_id=chat_id, user_id=user_id, model_id=model_id, user_text=user_text
            )

        log.info(
            "stream.start",
            chat_id=chat_id,
            user_id=user_id,
            msg_id=msg_id,
            model_id=model_id,
        )

        # STREAMS_ACTIVE + active-stream marker are established together
        # HERE (paired with the single finally below) and torn down EXACTLY
        # ONCE for every exit — normal completion, each early error-return,
        # every in-body error path, an exception out of the TaskGroup, and
        # GeneratorExit (client disconnect). Synchronous gauge ops can't
        # raise, so the inc is always balanced by the dec.
        STREAMS_ACTIVE.inc()
        mark_active(chat_id)
        # Pre-bind before the try so the teardown finally's salvage can
        # always read _state, even if the init below were interrupted.
        _state: dict[str, object] = {}
        try:
            # Shared between _persist_state_machine and _watch_disconnect via
            # closure; the event loop is single-threaded, no locking needed.
            _state = {
                "response_id": None,
                # From chat.end; persisted in _finalize_message so the FE
                # renders the Continue chip on reload when == "length".
                "stop_reason": None,
                "done": False,
                # Updated by _run_persist_and_yield on each content-bearing
                # event; polled by _watch_disconnect every 500ms tick.
                "last_content_ts": monotonic(),
                # `stall` is the idle-seconds the watcher captured;
                # `stall_handled` flips True once the persist body sees
                # stall_event and yields its error frame — the dead-man hedge
                # only fires if the body misses within _STALL_GRACE_SEC.
                "stall": None,
                "stall_handled": False,
                # Set by _watch_disconnect the instant it observes
                # http.disconnect — BEFORE its own shielded safe_abort_draft
                # await, so a lost race (teardown reaches the finally first)
                # can't lose the fact that a client disconnect, not a terminal
                # error, is why this turn is ending. The outer finally reads
                # this to decide FINAL vs ABORTED_BY_CLIENT — see its comment.
                "disconnected": False,
            }

            # Lives outside _state (not dict-friendly). The persist generator
            # races `anext(upstream_iter)` against `stall_event.wait()` so a
            # stall fires the error frame from inside the generator body —
            # no cross-task raise, no TaskGroup abort.
            stall_event: asyncio.Event = asyncio.Event()

            # Compaction chain-reset backstop: the LM Studio resume anchor
            # (previous_response_id) is CLIENT-side (localStorage, cleared by
            # the FE on a successful /compact) — this is defense-in-depth for
            # a stale client that still sends the pre-compaction rid.
            #
            # Drop the incoming previous_response_id (force replay) when the
            # response_id is unknown, the anchor message is itself archived,
            # or — the case that actually matters — the anchor PREDATES the
            # chat's latest compaction. After a compact the FE's cached
            # anchor is the latest KEPT turn, but that turn's response_id
            # still maps to LM Studio's PRE-compaction server-side chain,
            # which still carries the full archived content; resuming it
            # would silently re-send everything the compact just archived.
            # Comparing timestamps (anchor vs. latest compaction) catches
            # this; the history query below already excludes archived rows,
            # so a forced replay re-sends summary + active only.
            if payload.payload.previous_response_id is not None:
                _incoming_rid = payload.payload.previous_response_id
                _drop_stale_rid = False
                try:
                    async with self._engine.connect() as _backstop_conn:
                        _anchor_row = (
                            await _backstop_conn.execute(
                                select(
                                    messages.c.created_at,
                                    messages.c.compaction_id,
                                )
                                .where(
                                    messages.c.chat_id == chat_id,
                                    messages.c.response_id == _incoming_rid,
                                )
                                .limit(1)
                            )
                        ).fetchone()
                        if _anchor_row is None:
                            _drop_stale_rid = True  # unknown response_id
                        elif _anchor_row.compaction_id is not None:
                            _drop_stale_rid = True  # anchor itself archived
                        else:
                            _latest_compaction_at = (
                                await _backstop_conn.execute(
                                    select(func.max(compactions.c.created_at)).where(
                                        compactions.c.chat_id == chat_id
                                    )
                                )
                            ).scalar()
                            if (
                                _latest_compaction_at is not None
                                and _anchor_row.created_at < _latest_compaction_at
                            ):
                                _drop_stale_rid = True
                except Exception as _backstop_exc:  # noqa: BLE001
                    # Fail closed: if the anchor can't be verified safe, drop
                    # it and force replay rather than resume a stale chain.
                    log.warning(
                        "stream.compaction_backstop_check_failed",
                        chat_id=chat_id,
                        msg_id=msg_id,
                        error=str(_backstop_exc),
                    )
                    _drop_stale_rid = True

                if _drop_stale_rid:
                    log.info(
                        "stream.compaction_chain_reset_backstop",
                        chat_id=chat_id,
                        msg_id=msg_id,
                        previous_response_id=_incoming_rid,
                    )
                    payload.payload = payload.payload.model_copy(
                        update={"previous_response_id": None}
                    )

            # System-prompt composition. NOTE: no followups directive is
            # injected into the main answer prompt — it was removed in the
            # OOB-followups decoupling because it poisoned reasoning (~30x).
            # Chips are generated post-chat.end by a separate thinking-disabled
            # call (_generate_followups_oob); lm_chat_followups_enabled only
            # gates that. The block below composes the project/chat prompt,
            # which must run regardless of that flag so the project prompt
            # always reaches the LLM.
            from lmchat.config import get_settings as _get_settings

            _settings = _get_settings()

            # Composition order asserted by test_streaming_a1_composition.py:
            #   [RAG_context][project_prompt][chat_prompt][history]
            # RAG is prepended later; this builds
            # [project_prompt][chat_prompt] into `_existing_sys`.
            _existing_sys = payload.payload.system_prompt or ""
            if chat_project_id is not None and self._projects_service is not None:
                _proj = await self._projects_service.get(  # type: ignore[attr-defined]
                    user_id=user_id, project_id=chat_project_id
                )
                if _proj is None:
                    # chats.project_id points at a deleted/missing project —
                    # log and degrade gracefully (no project prompt); the FK
                    # cascade SET NULL cleans it up on the next mutation.
                    log.warning(
                        "stream.project_lookup_miss",
                        chat_id=chat_id,
                        user_id=user_id,
                        project_id=chat_project_id,
                    )
                    _project_prompt = ""
                else:
                    _project_prompt = _proj.system_prompt or ""
                if _project_prompt:
                    _existing_sys = (
                        f"{_project_prompt}\n\n{_existing_sys}"
                        if _existing_sys
                        else _project_prompt
                    )
                # The project's rolling auto-summary (see
                # project_summary_service.refresh_project_summary), prepended
                # ahead of project_prompt so explicit instructions stay
                # closest to the chat's own system prompt.
                _project_summary = (
                    (getattr(_proj, "summary", "") or "") if _proj is not None else ""
                )
                if _project_summary:
                    _summary_block = f"Project summary:\n{_project_summary}"
                    _existing_sys = (
                        f"{_summary_block}\n\n{_existing_sys}"
                        if _existing_sys
                        else _summary_block
                    )

            # Surface tool availability so reasoning models don't narrate
            # tool calls they can't invoke ("Let me run these searches now."
            # then chat.end with no answer). Prepended so the model sees the
            # constraint before its first generation token.
            _tool_avail = payload.payload.integrations or []
            if not _tool_avail:
                _TOOL_AVAILABILITY_NOTE = (
                    "[Runtime: this chat has no live tools — no web search, no "
                    "code execution, no file I/O. Answer directly from your "
                    "knowledge. Do NOT say 'let me search', 'let me run', or "
                    "'I'll look that up' — there's no wrapper to interpret it. "
                    "If you genuinely don't know, say so and offer your best "
                    "approximation.]"
                )
                _existing_sys = (
                    f"{_TOOL_AVAILABILITY_NOTE}\n\n{_existing_sys}"
                    if _existing_sys
                    else _TOOL_AVAILABILITY_NOTE
                )

            # Provider / context-mode resolution: read chat.settings early so
            # we know whether this chat routes to a replay (cloud) provider
            # before building the system prompt. context_mode: "chain" is LM
            # Studio (default), "replay" is cloud/OAI-compat with full
            # history on the wire.
            _chat_settings: dict = {}  # type: ignore[type-arg]
            try:
                async with self._engine.connect() as _a3_conn:
                    _a3_row = (
                        await _a3_conn.execute(
                            select(chats.c.settings).where(chats.c.id == chat_id)
                        )
                    ).fetchone()
                _chat_settings = _a3_row.settings if _a3_row and _a3_row.settings else {}
            except Exception as _a3_exc:  # noqa: BLE001
                log.warning(
                    "stream.provider_resolution_settings_failed",
                    chat_id=chat_id,
                    msg_id=msg_id,
                    error=str(_a3_exc),
                )
                # A failed settings read must not silently revert this chat
                # to lmstudio/chain-mode defaults — that downgrades provider,
                # context_mode, SC/CoVe, and reasoning_effort with no signal
                # to the user. Fail loudly instead (mirrors the
                # unknown-provider path below); the single try/finally still
                # owns lifecycle teardown.
                yield _format_error_frame(
                    msg_id=msg_id,
                    code="settings_unavailable",
                    detail="Couldn't load this chat's settings. Please try again.",
                )
                STREAMS_FAILED.labels(reason="settings_unavailable").inc()
                return

            # Effective-K resolution for the tool-call repeat-loop cut:
            # per-chat override (chats.settings.repeat_warning_cut_k) ->
            # global admin default (resolve_repeat_warning_cut_k) -> config
            # default. Consumed by _track_loop_cut_signals below, well after
            # the tool loop starts. bool is excluded even though it's an int
            # subclass — the JSON blob should never legitimately hold one
            # here, but a stray True/False must not silently become 1/0.
            _repeat_warning_cut_k_override = _chat_settings.get("repeat_warning_cut_k")
            if isinstance(_repeat_warning_cut_k_override, int) and not isinstance(
                _repeat_warning_cut_k_override, bool
            ):
                _repeat_warning_cut_k = max(0, min(100, _repeat_warning_cut_k_override))
            else:
                _repeat_warning_cut_k = await resolve_repeat_warning_cut_k(self._engine)

            _provider_name: str = _chat_settings.get("provider", "lmstudio") or "lmstudio"
            _provider_resolution = await self._resolve_provider_and_context_mode(
                chat_id=chat_id,
                msg_id=msg_id,
                provider_name=_provider_name,
            )
            if _provider_resolution.error_code is not None:
                # error_detail is always set alongside error_code by the
                # resolver; the `or` fallback avoids leaning on a
                # python-O-strippable assert.
                yield _format_error_frame(
                    msg_id=msg_id,
                    code=_provider_resolution.error_code,
                    detail=(
                        _provider_resolution.error_detail
                        or "Provider is not configured. Add it in "
                        "Settings → Providers before sending a message."
                    ),
                )
                # The single try/finally wrapping the rest of stream_chat
                # owns lifecycle teardown (STREAMS_ACTIVE decrement, stuck
                # draft release, mark_inactive) — this path only records the
                # failure metric and returns.
                STREAMS_FAILED.labels(reason="unknown_provider").inc()
                return
            _context_mode: str = _provider_resolution.context_mode
            _dispatch_provider = _provider_resolution.dispatch_provider
            # Gates the app-executed web_search tool at the dispatch fork
            # below — set ONLY by the openai_compat branch of the resolver.
            _builtin_web_search = _provider_resolution.builtin_web_search

            # Store-routed native turn: a Store-installed MCP tool was picked
            # while LM Studio is in NATIVE mode, which forwards integrations
            # to LM Studio's own server-side mcp.json host — a Store server
            # isn't in that file, so the tool would silently do nothing.
            # Route it through the same replay + agentic-MCP dispatch
            # openai_compat uses (AgenticMcpProvider runs it client-side
            # against this same local model). Only fires when a requested
            # integration resolves to an enabled+consented Store server; a
            # curated-only turn is untouched.
            if (
                _provider_name == "lmstudio"
                and _context_mode == "chain"
                and self._provider_registry is not None
                and await _has_store_integration(
                    request=request, integrations=payload.payload.integrations
                )
            ):
                _lmstudio_native_for_store = self._provider_registry.get(  # type: ignore[attr-defined]
                    "lmstudio"
                )
                if _lmstudio_native_for_store is not None:
                    _context_mode = "replay"
                    _dispatch_provider = (
                        _lmstudio_native_for_store.as_openai_compat_provider()  # type: ignore[attr-defined]
                    )
                    log.info(
                        "stream.lmstudio_store_integration_dispatch",
                        chat_id=chat_id,
                        msg_id=msg_id,
                    )

            reasoning_payload = await self._assemble_system_prompt(
                chat_id=chat_id,
                msg_id=msg_id,
                user_id=user_id,
                model_id=model_id,
                payload=payload,
                _context_mode=_context_mode,
                _existing_sys=_existing_sys,
                _chat_settings=_chat_settings,
                builtin_web_search=_builtin_web_search,
            )

            # Cloud has no "loaded instances" — the model string on a replay
            # request is already the wire id, so LM Studio-specific
            # resolution + the integrations gate are skipped entirely.
            wire_payload = reasoning_payload
            if _context_mode == "chain" and self._models_service is not None:
                # Read-only: lets the gate's Layer-2 budget computation probe
                # each integration's real tool-schema size for any already-
                # connected server, instead of the flat guess alone.
                _mcp_host_for_budget = getattr(request.app.state, "mcp_host", None)
                _gate = await self._resolve_model_and_integrations_gate(
                    model_id=model_id,
                    chat_stored_model_id=chat_stored_model_id,
                    wire_payload=wire_payload,
                    mcp_host=_mcp_host_for_budget,
                )
                if _gate.resolution == "no_model_loaded":
                    yield _format_error_frame(
                        code="upstream_unavailable",
                        detail=(
                            "No language model is loaded in LM Studio. Load a model "
                            "(or enable JIT loading) and try again."
                        ),
                        msg_id=msg_id,
                    )
                    # The single try/finally below releases the draft row
                    # (idempotent) and tears down the gauge + active marker,
                    # so the next send isn't 409'd.
                    STREAMS_FAILED.labels(reason="upstream_unavailable").inc()
                    return
                if _gate.resolution == "requested_model_unloaded":
                    log.info(
                        "stream.requested_model_unloaded",
                        chat_id=chat_id,
                        msg_id=msg_id,
                        requested=model_id,
                        loaded_alt=_gate.fallback_key,
                    )
                    yield _format_error_frame(
                        code="upstream_unavailable",
                        detail=(
                            f"Model {model_id!r} isn't loaded in LM Studio. Pick a "
                            f"loaded model (e.g. {_gate.fallback_key!r}) or load "
                            f"{model_id!r}."
                        ),
                        msg_id=msg_id,
                    )
                    # The single try/finally below releases the draft row so
                    # the resend (with a loaded model) isn't 409'd — without
                    # this an unloaded-model send bricks the chat.
                    STREAMS_FAILED.labels(reason="upstream_unavailable").inc()
                    return
                if _gate.resolution == "implicit_fallback":
                    # See _resolve_model_and_integrations_gate for why the
                    # fallback key is deliberately NOT persisted as
                    # chats.model_id here.
                    log.info(
                        "stream.implicit_default_fell_back",
                        chat_id=chat_id,
                        msg_id=msg_id,
                        requested=model_id,
                        fallback=_gate.fallback_key,
                        wire_id=_gate.wire_model_id,
                    )
                    _wire_model_id = _gate.wire_model_id
                    wire_payload = reasoning_payload.model_copy(
                        update={"model": _wire_model_id}
                    )
                else:
                    _wire_model_id = _gate.wire_model_id
                    if _wire_model_id != model_id:
                        wire_payload = reasoning_payload.model_copy(
                            update={"model": _wire_model_id}
                        )
                        log.info(
                            "stream.model_id_resolved",
                            chat_id=chat_id,
                            msg_id=msg_id,
                            stored_key=model_id,
                            wire_id=_wire_model_id,
                        )

                # Two-layer check — see _resolve_model_and_integrations_gate.
                if _gate.integrations_action == "drop_all":
                    _dropped = list(wire_payload.integrations or [])
                    wire_payload = wire_payload.model_copy(update={"integrations": []})
                    # The capability legend (already baked into
                    # wire_payload.system_prompt by _assemble_system_prompt,
                    # BEFORE this gate ran) still advertises the just-dropped
                    # tools under "Tools you can call directly" — tell the
                    # model they're gone this turn, or it emits the call as
                    # literal JSON text instead of a real tool_call. See
                    # apply_tools_unavailable_corrective's docstring.
                    from lmchat.services.prompt_assembly import (  # noqa: PLC0415
                        apply_tools_unavailable_corrective,
                    )

                    wire_payload = apply_tools_unavailable_corrective(
                        wire_payload, _dropped
                    )
                    log.warning(
                        "stream.integrations_dropped_for_non_tool_model",
                        chat_id=chat_id,
                        msg_id=msg_id,
                        model_id=_wire_model_id,
                        dropped=_dropped,
                        hint=(
                            "Model isn't trained for tool use; integrations "
                            "would be wasted prompt tokens and can overflow "
                            "small contexts. Switch to a tool-trained model "
                            "to use these integrations."
                        ),
                    )

                # ---- Layer 2: context-budget gate ----
                if _gate.context_budget_terminate:
                    log.warning(
                        "stream.context_budget_unsalvageable",
                        chat_id=chat_id,
                        msg_id=msg_id,
                        model_id=_wire_model_id,
                        estimated_total=_gate.budget_estimated_total,
                        max_with_headroom=_gate.budget_max_with_headroom,
                    )
                    STREAMS_FAILED.labels(reason="context_budget_unsalvageable").inc()
                    yield _format_error_frame(
                        code="context_budget_exceeded",
                        detail=(
                            f"Request needs roughly {_gate.budget_estimated_total} "
                            f"tokens but this model's loaded context only "
                            f"leaves {_gate.budget_max_with_headroom} for input. "
                            "Shorten the system prompt or message, or reload "
                            "the model in LM Studio with a larger context."
                        ),
                        msg_id=msg_id,
                    )
                    return
                if _gate.integrations_action == "trim":
                    # Stream proceeds with the kept set; the warning frame
                    # lets the FE surface the change to the user.
                    wire_payload = wire_payload.model_copy(
                        update={"integrations": _gate.trimmed_kept}
                    )
                    # Same stale-legend correction as the drop_all branch
                    # above — the legend was built from the full pre-trim
                    # request and still lists the tools trimmed here.
                    from lmchat.services.prompt_assembly import (  # noqa: PLC0415
                        apply_tools_unavailable_corrective,
                    )

                    wire_payload = apply_tools_unavailable_corrective(
                        wire_payload, _gate.trimmed_dropped
                    )
                    log.warning(
                        "stream.integrations_trimmed_for_context",
                        chat_id=chat_id,
                        msg_id=msg_id,
                        model_id=_wire_model_id,
                        dropped=_gate.trimmed_dropped,
                        kept=_gate.trimmed_kept,
                        estimated_total=_gate.budget_estimated_total,
                        max_with_headroom=_gate.budget_max_with_headroom,
                    )
                    yield _format_warning_frame(
                        code="integrations_trimmed_for_context",
                        detail=(
                            f"This model's context only fits "
                            f"{len(_gate.trimmed_kept)} of "
                            f"{len(_gate.trimmed_kept) + len(_gate.trimmed_dropped)} "
                            f"tools. Trimmed: {', '.join(_gate.trimmed_dropped)}."
                        ),
                        msg_id=msg_id,
                    )

            elif (
                _provider_name == "lmstudio"
                and _context_mode == "replay"
                and self._models_service is not None
            ):
                # This IS LM Studio (via its OpenAI-compat endpoint), and
                # that endpoint routes by LOADED-INSTANCE LABEL, not the
                # catalog key — the bare key 400s as model_not_found.
                # Resolve key -> wire_id. The integrations gate does NOT run
                # here: integrations are handled client-side by
                # AgenticMcpProvider in the replay dispatch below.
                _replay_res = await self._models_service.resolve_to_loaded_or_fallback(
                    model_id
                )
                if _replay_res.wire_id is None:
                    yield _format_error_frame(
                        code="upstream_unavailable",
                        detail=(
                            "No language model is loaded in LM Studio. Load a "
                            "model (or enable JIT loading) and try again."
                        ),
                        msg_id=msg_id,
                    )
                    STREAMS_FAILED.labels(reason="upstream_unavailable").inc()
                    return
                if _replay_res.wire_id != model_id:
                    wire_payload = reasoning_payload.model_copy(
                        update={"model": _replay_res.wire_id}
                    )
                    log.info(
                        "stream.replay_model_id_resolved",
                        chat_id=chat_id,
                        msg_id=msg_id,
                        stored_key=model_id,
                        wire_id=_replay_res.wire_id,
                    )

            # Chain mode + LM Studio integrations -> store=False. With
            # store=True (the default), a tool turn causes the model to
            # reason endlessly and never emit a working tool_call (measured:
            # 94 reasoning deltas, zero tool_call; immediate tool_call.start
            # with store=false). This opts the turn out of LM Studio's
            # server-side chain, so previous_response_id must be cleared and
            # history supplied ourselves below. Gated on integrations still
            # being non-empty after the gates above, so a fully-trimmed turn
            # stays on the normal chain path.
            if _context_mode == "chain" and wire_payload.integrations:
                wire_payload = wire_payload.model_copy(
                    update={"store": False, "previous_response_id": None}
                )
                log.info(
                    "stream.tool_turn_store_false",
                    chat_id=chat_id,
                    msg_id=msg_id,
                    integrations=list(wire_payload.integrations or []),
                )

            # Quality modes fire N INDEPENDENT generations, so they can't ride
            # LM Studio's server-side chain — each would branch it, and the
            # assembled turn context would never reach them. Force replay by
            # clearing the rid: the replay-history load below then composes
            # the full prior context into system_prompt for every internal
            # generation. Without this the modes answer context-blind.
            if (
                _context_mode == "chain"
                and wire_payload.previous_response_id is not None
                and (
                    _chat_settings.get("self_consistency_enabled")
                    or _chat_settings.get("chain_of_verification_enabled")
                )
            ):
                wire_payload = wire_payload.model_copy(
                    update={"previous_response_id": None}
                )
                log.info(
                    "stream.quality_mode.force_replay",
                    chat_id=chat_id,
                    msg_id=msg_id,
                )

            # See _load_replay_history for the full rationale.
            wire_payload, _replay_history = await self._load_replay_history(
                chat_id=chat_id,
                msg_id=msg_id,
                wire_payload=wire_payload,
                context_mode=_context_mode,
            )

            # Chain mode: do NOT pass history (LM Studio client defaults
            # history=None). Replay mode: pass the loaded full history so
            # encode_compat assembles the complete conversation on the wire.
            if _context_mode == "replay" and _dispatch_provider is not None:
                # LmstudioStreamingClient is stateless, so constructing
                # per-turn is safe and keeps all wrapping logic (tool-call
                # accumulation, coerce repair, repeat/failure-streak
                # detection, chat.end termination) intact.

                # Cloud + MCP integrations wrap in the agentic loop (SSOT in
                # mcp/agentic.py); LM Studio and cloud-without-integrations
                # are untouched.
                from lmchat.mcp.agentic import maybe_wrap_agentic  # noqa: PLC0415

                # App-executed web_search, gated behind the openai_compat-only
                # flag set above. Every other path leaves both args None.
                _builtin_registry_arg = None
                _builtin_ctx_arg = None
                if _builtin_web_search:
                    _web_search_service = getattr(
                        request.app.state, "web_search_service", None
                    )
                    if _web_search_service is not None:
                        from lmchat.services.builtin_tools import (  # noqa: PLC0415
                            BUILTIN_TOOL_REGISTRY,
                            BuiltinToolContext,
                        )

                        _builtin_registry_arg = BUILTIN_TOOL_REGISTRY
                        _builtin_ctx_arg = BuiltinToolContext(
                            web_search_service=_web_search_service
                        )

                _effective_provider = await maybe_wrap_agentic(
                    _dispatch_provider,
                    wire_payload.integrations,
                    request.app.state,
                    log_ctx={"site": "stream", "chat_id": chat_id, "msg_id": msg_id},
                    builtin_registry=_builtin_registry_arg,
                    builtin_ctx=_builtin_ctx_arg,
                )

                _replay_client = LmstudioStreamingClient(adapter=_effective_provider)  # type: ignore[arg-type]
                upstream_iter = _replay_client.stream(
                    request=wire_payload,
                    history=_replay_history,
                    cumulative_tool_rounds=self._tool_round_counts.get(chat_id, 0),
                )
            else:
                upstream_iter = self._lm_client.stream(
                    request=wire_payload,
                    cumulative_tool_rounds=self._tool_round_counts.get(chat_id, 0),
                )

            # Quality modes (self-consistency / chain-of-verification).
            #
            # Isolation contract: when BOTH flags are false (the common case)
            # this does nothing beyond two dict.get reads — upstream_iter is
            # left exactly as built above and downstream machinery runs
            # byte-identically to pre-quality-mode code.
            #
            # When a flag IS set, upstream_iter is REPLACED with a synthetic
            # one-shot CanonicalEvent stream that runs the quality method and
            # emits its answer as message.delta + chat.end, so the existing
            # _run_persist_and_yield machinery handles persistence with zero
            # duplicated logic. response_id stays None on the synthetic
            # chat.end (no single chainable LM Studio response), so the next
            # turn starts a fresh chain.
            #
            # Applies ONLY on the LM Studio chain path — replay/cloud ignore
            # the flags. Precedence when both are set: chain-of-verification
            # wins (more thorough: 4-step verify+revise vs. a single N-draft
            # vote).
            _sc = bool(_chat_settings.get("self_consistency_enabled"))
            _cove = bool(_chat_settings.get("chain_of_verification_enabled"))
            if (_sc or _cove) and self._quality_mode_service is not None:
                if _provider_name != "lmstudio":
                    log.info(
                        "stream.quality_mode.skipped_non_lmstudio",
                        chat_id=chat_id,
                        msg_id=msg_id,
                        provider_name=_provider_name,
                        self_consistency=_sc,
                        chain_of_verification=_cove,
                    )
                else:
                    _qm_mode = "cove" if _cove else "self_consistency"
                    _qm_prompt = user_text
                    _qm_model_id = wire_payload.model
                    _qm_integrations = list(wire_payload.integrations or [])
                    # Hand the quality generations the same assembled context
                    # the normal turn uses instead of the bare user text.
                    # Chain mode already composed history into system_prompt
                    # above; replay mode keeps history in _replay_history
                    # (out of system_prompt for the normal wire path), so
                    # fold it in here since quality modes bypass that path.
                    _qm_system_prompt = wire_payload.system_prompt or ""
                    if _context_mode == "replay" and _replay_history:
                        from lmchat.services.prompt_assembly import (  # noqa: PLC0415
                            serialize_prior_turns,
                        )

                        _qm_system_prompt = _qm_system_prompt + serialize_prior_turns(
                            [
                                (_hm.role or "user", _hm.content or "")
                                for _hm in _replay_history
                            ]
                        )
                    _qm_system_prompt = _qm_system_prompt or None
                    _qm_service = self._quality_mode_service
                    _qm_fallback_iter = upstream_iter
                    from lmchat.config import get_settings as _get_settings  # noqa: PLC0415

                    _qm_settings = _get_settings()
                    log.info(
                        "stream.quality_mode.dispatch",
                        chat_id=chat_id,
                        msg_id=msg_id,
                        mode=_qm_mode,
                        model_id=_qm_model_id,
                        has_integrations=bool(_qm_integrations),
                    )

                    async def _quality_mode_event_stream() -> AsyncIterator[CanonicalEvent]:
                        """Run the active quality mode and synthesise a one-shot
                        answer stream the existing persist machinery consumes.

                        Emits ``prompt_processing.start`` first (existing FE
                        event, rendered as "Processing...") and bumps
                        ``_state["last_content_ts"]`` on a heartbeat so the
                        idle-timeout watcher doesn't fire a spurious stall
                        during the slow multi-generation wait.

                        On ANY failure, delegates to the unconsumed real
                        upstream iterator — the user always gets a finalised
                        answer, never an unhandled 500 or half-persisted
                        message.
                        """
                        yield CanonicalEvent(type="prompt_processing.start")

                        # Run as a task so the idle clock can be kept fresh.
                        if _qm_mode == "cove":
                            _qm_coro = _qm_service.chain_of_verification(
                                prompt=_qm_prompt,
                                model_id=_qm_model_id,
                                integrations=_qm_integrations or None,
                                system_prompt=_qm_system_prompt,
                            )
                        else:
                            # _qm_integrations intentionally NOT passed:
                            # self_consistency is a deliberately tool-free
                            # consistency measurement (see its docstring in
                            # quality_modes.py) — an asymmetry with CoVe, not
                            # a bug; don't "fix" without updating
                            # test_quality_sc_no_integrations.py.
                            _qm_coro = _qm_service.self_consistency(
                                prompt=_qm_prompt,
                                model_id=_qm_model_id,
                                system_prompt=_qm_system_prompt,
                            )
                        _qm_task: asyncio.Task[object] = asyncio.create_task(
                            _qm_coro, name=f"quality_mode_{_qm_mode}_{msg_id}"
                        )
                        # Watchdog: the heartbeat below deliberately keeps the
                        # idle-stall watcher quiet, so a genuinely-hung call
                        # would block forever without this bound (default
                        # 7200s — far beyond any real run, catches a true hang
                        # without affecting hour-long deep analyses).
                        _qm_start = monotonic()
                        _qm_timeout_sec = float(_qm_settings.lm_chat_quality_mode_timeout_sec)
                        try:
                            while not _qm_task.done():
                                # Heartbeat: keep the idle-timeout watcher quiet and
                                # the FE "Processing…" line alive during the wait.
                                _state["last_content_ts"] = monotonic()
                                _qm_elapsed = monotonic() - _qm_start
                                if _qm_elapsed > _qm_timeout_sec:
                                    # Hung call: cancel (await so it doesn't
                                    # leak) and delegate to the still-
                                    # unconsumed real upstream — same fallback
                                    # as the except path below.
                                    _qm_task.cancel()
                                    with suppress(asyncio.CancelledError):
                                        await _qm_task
                                    log.warning(
                                        "stream.quality_mode.timeout_fallback",
                                        chat_id=chat_id,
                                        msg_id=msg_id,
                                        mode=_qm_mode,
                                        elapsed_sec=round(_qm_elapsed, 3),
                                    )
                                    _state["last_content_ts"] = monotonic()
                                    async for _ev in _qm_fallback_iter:
                                        yield _ev
                                    return
                                await asyncio.sleep(_DISCONNECT_POLL_SEC)
                                yield CanonicalEvent(
                                    type="prompt_processing.progress", progress=0.0
                                )
                            _qm_result = _qm_task.result()
                        except Exception as _qm_exc:  # noqa: BLE001
                            # Graceful degradation: delegate to the real
                            # upstream so the user gets a standard answer.
                            log.warning(
                                "stream.quality_mode.failed_fallback",
                                chat_id=chat_id,
                                msg_id=msg_id,
                                mode=_qm_mode,
                                error_type=type(_qm_exc).__name__,
                                error=str(_qm_exc),
                            )
                            _state["last_content_ts"] = monotonic()
                            async for _ev in _qm_fallback_iter:
                                yield _ev
                            return

                        _qm_answer: str
                        if _qm_mode == "cove":
                            # revised_answer is the final answer (==
                            # initial_answer when no revision needed); a
                            # reasoning-heavy model can leave both empty when
                            # reasoning alone exhausts the token budget, so
                            # fall back to initial_answer via
                            # oob_message_text's content-> fallback-field
                            # policy (reused via a synthetic message dict).
                            _qm_answer = oob_message_text(
                                {
                                    "content": getattr(_qm_result, "revised_answer", "") or "",
                                    "reasoning_content": (
                                        getattr(_qm_result, "initial_answer", "") or ""
                                    ),
                                }
                            )
                        else:
                            # self_consistency returns the chosen central draft
                            # str; no secondary field to fall back to.
                            _qm_answer = str(_qm_result or "").strip()

                        if not _has_real_answer(_qm_answer):
                            # Persisting "" would silently drop this turn as an
                            # empty bubble — surface an honest message instead
                            # (mirrors the tool-turn no-answer policy in
                            # substance_fold.resolve_terminal_content).
                            log.warning(
                                "stream.quality_mode.empty_answer_fallback",
                                chat_id=chat_id,
                                msg_id=msg_id,
                                mode=_qm_mode,
                            )
                            _qm_answer = _QM_EMPTY_ANSWER_FALLBACK.format(
                                mode=_QM_MODE_LABELS[_qm_mode]
                            )

                        log.info(
                            "stream.quality_mode.completed",
                            chat_id=chat_id,
                            msg_id=msg_id,
                            mode=_qm_mode,
                            answer_len=len(_qm_answer),
                        )
                        # Synthetic one-shot stream drives the existing
                        # persist/finalize/followups machinery unchanged.
                        yield CanonicalEvent(type="prompt_processing.end")
                        yield CanonicalEvent(type="message.start")
                        yield CanonicalEvent(type="message.delta", content=_qm_answer)
                        yield CanonicalEvent(type="message.end")
                        # response_id=None → next turn starts a fresh chain (the
                        # quality answer has no single chainable LM Studio anchor).
                        yield CanonicalEvent(type="chat.end", stop_reason="stop")

                    upstream_iter = _quality_mode_event_stream()

            # Grammar-parse robustness degrade: LM Studio's grammar generator
            # inspects every offered MCP tool schema, and a malformed one
            # (e.g. a stale firecrawl MCP) gets the whole turn rejected with a
            # 400 "failed to parse grammar".
            #
            # On the native chain surface this does NOT arrive as a yielded
            # error event — upstream yields only chat.start then exhausts, so
            # the "generator_exhausted_without_terminal" branch below probes
            # for it via a non-streaming re-issue. The degrade lives there
            # (and, belt-and-suspenders, at the yielded-error branch too).
            #
            # When triggered: warn naming the active integrations, then retry
            # the SAME draft turn with integrations stripped (a clean
            # tool-less turn). Degrade-once — a second failure surfaces
            # normally. Non-grammar errors and non-erroring turns are
            # byte-identical.
            _original_integrations: list[str] = list(wire_payload.integrations or [])

            def _build_toolless_retry_iter() -> AsyncIterator[CanonicalEvent]:
                """Open a fresh upstream stream with integrations stripped.

                Reuses the fully-assembled ``wire_payload`` and only strips
                integrations. ``store`` resets to None (the adapter default)
                since a tool-less retry must not carry the tool turn's
                ``store=False``. ``previous_response_id`` is already None and
                history is already composed into ``system_prompt``, so this
                is byte-identical to a normal broken-chain tool-less turn.
                """
                _toolless_req = wire_payload.model_copy(
                    update={"integrations": [], "store": None}
                )
                if _context_mode == "replay" and _dispatch_provider is not None:
                    _retry_client = LmstudioStreamingClient(
                        adapter=_dispatch_provider  # type: ignore[arg-type]
                    )
                    return _retry_client.stream(
                        request=_toolless_req,
                        history=_replay_history,
                        cumulative_tool_rounds=self._tool_round_counts.get(chat_id, 0),
                    )
                return self._lm_client.stream(
                    request=_toolless_req,
                    cumulative_tool_rounds=self._tool_round_counts.get(chat_id, 0),
                )

            async def _watch_disconnect(  # noqa: ANN202  (nested; return type inferred)
                request: Request,
                msg_id: int,
            ) -> None:
                """Watch for client disconnect via receive(); fire idle-timeout.

                On disconnect: transition draft -> aborted_by_client (a no-op
                via safe_abort_draft if the row already left DRAFT).

                Also checks the idle-timeout on every tick and signals
                ``_state["stall"]`` if the clock expires — covers the case
                where LM Studio keeps TCP alive with heartbeats but no
                content-bearing event arrives.
                """
                # Uses a BLOCKING request.receive() (wrapped in
                # asyncio.wait_for(..., 0.5s) so the loop still ticks for the
                # idle check) rather than polling is_disconnected(): while
                # uvicorn is continuously WRITING the streaming response it
                # never reads the socket, so the non-blocking
                # is_disconnected() never observes a queued http.disconnect
                # and returns False forever, wedging the chat at HTTP 409
                # until the reaper clears the draft. This watcher is the SOLE
                # consumer of receive() — the underlying ASGI queue.get() is
                # cancel-safe, so a queued disconnect is never lost across
                # wait_for timeouts.
                while not _state["done"]:
                    try:
                        _msg = await asyncio.wait_for(
                            request.receive(), timeout=_DISCONNECT_POLL_SEC
                        )
                    except TimeoutError:
                        _msg = None

                    # Fires if no content-bearing event arrived for
                    # idle_timeout_sec, regardless of heartbeat flow. Checked
                    # every tick.
                    _last_ts = _state["last_content_ts"]
                    assert isinstance(_last_ts, (int, float)), "last_content_ts must be numeric"
                    idle_s = monotonic() - float(_last_ts)
                    if idle_s > self._idle_timeout_sec and not _state["done"]:
                        # Signal-not-raise: a bare `raise _StreamStall` here
                        # would abort the TaskGroup and cancel uvicorn's host
                        # task before the error frame could reach the client.
                        # Instead: set state, signal the event, give the
                        # persist body _STALL_GRACE_SEC to handle it in-body;
                        # the dead-man hedge below only fires if it misses.
                        log.warning(
                            "stream.idle_timeout",
                            msg_id=msg_id,
                            idle_s=round(idle_s, 1),
                        )
                        _state["stall"] = idle_s
                        stall_event.set()
                        # Dead-man hedge: the grace window doesn't add
                        # user-visible latency — the body races the event and
                        # yields the frame within an event-loop tick.
                        await asyncio.sleep(_STALL_GRACE_SEC)
                        if not _state["stall_handled"] and not _state["done"]:
                            log.warning(
                                "stream.stall_dead_man_fallback_fired",
                                msg_id=msg_id,
                                idle_s=round(idle_s, 1),
                            )
                            raise _StreamStall(idle_s)
                        return

                    if _msg is not None and _msg.get("type") == "http.disconnect":
                        log.info("stream.disconnected", msg_id=msg_id)
                        # Record the CAUSE before the await below, not after —
                        # a synchronous dict write can't be lost to a lost
                        # race the way an awaited UPDATE can. The outer
                        # finally reads this to salvage toward
                        # ABORTED_BY_CLIENT instead of FINAL regardless of
                        # which of the two writers actually wins the
                        # draft-row race (see that finally's comment).
                        _state["disconnected"] = True
                        # shield(): the TaskGroup cancels this watcher the
                        # instant the persist task tears down on disconnect,
                        # which could cancel this UPDATE mid-flight. Belt-and-
                        # suspenders — the real guarantee is the shielded
                        # abort-or-release in stream_chat's finally.
                        aborted = await asyncio.shield(
                            safe_abort_draft(engine=self._engine, message_id=msg_id)
                        )
                        if aborted:
                            # safe_abort_draft already moved the row to ABORTED;
                            # the gauge dec + active-marker teardown is owned by
                            # the single try/finally in stream_chat (the finally's
                            # _release_stuck_draft is then a no-op — the row has
                            # left DRAFT).
                            STREAMS_FAILED.labels(reason="client_disconnect").inc()
                            await self._write_audit(
                                event="stream.disconnected",
                                user_id=user_id,
                                detail={"msg_id": msg_id, "chat_id": chat_id},
                            )
                        return

            async def _run_persist_and_yield() -> AsyncIterator[bytes]:
                """Core persist state machine + SSE frame yielder.

                Iterates upstream events, coalesces deltas, drives state
                transitions, detects idle timeout, and yields SSE frames.

                Idle-timeout detection is two complementary mechanisms: on
                each non-content-bearing event, check elapsed idle time; the
                _watch_disconnect task also polls it every 500ms tick,
                covering LM Studio keeping TCP alive with heartbeats but no
                new events. When the watcher fires, ``_state["stall"]`` is
                set and this generator yields the error frame on its next
                iteration.
                """
                coalesce = _CoalesceTimer(
                    engine=self._engine, message_id=msg_id, state=_state
                )
                accumulated_content: str = ""
                accumulated_reasoning: str = ""
                # Persisted to messages.tool_calls at finalize (FE ToolCall
                # shape) so cards survive reload.
                accumulated_tool_calls: list[dict[str, object]] = []
                # THIS-turn-only count (not the cross-turn _tool_round_counts
                # LRU); exceeding _MAX_TOOL_ROUNDS_PER_TURN cuts a runaway
                # loop (see the cap block below).
                turn_tool_rounds: int = 0
                # Consecutive identical (name+args) tool calls; any different
                # call resets the streak, so varied research never trips it —
                # only a model stuck re-issuing the same call does.
                last_tool_sig: str | None = None
                consecutive_identical_rounds: int = 0
                # Fast cut via the client's own repeat-warning / failure-
                # streak-warning events, which set _early_loop_cut_reason and
                # fall through to the shared loop-cut block below.
                _repeat_warn_counts: dict[tuple[str, str], int] = {}
                _early_loop_cut_reason: str | None = None
                # Guards a single grammar-degrade tool-less retry — local
                # (not _state) since the retry runs entirely within this
                # generator's own iteration.
                _grammar_degraded: bool = False
                # Once any content-bearing frame is forwarded, a grammar
                # error can no longer be swallowed + retried.
                _content_emitted: bool = False

                try:
                    # Races `anext(aiter_iter)` against `stall_event` so a
                    # stall yields the error frame from inside the generator
                    # body (no cross-task raise, no TaskGroup leak). On a
                    # grammar-degrade retry, aiter_iter is rebound to a fresh
                    # upstream and this same loop re-runs against it, writing
                    # into the SAME draft/msg_id.
                    aiter_iter = aiter(upstream_iter)

                    async def _next_upstream_event() -> CanonicalEvent:
                        """Coroutine wrapper (pyright won't accept bare anext()).

                        Reads the current ``aiter_iter`` binding at call time,
                        so a grammar-degrade retry rebind is picked up
                        transparently on the next iteration.
                        """
                        return await anext(aiter_iter)

                    while True:
                        anext_task = asyncio.create_task(
                            _next_upstream_event(),
                            name=f"persist_anext_{msg_id}",
                        )
                        stall_task = asyncio.create_task(
                            stall_event.wait(),
                            name=f"persist_stall_wait_{msg_id}",
                        )
                        done_set, pending = await asyncio.wait(
                            [anext_task, stall_task],
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        # Cancel + drain whichever didn't fire so we don't leak
                        # a pending Task into the surrounding TaskGroup.
                        for t in pending:
                            t.cancel()
                            with suppress(asyncio.CancelledError):
                                await t

                        if stall_task in done_set and not _state["stall_handled"]:
                            idle_s_raw = _state["stall"]
                            idle_s = (
                                float(idle_s_raw) if isinstance(idle_s_raw, (int, float)) else 0.0
                            )
                            log.warning(
                                "stream.stall_handled_in_body",
                                msg_id=msg_id,
                                idle_s=round(idle_s, 1),
                            )
                            STREAMS_FAILED.labels(reason="upstream_stall").inc()
                            yield _format_error_frame(
                                code="upstream_stall",
                                detail=(
                                    f"No content-bearing event for {int(idle_s)}s — "
                                    "the model is likely overwhelmed, the context "
                                    "is full, or the upstream connection is stuck. "
                                    "Try sending again with fewer tools enabled."
                                ),
                                msg_id=msg_id,
                            )
                            _state["stall_handled"] = True
                            return

                        try:
                            event = anext_task.result()
                        except StopAsyncIteration:
                            # LM Studio's native surface collapses upstream
                            # failures into a bare chat.start + close, so a
                            # grammar-parse rejection surfaces HERE, not as a
                            # yielded error. Probe (non-streaming re-issue) to
                            # recover the real message.
                            log.warning(
                                "stream.generator_exhausted_without_terminal",
                                msg_id=msg_id,
                            )
                            _probed_detail: str | None = None
                            # probe_for_error is LM Studio-specific; skip in replay.
                            if _context_mode == "chain":
                                try:
                                    _probed_detail = await self._lm_client.probe_for_error(
                                        wire_payload
                                    )
                                    if _probed_detail is not None:
                                        log.info(
                                            "stream.upstream_error_probed",
                                            msg_id=msg_id,
                                            detail=_probed_detail[:200],
                                        )
                                except Exception as _probe_exc:  # noqa: BLE001
                                    log.warning(
                                        "stream.upstream_error_probe_failed",
                                        msg_id=msg_id,
                                        error=str(_probe_exc),
                                    )

                            if _grammar_degrade_eligible(
                                is_native_path=_context_mode == "chain",
                                has_integrations=bool(_original_integrations),
                                content_emitted=_content_emitted,
                                already_degraded=_grammar_degraded,
                                error_detail=_probed_detail or "",
                            ):
                                _grammar_degraded = True
                                async for _w in self._emit_grammar_degrade_warning(
                                    original_integrations=_original_integrations,
                                    detail=_probed_detail or "",
                                    msg_id=msg_id,
                                    chat_id=chat_id,
                                    model_id=model_id,
                                ):
                                    yield _w
                                aiter_iter = aiter(_build_toolless_retry_iter())
                                continue  # aiter_iter now points at the tool-less retry

                            STREAMS_FAILED.labels(reason="upstream_error").inc()
                            yield _format_error_frame(
                                code="upstream_error",
                                detail=(
                                    _probed_detail
                                    or "Upstream stream ended without chat.end event."
                                ),
                                msg_id=msg_id,
                            )
                            return

                        # Polled by _watch_disconnect every 500ms tick;
                        # heartbeats reset it too so a slow-but-alive local
                        # model isn't aborted mid-processing.
                        if (
                            event.type in _CONTENT_BEARING
                            or event.type in _KEEPALIVE_HEARTBEAT
                        ):
                            _state["last_content_ts"] = monotonic()

                        # Once content has flowed, a later error can no
                        # longer be swallowed and retried tool-less.
                        if event.type in _CONTENT_EMITTED_EVENTS:
                            _content_emitted = True

                        if event.type == "message.delta" and event.content:
                            coalesce.add(event.content)
                            accumulated_content += event.content
                            # Mirrored into _state so the teardown salvage can
                            # recover a partial answer on a NON-graceful
                            # terminal instead of finalizing an empty bubble.
                            _state["acc_content"] = accumulated_content
                            if coalesce.should_flush():
                                await coalesce.flush()
                        elif event.type == "reasoning.delta" and event.content:
                            # The answer text itself is never coalesced
                            # incrementally (not shown live) — but the
                            # reasoning value now IS periodically persisted,
                            # via the SAME _CoalesceTimer, so a kill during a
                            # pure-reasoning phase (no content yet — the only
                            # case message.delta's flush() trigger below
                            # never reaches) doesn't lose it. Mirrored into
                            # _state first so a non-graceful terminal's
                            # salvage also sees the freshest value.
                            accumulated_reasoning += event.content
                            _state["acc_reasoning"] = accumulated_reasoning
                            if coalesce.should_flush():
                                await coalesce.flush()

                        if event.type == "chat.end" and event.response_id:
                            _state["response_id"] = event.response_id

                        # Persisted by _finalize_message so the Continue chip
                        # survives reload.
                        if event.type == "chat.end" and event.stop_reason:
                            _state["stop_reason"] = event.stop_reason

                        (
                            turn_tool_rounds,
                            last_tool_sig,
                            consecutive_identical_rounds,
                        ) = self._apply_tool_call_delta(
                            event=event,
                            chat_id=chat_id,
                            state=_state,
                            accumulated_tool_calls=accumulated_tool_calls,
                            turn_tool_rounds=turn_tool_rounds,
                            last_tool_sig=last_tool_sig,
                            consecutive_identical_rounds=consecutive_identical_rounds,
                        )

                        # Touch last_activity_at so the reaper's inactivity
                        # threshold doesn't fire mid tool-chain.
                        if event.type in (
                            "tool_call.start",
                            "tool_call.name",
                            "tool_call.arguments",
                            "tool_call.success",
                            "tool_call.failure",
                        ):
                            await coalesce.touch_activity()

                        # Reset so the next retry starts a fresh MTP-detection
                        # window instead of the predicate firing on round 1 of
                        # every subsequent attempt (FE dedupe then suppresses
                        # it silently — the banner still shows once per tab).
                        if (
                            event.type == "error"
                            and event.error is not None
                            and event.error.get("code") == "mtp_suspected"
                        ):
                            self.reset_counter(chat_id)

                        # Translate the provider's cryptic "no endpoints
                        # support tool use" 404 into an actionable message —
                        # cloud models otherwise surface a raw OpenRouter
                        # error (the non-tool-model drop only runs on LM
                        # Studio). Reproduced with gpt-4o-mini-search-preview.
                        if (
                            event.type == "error"
                            and event.error is not None
                            and "support tool use" in str(event.error.get("message", "")).lower()
                        ):
                            event = event.model_copy(
                                update={
                                    "error": {
                                        **event.error,
                                        "message": (
                                            "This model doesn't support tools. Turn "
                                            "off integrations for this chat, or pick "
                                            "a tool-capable model."
                                        ),
                                    }
                                }
                            )

                        # Belt-and-suspenders: the native path normally
                        # surfaces a grammar rejection via the exhausted-
                        # without-terminal probe above, not as an error event
                        # — but degrade here too in case a future LM Studio
                        # build emits it early. Placed BEFORE the SSE yield so
                        # a degrading error never reaches the client.
                        if event.type == "error":
                            _err_msg = str((event.error or {}).get("message", ""))
                            if _grammar_degrade_eligible(
                                is_native_path=_context_mode == "chain",
                                has_integrations=bool(_original_integrations),
                                content_emitted=_content_emitted,
                                already_degraded=_grammar_degraded,
                                error_detail=_err_msg,
                            ):
                                _grammar_degraded = True
                                async for _w in self._emit_grammar_degrade_warning(
                                    original_integrations=_original_integrations,
                                    detail=_err_msg,
                                    msg_id=msg_id,
                                    chat_id=chat_id,
                                    model_id=model_id,
                                ):
                                    yield _w
                                aiter_iter = aiter(_build_toolless_retry_iter())
                                continue  # aiter_iter now points at the tool-less retry

                        yield _format_sse_frame(event, msg_id=msg_id)

                        if event.type == "message.end":
                            await coalesce.flush()

                        # Fast loop-cut via streaming-client advisory events,
                        # acted on after the SSE frame is yielded so the FE
                        # sees the warning before we abort.
                        _early_loop_cut_reason = self._track_loop_cut_signals(
                            event=event,
                            repeat_warn_counts=_repeat_warn_counts,
                            early_cut_reason=_early_loop_cut_reason,
                            msg_id=msg_id,
                            repeat_warning_cut_k=_repeat_warning_cut_k,
                        )

                        # Per-turn tool-loop cap: LM Studio drives the MCP
                        # loop natively for local models and never stops on
                        # its own if the model keeps re-deciding to call
                        # tools. Once rounds exceed the cap, abort upstream
                        # and finalize with whatever's written — never a
                        # silent empty death. A tools-off re-synthesis is NOT
                        # attempted here (unlike the cloud agentic path,
                        # which owns working_history): the gathered tool
                        # results live in LM Studio's server-side state,
                        # which only becomes a chainable response_id once
                        # THIS turn completes with a chat.end that never
                        # arrived, so there's no usable anchor to resynthesize
                        # against.
                        #
                        # See _decide_loop_cut / _LoopCutDecision for the
                        # early client-advisory cut, the consecutive-identical
                        # backstop, and the per-turn backstop.
                        _loop_cut_decision = _decide_loop_cut(
                            early_cut_reason=_early_loop_cut_reason,
                            event_type=event.type,
                            consecutive_identical_rounds=consecutive_identical_rounds,
                            turn_tool_rounds=turn_tool_rounds,
                        )
                        _loop_cut = _loop_cut_decision.should_cut
                        if _loop_cut:
                            async for _f in self._finalize_tool_loop_cut(
                                loop_cut_decision=_loop_cut_decision,
                                msg_id=msg_id,
                                chat_id=chat_id,
                                model_id=model_id,
                                user_id=user_id,
                                user_text=user_text,
                                chat_project_id=chat_project_id,
                                turn_tool_rounds=turn_tool_rounds,
                                accumulated_content=accumulated_content,
                                accumulated_reasoning=accumulated_reasoning,
                                accumulated_tool_calls=accumulated_tool_calls,
                                aiter_iter=aiter_iter,
                                coalesce=coalesce,
                                state=_state,
                            ):
                                yield _f
                            return

                        if event.type == "chat.end":
                            # Silence the watcher before the epilogue below
                            # (finalize + OOB followups + memory-distill
                            # wait), mirroring the tool-loop-cap exit — a slow
                            # followups/distill wait on an already-finished
                            # turn can otherwise outlast the idle timeout and
                            # trigger a spurious stall.
                            _state["done"] = True
                            _state["stall_handled"] = True

                            await coalesce.flush()

                            # XML tool-call recovery: Qwen3-Coder-derived
                            # reasoning models emit tool calls as XML inside
                            # message.delta content when LM Studio's native
                            # parser misses them, leaking the wrapper text
                            # into displayed content. Strip + log so the admin
                            # sees the attempt (not executed — re-issuing is
                            # out of the single-turn chat contract). Returns
                            # None when no XML wrapper is present.
                            from lmchat.services.tool_args import recover_xml_tool_calls

                            _xml_recovery = recover_xml_tool_calls(accumulated_content)
                            if _xml_recovery is not None:
                                _recovered_calls, _cleaned = _xml_recovery
                                log.warning(
                                    "stream.xml_tool_calls_leaked",
                                    msg_id=msg_id,
                                    chat_id=chat_id,
                                    model_id=model_id,
                                    recovered_count=len(_recovered_calls),
                                    recovered_names=[
                                        c["function"]["name"] for c in _recovered_calls
                                    ],
                                    stripped_chars=len(accumulated_content) - len(_cleaned),
                                )
                                accumulated_content = _cleaned
                                # Persist recovered calls into the FE ToolCall
                                # shape so the card renders instead of the
                                # message vanishing (status="failure" + a
                                # diagnostic result — otherwise an XML-only
                                # turn persisted as empty content).
                                _xml_recovery_note = (
                                    "Recovered from leaked XML — the model"
                                    " emitted this tool call inside message"
                                    " content. Not executed: single-turn chat"
                                    " contract doesn't re-issue the request."
                                )
                                for _rc in _recovered_calls:
                                    accumulated_tool_calls.append(
                                        {
                                            "id": str(_rc["id"]),
                                            "name": str(_rc["function"]["name"]),
                                            "arguments": str(_rc["function"]["arguments"]),
                                            "status": "failure",
                                            "result": _xml_recovery_note,
                                        }
                                    )

                            # Passing tool activity lets resolve_terminal_content
                            # emit an actionable message when tools failed with
                            # no answer, reserving the reasoning-surface for the
                            # no-tools parked-answer case it was designed for.
                            _terminal = resolve_terminal_content(
                                accumulated_content,
                                accumulated_reasoning,
                                had_tool_calls=bool(accumulated_tool_calls)
                                or turn_tool_rounds > 0,
                                tool_rounds=turn_tool_rounds,
                            )
                            if _terminal.kind == "graceful":
                                log.warning(
                                    "stream.tool_turn_no_final_answer",
                                    msg_id=msg_id,
                                    chat_id=chat_id,
                                    tool_rounds=turn_tool_rounds,
                                    reasoning_len=len(accumulated_reasoning),
                                )
                                STREAMS_SALVAGED.labels(reason="tool_turn_graceful").inc()
                            elif _terminal.kind == "salvaged":
                                log.warning(
                                    "stream.content_starvation_salvaged",
                                    msg_id=msg_id,
                                    chat_id=chat_id,
                                    reasoning_len=len(accumulated_reasoning),
                                    base_len=len(accumulated_content),
                                )
                                STREAMS_SALVAGED.labels(reason="substance_fold_applied").inc()
                            accumulated_content = _terminal.content
                            accumulated_reasoning = _terminal.reasoning or ""

                            _resp_id = _state["response_id"]
                            assert _resp_id is None or isinstance(_resp_id, str), (
                                "response_id must be str | None"
                            )
                            _stop_reason = _state["stop_reason"]
                            assert _stop_reason is None or isinstance(_stop_reason, str), (
                                "stop_reason must be str | None"
                            )
                            final_ok = await self._finalize_message(
                                msg_id=msg_id,
                                response_id=_resp_id,
                                final_content=accumulated_content,
                                final_reasoning=accumulated_reasoning,
                                stop_reason=_stop_reason,
                                # NULL (not []) when no tool calls ran.
                                tool_calls=accumulated_tool_calls or None,
                            )
                            if final_ok:
                                _distill_task = self._fire_post_finalize_background(
                                    msg_id=msg_id,
                                    chat_id=chat_id,
                                    user_id=user_id,
                                    model_id=model_id,
                                    content=accumulated_content,
                                    user_text=user_text,
                                    project_id=chat_project_id,
                                    with_distill_and_summary=True,
                                )
                                log.info(
                                    "stream.completed",
                                    msg_id=msg_id,
                                    chat_id=chat_id,
                                    response_id=_resp_id,
                                )
                                await self._write_audit(
                                    event="stream.completed",
                                    user_id=user_id,
                                    detail={
                                        "msg_id": msg_id,
                                        "chat_id": chat_id,
                                        "response_id": _resp_id,
                                    },
                                )
                                # Out-of-band followups: main answer + chat.end
                                # already streamed; a separate lightweight call
                                # yields the followups frame so the FE renders
                                # chips without delaying the visible answer.
                                if _settings.lm_chat_followups_enabled:
                                    _followups_conv: list[dict] = []  # type: ignore[type-arg]
                                    _current_user_text = " ".join(
                                        block.content or ""
                                        for block in payload.payload.input
                                        if block.type == "text" and block.content
                                    ).strip()
                                    if _current_user_text:
                                        _followups_conv.append(
                                            {"role": "user", "content": _current_user_text}
                                        )
                                    _followups_model_key = await resolve_background_model_id(
                                        engine=self._engine,
                                        models_service=self._models_service,
                                        chat_model_id=model_id,
                                    )
                                    # Resolve catalog key to live wire-id
                                    # (bare key 400s).
                                    if self._models_service is not None:
                                        _ms = self._models_service
                                        _fu_res = await _ms.resolve_to_loaded_or_fallback(
                                            _followups_model_key
                                        )
                                        _followups_wire_id = _fu_res.wire_id
                                    else:
                                        _followups_wire_id = _followups_model_key
                                    if _followups_wire_id is None:
                                        _followups_list = []  # nothing loaded
                                    else:
                                        _followups_list = await _generate_followups_oob(
                                            lm_client=self._lm_client,
                                            model=_followups_wire_id,
                                            conversation_messages=_followups_conv,
                                            assistant_answer=accumulated_content,
                                            timeout_sec=self._aux_model_timeout_sec,
                                        )
                                    log.debug(
                                        "stream.followups_oob_done",
                                        msg_id=msg_id,
                                        count=len(_followups_list),
                                    )
                                    yield _format_followups_frame(
                                        followups=_followups_list,
                                        msg_id=msg_id,
                                    )
                                # Out-of-band C3 mode adoption: ask a
                                # separate lightweight call which role
                                # preset (if any) the NEXT turn should run
                                # under, using this just-finished exchange
                                # as evidence. Computed independently of
                                # the followups block above (own flag, own
                                # resolution) so either feature can be
                                # toggled without affecting the other. See
                                # _infer_mode_oob's docstring for why this
                                # MUST stay out-of-band rather than an
                                # inline directive on the main prompt.
                                if _settings.lm_chat_mode_adoption_enabled:
                                    _mode_conv: list[dict] = []  # type: ignore[type-arg]
                                    _mode_user_text = " ".join(
                                        block.content or ""
                                        for block in payload.payload.input
                                        if block.type == "text" and block.content
                                    ).strip()
                                    if _mode_user_text:
                                        _mode_conv.append(
                                            {"role": "user", "content": _mode_user_text}
                                        )
                                    _mode_model_key = await resolve_background_model_id(
                                        engine=self._engine,
                                        models_service=self._models_service,
                                        chat_model_id=model_id,
                                    )
                                    # Resolve catalog key to live wire-id
                                    # (bare key 400s) — same as followups.
                                    if self._models_service is not None:
                                        _mode_ms = self._models_service
                                        _mode_res = await _mode_ms.resolve_to_loaded_or_fallback(
                                            _mode_model_key
                                        )
                                        _mode_wire_id = _mode_res.wire_id
                                    else:
                                        _mode_wire_id = _mode_model_key
                                    if _mode_wire_id is None:
                                        _adopted_preset_id = None  # nothing loaded
                                    else:
                                        _adopted_preset_id = await _infer_mode_oob(
                                            lm_client=self._lm_client,
                                            model=_mode_wire_id,
                                            conversation_messages=_mode_conv,
                                            assistant_answer=accumulated_content,
                                            timeout_sec=self._aux_model_timeout_sec,
                                        )
                                    log.debug(
                                        "stream.mode_adopt_oob_done",
                                        msg_id=msg_id,
                                        preset_id=_adopted_preset_id,
                                    )
                                    yield _format_mode_adopt_frame(
                                        preset_id=_adopted_preset_id,
                                        msg_id=msg_id,
                                    )
                                # Auto-memory saved indicator (independent
                                # quiet signal). asyncio.shield is mandatory —
                                # without it wait_for's timeout would CANCEL
                                # the detached distillation task and the fact
                                # would be lost; with shield, only our wait
                                # gives up and the task keeps running.
                                if _distill_task is not None:
                                    try:
                                        _saved_count = await asyncio.wait_for(
                                            asyncio.shield(_distill_task),
                                            timeout=_MEMORY_SAVED_FRAME_WAIT_SEC,
                                        )
                                    except (TimeoutError, Exception):  # noqa: BLE001
                                        # Shielded task keeps running (and
                                        # still stores) — no inline frame this turn.
                                        _saved_count = 0
                                    if _saved_count > 0:
                                        yield _format_memory_saved_frame(
                                            count=_saved_count, msg_id=msg_id
                                        )
                            return

                        if event.type == "error":
                            err = event.error or {}
                            err_code = err.get("code", "upstream_error")
                            log.error(
                                "stream.upstream_error",
                                msg_id=msg_id,
                                error_code=err_code,
                                error_message=err.get("message"),
                            )
                            # Closed enum of reasons for the metric label.
                            if err_code == "upstream_timeout":
                                reason = "upstream_timeout"
                            elif err_code == "tool_call_malformed":
                                reason = "malformed_tool_call"
                            elif err_code == "upstream_stall":
                                reason = "upstream_stall"
                            elif err_code == "upstream_unavailable":
                                reason = "upstream_unavailable"
                            elif err_code == "upstream_stream_error":
                                reason = "upstream_stream_error"
                            elif err_code == "400":
                                reason = "upstream_400"
                            else:
                                reason = "upstream_error"
                            STREAMS_FAILED.labels(reason=reason).inc()
                            await self._write_audit(
                                event="stream.upstream_error",
                                user_id=user_id,
                                detail={"msg_id": msg_id, "error": err},
                            )
                            return

                    # The "upstream exhausted without chat.end/error" case is
                    # handled inline in the StopAsyncIteration branch above
                    # (probe + grammar-degrade-or-error), which either
                    # returns or continues — the loop never falls through to
                    # here; reaching this point would be a logic bug.

                except asyncio.CancelledError:
                    log.info("stream.persist_cancelled", msg_id=msg_id)
                    raise

            # This inner try/except* converts the TaskGroup's ExceptionGroups
            # into linear control flow; the single OUTER finally owns the
            # gauge/draft/marker teardown for every exit. mark_inactive uses
            # set.discard, so the teardown is idempotent regardless of which
            # exit fired.
            _stall_exc: _StreamStall | None = None
            try:
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(
                        _watch_disconnect(request, msg_id),
                        name=f"disconnect_watcher_{msg_id}",
                    )
                    async for frame in _run_persist_and_yield():
                        yield frame
                    _state["done"] = True
                    raise _StreamDone()
            except* _StreamDone:
                pass  # Normal completion — TaskGroup cancelled the watcher.
            except* _StreamStall as eg:
                # Emit the error frame outside the generator, after TaskGroup
                # cleanup is done.
                for exc in eg.exceptions:
                    if isinstance(exc, _StreamStall) and _stall_exc is None:
                        _stall_exc = exc
            except* GeneratorExit:
                # A consumer that stops iterating (Starlette exhausting the
                # response on client disconnect, or a test aclose()) throws
                # GeneratorExit into whichever `yield` is suspended INSIDE
                # the TaskGroup — asyncio.TaskGroup wraps it into a
                # BaseExceptionGroup like any other body exception (it is
                # not a CancelledError special case). GeneratorExit is a
                # BaseException, not an Exception subclass, so without this
                # clause it falls through `except* _StreamStall` and
                # `except* Exception` unmatched and re-propagates AS a
                # BaseExceptionGroup — which violates the async-generator
                # close() contract (a generator must exit via GeneratorExit,
                # StopAsyncIteration, or a genuine new exception, never a
                # wrapping group) and surfaces as a confusing crash on
                # teardown instead of a clean close. Swallowing it here lets
                # execution fall through to the finally below (the same
                # gauge-dec / draft-release-or-abort / salvage / mark_inactive
                # teardown every other exit already runs) and then return
                # normally — the "clean up, then close" idiom
                # `except GeneratorExit: ...` (no re-raise) is for. Mirrors
                # `_sub_session_sse`'s identical fix (routes/chats.py,
                # 1243294) — this path shares the same TaskGroup shape and
                # was missing the equivalent handler.
                pass
            except* Exception as eg:
                # Re-raise the first non-StreamDone, non-stall exception so
                # the caller (StreamingResponse) surfaces it correctly.
                for exc in eg.exceptions:
                    if not isinstance(exc, (_StreamDone, _StreamStall)):
                        raise exc from None

            # Emitted after TaskGroup cleanup (upstream generator cancelled,
            # connection clean). Teardown is owned by the finally below.
            if _stall_exc is not None:
                STREAMS_FAILED.labels(reason="upstream_stall").inc()
                yield _format_error_frame(
                    code="upstream_stall",
                    detail=f"No content-bearing event for {int(_stall_exc.idle_s)}s.",
                    msg_id=msg_id,
                )
        finally:
            # SINGLE guaranteed-once teardown for the three coupled lifecycle
            # primitives established before the try (STREAMS_ACTIVE gauge,
            # draft row, active-stream marker) — everything after the lock is
            # inside the one try above, so this runs for ALL exits including
            # GeneratorExit. `await` in a generator finally is safe here (it
            # runs on aclose, as the rest of this method already relies on).
            #
            # The gauge decrement below is the ONLY one in the method, so the
            # inc before the try is balanced exactly once. The draft release
            # is idempotent (acts only on state='draft') — a no-op once
            # _finalize_message or a disconnect path already moved the row.
            STREAMS_ACTIVE.dec()
            # shield(): when the browser closes mid-stream, Starlette
            # aclose()s this generator and the TaskGroup cancels sibling
            # tasks; a bare await here would get cancelled mid-flight BEFORE
            # the UPDATE commits, leaving the row stuck in 'draft' (resend
            # 409s). shield() detaches the inner coroutine so the DB UPDATE
            # runs to completion regardless — the outer await may still
            # raise CancelledError (caught below) but the row is released by
            # then. The gauge dec + mark_inactive stay un-shielded since they
            # don't await and can't be cancelled.
            # Read once, outside the try below, so both salvage calls in this
            # finally (the draft-release AND the aborted-row backfill) see the
            # same snapshot and neither reads a possibly-unbound name if the
            # first one raises something other than CancelledError.
            _acc_content = _state.get("acc_content")
            _acc_reasoning = _state.get("acc_reasoning")
            _acc_tools = _state.get("acc_tool_calls")
            _acc_rounds = _state.get("acc_tool_rounds", 0)
            # The CAUSE of this teardown decides the target state, not a race
            # between two writers that both only check WHERE state='draft'.
            # Before this fix, _release_stuck_draft (FINAL) and the
            # watcher's own safe_abort_draft (ABORTED_BY_CLIENT) raced on
            # every disconnect, so a turn the user walked away from could
            # settle as either — indistinguishable from a genuinely
            # completed turn when FINAL won (live-dogfood-confirmed,
            # 2026-08-14 J11, same run: one scenario landed FINAL, the other
            # ABORTED_BY_CLIENT, for the identical disconnect mechanism).
            # `_state["disconnected"]` is set by the watcher BEFORE its own
            # await (see _watch_disconnect), so it can't be lost to that
            # race even when this finally reaches the row first.
            _disconnected = bool(_state.get("disconnected"))
            if _disconnected:
                # Force ABORTED_BY_CLIENT regardless of which writer gets
                # there first — a no-op if the watcher's own safe_abort_draft
                # already won (WHERE state='draft' matches 0 rows either
                # way). aborted_by_client was ALREADY a possible outcome of
                # every disconnect (whenever the watcher won); this removes
                # the coin flip on which of the two outcomes a given
                # disconnect lands on, it introduces no new state to any
                # downstream reader.
                try:
                    await asyncio.shield(
                        safe_abort_draft(engine=self._engine, message_id=msg_id)
                    )
                except asyncio.CancelledError:
                    pass
            else:
                try:
                    # Salvages the buffer the persist generator mirrored into
                    # _state: a no-op on a graceful terminal (row already
                    # FINAL, WHERE state='draft' matches 0 rows); persists
                    # the partial answer + folded reasoning on a genuine
                    # terminal error/stall/loop-cap — those keep today's
                    # FINAL-with-salvage behavior unchanged. Disconnects
                    # never reach this branch (see the `if` above).
                    await asyncio.shield(
                        self._release_stuck_draft(
                            msg_id=msg_id,
                            chat_id=chat_id,
                            reason="stream_lifecycle_teardown",
                            salvage_content=(
                                _acc_content if isinstance(_acc_content, str) else None
                            ),
                            salvage_reasoning=(
                                _acc_reasoning
                                if isinstance(_acc_reasoning, str)
                                else None
                            ),
                            salvage_tool_calls=(
                                _acc_tools if isinstance(_acc_tools, list) else None
                            ),
                            had_tool_calls=bool(_acc_tools) or bool(_acc_rounds),
                            tool_rounds=(
                                _acc_rounds if isinstance(_acc_rounds, int) else 0
                            ),
                        )
                    )
                except asyncio.CancelledError:
                    # The shielded release still ran to completion; only the
                    # outer await was cancelled. Swallow — no caller is left
                    # to propagate to in this terminal teardown.
                    pass

            # Disconnect salvage: backfills the full accumulated
            # content/reasoning/tool_calls onto an ABORTED_BY_CLIENT row —
            # whether it got there via the watcher's own safe_abort_draft or
            # the explicit abort in the branch above. Otherwise a reloaded
            # disconnected chat loses reasoning + tool_calls, keeping only
            # whatever the (state-guarded) coalesce flush persisted before
            # the abort (strong seat, P2 review 2026-08-12, caught this for
            # sub-sessions via _salvage_aborted_row; the main chat shared the
            # same gap — see the disconnect dogfood, 2026-08-14). No-op on
            # any non-aborted row (WHERE state='aborted_by_client') — the
            # `else` branch above never produces one.
            try:
                await asyncio.shield(
                    _salvage_aborted_row(
                        self._engine,
                        msg_id,
                        content=(
                            _acc_content if isinstance(_acc_content, str) else None
                        ),
                        reasoning=(
                            _acc_reasoning if isinstance(_acc_reasoning, str) else None
                        ),
                        tool_calls=(
                            _acc_tools if isinstance(_acc_tools, list) else None
                        ),
                    )
                )
            except asyncio.CancelledError:
                pass
            mark_inactive(chat_id)
