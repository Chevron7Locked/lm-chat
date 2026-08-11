# SPDX-License-Identifier: Apache-2.0
"""httpx MockTransport that interleaves real upstream with chaos failures.

This is the fallback path when toxiproxy is unavailable.
The orchestrator activates it by setting
``STRESS_CHAOS_BACKEND=httpx-mock`` and monkey-patching
``app.state.http_client._transport`` to the ``FaultTransport`` below.

Design
------
- ``current_spec`` is a shared mutable holding the active ``FailureSpec``.
  The orchestrator updates it as the S5 timeline advances.
- On every request the transport reads ``current_spec`` and decides:
    * NONE      -> delegate to the real upstream transport.
    * LATENCY   -> sleep then delegate.
    * SERVER_ERROR / DOWN -> synthesise a 502 (or raise ConnectError).
    * MID_STREAM_DISCONNECT -> delegate, then truncate the body stream
      after ``body_bytes_before_drop``.

Both ``handle_request`` (sync) and ``handle_async_request`` are
implemented; lm-chat uses ``httpx.AsyncClient`` exclusively but tests
that exercise the sync surface still work.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field

import httpx

from .failure_modes import FailureKind, FailureSpec

log = logging.getLogger("stress.chaos.httpx_fault")


@dataclass
class SharedFailureState:
    """Mutable container for the active failure spec.

    Used by orchestrator + transport.  The lock makes spec mutations
    visible across greenlets / threads without ad-hoc volatility.
    """

    spec: FailureSpec = field(default_factory=FailureSpec)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def set(self, spec: FailureSpec) -> None:
        with self._lock:
            self.spec = spec

    def get(self) -> FailureSpec:
        with self._lock:
            return self.spec


class FaultTransport(httpx.AsyncBaseTransport):
    """Async httpx transport that injects failures around a real upstream.

    Args:
        inner:  The real underlying transport (typically the default
                ``httpx.AsyncHTTPTransport`` from ``httpx.AsyncClient``).
        state:  Shared mutable failure state; orchestrator updates this.
    """

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        state: SharedFailureState,
    ) -> None:
        self._inner = inner
        self._state = state

    async def handle_async_request(
        self, request: httpx.Request
    ) -> httpx.Response:
        spec = self._state.get()

        if spec.kind == FailureKind.NONE:
            return await self._inner.handle_async_request(request)

        if spec.kind == FailureKind.LATENCY:
            await asyncio.sleep(spec.latency_ms / 1000.0)
            return await self._inner.handle_async_request(request)

        if spec.kind == FailureKind.SERVER_ERROR:
            return _synth_502(request)

        if spec.kind == FailureKind.DOWN:
            raise httpx.ConnectError(
                "fault transport: chaos backend disabled upstream",
                request=request,
            )

        if spec.kind == FailureKind.BANDWIDTH:
            # Lightweight throttle: sleep for body-size / kbps.
            content_len = int(request.headers.get("content-length", "0") or 0)
            if spec.bandwidth_kbps > 0 and content_len > 0:
                seconds = content_len / 1024.0 / spec.bandwidth_kbps
                await asyncio.sleep(seconds)
            return await self._inner.handle_async_request(request)

        if spec.kind == FailureKind.MID_STREAM_DISCONNECT:
            real = await self._inner.handle_async_request(request)
            return _truncate_after(real, spec.body_bytes_before_drop, request)

        # Defensive fallthrough — should not happen because the enum is
        # exhaustive.  Delegate.
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:  # pragma: no cover - propagation
        await self._inner.aclose()


def _synth_502(request: httpx.Request) -> httpx.Response:
    """Return a synthesised 502 response with a small JSON body."""
    body = b'{"error":"chaos-backend synthesised 502","upstream":"lm-studio"}'
    return httpx.Response(
        status_code=502,
        content=body,
        headers={"Content-Type": "application/json"},
        request=request,
    )


def _truncate_after(
    response: httpx.Response,
    byte_limit: int,
    request: httpx.Request,
) -> httpx.Response:
    """Return a copy of ``response`` whose stream is truncated after N bytes.

    The truncation simulates an upstream socket-close mid-SSE: callers
    iterating the body iterator see clean closure with partial content,
    which is the exact symptom that triggers the "graceful upstream
    failure" path in StreamingService.
    """
    original_stream = response.stream

    async def _gen():  # type: ignore[no-untyped-def]
        total = 0
        async for chunk in original_stream:  # type: ignore[attr-defined]
            if total >= byte_limit:
                log.debug("fault transport: dropped after %d bytes", total)
                return
            remaining = byte_limit - total
            if len(chunk) > remaining:
                yield chunk[:remaining]
                total += remaining
                return
            yield chunk
            total += len(chunk)

    return httpx.Response(
        status_code=response.status_code,
        headers=response.headers,
        stream=httpx.AsyncByteStream(_gen()),  # type: ignore[arg-type]
        request=request,
    )


# ---------------------------------------------------------------------------
# Orchestration helper
# ---------------------------------------------------------------------------


async def play_timeline(
    state: SharedFailureState, timeline: list[FailureSpec]
) -> None:
    """Walk through ``timeline`` advancing the shared state per step.

    Used by the orchestrator inside an asyncio task.
    """
    for spec in timeline:
        log.info(
            "chaos timeline: setting %s for %.1fs",
            spec.kind.value, spec.duration_sec,
        )
        state.set(spec)
        await asyncio.sleep(spec.duration_sec)
    # Final: clear back to NONE.
    state.set(FailureSpec())


def play_timeline_sync(
    state: SharedFailureState, timeline: list[FailureSpec]
) -> None:
    """Synchronous version for thread-based orchestration."""
    for spec in timeline:
        state.set(spec)
        time.sleep(spec.duration_sec)
    state.set(FailureSpec())
