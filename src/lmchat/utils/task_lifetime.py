# SPDX-License-Identifier: Apache-2.0
"""Strong-reference helper for fire-and-forget ``asyncio.create_task`` calls.

A ``Task`` with no strong reference held anywhere is only weakly
referenced by the event loop internals — under GC pressure it can be
collected mid-execution, silently dropping the work with nothing surfaced
beyond a "Task was destroyed but it is pending" warning on stderr (see the
``asyncio.create_task`` docs). Any call site that fires a background task
and does not await it or store the returned ``Task`` somewhere with a
longer lifetime (e.g. ``app.state``) MUST route it through
:func:`spawn_background_task` instead of calling ``asyncio.create_task``
directly.
"""
from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from lmchat.logging import get_logger

log = get_logger(__name__)

# Strong references to every in-flight fire-and-forget task spawned via
# `spawn_background_task`. Each task removes itself once it completes —
# success, exception, or cancellation — via the done-callback below, so
# this set only ever holds tasks that are still running.
_background_tasks: set[asyncio.Task[Any]] = set()


def _on_task_done(task: asyncio.Task[Any]) -> None:
    """Release the strong reference and surface any unhandled exception.

    Callers don't await these tasks, so without this a crash inside a
    background coroutine would reach only asyncio's own "exception was
    never retrieved" logger (not the app's structured logs) — easy to
    miss. Retrieve and log it here so a background failure is always
    visible, regardless of whether the coroutine self-guarded.
    """
    _background_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error(
            "background_task.unhandled_exception",
            task_name=task.get_name(),
            error=str(exc),
            error_type=type(exc).__name__,
        )


def spawn_background_task(
    coro: Coroutine[Any, Any, Any], *, name: str | None = None
) -> asyncio.Task[Any]:
    """Fire-and-forget *coro* as a Task that cannot be GC'd mid-flight.

    Equivalent to ``asyncio.create_task`` except the returned Task is held
    in a module-level set until it finishes, so nothing needs to await or
    store it at the call site. Use this for any background task whose
    result the caller does not need to observe. Any unhandled exception is
    logged (see :func:`_on_task_done`).
    """
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)
    task.add_done_callback(_on_task_done)
    return task
