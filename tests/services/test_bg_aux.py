# SPDX-License-Identifier: Apache-2.0
"""bg_aux — background-aux serialization gate.

The live-dogfood harness found that auto-memory distillation, chat-title
generation, and follow-up-chip generation all fire ~concurrently at the end of a
turn against the same background model, contend on LM Studio's single-model
queue, and every one times out on a slow model. bg_aux.bg_aux_slot() serializes
them so each runs alone. These tests pin that serialization.
"""
from __future__ import annotations

import asyncio

import pytest

from lmchat.services.bg_aux import (
    _MAX_PENDING_AUX,
    _reset_gate_for_tests,
    bg_aux_overloaded,
    bg_aux_pending,
    bg_aux_slot,
)


@pytest.fixture(autouse=True)
def _fresh_gate() -> None:
    # Rebind the module gate to this test's event loop.
    _reset_gate_for_tests()


@pytest.mark.asyncio
async def test_bg_aux_slot_serializes_concurrent_calls() -> None:
    """Three tasks entering the slot concurrently must run ONE AT A TIME — the
    max observed concurrency is 1, not 3.

    RED-ON-REVERT: remove the `async with bg_aux_slot()` wrap around the aux
    POSTs and the aux calls contend (concurrency 3) → slow-model timeouts return.
    """
    concurrent = 0
    max_concurrent = 0
    order: list[int] = []

    async def worker(n: int) -> None:
        nonlocal concurrent, max_concurrent
        async with bg_aux_slot():
            concurrent += 1
            max_concurrent = max(max_concurrent, concurrent)
            order.append(n)
            # Yield control so, WITHOUT the gate, the others would interleave
            # here and drive max_concurrent to 3.
            await asyncio.sleep(0.02)
            concurrent -= 1

    await asyncio.gather(worker(1), worker(2), worker(3))

    assert max_concurrent == 1, (
        f"aux calls must be serialized (one at a time); saw {max_concurrent} concurrent"
    )
    assert sorted(order) == [1, 2, 3], "every queued call must still run"


@pytest.mark.asyncio
async def test_bg_aux_slot_releases_on_exception() -> None:
    """A raising body must not leak the slot — the next acquirer proceeds."""
    with pytest.raises(RuntimeError):
        async with bg_aux_slot():
            raise RuntimeError("boom")

    # If the slot leaked, this would deadlock; wait_for guards that.
    async def _acquire() -> str:
        async with bg_aux_slot():
            return "acquired"

    assert await asyncio.wait_for(_acquire(), timeout=2.0) == "acquired"



@pytest.mark.asyncio
async def test_bg_aux_overloaded_bounds_the_backlog() -> None:
    """The fire-and-forget backlog must be bounded (security review, P2): once
    _MAX_PENDING_AUX aux calls are holding/waiting for the single slot,
    bg_aux_overloaded() is True so the caller SKIPS new droppable work instead of
    queuing it unboundedly.

    RED-ON-REVERT: drop the _pending inc/dec in bg_aux_slot (or the
    bg_aux_overloaded guard at the distill create_task site) and the backlog
    grows without bound again.
    """
    assert bg_aux_pending() == 0
    assert bg_aux_overloaded() is False

    # Hold the single slot with one long call, then queue MANY behind it so the
    # pending counter climbs past the cap while they all wait.
    release = asyncio.Event()

    async def _holder() -> None:
        async with bg_aux_slot():
            await release.wait()

    async def _waiter() -> None:
        async with bg_aux_slot():
            pass

    holder = asyncio.create_task(_holder())
    await asyncio.sleep(0.02)  # let the holder acquire the slot
    waiters = [asyncio.create_task(_waiter()) for _ in range(_MAX_PENDING_AUX + 2)]
    await asyncio.sleep(0.02)  # let them all register as pending (waiting)

    assert bg_aux_pending() >= _MAX_PENDING_AUX
    assert bg_aux_overloaded() is True, "deep backlog must report overloaded"

    # Drain: release the holder, let everyone finish, backlog clears.
    release.set()
    await holder
    await asyncio.gather(*waiters)
    assert bg_aux_pending() == 0
    assert bg_aux_overloaded() is False
