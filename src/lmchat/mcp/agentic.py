# SPDX-License-Identifier: Apache-2.0
"""Agentic MCP tool-execution loop for cloud providers.

``AgenticMcpProvider`` wraps any replay-mode ``ChatProvider`` and an
``McpHost``.  When the inner provider requests tool calls the backend
executes them via the host, injects the results into history, and
re-issues the turn — up to ``max_rounds`` times.

LM Studio path is never touched: ``AgenticMcpProvider`` is only
constructed at dispatch sites when the provider is cloud AND integrations
are present.  When integrations are absent the plain provider is used
directly (byte-identical to today).

Synthesised tool events use a uuid4 threaded through
.start → .name → .arguments → .success, matching the shapes
native.py emits so StreamingService._accumulate_tool_call
and the FE render unchanged.

Events stream incrementally across rounds — never batched.

Cancellation propagates naturally through the async generator;
in-flight ``call_tool`` calls are abandoned (the host has its own
per-call timeout).
"""
from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

from lmchat.lmstudio.types import (
    CanonicalChatRequest,
    CanonicalEvent,
    CanonicalMessage,
    CanonicalTool,
    CanonicalToolCall,
)
from lmchat.logging import get_logger
from lmchat.mcp.host import McpHost
from lmchat.providers.base import ChatProvider
from lmchat.services.builtin_tools import BuiltinToolContext, BuiltinToolRegistry

log = get_logger(__name__)

# Maximum agentic rounds when none specified. This is a PATHOLOGICAL-LOOP
# BACKSTOP, not a research limiter — a real turn that searches, reads several
# sources and cross-checks them runs many rounds legitimately, and cutting it
# short produces a worse answer, not a safer one. Kept in step with
# ``streaming_service._MAX_TOOL_ROUNDS_PER_TURN`` (the equivalent cap on the
# local/LM Studio path) so the two paths do not silently disagree about how
# much work a turn is allowed to do. For reference, Open WebUI ships 256.
# Was 8 from v1.0 until 2026-08-16 — indefensibly low, and the only one of the
# four tool-loop caps with no override at all. It now takes an env override in
# the same shape as its three siblings (``_MAX_TOOL_ROUNDS_PER_TURN``,
# ``_MAX_IDENTICAL_TOOL_ROUNDS``, ``_REPEAT_WARNING_CUT_K``), so all four are
# tunable the same way.
_DEFAULT_MAX_ROUNDS = int(os.getenv("LM_CHAT_MAX_AGENTIC_ROUNDS", "256"))

# Warning message when the loop cap is reached.
_MAX_ROUNDS_WARNING = (
    "Agentic tool loop reached the maximum number of rounds "
    "without a natural stop. The model may need more rounds or "
    "the tools may be cycling."
)


def _integrations_to_server_ids(integrations: list[str]) -> list[str]:
    """Map ``integrations`` items to McpHost server IDs.

    The ``integrations`` list carries ids like ``"mcp/searxng"`` or
    ``"mcp/context7"``.  The server_id registered in ``McpHost`` is the
    slug after the slash (``"searxng"``, ``"context7"``).

    Non-mcp entries (e.g. native LM Studio integrations) are silently
    ignored — they are irrelevant for the cloud agentic path.
    """
    ids: list[str] = []
    for entry in integrations:
        if entry.startswith("mcp/"):
            slug = entry[len("mcp/"):]
            if slug:
                ids.append(slug)
    return ids


class AgenticMcpProvider:
    """Replay-mode ChatProvider wrapper that runs the agentic MCP loop.

    Wraps an inner cloud ``ChatProvider`` + ``McpHost``.  Each call to
    ``stream_chat`` advertises the discovered MCP tools to the model,
    streams events live, intercepts the premature ``tool_call.success``
    terminator emitted by the compat decoder on finish_reason="tool_calls"
    (this event carries NO output — the model only requested the call),
    suppresses it, executes each call via the host, then yields the REAL
    ``tool_call.success`` carrying the result — and re-issues with updated
    history.

    Satisfies the ``ChatProvider`` Protocol so ``LmstudioStreamingClient``
    wraps it transparently and the FE sees identical canonical events.
    """

    #: ChatProvider protocol attribute — always replay for cloud agentic loop.
    context_mode: str = "replay"

    def __init__(
        self,
        inner: ChatProvider,
        mcp_host: McpHost,
        server_ids: list[str],
        max_rounds: int = _DEFAULT_MAX_ROUNDS,
        denied_tools: set[str] | None = None,
        builtin_registry: BuiltinToolRegistry | None = None,
        builtin_ctx: BuiltinToolContext | None = None,
    ) -> None:
        self._inner = inner
        self._mcp_host = mcp_host
        self._server_ids = server_ids
        self._max_rounds = max_rounds
        self._denied_tools = denied_tools or set()
        # Additive, off-by-default: app-executed tools (e.g. web_search) that
        # run in-process instead of proxying through McpHost. When both stay
        # None — every call site today — the MCP-only path is untouched.
        self._builtin_registry = builtin_registry
        # Normalize to a concrete context so the executor call site below
        # never passes None (BuiltinToolExecutor's ctx param isn't Optional).
        # Unused whenever builtin_registry is None.
        self._builtin_ctx = builtin_ctx if builtin_ctx is not None else BuiltinToolContext()
        # Mirror the inner provider's name with a suffix so logs are distinct.
        self.name = f"{inner.name}+mcp"

    async def stream_chat(  # type: ignore[override]
        self,
        request: CanonicalChatRequest,
        *,
        history: list[CanonicalMessage] | None,
        tools: list[CanonicalTool] | None = None,
        cumulative_tool_rounds: int = 0,
    ) -> AsyncIterator[CanonicalEvent]:
        """Run the agentic loop and yield CanonicalEvents.

        The method is itself an async generator so cancellation and
        backpressure propagate naturally through the yield points.

        Advertises MCP tools to the inner provider on every round.
        Intercepts and suppresses the premature ``tool_call.success``
        (no output), executes calls via the host, then yields the real
        ``tool_call.success`` carrying the result.
        Appends the assistant tool-calls message + tool-result messages
        to working_history before re-issuing.
        """
        working_history: list[CanonicalMessage] = list(history or [])

        # Connect the needed servers BEFORE advertising tools.  This is the
        # single authoritative connect point (the dispatch sites no longer
        # fire-and-forget connect()).  connect() is idempotent — it returns
        # instantly when the server is already in the pool, so warm runs add
        # ~0 latency.  A cold first run waits the few seconds needed to spawn
        # the npx/uvx servers, which is correct: tools can't be advertised
        # before they're discovered.  Lazy / per-need — we never connect_all.
        # A failed connect is non-fatal (a down server just contributes no
        # tools); connect() catches its own errors and returns False, and
        # return_exceptions=True guards against any unexpected raise.
        if self._server_ids:
            _results = await asyncio.gather(
                *[self._mcp_host.connect(sid) for sid in self._server_ids],
                return_exceptions=True,
            )
            for sid, res in zip(self._server_ids, _results, strict=True):
                if isinstance(res, BaseException):
                    log.warning(
                        "agentic.loop.connect_error",
                        server_id=sid,
                        error=str(res),
                    )
                elif res is False:
                    log.warning(
                        "agentic.loop.connect_failed",
                        server_id=sid,
                    )

        # Discover MCP tools from the now-connected servers.
        mcp_tools: list[CanonicalTool] = self._mcp_host.list_tools(self._server_ids)
        # Filter out tools whose namespaced name is in the per-server denylist.
        # LM Studio path is never touched; this only runs for cloud agentic loops.
        if self._denied_tools:
            mcp_tools = [t for t in mcp_tools if t.name not in self._denied_tools]
        # Additive: builtin (app-executed) tools, off by default. Builtin
        # names are bare (e.g. "web_search"); MCP names are namespaced
        # "<server>_<tool>" so a collision isn't expected — but if one
        # occurs, builtin wins: the execute step below checks the builtin
        # registry BEFORE McpHost, so the advertised descriptor must match
        # what actually runs.
        builtin_tools: list[CanonicalTool] = (
            self._builtin_registry.tools() if self._builtin_registry is not None else []
        )
        if builtin_tools:
            builtin_names = {t.name for t in builtin_tools}
            mcp_tools = [t for t in mcp_tools if t.name not in builtin_names]
        # Merge with any caller-supplied tools (unused in practice for B3).
        effective_tools: list[CanonicalTool] = list(tools or []) + builtin_tools + mcp_tools

        log.info(
            "agentic.loop.start",
            provider=self._inner.name,
            server_ids=self._server_ids,
            tool_count=len(mcp_tools),
            max_rounds=self._max_rounds,
        )

        for round_num in range(self._max_rounds):
            req = request.model_copy(update={"tools": effective_tools})

            # State for accumulating tool calls during this round.
            # The compat decoder (decode_compat) emits events for potentially
            # multiple parallel tool calls via index-keyed buffers.  We track
            # the "current" call being built by .start → .name → .arguments;
            # each .start resets to a new call.
            _pending_id: str | None = None
            _pending_name: str | None = None
            _pending_args: dict[str, Any] | None = None
            # Finalized calls from prior .start/.name/.arguments sequences.
            _accumulated_calls: list[tuple[str, str, dict[str, Any]]] = []

            captured: list[CanonicalToolCall] = []
            saw_tool_finish = False

            async for ev in self._inner.stream_chat(
                req,
                history=working_history,
                tools=effective_tools,
                cumulative_tool_rounds=cumulative_tool_rounds + round_num,
            ):
                etype = ev.type

                if etype == "tool_call.start":
                    # Flush any in-progress call before starting the next one.
                    if _pending_name is not None:
                        _accumulated_calls.append((
                            _pending_id or str(uuid.uuid4()),
                            _pending_name,
                            _pending_args or {},
                        ))
                    # Initialise state for the new call.
                    tc = ev.tool_call
                    _pending_id = (tc.id if tc else None) or str(uuid.uuid4())
                    _pending_name = (tc.name if tc else None) or None
                    _pending_args = None
                    # Pass through live so the FE shows the call forming.
                    yield ev

                elif etype == "tool_call.name":
                    tc = ev.tool_call
                    if tc:
                        if tc.name:
                            _pending_name = tc.name
                        if tc.id:
                            _pending_id = tc.id
                    yield ev

                elif etype == "tool_call.arguments":
                    tc = ev.tool_call
                    if tc:
                        if isinstance(tc.arguments, dict):
                            _pending_args = tc.arguments
                        if tc.id:
                            _pending_id = tc.id
                    yield ev

                elif etype == "tool_call.success":
                    # PREMATURE terminator from decode_compat on
                    # finish_reason="tool_calls".  The compat decoder emits this
                    # with an EMPTY CanonicalToolCall (no output) — it only signals
                    # that the model requested calls.  SUPPRESS it; we will yield
                    # the real tool_call.success (with output) after execution.
                    #
                    # Flush the last in-progress call into accumulated state.
                    if _pending_name is not None:
                        _accumulated_calls.append((
                            _pending_id or str(uuid.uuid4()),
                            _pending_name,
                            _pending_args or {},
                        ))
                        _pending_name = None
                        _pending_id = None
                        _pending_args = None

                    # Build CanonicalToolCall objects for execution.
                    for acc_id, acc_name, acc_args in _accumulated_calls:
                        captured.append(CanonicalToolCall(
                            id=acc_id,
                            name=acc_name,
                            arguments=acc_args,
                        ))
                    _accumulated_calls = []
                    saw_tool_finish = True
                    # Do NOT yield this premature success event.

                elif etype == "chat.end":
                    if saw_tool_finish:
                        # Turn ended requesting tool execution — break the inner
                        # loop WITHOUT yielding this chat.end; we continue after
                        # execution.
                        break
                    else:
                        # Natural end — model produced its final answer.
                        yield ev
                        return

                else:
                    # message/reasoning deltas, chat.start, prompt_processing.*,
                    # model_load.*, tool_call.failure, warning, error — pass
                    # through live.
                    yield ev

            if not captured:
                # Inner stream ended (or broke) without tool calls — done.
                log.debug("agentic.loop.no_tools_captured", round_num=round_num)
                return

            # Execute each captured call and emit events incrementally.
            for call in captured:
                # Ensure a stable uuid4 id threaded through all synthesised
                # events for this call.
                call_id = call.id or str(uuid.uuid4())

                log.info(
                    "agentic.loop.execute_tool",
                    round_num=round_num,
                    tool=call.name,
                    call_id=call_id,
                )

                # Builtin (app-executed) tools are checked FIRST: when a
                # registry is present and the call name is registered there,
                # route to its executor instead of McpHost. Builtin executors
                # are contracted to never raise and never return an
                # "[mcp_error] …" sentinel (services/builtin_tools.py) — the
                # try/except below is only a contract-violation backstop, not
                # the normal failure path. A name absent from the builtin
                # registry (or no registry supplied at all) falls through to
                # the existing McpHost path, unchanged.
                builtin_entry = (
                    self._builtin_registry.get(call.name)
                    if self._builtin_registry is not None
                    else None
                )
                result: str
                if builtin_entry is not None:
                    try:
                        result = await builtin_entry.executor(
                            call.arguments or {}, self._builtin_ctx
                        )
                    except Exception as exc:  # noqa: BLE001 — backstop only
                        result = f"builtin tool '{call.name}' failed: {exc}"
                        log.warning(
                            "agentic.loop.builtin_tool_exception",
                            tool=call.name,
                            error=str(exc),
                        )
                else:
                    # Execute via the host — non-fatal; errors are returned as an
                    # "[mcp_error] …" string so the model can react.
                    try:
                        result = await self._mcp_host.call_tool(
                            call.name,
                            call.arguments if call.arguments else None,
                        )
                    except Exception as exc:  # noqa: BLE001
                        result = f"[mcp_error] {exc}"
                        log.warning(
                            "agentic.loop.call_tool_exception",
                            tool=call.name,
                            error=str(exc),
                        )

                # The host returns an "[mcp_error] …" sentinel when a tool
                # was not found / gone / timed out / raised / returned isError
                # (host.py). Emitting tool_call.success for those gave the FE a green
                # success card AND let the model read the failure as a win. Branch to
                # tool_call.failure using the canonical shape (result rides
                # event.error as {code, tool, message, output};
                # tool_call.result stays None). The tool message appended to
                # working_history below carries the "[mcp_error] …" text either way,
                # so the model always sees the failure detail.
                if result.startswith("[mcp_error]"):
                    yield CanonicalEvent(
                        type="tool_call.failure",
                        tool_call=CanonicalToolCall(
                            id=call_id,
                            name=call.name,
                            arguments=call.arguments or {},
                        ),
                        error={
                            "code": "tool_call_failed",
                            "tool": call.name,
                            "message": result,
                            "output": result,
                        },
                    )
                else:
                    # Yield the REAL tool_call.success carrying the output.
                    # Shape mirrors native.py:
                    #   tool_call.success carries id/name/arguments/result (output).
                    # Same call_id threaded from the .start/.name/.arguments.
                    yield CanonicalEvent(
                        type="tool_call.success",
                        tool_call=CanonicalToolCall(
                            id=call_id,
                            name=call.name,
                            arguments=call.arguments or {},
                            result=result,
                        ),
                    )

                # Inject tool context into working_history for the next round.
                # Two messages per call:
                #   1. assistant message carrying the tool_calls list (the request).
                #   2. tool message carrying the result + tool_call_id for replay.
                working_history.append(
                    CanonicalMessage(
                        role="assistant",
                        tool_calls=[
                            CanonicalToolCall(
                                id=call_id,
                                name=call.name,
                                arguments=call.arguments or {},
                            )
                        ],
                    )
                )
                working_history.append(
                    CanonicalMessage(
                        role="tool",
                        content=result,
                        tool_call_id=call_id,
                    )
                )

            log.debug(
                "agentic.loop.round_complete",
                round_num=round_num,
                captured_count=len(captured),
                history_len=len(working_history),
            )
            # Loop: re-issue with updated working_history.

        # Hit max_rounds without a natural stop — the model spent every round
        # calling tools and never wrote a reply. Ending here would surface
        # nothing (the cloud agentic turn otherwise "thinks forever,
        # then empty"). Instead make ONE final pass with NO tools advertised so the
        # model must synthesize an answer from the tool results already in
        # working_history. A real answer from the gathered work beats silence.
        log.warning("agentic.loop.max_rounds_hit", max_rounds=self._max_rounds)
        yield CanonicalEvent(
            type="warning",
            warning={
                "code": "agentic_max_rounds",
                "message": _MAX_ROUNDS_WARNING,
                "max_rounds": self._max_rounds,
            },
        )
        final_req = request.model_copy(update={"tools": []})
        # Wrap the final synthesis in try/except to guarantee a
        # terminal frame in all cases:
        #   Happy path   → pass content events through, then yield chat.end.
        #   Error event  → pass the error through; do NOT emit trailing
        #                  chat.end (the error IS the terminator for
        #                  StreamingService, which returns on error).
        #   Exception    → log, yield a graceful error event, then chat.end
        #                  (generator must not propagate raw exceptions).
        _synthesis_saw_error = False
        try:
            async for ev in self._inner.stream_chat(
                final_req,
                history=working_history,
                tools=[],
                cumulative_tool_rounds=cumulative_tool_rounds + self._max_rounds,
            ):
                # Pass through the synthesized content (message/reasoning deltas,
                # prompt_processing, errors). Swallow chat.start/chat.end (we emit
                # our own chat.end below) and any tool_call.* (no tools were
                # advertised; a hallucinated call has no executor here).
                if ev.type in ("chat.start", "chat.end"):
                    continue
                if ev.type.startswith("tool_call."):
                    continue
                if ev.type == "error":
                    _synthesis_saw_error = True
                yield ev
        except Exception as exc:  # noqa: BLE001
            log.error(
                "agentic.loop.final_synthesis_exception",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            yield CanonicalEvent(
                type="error",
                error={
                    "code": "agentic_synthesis_error",
                    "message": f"Final synthesis failed: {exc}",
                },
            )
            # Fall through: we must still emit chat.end after a caught
            # exception so the FE receives a terminal frame.
        if not _synthesis_saw_error:
            yield CanonicalEvent(type="chat.end", stop_reason="stop")


async def maybe_wrap_agentic(
    inner: ChatProvider,
    integrations: list[str] | None,
    app_state: Any,
    *,
    log_ctx: dict[str, Any] | None = None,
    builtin_registry: BuiltinToolRegistry | None = None,
    builtin_ctx: BuiltinToolContext | None = None,
) -> ChatProvider | AgenticMcpProvider:
    """Return ``inner``, or an ``AgenticMcpProvider`` wrapping it, per integrations.

    Single source of truth for the "cloud provider + MCP integrations → run the
    agentic tool loop" dispatch decision.  Both the main replay path
    (``StreamingService.stream_chat``) and the sub-session path
    (``routes.chats.sub_session_stream``) call this instead of hand-rolling the
    same ~40-line block.

    Returns ``inner`` UNCHANGED when either:

      * there are no ``mcp/*`` entries in ``integrations`` (native LM Studio
        integrations, or none at all) AND no ``builtin_registry`` was passed, or
      * no ``McpHost`` is present on ``app_state`` (nothing to execute against).

    ``builtin_registry``/``builtin_ctx`` carry the app-executed tools (e.g.
    web_search) increment: passing a registry keeps the wrap active even when
    there are zero ``mcp/*`` integrations, so a turn with no MCP servers
    selected still gets the builtin tools advertised. With no
    ``builtin_registry`` (every call site before this increment), behavior is
    byte-identical.

    Otherwise resolves the server ids, assembles the per-server denied-tool set
    from ``app_state.mcp_server_store`` (per-server tool policy), and returns an
    ``AgenticMcpProvider``.  The LM Studio (``context_mode="chain"``) path never
    reaches this function — callers gate it behind the cloud/replay branch.

    ``log_ctx`` is merged into the emitted log events so each call site keeps its
    identifying context (site / chat_id / msg_id).
    """
    _ctx = log_ctx or {}
    mcp_integrations = [i for i in (integrations or []) if i.startswith("mcp/")]
    if not mcp_integrations and builtin_registry is None:
        return inner

    mcp_host = getattr(app_state, "mcp_host", None)
    if mcp_host is None:
        # No host to execute tools against — fall back to the plain provider
        # (byte-identical request to the cloud-without-integrations path).
        log.warning("agentic.maybe_wrap.no_host", **_ctx)
        return inner

    server_ids = _integrations_to_server_ids(mcp_integrations)

    # Assemble per-server denied_tools from the store.  AgenticMcpProvider
    # awaits connect() for each server before advertising tools — the single
    # authoritative connect point — so we deliberately do NOT connect here.
    denied: set[str] = set()
    store = getattr(app_state, "mcp_server_store", None)
    if store is not None:
        for sid in server_ids:
            view = await store.get(sid)
            if view is not None:
                denied.update(view.tool_policy)

    provider = AgenticMcpProvider(
        inner=inner,
        mcp_host=mcp_host,
        server_ids=server_ids,
        denied_tools=denied if denied else None,
        builtin_registry=builtin_registry,
        builtin_ctx=builtin_ctx,
    )
    log.info("agentic.maybe_wrap.active", server_ids=server_ids, **_ctx)
    return provider
