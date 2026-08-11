# SPDX-License-Identifier: Apache-2.0
"""Streaming chat routes for lm-chat.

Endpoints
---------
POST /api/chat/stream
    Stream an assistant response. Requires authentication (401 if absent),
    per-user rate limit (429 if exhausted), and single-stream-per-chat
    enforcement (409 if a draft/pending row already exists for chat_id).

    Returns a ``StreamingResponse`` with ``Content-Type: text/event-stream``.
    Each SSE frame carries ``msg_id`` (the lm-chat draft messages.id) in its
    data payload so the ``useSSE`` reconciliation hook can identify the stream
    from any frame.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from lmchat.logging import get_logger

if TYPE_CHECKING:
    from lmchat.services.integrations_service import IntegrationsService

from lmchat.routes._dependencies import (
    get_integrations_service_dep,
    get_streaming_service_dep,
    require_user,
    stream_rate_limit,
)
from lmchat.services.auth_service import User
from lmchat.services.chat_service import ChatNotFoundError
from lmchat.services.streaming_errors import StreamInProgressError
from lmchat.services.streaming_service import ChatStreamRequest, StreamingService

log = get_logger(__name__)

router = APIRouter(prefix="/api/chat", tags=["streaming"])


async def _resolve_store_available(request: Request) -> set[str]:
    """Return ``mcp/<slug>`` values for enabled+consented MCP-Store servers.

    Merge counterpart to ``routes/integrations.py``'s composer listing
    (``list_available``): a Store-installed server is a valid pre-flight
    integration target even though it never appears in
    ``integrations_service.list_available()`` (the curated,
    LM-Studio-source-only catalog) — the two are different runtimes for the
    same ``mcp/<slug>`` id. Without this, a Store-sourced selection would be
    treated as a stale/unknown id and dropped before ``streaming_service``
    ever sees it, exactly like the removed-firecrawl regression this
    pre-flight filter exists to catch.

    Degrades to an empty set (never raises) when the store isn't wired up on
    ``app.state`` or the read fails — callers then fall back to curated-only
    validation, unchanged from before this merge existed.
    """
    store = getattr(request.app.state, "mcp_server_store", None)
    if store is None:
        return set()
    try:
        store_servers = await store.list_all()
    except Exception:  # noqa: BLE001
        log.warning("streaming.store_available_lookup_failed")
        return set()
    return {
        f"mcp/{s.slug}"
        for s in store_servers
        if s.enabled and getattr(s, "consented", True)
    }


def _drop_not_live(ids: list[str], live: set[str]) -> list[str]:
    """Intersect ``ids`` with the LIVE serveable set, logging what's dropped.

    Shared by both integrations-resolution branches in ``chat_stream``
    (admin-default injection and explicit-list) — resilience-fix "Layer 2":
    a proactive pre-flight filter against LM Studio's ACTUAL live plugin
    set, catching what the admin-curated catalog filter alone misses (a
    DB-configured id LM Studio itself no longer serves, e.g. a removed or
    renamed MCP server left ``enabled_by_default``). Callers only invoke
    this once ``live`` is already known to be non-empty — an empty
    ``live`` set means "unknown" (remote deploy, no same-host mcp.json),
    not "nothing is live", and must never reach here.

    LOAD-BEARING INVARIANT: every genuinely-serveable integration id must
    appear in ``live_serveable_ids() | store_available`` at both call
    sites — those are the only two ways this app currently knows how to
    serve an ``mcp/<name>`` id (LM Studio's own mcp.json, or LM Chat's own
    MCP Store runtime). A future third serving path (e.g. a hardcoded
    builtin that's neither written to mcp.json nor a Store row) would be
    silently stripped by this filter whenever the live set is non-empty —
    any such addition MUST also be folded into the union passed in here.

    Args:
        ids:  The integrations to filter.
        live: The known-non-empty live serveable set to filter against.

    Returns:
        The subset of ``ids`` present in ``live``.
    """
    kept = [i for i in ids if i in live]
    if len(kept) != len(ids):
        log.warning(
            "streaming.integrations_dropped_not_live",
            dropped=[i for i in ids if i not in live],
            kept=kept,
        )
    return kept


@router.post(
    "/stream",
    responses={400: {"description": "Invalid request payload — malformed JSON or missing fields"}},
)
async def chat_stream(
    payload: ChatStreamRequest,
    request: Request,
    user: User = Depends(require_user),
    _rl: None = Depends(stream_rate_limit),
    streaming_service: StreamingService = Depends(get_streaming_service_dep),
    integrations_service: IntegrationsService = Depends(get_integrations_service_dep),
) -> StreamingResponse:
    """Stream an assistant response for the given chat.

    Accepts a JSON body with ``chat_id`` (int) and ``payload``
    (``CanonicalChatRequest``).

    Requires the ``lmchat_session`` cookie (401 if absent).
    Rate-limited to ``LM_CHAT_STREAM_RATE_LIMIT_PER_MIN`` (default 30) streams
    per minute per user (429 if exceeded).
    Returns 409 if another stream is already in progress for ``chat_id``.

    The response is a Server-Sent Events stream with
    ``Content-Type: text/event-stream``.  Each frame has:

        event: <type>
        data: {"type": "<type>", "msg_id": <int>, ...}

    ``msg_id`` is present on every frame (including ``chat.start``) so the
    client can correlate frames to the draft row without waiting for
    ``chat.end``.

    Args:
        payload:           JSON body with ``chat_id`` + ``CanonicalChatRequest``.
        request:           FastAPI ``Request`` for disconnect polling.
        user:              Authenticated user from ``require_user``.
        _rl:               Rate-limit sentinel from ``stream_rate_limit``.
        streaming_service: The ``StreamingService`` from ``app.state``.

    Returns:
        A :class:`~fastapi.responses.StreamingResponse` with media_type
        ``"text/event-stream"``.

    Raises:
        HTTPException: 409 if a stream is already in progress for ``chat_id``.
        HTTPException: 401 if the request is unauthenticated.
        HTTPException: 429 if the per-user rate limit is exhausted.
    """
    # Resolve + validate the turn's integrations against the available catalog:
    #   None → FE omitted the field → apply admin defaults (enabled_by_default).
    #   []   → "user wants no tools" → left untouched.
    #   list → used as given, BUT filtered to ids that still exist in the
    #          catalog.  A stale composer selection (e.g. a removed MCP server
    #          such as firecrawl lingering in the FE's cached per-chat state)
    #          must never reach LM Studio: it rejects an unknown plugin id and
    #          kills the whole stream ("Cannot find plugin handle for plugin:
    #          mcp/firecrawl").  Dropping it here keeps the turn alive with the
    #          remaining valid tools.
    # Wrap list_available() so a transient DB error (WAL checkpoint,
    # mid-migration OperationalError) degrades gracefully instead of 500-ing —
    # no-tools for the None case, pass-through for an explicit list.
    _requested = payload.payload.integrations
    if _requested is None:
        # FE omitted → admin defaults (enabled_by_default); no-tools on DB error.
        try:
            _entries = await integrations_service.list_available()
            _resolved = [e.value for e in _entries if e.enabled_by_default]
        except Exception:  # noqa: BLE001
            _resolved = []
            log.warning(
                "streaming.integrations_default_lookup_failed",
                hint="Falling back to no-tools for this turn; transient DB error.",
            )
        # Layer 2 (resilience fix, proactive pre-validation): further
        # intersect the admin defaults with LM Studio's LIVE serveable set,
        # when that set is known. Catches what the catalog lookup above
        # misses — an admin-curated/DB-configured default LM Studio itself
        # no longer serves (e.g. a removed/renamed MCP server still left
        # enabled_by_default). Store-installed servers run through their
        # own runtime, not LM Studio's mcp.json, so they're unioned in as
        # "live" too — same treatment the catalog filter below gives them.
        _store_available = await _resolve_store_available(request)
        _live = integrations_service.live_serveable_ids() | _store_available
        if _live:
            _resolved = _drop_not_live(_resolved, _live)
        payload.payload = payload.payload.model_copy(
            update={"integrations": _resolved}
        )
    elif _requested:
        # Explicit non-empty list → drop ids no longer in the catalog so a stale
        # selection can't crash the stream at the LM Studio layer. Guard: only
        # filter against a NON-EMPTY catalog — an empty result (unconfigured
        # install / transient DB error) must never drop the user's explicit
        # selection.
        _kept = _requested
        _store_available = await _resolve_store_available(request)
        try:
            _available = {e.value for e in await integrations_service.list_available()}
            _available |= _store_available
            if _available:
                _kept = [i for i in _requested if i in _available]
                if len(_kept) != len(_requested):
                    log.warning(
                        "streaming.integrations_dropped_unavailable",
                        dropped=[i for i in _requested if i not in _available],
                        kept=_kept,
                    )
        except Exception:  # noqa: BLE001
            pass
        # Layer 2: same live-set intersection as the default-injection path
        # above, applied to whatever the catalog filter kept.
        _live = integrations_service.live_serveable_ids() | _store_available
        if _live:
            _kept = _drop_not_live(_kept, _live)
        if _kept != _requested:
            payload.payload = payload.payload.model_copy(
                update={"integrations": _kept}
            )
    # else: explicit [] → user chose no tools; leave untouched.

    # stream_chat is an async generator; the per-chat lock + single-stream
    # invariant check runs before the first yield, but since generators
    # execute lazily we must prime the generator here (via a wrapper) to
    # surface StreamInProgressError as HTTP 409 before committing to a
    # StreamingResponse.
    gen = streaming_service.stream_chat(
        chat_id=payload.chat_id,
        user=user,
        payload=payload,
        request=request,
    )

    # Eagerly advance to the first frame so that StreamInProgressError
    # (raised before any yield inside stream_chat) propagates here.
    # If the stream is already done (zero frames), the first_frame is None.
    try:
        first_frame = await gen.__anext__()
    except StopAsyncIteration:
        first_frame = None
    except ChatNotFoundError as exc:
        raise HTTPException(status_code=404, detail="chat not found") from exc
    except StreamInProgressError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "stream_in_progress", "chat_id": exc.chat_id},
        ) from exc

    async def _prepend_first_frame() -> AsyncIterator[bytes]:
        """Yield the eagerly-fetched first frame then the remainder of gen."""
        if first_frame is not None:
            yield first_frame
        async for frame in gen:
            yield frame

    return StreamingResponse(
        _prepend_first_frame(),
        media_type="text/event-stream",
        headers={
            # Prevent proxy buffering.
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
