# SPDX-License-Identifier: Apache-2.0
"""Unit tests for lmchat.utils.task_lifetime.spawn_background_task.

A bare ``asyncio.create_task(...)`` with no held reference is only weakly
referenced by the event loop, so it can be silently garbage-collected
mid-flight. These tests prove ``spawn_background_task`` closes that hole:
the module-level set holds a strong reference while the task is running
and releases it once the task finishes, regardless of outcome.
"""
from __future__ import annotations

import asyncio
import gc

import pytest

from lmchat.utils.task_lifetime import _background_tasks, spawn_background_task


@pytest.mark.asyncio
async def test_spawn_background_task_survives_gc_while_running() -> None:
    """A task with NO local reference must still run to completion.

    Simulates the exact footgun this helper fixes: the caller discards
    the returned Task immediately and a GC pass runs before the task's
    coroutine has a chance to execute. Without the module-level strong
    reference, ``gc.collect()`` here could drop the task before it ever
    sets the event, and the test would hang/timeout instead of passing.
    """
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def _work() -> None:
        started.set()
        await release.wait()
        finished.set()

    spawn_background_task(_work(), name="test_spawn_bg_task")  # no local ref kept
    gc.collect()

    await asyncio.wait_for(started.wait(), timeout=2)
    assert not finished.is_set()

    release.set()
    await asyncio.wait_for(finished.wait(), timeout=2)


@pytest.mark.asyncio
async def test_spawn_background_task_is_tracked_then_released() -> None:
    """The task is present in the module-level set while running and
    removed from it once it completes — the done-callback must not leak
    a permanently-growing set of finished tasks."""
    gate = asyncio.Event()

    async def _work() -> None:
        await gate.wait()

    task = spawn_background_task(_work(), name="test_spawn_bg_task_tracked")
    await asyncio.sleep(0)  # let the task start
    assert task in _background_tasks

    gate.set()
    await asyncio.wait_for(task, timeout=2)
    # The done-callback runs via call_soon; yield once so it has fired.
    await asyncio.sleep(0)
    assert task not in _background_tasks


@pytest.mark.asyncio
async def test_spawn_background_task_releases_reference_on_exception() -> None:
    """A task that raises must still be discarded from the tracking set —
    the done-callback fires on ANY completion outcome, not just success."""

    async def _boom() -> None:
        raise ValueError("boom")

    task = spawn_background_task(_boom(), name="test_spawn_bg_task_exc")
    with pytest.raises(ValueError, match="boom"):
        await asyncio.wait_for(task, timeout=2)
    await asyncio.sleep(0)
    assert task not in _background_tasks


@pytest.mark.asyncio
async def test_unhandled_exception_in_fire_and_forget_is_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fire-and-forget task that raises — never awaited by the caller — must
    surface its exception to the app's structured logger via the done-callback,
    not lose it to asyncio's own 'exception was never retrieved' path."""
    import lmchat.utils.task_lifetime as bt

    calls: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        bt.log, "error", lambda event, **kw: calls.append((event, kw))
    )

    async def _boom() -> None:
        raise RuntimeError("kaboom")

    spawn_background_task(_boom(), name="test_unhandled_exc")  # not awaited
    for _ in range(5):  # let the task run + the done-callback fire
        await asyncio.sleep(0)

    assert any(
        event == "background_task.unhandled_exception"
        and kw.get("task_name") == "test_unhandled_exc"
        and kw.get("error_type") == "RuntimeError"
        for event, kw in calls
    ), calls
