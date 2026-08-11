# SPDX-License-Identifier: Apache-2.0
"""Web search route for lm-chat.

Endpoint
--------
POST /api/search/web
    Form-encoded ``q: str``.  Returns ``list[SearchResult]``.
    Auth-gated.  Returns 502 if the search itself failed — every
    configured backend (SearXNG and, if applicable, the DDG fallback)
    was unreachable for the query.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from lmchat.logging import get_logger
from lmchat.routes._dependencies import require_user
from lmchat.services.auth_service import User
from lmchat.services.web_search_service import (
    SearchResult,
    WebSearchService,
    WebSearchUnavailable,
)

log = get_logger(__name__)

router = APIRouter(prefix="/api/search", tags=["web-search"])


def _get_web_search_service(request: Request) -> WebSearchService:
    """Return ``app.state.web_search_service``; raise ``RuntimeError`` if unset."""
    svc: WebSearchService | None = getattr(request.app.state, "web_search_service", None)
    if svc is None:
        raise RuntimeError(
            "app.state.web_search_service is not set — "
            "WebSearchService must be initialised in the app lifespan."
        )
    return svc


@router.post("/web", response_model=list[SearchResult])
async def web_search(
    request: Request,
    q: str = Form(..., min_length=1, max_length=512),
    user: User = Depends(require_user),
) -> list[SearchResult]:
    """Search the web and return structured results.

    Routed to SearXNG (or DuckDuckGo fallback per config). Returns a plain
    JSON array of ``SearchResult`` objects (Invariant 3).

    Returns:
        List of ``SearchResult`` objects (empty ONLY on a genuine
        zero-result search — never used to mask a backend failure).

    Raises:
        502: If every configured backend was unreachable for this query —
            distinct from an empty result list.
    """
    svc = _get_web_search_service(request)

    log.info("web_search.request", user_id=user.id, q_len=len(q))

    try:
        results = await svc.search(q)
    except WebSearchUnavailable as exc:
        log.error("web_search.unavailable", user_id=user.id, error=str(exc))
        raise HTTPException(
            status_code=502,
            detail=(
                "Web search failed: both SearXNG and the DuckDuckGo fallback "
                "were unreachable. This is not the same as zero results — "
                "try again shortly."
            ),
        ) from exc

    log.info("web_search.response", user_id=user.id, result_count=len(results))
    return results
