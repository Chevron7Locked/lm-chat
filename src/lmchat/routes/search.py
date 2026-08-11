# SPDX-License-Identifier: Apache-2.0
"""Message search route for lm-chat.

The search endpoint delegates to ``MessageService.search()``, which is
dialect-aware (FTS5 on SQLite, pg_trgm / ILIKE on Postgres).  The
``has_pg_trgm`` flag is read from ``app.state.has_pg_trgm`` which is
probed once at startup.

**Scope behaviour**

- ``scope=messages`` (default) — FTS5 / similarity / ILIKE on the
  ``messages`` table via ``message_service.search()``. Returns
  ``list[Message]``.
- ``scope=chats`` — LIKE on ``chats.title`` (case-insensitive via LOWER).
  Returns ``list[dict]`` (chats table projection).
- ``scope=memory`` — fetches ``memory_service.list_pinned(user_id, project_id=...)`` (≤100
  rows per user cap) and filters in-Python using case-folded
  substring matching.  Returns ``list[MemoryInsight]``.
- ``scope=all`` — fan-out across all three; merges results into a dict with
  three keys: ``{"messages": [...], "chats": [...], "memory": [...]}``.
  All searches run concurrently via ``asyncio.gather``.

Results are ALWAYS scoped to the authenticated user — no cross-user data
is returned.  The route raises 401 for unauthenticated callers.
"""
from __future__ import annotations

import asyncio
from typing import Any, Final, Literal

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from lmchat.db.schema import chats as chats_table
from lmchat.logging import get_logger
from lmchat.routes._dependencies import (
    get_engine_dep,
    get_memory_service_dep,
    get_message_service_dep,
    require_user,
)
from lmchat.services.auth_service import User
from lmchat.services.memory_service import MemoryInsight, MemoryService
from lmchat.services.message_service import MessageService

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_LIMIT: Final[int] = 20

# ---------------------------------------------------------------------------
# Type alias for scope
# ---------------------------------------------------------------------------

SearchScope = Literal["chats", "messages", "memory", "all"]

# ---------------------------------------------------------------------------
# Module-local dependency helpers
# ---------------------------------------------------------------------------


def _get_message_service(request: Request) -> MessageService:
    """Return ``app.state.message_service``; raise ``RuntimeError`` if unset."""
    return get_message_service_dep(request)


def _get_projects_service(request: Request):  # noqa: ANN202
    """Return ``app.state.projects_service``; raise ``RuntimeError`` if unset.

    The search route accepts ``?project_id=``; this dep lets us
    404 on unknown / foreign IDs.
    """
    svc = getattr(request.app.state, "projects_service", None)
    if svc is None:
        raise RuntimeError(
            "app.state.projects_service is unset — lifespan did not run."
        )
    return svc


def _get_memory_service(request: Request) -> MemoryService:
    """Return ``app.state.memory_service``; raise ``RuntimeError`` if unset."""
    return get_memory_service_dep(request)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _search_chats(
    q: str,
    user_id: int,
    limit: int,
    engine: AsyncEngine,
    project_id: int | None = None,
) -> list[dict[str, Any]]:
    """Search chat titles with a case-insensitive LIKE.

    Args:
        q:          Search query.
        user_id:    Owning user ID (enforces user-scoping).
        limit:      Maximum rows returned.
        engine:     DB engine.
        project_id: When set, restrict to chats whose ``project_id``
                    matches. ``None`` preserves the legacy user-scoped
                    union.

    Returns:
        List of dicts matching the ``chats`` table projection.
    """
    pattern = f"%{q}%"
    where_clauses = [
        chats_table.c.user_id == user_id,
        func.lower(chats_table.c.title).like(func.lower(pattern)),
    ]
    if project_id is not None:
        where_clauses.append(chats_table.c.project_id == project_id)
    stmt = (
        select(chats_table)
        .where(*where_clauses)
        .order_by(chats_table.c.updated_at.desc())
        .limit(limit)
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(stmt)).fetchall()
    return [dict(r._mapping) for r in rows]


def _filter_memory(
    insights: list[MemoryInsight],
    q: str,
    limit: int,
) -> list[MemoryInsight]:
    """Filter pinned memory insights with a case-folded substring match.

    Args:
        insights: Pre-fetched list from ``memory_service.list_pinned()``.
        q:        Search query.
        limit:    Maximum rows returned.

    Returns:
        Filtered list (≤ limit items).
    """
    needle = q.casefold()
    return [m for m in insights if needle in m.text.casefold()][:limit]


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router: APIRouter = APIRouter(prefix="/api", tags=["search"])


# ---------------------------------------------------------------------------
# GET /api/search
# ---------------------------------------------------------------------------


@router.get("/search")
async def search_messages(
    request: Request,
    q: str = Query(..., min_length=1, max_length=1024),
    chat_id: int | None = None,
    limit: int = _DEFAULT_LIMIT,
    scope: SearchScope = "messages",
    project_id: int | None = None,
    user: User = Depends(require_user),
    message_service: MessageService = Depends(_get_message_service),
    memory_service: MemoryService = Depends(_get_memory_service),
    projects_service=Depends(_get_projects_service),
    engine: AsyncEngine = Depends(get_engine_dep),
) -> Any:
    """Search across user-owned surfaces matching query ``q``.

    All results are scoped to the authenticated user — no cross-user data
    is returned.

    **Response shapes by scope:**

    - ``messages`` → ``list[Message]``
    - ``chats``    → ``list[dict]`` (chats table projection)
    - ``memory``   → ``list[MemoryInsight]``
    - ``all``      → ``{"messages": list[Message], "chats": list[dict],
                         "memory": list[MemoryInsight]}``

    Args:
        request:         FastAPI Request (for ``app.state.has_pg_trgm``).
        q:               Free-text search query (1–1024 chars).
        chat_id:         Optional — restrict messages search to a single
                         chat.  Ignored for ``scope=chats`` and
                         ``scope=memory``.
        limit:           Maximum results per scope (default 20).
        scope:           One of ``messages``, ``chats``, ``memory``,
                         ``all``.  Defaults to ``messages`` for backwards
                         compatibility.
        project_id:      When set, restrict
                         memory-pin results (both ``scope=memory`` and
                         the ``memory`` arm of ``scope=all``) to pins
                         tagged with this project. 404 when the project
                         is unknown or not owned by the caller (ownership
                         check). Omits the filter when None
                         (legacy behavior).
        user:            Authenticated user.
        message_service: Injected ``MessageService``.
        memory_service:  Injected ``MemoryService``.
        projects_service: Injected ``ProjectsService`` for the
                         project_id ownership check.
        engine:          DB engine dependency.

    Returns:
        Varies by scope; see above.
    """
    has_pg_trgm: bool = bool(getattr(request.app.state, "has_pg_trgm", False))

    log.info(
        "search.request",
        user_id=user.id,
        q_len=len(q),
        chat_id=chat_id,
        limit=limit,
        scope=scope,
        project_id=project_id,
        has_pg_trgm=has_pg_trgm,
    )

    # Ownership check: when the caller asks for a specific project_id,
    # verify ownership BEFORE the scope branches so unknown / foreign
    # IDs 404 uniformly across every scope (rather than silently
    # 200-empty per the data-leak-blocked-but-confusing path). Skip
    # when project_id is None — that's the user-scoped union default.
    if project_id is not None:
        project = await projects_service.get(
            user_id=user.id, project_id=project_id
        )
        if project is None:
            from fastapi import HTTPException as _HTTPException

            raise _HTTPException(
                status_code=404, detail="project not found"
            )

    if scope == "messages":
        # Forward project_id so the parent chat JOIN inside
        # ``MessageService.search`` filters by it.
        return await message_service.search(
            user_id=user.id,
            query=q,
            chat_id=chat_id,
            project_id=project_id,
            limit=limit,
            has_pg_trgm=has_pg_trgm,
        )

    if scope == "chats":
        # Forward project_id to the helper.
        return await _search_chats(
            q,
            user_id=user.id,
            limit=limit,
            engine=engine,
            project_id=project_id,
        )

    if scope == "memory":
        # project_id forwards to list_pinned so pinned
        # insights from other projects don't leak into the search
        # results. None passes through unchanged (legacy behavior).
        pinned = await memory_service.list_pinned(
            user.id, project_id=project_id
        )
        return _filter_memory(pinned, q, limit)

    # scope == "all"
    # Fan-out with return_exceptions=True so a transient failure in one
    # scope (DB blip, LM Studio unreachable) does not 500 the whole
    # search — partial results are more useful than no results. Per-scope
    # exceptions are logged + included in the response under a "_errors"
    # key so clients can surface them.
    # Same project_id forwarding across ALL three sub-paths so the
    # scope=all fan-out inherits the scoping.
    messages_fut = message_service.search(
        user_id=user.id,
        query=q,
        chat_id=chat_id,
        project_id=project_id,
        limit=limit,
        has_pg_trgm=has_pg_trgm,
    )
    chats_fut = _search_chats(
        q,
        user_id=user.id,
        limit=limit,
        engine=engine,
        project_id=project_id,
    )
    memory_fut = memory_service.list_pinned(
        user.id, project_id=project_id
    )

    msg_result, chat_result, raw_memory = await asyncio.gather(
        messages_fut, chats_fut, memory_fut, return_exceptions=True
    )

    errors: dict[str, str] = {}

    if isinstance(msg_result, BaseException):
        log.warning("search.scope_all.messages_failed", error=str(msg_result))
        errors["messages"] = type(msg_result).__name__
        msg_results: list = []
    else:
        msg_results = msg_result  # type: ignore[assignment]

    if isinstance(chat_result, BaseException):
        log.warning("search.scope_all.chats_failed", error=str(chat_result))
        errors["chats"] = type(chat_result).__name__
        chat_results: list = []
    else:
        chat_results = chat_result  # type: ignore[assignment]

    if isinstance(raw_memory, BaseException):
        log.warning("search.scope_all.memory_failed", error=str(raw_memory))
        errors["memory"] = type(raw_memory).__name__
        memory_results: list = []
    else:
        memory_results = _filter_memory(raw_memory, q, limit)  # type: ignore[arg-type]

    response: dict[str, Any] = {
        "messages": msg_results,
        "chats": chat_results,
        "memory": memory_results,
    }
    if errors:
        response["_errors"] = errors
    return response
