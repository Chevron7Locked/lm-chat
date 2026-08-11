# SPDX-License-Identifier: Apache-2.0
"""Decoder-level error contract tests for LM Studio surfaces.

LM Studio's mid-stream context-window overflow arrives as
``error.type == "exceed_context_size_error"`` on both the native
(/api/v1/chat) and compat (/v1/chat/completions) surfaces. The decoders
translate this to the canonical ``context_window_exceeded`` error code so
the SPA can render a specific message instead of the generic
``upstream_unavailable``. ISSUE-8.

Tests use a fake httpx.Response whose body is a bytes SSE / chunked JSON
stream — no live LM Studio required, no real network.
"""
from __future__ import annotations

import textwrap

import httpx
import pytest

from lmchat.lmstudio.compat import decode_compat
from lmchat.lmstudio.native import decode_native
from lmchat.lmstudio.types import CanonicalEvent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_streaming_response(body: str) -> httpx.Response:
    """Build a synthetic httpx.Response carrying *body* as its content."""
    return httpx.Response(
        status_code=200,
        headers={"content-type": "text/event-stream"},
        content=body.encode("utf-8"),
    )


async def _collect_native(response: httpx.Response) -> list[CanonicalEvent]:
    return [e async for e in decode_native(response)]


async def _collect_compat(response: httpx.Response) -> list[CanonicalEvent]:
    return [e async for e in decode_compat(response)]


# ---------------------------------------------------------------------------
# Native surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_native_decoder_emits_context_window_exceeded() -> None:
    """Native ``exceed_context_size_error`` translates to ``context_window_exceeded``.

    The wire payload is the LM Studio error type; the decoder must NOT
    surface it as a generic ``upstream_*`` error.
    """
    sse = textwrap.dedent("""\
        event: error
        data: {"error":{"type":"exceed_context_size_error","message":"Prompt is too large","n_prompt_tokens":40000,"n_ctx":32768}}

    """)
    response = _make_streaming_response(sse)
    events = await _collect_native(response)

    assert len(events) == 1
    assert events[0].type == "error"
    assert events[0].error is not None
    assert events[0].error["code"] == "context_window_exceeded"
    assert events[0].error["n_prompt_tokens"] == 40000
    assert events[0].error["n_ctx"] == 32768
    # Must NOT collapse to upstream_unavailable / upstream_stream_error.
    assert events[0].error["code"] != "upstream_unavailable"


@pytest.mark.asyncio
async def test_native_decoder_passes_through_other_errors() -> None:
    """Non-context errors are surfaced with their original shape.

    Regression guard: the new context-overflow branch must not swallow
    rate-limit / other error events.
    """
    sse = textwrap.dedent("""\
        event: error
        data: {"error":{"code":"rate_limited","message":"slow down"}}

    """)
    response = _make_streaming_response(sse)
    events = await _collect_native(response)

    assert len(events) == 1
    assert events[0].type == "error"
    assert events[0].error is not None
    assert events[0].error.get("code") == "rate_limited"


# ---------------------------------------------------------------------------
# Compat surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compat_decoder_emits_context_window_exceeded() -> None:
    """Compat ``exceed_context_size_error`` translates to ``context_window_exceeded``.

    Some LM Studio deployments route the compat surface as well; the
    decoder must give the SPA the same canonical error code so the
    UX is consistent across surfaces.
    """
    body = (
        'data: {"error":{"type":"exceed_context_size_error",'
        '"message":"Context exhausted","n_prompt_tokens":18000,'
        '"n_ctx":8192}}\n\n'
    )
    response = _make_streaming_response(body)
    events = await _collect_compat(response)

    assert len(events) == 1
    assert events[0].type == "error"
    assert events[0].error is not None
    assert events[0].error["code"] == "context_window_exceeded"
    assert events[0].error["n_prompt_tokens"] == 18000
    assert events[0].error["n_ctx"] == 8192
