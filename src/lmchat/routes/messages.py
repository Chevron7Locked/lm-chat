# SPDX-License-Identifier: Apache-2.0
"""Message edit / delete routes for lm-chat.

All routes:
- Require ``Depends(require_user)`` — unauthenticated requests → 401.
- Return 404 (never 403) on cross-user access.

Error mapping
-------------
``MessageNotFoundError``  → HTTP 404
``EditNotAllowedError``   → HTTP 400 (edit on non-user-role message)
"""
from __future__ import annotations

import asyncio
from typing import Final

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from lmchat.db.schema import messages as messages_table
from lmchat.logging import get_logger
from lmchat.routes._dependencies import (
    get_chat_locks_dep,
    get_engine_dep,
    get_message_service_dep,
    require_user,
)
from lmchat.services.auth_service import User
from lmchat.services.message_service import (
    EditNotAllowedError,
    Message,
    MessageNotFoundError,
    MessageService,
)

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# HTTP status constants
# ---------------------------------------------------------------------------

_HTTP_204: Final[int] = 204
_HTTP_400: Final[int] = 400
_HTTP_403: Final[int] = 403
_HTTP_404: Final[int] = 404
_HTTP_412: Final[int] = 412

# ---------------------------------------------------------------------------
# Module-local dependency helpers
# ---------------------------------------------------------------------------


def _get_message_service(request: Request) -> MessageService:
    """Return ``app.state.message_service``; raise ``RuntimeError`` if unset."""
    return get_message_service_dep(request)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router: APIRouter = APIRouter(prefix="/api/messages", tags=["messages"])


# ---------------------------------------------------------------------------
# PATCH /api/messages/{message_id} — edit content
# ---------------------------------------------------------------------------


@router.patch("/{message_id}", response_model=Message)
async def edit_message(
    message_id: int,
    request: Request,
    content: str = Form(...),
    user: User = Depends(require_user),
    message_service: MessageService = Depends(_get_message_service),
) -> Message:
    """Edit the content of a user-role message.

    Wire contract:
    * 400 with ``"only user messages are editable"`` when the message
      role is not ``"user"`` — assistant history is immutable.
    * 403 when the chat is owned by another user.
    * 404 when the message id does not exist.
    * 200 with the updated :class:`Message` on success.

    Args:
        message_id:      PK of the message to edit.
        request:         FastAPI Request.
        content:         Replacement content (form field).
        user:            Authenticated user.
        message_service: Injected ``MessageService``.

    Returns:
        The updated :class:`~lmchat.services.message_service.Message`.

    Raises:
        HTTPException: 400 / 403 / 404 per the contract above.
    """
    log.info("messages.edit.request", message_id=message_id, user_id=user.id)
    try:
        return await message_service.edit_user_message(
            message_id=message_id, user_id=user.id, content=content
        )
    except MessageNotFoundError as exc:
        raise HTTPException(
            status_code=_HTTP_404, detail="message not found"
        ) from exc
    except PermissionError as exc:
        raise HTTPException(
            status_code=_HTTP_403, detail="message not owned by you"
        ) from exc
    except EditNotAllowedError as exc:
        raise HTTPException(status_code=_HTTP_400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# DELETE /api/messages/{message_id}
# ---------------------------------------------------------------------------


@router.delete("/{message_id}", status_code=_HTTP_204)
async def delete_message(
    message_id: int,
    request: Request,
    user: User = Depends(require_user),
    message_service: MessageService = Depends(_get_message_service),
    engine: AsyncEngine = Depends(get_engine_dep),
    chat_locks: dict[int, asyncio.Lock] = Depends(get_chat_locks_dep),
) -> None:
    """Delete a message owned by the authenticated user.

    Cross-user access returns 404.

    Acquires the per-chat lock around the delete so that a concurrent
    ``ChatService.compact()`` on the same chat does not race against
    this standalone-message delete. SQLite row-level locking already
    serializes the writes, but acquiring the chat lock keeps the
    invariant set consistent (compact's archive candidates were computed
    against a message set that includes this row; without the lock,
    compact could compute its archive list, this delete fires between
    that and compact's actual archive ``UPDATE`` (hybrid compaction —
    archive, not delete), and compact's ``UPDATE ... WHERE id IN (...)``
    would silently affect 0 rows for the already-deleted message).

    Args:
        message_id:      PK of the message to delete.
        request:         FastAPI Request.
        user:            Authenticated user.
        message_service: Injected ``MessageService``.
        engine:          Injected engine (for chat-id lookup before lock).
        chat_locks:      Shared per-chat lock dict.

    Returns:
        204 No Content on success.

    Raises:
        HTTPException: 404 if the message is not found or not owned by the user.
    """
    log.info("messages.delete.request", message_id=message_id, user_id=user.id)

    # Resolve the chat_id so we can lock the right chat. Cross-user check
    # is the service's responsibility; here we just need the chat key.
    async with engine.connect() as conn:
        chat_id_result = await conn.execute(
            select(messages_table.c.chat_id).where(messages_table.c.id == message_id)
        )
        row = chat_id_result.first()
    if row is None:
        # Message doesn't exist — let the service raise MessageNotFoundError
        # for the canonical 404 response.
        try:
            await message_service.delete(message_id, user_id=user.id)
        except MessageNotFoundError as exc:
            raise HTTPException(
                status_code=_HTTP_404, detail="message not found"
            ) from exc
        return

    chat_id = int(row[0])
    lock = chat_locks.setdefault(chat_id, asyncio.Lock())
    async with lock:
        try:
            await message_service.delete(message_id, user_id=user.id)
        except MessageNotFoundError as exc:
            raise HTTPException(
                status_code=_HTTP_404, detail="message not found"
            ) from exc
