# SPDX-License-Identifier: Apache-2.0
"""Public unauthenticated share endpoint.

This router exposes a single read-only endpoint that takes a share token
(minted by ``POST /api/chats/{id}/share``) and returns a public view of
the chat plus its ordered messages, with no authentication required.

Mounted under ``/api/share`` so it remains distinct from the
authenticated ``/api/chats`` namespace (no overlapping prefix; no
dependency chain that would reject anonymous callers).

Privacy posture:
- Incognito chats can never reach this endpoint because the route layer
  refuses to mint a share token for them in the first place.
- Even if a row leaked into ``chat_shares`` for an incognito chat (e.g.
  a hostile direct INSERT), the public-view handler defensively checks
  the chat's incognito flag here too and returns 404.
- The response omits sensitive columns: no ``user_id``, no per-chat
  settings JSON blob, no audit-log timestamps beyond what's needed to
  render the public conversation.
"""
from __future__ import annotations

from datetime import datetime
from typing import Final

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from lmchat.logging import get_logger
from lmchat.routes._dependencies import get_chat_service_dep
from lmchat.services.chat_service import ChatService

log = get_logger(__name__)

_HTTP_404: Final[int] = 404


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class PublicMessage(BaseModel):
    """One message in a public share view.

    Strips out the columns a public viewer should not see (state, owner,
    response_id, model_id), retaining only what's needed to render the
    conversation.

    Attributes:
        id:                Row PK.
        role:              ``user`` | ``assistant`` | ``system`` | ``tool``.
        content:           Message body.
        reasoning_content: Optional chain-of-thought string.
        created_at:        Row creation timestamp.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    role: str
    content: str
    reasoning_content: str | None
    created_at: datetime


class PublicShareView(BaseModel):
    """Top-level response envelope for ``GET /api/share/{token}``.

    Attributes:
        title:      Chat display title.
        created_at: Chat creation timestamp.
        messages:   Ordered list of public messages (oldest first).
    """

    model_config = ConfigDict(from_attributes=True)

    title: str
    created_at: datetime
    messages: list[PublicMessage]


# ---------------------------------------------------------------------------
# Module-local dependency helpers
# ---------------------------------------------------------------------------


def _get_chat_service(request: Request) -> ChatService:
    """Return ``app.state.chat_service``."""
    return get_chat_service_dep(request)


router: APIRouter = APIRouter(prefix="/api/share", tags=["share"])


@router.get(
    "/{token}",
    response_model=PublicShareView,
    responses={
        404: {"description": "Share token not found, expired, revoked, or chat deleted/incognito"},
    },
)
async def get_public_share(
    token: str,
    chat_service: ChatService = Depends(_get_chat_service),
) -> PublicShareView:
    """Return the public read-only view of a shared chat.

    No authentication required.  Returns 404 when the token is unknown,
    when the underlying chat has been deleted, or when the chat is
    incognito (defensive double-check; the route that mints tokens
    already refuses incognito chats).

    Args:
        token:           Share token from the URL path.
        request:         FastAPI Request.
        chat_service:    Injected ``ChatService``.
        message_service: Injected ``MessageService`` (for message list).

    Returns:
        The :class:`PublicShareView` envelope.

    Raises:
        HTTPException: 404 if the token is unknown, the chat is gone,
                       or the chat is incognito.
    """
    share = await chat_service.get_share_by_token(token)
    if share is None:
        raise HTTPException(status_code=_HTTP_404, detail="share not found")

    chat = await chat_service.get_chat_unscoped(share.chat_id)
    if chat is None:
        # Chat was deleted after the share row was minted; the CASCADE on
        # the FK should usually clean this up but guard defensively.
        raise HTTPException(status_code=_HTTP_404, detail="share not found")

    # Defensive double-check: NEVER expose incognito chats publicly
    # even if a chat_shares row leaked in via direct INSERT.
    if chat.incognito:
        log.warning(
            "share.refused_incognito_at_read",
            chat_id=share.chat_id,
            token=token,
        )
        raise HTTPException(status_code=_HTTP_404, detail="share not found")

    rows = await chat_service.list_messages_unscoped(share.chat_id)

    return PublicShareView(
        title=chat.title,
        created_at=chat.created_at,
        messages=[
            PublicMessage(
                id=int(r.id),
                role=str(r.role),
                content=str(r.content),
                reasoning_content=(
                    str(r.reasoning_content) if r.reasoning_content is not None else None
                ),
                created_at=r.created_at,
            )
            for r in rows
        ],
    )
