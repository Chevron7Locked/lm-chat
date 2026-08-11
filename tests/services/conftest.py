# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for services tests.

The ``LOW_COST`` dict defined in each test module provides low-cost scrypt
parameters so tests run within the OpenSSL maxmem limit of this environment.
See test_auth_service.py and test_single_session_warning.py for usage.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


def make_disconnect_receive(
    disconnected: bool,
) -> Callable[[], Awaitable[dict[str, str]]]:
    """Build a mock ASGI ``request.receive`` for streaming tests.

    ``StreamingService._watch_disconnect`` is the sole consumer of
    ``request.receive()``: it drains the channel with a 0.5s timeout each tick
    and aborts the draft on ``{"type": "http.disconnect"}``. This factory models
    both connection states:

    * ``disconnected=False`` — a live connection with no queued client frame.
      ``receive()`` blocks forever (until the watcher's ``asyncio.wait_for``
      cancels it on timeout), exactly like a real ASGI server. Without this a
      bare ``AsyncMock().receive()`` would return immediately every tick and
      spin the watcher in a busy loop.
    * ``disconnected=True`` — the client has gone away. ``receive()`` returns
      ``{"type": "http.disconnect"}`` so the watcher aborts the draft.

    Args:
        disconnected: Whether ``receive()`` should yield ``http.disconnect``.

    Returns:
        A coroutine function suitable for ``request.receive``.
    """
    if disconnected:

        async def _disconnect() -> dict[str, str]:
            return {"type": "http.disconnect"}

        return _disconnect

    _never = asyncio.Event()

    async def _block_forever() -> dict[str, str]:
        await _never.wait()
        return {"type": "http.request"}  # pragma: no cover (never reached)

    return _block_forever
