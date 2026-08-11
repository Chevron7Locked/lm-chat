# SPDX-License-Identifier: Apache-2.0
"""Serialization gate for best-effort background aux LLM calls.

Auto-memory distillation, chat-title generation, and follow-up-chip generation
all fire within ~a second of each other at the end of a turn, and all target the
SAME background-tasks model. On a slow model they CONTEND on LM Studio's
single-model request queue, so each blows its own timeout — and on a slow-only
fleet all three silently fail. This was found by the live-dogfood harness:
distill + title + followups every one ReadTimeout at 120s under real concurrent
load on a 122B-MoE.

Fix: funnel the three aux calls through a single-slot gate so each runs ALONE
with the model's full throughput and completes within its own timeout budget.
Each call's per-request timeout starts when it actually begins hitting the model
(after acquiring the gate), so waiting for the gate never consumes the budget.

Global (not per-model) serialization is deliberate and correct for this app: it
is single-admin and low-concurrency, so a global one-at-a-time gate on
background work has no meaningful downside and the simplest possible semantics.
The gate is lazily created on first use so it binds to the running event loop.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

_gate: asyncio.Semaphore | None = None

# Number of aux calls currently holding-or-waiting-for the slot. Used to bound
# DROPPABLE fire-and-forget work (see bg_aux_overloaded) so the single-slot
# serialization can't turn into an unbounded backlog of queued tasks — each of
# which pins the turn's text — if turns arrive faster than the aux calls drain.
_pending = 0

# Backlog depth past which new droppable background work should be SKIPPED
# rather than queued. Generous for a single-admin app (a handful of turns'
# worth of aux in flight); beyond it, best-effort work is dropped, not queued.
_MAX_PENDING_AUX = 8


def bg_aux_pending() -> int:
    """Aux calls currently holding or waiting for the single slot."""
    return _pending


def bg_aux_overloaded() -> bool:
    """True when the aux backlog is deep enough that new DROPPABLE background
    work (e.g. fire-and-forget auto-memory distillation) should be SKIPPED
    instead of queued. Awaited aux calls (follow-ups in the SSE epilogue,
    titles in their request) are naturally bounded by their caller's lifecycle;
    only the fire-and-forget path can accumulate, so only it consults this.
    """
    return _pending >= _MAX_PENDING_AUX


def _get_gate() -> asyncio.Semaphore:
    """Return the process-wide background-aux gate, creating it on first use.

    Lazy creation binds the semaphore to the event loop that first runs a
    background aux call (the app loop), avoiding a loop-binding mismatch from
    constructing it at import time.
    """
    global _gate
    if _gate is None:
        _gate = asyncio.Semaphore(1)
    return _gate


@asynccontextmanager
async def bg_aux_slot() -> AsyncIterator[None]:
    """Serialize the wrapped background aux LLM call against the other aux calls.

    Usage::

        async with bg_aux_slot():
            resp = await http_client.post(url, json=body, timeout=timeout_sec)

    Only the model-hitting call belongs inside the slot; keep prompt assembly and
    result parsing outside so the gate is held for the minimum time.
    """
    global _pending
    _pending += 1
    try:
        async with _get_gate():
            yield
    finally:
        _pending -= 1


def _reset_gate_for_tests() -> None:
    """Test hook: drop the cached semaphore + backlog so the next use rebinds to
    the current loop. Never call in production."""
    global _gate, _pending
    _gate = None
    _pending = 0
