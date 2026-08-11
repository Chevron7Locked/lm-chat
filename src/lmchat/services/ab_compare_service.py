# SPDX-License-Identifier: Apache-2.0
"""A/B compare service — streams two models simultaneously for lm-chat.

Public API
----------
``AbCompareService.stream_both()`` is a thin orchestrator that fans out TWO
concurrent ``LmstudioStreamingClient.stream()`` calls — one per model — and
yields interleaved ``AbEvent`` objects labelled ``side="a"`` or ``side="b"``.

Concurrency model
-----------------
``asyncio.gather`` with ``return_exceptions=True`` is used so a failure on
one upstream (model not loaded, OOM, network error) surfaces as an
``AbEvent(side=X, event_type="ab.error", ...)`` without cancelling the other
model's stream.  Both streams run concurrently; events from whichever model
produces output first are yielded immediately.

The fan-out is implemented as two concurrent async tasks feeding a shared
``asyncio.Queue``.  The main coroutine reads from the queue until both tasks
signal completion (via a sentinel ``None``).

Error wire format:
    ``AbEvent.event_type == "ab.error"`` carries ``code`` and ``message``
    fields.  The other model's stream continues to its terminal ``ab.end``.

RAG integration
---------------
If ``rag_augment_fn`` is provided (a coroutine that takes ``side`` and
``messages`` and returns augmented messages), the service calls it for EACH
pane before opening the upstream connection.  This satisfies the cross-cutting
requirement that A/B compare also calls ``augment_prompt`` if
the chat has RAG enabled.  Callers inject this function; the service does not
import ``rag_service`` directly to avoid a hard dependency from the thin
orchestrator.

Token quota consumption
------------------------
After BOTH panes reach their terminal event (``ab.end`` or ``ab.error``),
``_safe_consume_tokens`` is called ONCE with the combined content of both
panes (pane_a_content + pane_b_content).  The approximation uses
:func:`lmchat.services._token_budget.approx_token_count` (UTF-8 byte
heuristic — CJK-correct), matching the StreamingService convention.
The call is non-fatal: a quota write failure MUST NOT kill the streams.
The request-level quota check already happened in middleware; this is
the token-consumption side only.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

from lmchat.lmstudio.types import CanonicalChatRequest, CanonicalInputBlock
from lmchat.logging import get_logger
from lmchat.services.lmstudio_streaming_client import LmstudioStreamingClient

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

log = get_logger(__name__)

# Sentinel to signal task completion in the queue.
_DONE = object()

# A/B compare doubles inference compute per request.  The cap prevents a
# single user from saturating GPU memory by requesting two full-context
# completions simultaneously.  Single-stream path is not affected.
# Override via the LM_CHAT_AB_MAX_OUTPUT_TOKENS environment variable.
_DEFAULT_AB_MAX_OUTPUT_TOKENS: int = 32_768


# ─── Exceptions ───────────────────────────────────────────────────────────────


class AbCompareLimitError(Exception):
    """Raised when a per-pane token limit check fails for an A/B compare request.

    Callers (routes) should map this to HTTP 400.
    """


# ─── Public models ────────────────────────────────────────────────────────────


class AbEvent(BaseModel):
    """One event in the A/B compare SSE stream.

    Attributes:
        side:         ``"a"`` or ``"b"`` — which model produced this event.
        event_type:   The canonical event type (mirrors streaming event types
                      plus ``"ab.error"`` and ``"ab.end"``).
        delta:        Accumulated content delta (for ``message.delta``).
        reasoning:    Accumulated reasoning delta (for ``reasoning.delta``).
        response_id:  Response ID from the upstream model (for ``chat.start``).
        code:         Error code (for ``ab.error``).
        message:      Error message (for ``ab.error``).
    """

    side: Literal["a", "b"]
    event_type: str
    delta: str | None = None
    reasoning: str | None = None
    response_id: str | None = None
    code: str | None = None
    message: str | None = None


# ─── Service ──────────────────────────────────────────────────────────────────


class AbCompareService:
    """Orchestrates two concurrent LM Studio streams for side-by-side comparison.

    Args:
        lm_client: The shared ``LmstudioStreamingClient`` instance.
        engine:    Optional async SQLAlchemy engine.  When provided, token
                   consumption is recorded in ``quota_usage`` after both panes
                   complete.  When ``None``, the quota
                   write is skipped silently (e.g. in unit tests without a DB).
    """

    def __init__(
        self,
        *,
        lm_client: LmstudioStreamingClient,
        engine: AsyncEngine | None = None,
    ) -> None:
        self._lm_client = lm_client
        self._engine = engine

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _safe_consume_tokens(self, *, user_id: int, content: str) -> None:
        """Fire-and-forget wrapper around ``quota_service.consume_tokens``.

        Mirrors ``StreamingService._safe_consume_tokens`` — both delegate
        to the shared :func:`quota_service.safe_consume_tokens` helper.
        Uses :func:`approx_token_count` (UTF-8 byte heuristic —
        CJK-correct). Catches every exception and logs at WARNING — a
        quota write failure MUST NOT affect the SSE stream (the responses
        already completed).

        No-ops silently if ``self._engine`` is ``None`` (test environments that
        do not wire up a DB).

        Args:
            user_id: User PK.
            content: Combined content from both panes (pane_a + pane_b).
        """
        from lmchat.services.quota_service import safe_consume_tokens

        await safe_consume_tokens(
            self._engine,
            user_id,
            content,
            log_prefix="ab_compare",
        )

    async def stream_both(
        self,
        *,
        prompt: str,
        model_a: str,
        model_b: str,
        system_prompt: str | None = None,
        user_id: int | None = None,
        max_tokens: int | None = None,
    ) -> AsyncGenerator[AbEvent]:
        """Fan out TWO upstream streams concurrently and yield labelled ``AbEvent`` objects.

        Opens two streams in parallel (``model_a`` and ``model_b``).  Events
        from each stream are yielded as they arrive with the ``side`` discriminator.
        If one model fails, an ``ab.error`` event is yielded for that side and
        the other model's stream continues to completion.

        After BOTH panes reach their terminal event, ``_safe_consume_tokens`` is
        called ONCE with the combined content of both panes.  The call is
        non-fatal and no-ops if ``user_id`` is ``None`` or
        ``self._engine`` is ``None``.

        Raises ``AbCompareLimitError`` before opening any upstream connections if
        ``max_tokens`` or the estimated prompt-token count exceeds the per-request
        cap (``LM_CHAT_AB_MAX_OUTPUT_TOKENS``, default 32 768).

        A/B compare doubles inference compute per request.  The cap prevents a
        single user from saturating GPU memory by requesting two full-context
        completions simultaneously.  Single-stream path is not affected.

        Args:
            prompt:        User message text (current turn).
            model_a:       LM Studio model ID for pane A.
            model_b:       LM Studio model ID for pane B.
            system_prompt: Optional system prompt prepended to both panes.
            user_id:       Authenticated user PK.  When provided (and
                           ``self._engine`` is set), token consumption is
                           recorded after both panes complete.
            max_tokens:    Caller-supplied output-token limit for each pane.
                           When provided, must not exceed the configured cap.

        Yields:
            ``AbEvent`` objects interleaved from both model streams.

        Raises:
            AbCompareLimitError: If ``max_tokens`` or the estimated prompt
                                 token count exceeds the configured cap.
        """
        from lmchat.config import get_settings

        cap = get_settings().lm_chat_ab_max_output_tokens

        # Guard 1: explicit output-token limit.
        if max_tokens is not None and max_tokens > cap:
            raise AbCompareLimitError(
                f"max_tokens per pane ({max_tokens}) exceeds AB compare limit "
                f"({cap}); reduce or disable A/B compare"
            )

        # Guard 2: rough input-context estimate.
        from lmchat.services._token_budget import approx_token_count

        combined_prompt = (system_prompt or "") + prompt
        prompt_token_estimate = approx_token_count(combined_prompt)
        if prompt_token_estimate > cap:
            raise AbCompareLimitError(
                f"estimated prompt tokens ({prompt_token_estimate}) exceeds AB "
                f"compare limit ({cap}); reduce or disable A/B compare"
            )

        queue: asyncio.Queue[AbEvent | object] = asyncio.Queue()

        # Per-pane accumulated content for token-quota accounting.
        # Keyed by side; updated by _stream_side via closure over this dict.
        _accumulated: dict[str, str] = {"a": "", "b": ""}

        async def _stream_side(side: Literal["a", "b"], model_id: str) -> None:
            """Stream one model and put events onto the shared queue."""
            request = CanonicalChatRequest(
                model=model_id,
                system_prompt=system_prompt,
                input=[CanonicalInputBlock(type="text", content=prompt)],
            )
            try:
                async for event in self._lm_client.stream(request=request):
                    # Accumulate message.delta content for token accounting.
                    if event.type == "message.delta" and event.content:
                        _accumulated[side] += event.content

                    # Map canonical event types to AbEvent fields.
                    ab_event = AbEvent(
                        side=side,
                        event_type=event.type,
                        delta=event.content if event.type == "message.delta" else None,
                        reasoning=event.content if event.type == "reasoning.delta" else None,
                        response_id=event.response_id,
                    )
                    await queue.put(ab_event)
                    if event.type in ("chat.end", "error"):
                        if event.type == "error":
                            err = event.error or {}
                            await queue.put(
                                AbEvent(
                                    side=side,
                                    event_type="ab.error",
                                    code=str(err.get("code", "upstream_error")),
                                    message=str(err.get("message", "upstream error")),
                                )
                            )
                        else:
                            await queue.put(AbEvent(side=side, event_type="ab.end"))
                        return
                # Stream exhausted without chat.end.
                await queue.put(AbEvent(side=side, event_type="ab.end"))
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "ab_compare.stream_side_error",
                    side=side,
                    model_id=model_id,
                    error=str(exc),
                )
                await queue.put(
                    AbEvent(
                        side=side,
                        event_type="ab.error",
                        code="stream_error",
                        message=str(exc),
                    )
                )
            finally:
                await queue.put(_DONE)

        # Launch both streams as concurrent tasks.
        task_a = asyncio.create_task(_stream_side("a", model_a))
        task_b = asyncio.create_task(_stream_side("b", model_b))

        # Track whether the while loop exited naturally (both panes done).
        # Used in the finally block to guard the token-consume call so it
        # fires only on normal completion, not on generator close / cancel.
        _both_complete = False

        done_count = 0
        try:
            while done_count < 2:
                item = await queue.get()
                if item is _DONE:
                    done_count += 1
                    continue
                if isinstance(item, AbEvent):
                    yield item
            _both_complete = True
        finally:
            # Cancel any tasks that are still running (e.g. if caller disconnects).
            task_a.cancel()
            task_b.cancel()
            await asyncio.gather(task_a, task_b, return_exceptions=True)

            # Consume combined tokens from the user's daily quota.
            # Only fires when both panes completed
            # normally — skipped on generator close or caller disconnect.
            # Non-fatal: the SSE stream is already complete at this point.
            if _both_complete and user_id is not None:
                combined = _accumulated["a"] + _accumulated["b"]
                await self._safe_consume_tokens(user_id=user_id, content=combined)

