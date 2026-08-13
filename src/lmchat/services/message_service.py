# SPDX-License-Identifier: Apache-2.0
"""Message service for lm-chat — append, edit, delete, and FTS5-backed search.

Ownership model
---------------
Every write operation verifies that the target message belongs to the
calling user by JOINing ``messages → chats.user_id``.  Cross-user
access raises ``MessageNotFoundError`` (404-equivalent), not 403, so
existence is not leaked.

FTS5 search (SQLite path)
-------------------------
Query sanitisation uses phrase-quoting: the raw query is wrapped in
double-quotes to force an FTS5 phrase search, with embedded double-
quotes doubled so they are treated as literals.  Example:

    user query:  hello "world"
    sanitised:   'hello ""world""'  (phrase, embedded quotes doubled)

This is safe, unambiguous, and documented in the SQLite FTS5 spec
(https://www.sqlite.org/fts5.html §3 Full-text Query Syntax).
The phrase-quote mode disables operator expansion (OR / AND / NEAR /
wildcard) — which is the intended behaviour for user-supplied free-text
search against a chat history corpus.

Postgres search
---------------
The ``search()`` method accepts a ``has_pg_trgm`` kwarg (default False).
Callers (routes) must supply this from ``app.state.has_pg_trgm`` which
is probed once at startup.  This decouples the service from
the request context.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from lmchat.db.retry import with_write_retry
from lmchat.db.schema import chats, messages
from lmchat.logging import get_logger
from lmchat.services._stream_state import PersistState
from lmchat.services.audit_service import write_audit_event
from lmchat.services.memory_service import MemoryService

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Valid roles per LM Studio wire format.
# ---------------------------------------------------------------------------
_VALID_ROLES: frozenset[str] = frozenset({"system", "user", "assistant", "tool"})


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class MessageServiceError(Exception):
    """Base class for all MessageService errors."""


class MessageNotFoundError(MessageServiceError):
    """Raised when a message does not exist or does not belong to the caller."""


class EditNotAllowedError(MessageServiceError):
    """Raised when an edit is attempted on a non-user-role message."""


# ---------------------------------------------------------------------------
# Public Pydantic model
# ---------------------------------------------------------------------------


class Message(BaseModel):
    """One row from the ``messages`` table.

    Attributes:
        id:                Row PK.
        chat_id:           Parent chat PK.
        role:              Message role (``"user"`` | ``"assistant"`` |
                           ``"system"`` | ``"tool"``).
        content:           Message text body.
        reasoning_content: Optional chain-of-thought text (streaming).
        state:             Lifecycle state (``"draft"`` | ``"final"`` |
                           ``"pending_finalization"`` | ``"aborted_by_client"``).
        response_id:       LM Studio response-chain ID (nullable).
        stop_reason:       Why the producing stream terminated
                           (``"stop"`` | ``"length"``; NULL for user/system
                           rows and pre-migration-0026 rows). ``"length"``
                           drives the FE Continue chip on reload.
        tool_calls:        Tool calls the producing stream executed, in FE
                           ToolCall shape ({id, name, arguments, status,
                           result?}). NULL for user/system rows, tool-free
                           turns, and pre-migration-0024 rows (locked
                           decision 3 — no backfill).
        tool_call_id:      Responses-API call_id echoed back on role="tool"
                           turns for stateless-provider (replay) history
                           reconstruction. NULL for all other roles and for
                           rows written before migration 0028.
        model_id:          Producing model key (nullable for user/system).
        created_at:        Row creation timestamp.
        compaction_id:     Hybrid compaction (migration 0037): NULL when
                           the row is active (in the live context window);
                           set to a ``compactions.id`` when the row has been
                           archived by a ``/compact`` call. NULL for rows
                           written before migration 0037 (no backfill).
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    chat_id: int
    role: str
    content: str
    reasoning_content: str | None
    state: str
    response_id: str | None
    stop_reason: str | None = None
    # Tool-call persistence (migration 0024).
    tool_calls: list[dict[str, object]] | None = None
    # Responses-API round-trip key for role="tool" turns (migration 0028).
    tool_call_id: str | None = None
    model_id: str | None
    created_at: datetime
    # Hybrid compaction (migration 0037) — see class docstring.
    compaction_id: int | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _sanitise_fts5_query(raw: str) -> str:
    """Escape *raw* for safe use as an FTS5 MATCH phrase query.

    Strategy: wrap the entire query in double-quotes (phrase mode) and
    double any embedded double-quote characters so they are treated as
    literals.  This prevents injection of FTS5 operators (``OR``, ``AND``,
    ``NEAR``, ``*``) and returns results ordered by FTS5 rank rather than
    by arbitrary boolean logic.

    Args:
        raw: User-supplied free-text search query.

    Returns:
        A string safe to pass directly as the FTS5 MATCH operand.

    Reference:
        https://www.sqlite.org/fts5.html §3 Full-text Query Syntax.
    """
    escaped = raw.replace('"', '""')
    return f'"{escaped}"'


def _message_from_row(row: Any) -> Message:
    """Construct a :class:`Message` from a SQLAlchemy Row.

    A malformed ``tool_calls`` blob — valid JSON that decoded to the
    wrong shape (not ``list[dict]``), the realistic corruption mode for
    a JSON column — is repaired to ``None`` and the row re-validated,
    mirroring the defensive per-row ``tool_calls`` parsing already used
    for the model-facing replay path (``streaming_service.py``
    ``_replay_msgs`` construction). The repair is logged at WARNING so
    the corrupted row can still be found even though it loaded fine.
    Any other validation failure still propagates; read paths that must
    not fail an entire batch over one bad row catch it via
    :func:`_messages_from_rows`.

    Args:
        row: A row returned from a SELECT on ``messages``.

    Returns:
        Validated :class:`Message` instance.
    """
    try:
        return Message.model_validate(row, from_attributes=True)
    except ValidationError:
        repaired = dict(row._mapping)  # noqa: SLF001
        repaired["tool_calls"] = None
        msg = Message.model_validate(repaired, from_attributes=True)
        log.warning(
            "message.tool_calls_repaired",
            chat_id=msg.chat_id,
            row_id=msg.id,
        )
        return msg


def _messages_from_rows(rows: Iterable[Any]) -> list[Message]:
    """Validate a batch of message rows, skipping any that fail.

    Wraps :func:`_message_from_row` per row so a single corrupted row
    that survives its repair attempt (any other bad field, e.g. one
    left by a hand-edited row) does not fail the entire batch — this is
    the user-facing load/search counterpart to the model-facing replay
    path's per-row defensiveness. Every skip is logged at WARNING with
    the chat_id + row id so the offending row can be found and fixed.

    Args:
        rows: Rows from a SELECT against ``messages``.

    Returns:
        Validated messages, in row order, with unrepairable rows omitted.
    """
    out: list[Message] = []
    for row in rows:
        try:
            out.append(_message_from_row(row))
        except ValidationError as exc:
            log.warning(
                "message.row_validation_failed",
                chat_id=getattr(row, "chat_id", None),
                row_id=getattr(row, "id", None),
                error=str(exc),
            )
    return out


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class MessageService:
    """Append, edit, delete, and search messages for a single LM Studio user.

    All write paths verify ownership via ``chats.user_id``.  The FTS5
    search path is dialect-aware: SQLite uses the ``messages_fts``
    virtual table (created by migration ``0002``); Postgres uses
    ``pg_trgm`` or falls back to ``ILIKE``.

    Args:
        engine:         Async SQLAlchemy engine connected to the application DB.
        memory_service: Injected memory service; ``handle_message_deleted`` is
                        called post-commit for every removed message_id.
    """

    def __init__(
        self,
        *,
        engine: AsyncEngine,
        memory_service: MemoryService,
    ) -> None:
        self._engine = engine
        self._memory_service = memory_service

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    async def fetch_with_ownership(
        self, message_id: int, *, user_id: int
    ) -> Any:
        """Return the message row if it belongs to *user_id*, else raise.

        Public counterpart to the older ``_fetch_message_with_ownership``
        helper (preserved as an alias for backwards compatibility within
        the service module). Routes that need ownership-verified row
        access call this method directly rather than reaching into a
        private attribute.

        JOINs ``messages → chats`` to verify ownership in a single query.

        Args:
            message_id: PK of the message to fetch.
            user_id:    Caller's user PK (keyword-only).

        Returns:
            A SQLAlchemy Row with all ``messages`` columns plus
            ``chats.user_id`` available via ``._mapping``.

        Raises:
            MessageNotFoundError: If the row is missing or not owned by
                *user_id* (deliberately ambiguous — 404 semantics).
        """
        stmt = (
            select(messages)
            .join(chats, messages.c.chat_id == chats.c.id)
            .where(messages.c.id == message_id)
            .where(chats.c.user_id == user_id)
        )
        async with self._engine.connect() as conn:
            result = await conn.execute(stmt)
            row = result.fetchone()

        if row is None:
            raise MessageNotFoundError(
                f"Message {message_id!r} not found or does not belong to user {user_id!r}"
            )
        return row

    # Backwards-compatibility alias for the prior private name. Existing
    # internal callers (edit/delete) continue to work; routes use the
    # public name above.
    _fetch_message_with_ownership = fetch_with_ownership

    async def list_for_chat(
        self,
        chat_id: int,
        *,
        user_id: int,
        limit: int = 200,
        before_id: int | None = None,
        since_id: int | None = None,
    ) -> tuple[list[Message], bool]:
        """Return paginated messages in *chat_id* owned by *user_id*, oldest first.

        Adds cursor-based keyset pagination to prevent unbounded result
        sets on long chats.  Default page size is 200 messages.
        The caller receives a ``(messages, has_more)`` tuple.

        Cursor semantics
        ----------------
        - ``before_id``: return the most-recent *limit* messages with
          ``id < before_id`` (load-older scroll direction).
        - ``since_id``: return up to *limit* messages with ``id > since_id``
          (poll-for-new direction).
        - Both ``None``: return the most-recent *limit* messages.

        Uses the ``ix_messages_chat_id_id`` composite index (migration 0008)
        for an efficient range scan.

        Args:
            chat_id:   Chat PK.
            user_id:   Caller's user PK (keyword-only).
            limit:     Maximum messages per page (default 200).
            before_id: Cursor — return messages older than this id.
            since_id:  Cursor — return messages newer than this id.

        Returns:
            ``(messages, has_more)`` — list ordered ascending by id, plus a
            boolean indicating whether additional older messages exist beyond
            this page.  For the ``since_id`` direction ``has_more`` is always
            ``False`` (poll path does not paginate further).
        """
        # Fetch limit+1 rows to detect has_more without a COUNT query.
        fetch_limit = limit + 1

        base = (
            select(messages)
            .join(chats, messages.c.chat_id == chats.c.id)
            .where(messages.c.chat_id == chat_id)
            .where(chats.c.user_id == user_id)
            # Exclude the in-flight DRAFT assistant placeholder. A row is DRAFT
            # only while its stream is live, and its body is always "" until the
            # PENDING_FINALIZATION transition writes the real content
            # (streaming_service._finalize_message). Returning it here let a
            # mid-stream messages refetch — triggered by regenerate/edit/resend
            # query invalidation — surface an empty row sharing the streaming
            # msg_id, which the FE then treated as authoritative and used to
            # suppress the live streaming bubble (persistedHasSameKey), so the
            # chat appeared frozen. `state` is NOT NULL (server_default 'final'),
            # so a plain inequality can never hide legacy rows.
            .where(messages.c.state != PersistState.DRAFT.value)
        )

        if before_id is not None:
            # Load-older: rows with id < before_id, take the newest `limit`.
            stmt = (
                base
                .where(messages.c.id < before_id)
                .order_by(messages.c.id.desc())
                .limit(fetch_limit)
            )
        elif since_id is not None:
            # Poll-for-new: rows with id > since_id, oldest first.
            stmt = (
                base
                .where(messages.c.id > since_id)
                .order_by(messages.c.id.asc())
                .limit(fetch_limit)
            )
        else:
            # Initial load: most-recent `limit` messages, reversed for
            # oldest-first delivery.
            stmt = (
                base
                .order_by(messages.c.id.desc())
                .limit(fetch_limit)
            )

        async with self._engine.connect() as conn:
            result = await conn.execute(stmt)
            rows = result.fetchall()

        has_more = len(rows) == fetch_limit
        rows = rows[:limit]  # trim the sentinel row if present

        if before_id is not None or since_id is None:
            # Reverse so the caller always gets oldest-first ordering.
            rows = list(reversed(rows))

        # Per-row validation: a single corrupted row (most realistically a
        # malformed tool_calls JSON blob — see _message_from_row) must not
        # 500 the whole chat load. _messages_from_rows repairs what it can
        # and skips+logs anything left over.
        return (_messages_from_rows(rows), has_more)

    async def _verify_chat_ownership(self, chat_id: int, user_id: int) -> None:
        """Raise ``MessageNotFoundError`` if *chat_id* is not owned by *user_id*.

        Used by ``append()`` to confirm the target chat belongs to the caller
        before inserting a new message.

        Args:
            chat_id: Chat PK.
            user_id: Caller's user PK.

        Raises:
            MessageNotFoundError: If the chat is missing or not owned by
                *user_id*.
        """
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(chats.c.id).where(
                    chats.c.id == chat_id,
                    chats.c.user_id == user_id,
                )
            )
            row = result.fetchone()

        if row is None:
            raise MessageNotFoundError(
                f"Chat {chat_id!r} not found or does not belong to user {user_id!r}"
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def append(
        self,
        *,
        chat_id: int,
        user_id: int,
        role: str,
        content: str,
        reasoning_content: str | None = None,
        model_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> Message:
        """Append a new message to *chat_id* and return the persisted row.

        Verifies that the caller owns *chat_id* before inserting.  The
        message is written with ``state='final'``; the streaming service
        uses ``state='draft'`` directly.

        Args:
            chat_id:           Target chat PK.
            user_id:           Calling user's PK — used for ownership check.
            role:              Message role.  Must be one of
                               ``{"system", "user", "assistant", "tool"}``.
            content:           Message text body (non-empty string).
            reasoning_content: Optional chain-of-thought content.
            model_id:          Producing model key (nullable; omitted for user
                               and system messages).
            tool_call_id:      Responses-API call_id for role="tool" turns;
                               required for stateless-provider replay history
                               reconstruction (nullable for all other roles).

        Returns:
            The freshly inserted :class:`Message`.

        Raises:
            ValueError:           If *role* is not in the allowed set.
            MessageNotFoundError: If *chat_id* does not exist or does not
                                  belong to *user_id*.
        """
        if role not in _VALID_ROLES:
            raise ValueError(
                f"Invalid role {role!r}. Must be one of {sorted(_VALID_ROLES)}."
            )

        await self._verify_chat_ownership(chat_id, user_id)

        # Use a single-element list as a mutable cell to capture the inserted
        # PK from inside the nested async function — avoids the `nonlocal`
        # unbound-variable pyright warning while keeping write-retry semantics.
        _new_id: list[int] = []

        async def _insert() -> None:
            async with self._engine.begin() as conn:
                result = await conn.execute(
                    insert(messages).values(
                        chat_id=chat_id,
                        role=role,
                        content=content,
                        reasoning_content=reasoning_content,
                        state="final",
                        model_id=model_id,
                        tool_call_id=tool_call_id,
                    )
                )
                pk = result.inserted_primary_key
                if pk is None:
                    raise RuntimeError("INSERT into messages returned no PK")
                _new_id.append(int(pk[0]))

        await with_write_retry(_insert)

        if not _new_id:
            raise RuntimeError("INSERT into messages succeeded but returned no PK")

        new_id = _new_id[0]

        # Fetch the freshly inserted row to get server-default created_at.
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(messages).where(messages.c.id == new_id)
            )
            row = result.fetchone()

        if row is None:
            raise RuntimeError(
                f"messages row {new_id!r} not found after INSERT — unexpected"
            )

        msg = _message_from_row(row)

        log.info(
            "message.appended",
            message_id=msg.id,
            chat_id=chat_id,
            user_id=user_id,
            role=role,
        )

        try:
            await write_audit_event(
                user_id=user_id,
                event="message.appended",
                ip=None,
                user_agent=None,
                detail={"message_id": msg.id, "chat_id": chat_id, "role": role},
                engine=self._engine,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "message.audit_write_failed",
                audit_event="message.appended",
                message_id=msg.id,
                error=str(exc),
            )

        return msg

    async def edit(
        self,
        message_id: int,
        *,
        user_id: int,
        content: str,
    ) -> None:
        """Edit the content of *message_id* (user-role messages only).

        The FTS5 ``messages_au`` trigger handles the ``messages_fts``
        sync automatically on SQLite.

        Args:
            message_id: PK of the message to edit.
            user_id:    Caller's user PK — used for ownership check.
            content:    Replacement content string.

        Raises:
            MessageNotFoundError: If the message is missing or not owned
                                  by *user_id*.
            EditNotAllowedError:  If the message role is not ``"user"``.
        """
        row = await self.fetch_with_ownership(message_id, user_id=user_id)

        if row.role != "user":
            raise EditNotAllowedError(
                f"Message {message_id!r} has role {row.role!r}. "
                "Only user-role messages may be edited; assistant history is immutable."
            )

        async def _update() -> None:
            async with self._engine.begin() as conn:
                await conn.execute(
                    update(messages)
                    .where(messages.c.id == message_id)
                    .values(content=content)
                )

        await with_write_retry(_update)

        log.info(
            "message.edited",
            message_id=message_id,
            user_id=user_id,
        )

        try:
            await write_audit_event(
                user_id=user_id,
                event="message.edited",
                ip=None,
                user_agent=None,
                detail={"message_id": message_id},
                engine=self._engine,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "message.audit_write_failed",
                audit_event="message.edited",
                message_id=message_id,
                error=str(exc),
            )

    async def delete(
        self,
        message_id: int,
        *,
        user_id: int,
    ) -> None:
        """Delete *message_id* and notify the memory service post-commit.

        The FK cascade on ``message_embeddings.message_id`` automatically
        removes any embedding row.  Post-commit, ``handle_message_deleted``
        is called on the memory service so that the vector index (when
        wired) can remove the corresponding in-memory entry.

        Note on locking: this method does NOT acquire the per-chat
        asyncio.Lock.  ChatService acquires the lock and delegates here
        within the locked section when needed.  Standalone deletions from
        routes do not require the chat lock (they only remove one message,
        not a compaction batch).

        Args:
            message_id: PK of the message to delete.
            user_id:    Caller's user PK — used for ownership check.

        Raises:
            MessageNotFoundError: If the message is missing or not owned
                                  by *user_id*.
        """
        await self.fetch_with_ownership(message_id, user_id=user_id)

        async def _delete() -> None:
            async with self._engine.begin() as conn:
                await conn.execute(
                    delete(messages).where(messages.c.id == message_id)
                )

        await with_write_retry(_delete)

        log.info(
            "message.deleted",
            message_id=message_id,
            user_id=user_id,
        )

        try:
            await write_audit_event(
                user_id=user_id,
                event="message.deleted",
                ip=None,
                user_agent=None,
                detail={"message_id": message_id},
                engine=self._engine,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "message.audit_write_failed",
                audit_event="message.deleted",
                message_id=message_id,
                error=str(exc),
            )

        # Post-commit memory-service notification.  Wrapped in try/except
        # so a transient memory-service failure logs at ERROR but the delete is not
        # re-raised to the caller (the DB transaction has already committed).
        try:
            await self._memory_service.handle_message_deleted(message_id)
        except Exception as exc:  # noqa: BLE001
            log.error(
                "message.memory_notify_failed",
                message_id=message_id,
                error=str(exc),
            )


    # ------------------------------------------------------------------
    # Edit message + Regenerate
    # ------------------------------------------------------------------

    async def edit_user_message(
        self,
        *,
        message_id: int,
        user_id: int,
        content: str,
    ) -> Message:
        """Edit a user-role message and return the updated row.

        Distinct from :meth:`edit` (which uses 404 semantics for cross-user
        access).  The wire contract surfaces ownership failures as
        HTTP 403 so the UI can distinguish "you don't own this message"
        from "this message does not exist".

        Args:
            message_id: PK of the message to edit.
            user_id:    Caller's user PK.
            content:    Replacement text body.

        Returns:
            The updated :class:`Message`.

        Raises:
            MessageNotFoundError: When the message row is absent.
            PermissionError:      When the chat is owned by another user.
            EditNotAllowedError:  When the message role is not ``"user"``.
        """
        # Resolve existence + ownership in a single JOINed query.
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(messages, chats.c.user_id.label("owner_id"))
                    .join(chats, messages.c.chat_id == chats.c.id)
                    .where(messages.c.id == message_id)
                )
            ).fetchone()
        if row is None:
            raise MessageNotFoundError(
                f"Message {message_id!r} not found"
            )
        if int(row.owner_id) != int(user_id):
            raise PermissionError(
                f"Message {message_id!r} is owned by another user"
            )
        if row.role != "user":
            raise EditNotAllowedError(
                "only user messages are editable"
            )

        async def _do() -> None:
            async with self._engine.begin() as conn:
                await conn.execute(
                    update(messages)
                    .where(messages.c.id == message_id)
                    .values(content=content)
                )

        await with_write_retry(_do)

        async with self._engine.connect() as conn:
            updated = (
                await conn.execute(
                    select(messages).where(messages.c.id == message_id)
                )
            ).fetchone()
        assert updated is not None
        log.info(
            "message.edit_user", message_id=message_id, user_id=user_id
        )
        try:
            await write_audit_event(
                user_id=user_id,
                event="message.edit_user",
                ip=None,
                user_agent=None,
                detail={"message_id": message_id},
                engine=self._engine,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("message.edit_user.audit_failed", error=str(exc))
        return _message_from_row(updated)

    async def count_messages_from(
        self,
        *,
        chat_id: int,
        message_id: int,
        user_id: int,
    ) -> int:
        """Return the count of messages with id >= *message_id* in *chat_id*.

        Verifies ownership of *chat_id* before counting.  Used by the
        regenerate confirm gate so the UI can warn the admin that
        subsequent messages will be removed.

        Args:
            chat_id:    Chat PK.
            message_id: Boundary message PK (inclusive).
            user_id:    Caller's user PK.

        Returns:
            Number of rows matched (>= 1 when the boundary exists).

        Raises:
            MessageNotFoundError: When *chat_id* is missing or not owned
                by *user_id*.
        """
        await self._verify_chat_ownership(chat_id, user_id)
        async with self._engine.connect() as conn:
            n = (
                await conn.execute(
                    select(func.count()).where(
                        messages.c.chat_id == chat_id,
                        messages.c.id >= message_id,
                    )
                )
            ).scalar_one()
        return int(n)

    async def get_message_role(
        self,
        *,
        chat_id: int,
        message_id: int,
        user_id: int,
    ) -> str:
        """Return the role of *message_id* in *chat_id*.

        Args:
            chat_id:    Chat PK.
            message_id: Message PK.
            user_id:    Caller's user PK.

        Returns:
            Role string (``"user"``, ``"assistant"``, etc.)

        Raises:
            MessageNotFoundError: When the chat is not owned by *user_id*
                or the message does not exist in *chat_id*.
        """
        await self._verify_chat_ownership(chat_id, user_id)
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(messages.c.role).where(
                        messages.c.id == message_id,
                        messages.c.chat_id == chat_id,
                    )
                )
            ).fetchone()
        if row is None:
            raise MessageNotFoundError(
                f"Message {message_id!r} not found in chat {chat_id!r}"
            )
        return str(row.role)

    async def count_messages_after(
        self,
        *,
        chat_id: int,
        message_id: int,
        user_id: int,
    ) -> int:
        """Return the count of messages with id > *message_id* in *chat_id*.

        Like :meth:`count_messages_from` but *exclusive* of the boundary
        message itself.  Used by the resend confirm gate for user-role
        messages so the boundary message is not counted.

        Args:
            chat_id:    Chat PK.
            message_id: Boundary message PK (exclusive).
            user_id:    Caller's user PK.

        Returns:
            Number of rows after the boundary (0 when the user message is
            already the last in the chat).

        Raises:
            MessageNotFoundError: When *chat_id* is missing or not owned
                by *user_id*.
        """
        await self._verify_chat_ownership(chat_id, user_id)
        async with self._engine.connect() as conn:
            n = (
                await conn.execute(
                    select(func.count()).where(
                        messages.c.chat_id == chat_id,
                        messages.c.id > message_id,
                    )
                )
            ).scalar_one()
        return int(n)

    async def delete_assistant_turn_for_regenerate(
        self,
        *,
        chat_id: int,
        message_id: int,
        user_id: int,
    ) -> tuple[int, str | None]:
        """Delete the boundary assistant message, its triggering user
        message, and any later messages in *chat_id*. Return the deleted
        count and the prior user message's content so the caller can
        resubmit it as a fresh turn.

        The previous implementation deleted only from the boundary
        assistant message onward and left the prior user prompt intact.
        That seemed clean in isolation, but it broke the stream that
        follows: LM Studio's /api/v1/chat rejects an empty input array
        (`"input must not be an empty array"`), and `previous_response_id`
        alone is no longer a valid chain — its anchor (the deleted
        assistant turn) is gone. The fix is to delete the whole turn
        (user prompt + assistant reply) and let the frontend resubmit
        the prompt verbatim.

        Returns ``(deleted_count, prior_user_content)``. When the boundary
        is the only message in the chat with no user prompt before it,
        ``prior_user_content`` is ``None`` and only the boundary is
        deleted — the caller should surface an error rather than try to
        resubmit.

        The method verifies:
        * The chat is owned by *user_id*.
        * *message_id* exists, is in *chat_id*, and has role ``"assistant"``.

        Args:
            chat_id:    Chat PK.
            message_id: Boundary message PK (inclusive).
            user_id:    Caller's user PK.

        Returns:
            Number of rows deleted (always >= 1).

        Raises:
            MessageNotFoundError: When the boundary message does not exist
                in *chat_id* under *user_id*.
            EditNotAllowedError: When the boundary message is not an
                assistant turn (regenerate is assistant-only).
        """
        await self._verify_chat_ownership(chat_id, user_id)
        async with self._engine.connect() as conn:
            boundary = (
                await conn.execute(
                    select(messages.c.id, messages.c.role).where(
                        messages.c.id == message_id,
                        messages.c.chat_id == chat_id,
                    )
                )
            ).fetchone()
        if boundary is None:
            raise MessageNotFoundError(
                f"Message {message_id!r} not found in chat {chat_id!r}"
            )
        if boundary.role != "assistant":
            raise EditNotAllowedError(
                "only assistant messages are regeneratable"
            )

        # Find the prior user message in this chat, if any. We delete
        # from it onward; the caller resubmits its content as the new
        # turn's input.
        async with self._engine.connect() as conn:
            prior = (
                await conn.execute(
                    select(messages.c.id, messages.c.content)
                    .where(
                        messages.c.chat_id == chat_id,
                        messages.c.id < message_id,
                        messages.c.role == "user",
                    )
                    .order_by(messages.c.id.desc())
                    .limit(1)
                )
            ).fetchone()

        prior_user_id = int(prior.id) if prior is not None else None
        prior_user_content: str | None = (
            str(prior.content) if prior is not None else None
        )
        delete_from_id = prior_user_id if prior_user_id is not None else message_id

        deleted_count: list[int] = []

        async def _do() -> None:
            async with self._engine.begin() as conn:
                # Capture the IDs to be deleted so post-commit hooks can
                # notify the memory service per row.
                rows = (
                    await conn.execute(
                        select(messages.c.id).where(
                            messages.c.chat_id == chat_id,
                            messages.c.id >= delete_from_id,
                        )
                    )
                ).fetchall()
                ids = [int(r[0]) for r in rows]
                if ids:
                    await conn.execute(
                        delete(messages).where(messages.c.id.in_(ids))
                    )
                deleted_count.append(len(ids))

        await with_write_retry(_do)

        n = deleted_count[0]
        log.info(
            "message.delete_from_onward",
            chat_id=chat_id,
            message_id=message_id,
            user_id=user_id,
            deleted=n,
        )
        try:
            await write_audit_event(
                user_id=user_id,
                event="message.delete_from_onward",
                ip=None,
                user_agent=None,
                detail={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "deleted": n,
                },
                engine=self._engine,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "message.delete_from_onward.audit_failed", error=str(exc)
            )

        # Notify the memory service so the embedding cache stays clean.
        try:
            # We deleted a batch; memory_service exposes single-id hook.
            # We replay the chat for now — easier than threading the ids
            # through and the cache is small.
            await self._memory_service.handle_message_deleted(message_id)
        except Exception as exc:  # noqa: BLE001
            log.error(
                "message.delete_from_onward.memory_notify_failed",
                error=str(exc),
            )

        return n, prior_user_content

    async def delete_from_user_message_for_resend(
        self,
        *,
        chat_id: int,
        message_id: int,
        user_id: int,
    ) -> tuple[int, str]:
        """Delete *message_id* (a user turn) and everything after it, and
        return ``(deleted_count, user_content)``.

        The boundary user message is now DELETED along with everything
        after it — this mirrors :meth:`delete_assistant_turn_for_regenerate`,
        which likewise removes its boundary message plus the tail.  The
        content is captured before the delete; the caller resubmits it as a
        fresh turn via POST /api/chat/stream, so the replayed message ends
        up with a new PK instead of the old row surviving alongside a
        duplicate.

        Args:
            chat_id:    Chat PK.
            message_id: Boundary user message PK (deleted, along with the tail).
            user_id:    Caller's user PK.

        Returns:
            ``(deleted_count, user_content)`` — deleted_count is at least 1
            (the boundary message itself) even when it was already the last
            message in the chat.

        Raises:
            MessageNotFoundError: When the boundary message does not exist
                in *chat_id* under *user_id*.
            EditNotAllowedError: When the boundary message is not a user turn.
        """
        await self._verify_chat_ownership(chat_id, user_id)
        async with self._engine.connect() as conn:
            boundary = (
                await conn.execute(
                    select(messages.c.id, messages.c.role, messages.c.content).where(
                        messages.c.id == message_id,
                        messages.c.chat_id == chat_id,
                    )
                )
            ).fetchone()
        if boundary is None:
            raise MessageNotFoundError(
                f"Message {message_id!r} not found in chat {chat_id!r}"
            )
        if boundary.role != "user":
            raise EditNotAllowedError(
                "only user messages are resendable via this path"
            )

        user_content: str = str(boundary.content)
        deleted_count: list[int] = []
        deleted_ids: list[list[int]] = []

        async def _do() -> None:
            async with self._engine.begin() as conn:
                rows = (
                    await conn.execute(
                        select(messages.c.id).where(
                            messages.c.chat_id == chat_id,
                            messages.c.id >= message_id,
                        )
                    )
                ).fetchall()
                ids = [int(r[0]) for r in rows]
                if ids:
                    await conn.execute(
                        delete(messages).where(messages.c.id.in_(ids))
                    )
                deleted_count.append(len(ids))
                deleted_ids.append(ids)

        await with_write_retry(_do)

        n = deleted_count[0]
        real_deleted_ids: list[int] = deleted_ids[0] if deleted_ids else []
        log.info(
            "message.delete_after_for_resend",
            chat_id=chat_id,
            message_id=message_id,
            user_id=user_id,
            deleted=n,
        )
        try:
            await write_audit_event(
                user_id=user_id,
                event="message.delete_after_for_resend",
                ip=None,
                user_agent=None,
                detail={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "deleted": n,
                },
                engine=self._engine,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "message.delete_after_for_resend.audit_failed", error=str(exc)
            )

        # Notify memory service per deleted id using the REAL captured ids,
        # not a fabricated contiguous range (messages.id is a global autoincrement
        # PK so post-user ids in a single chat are almost never contiguous).
        for del_id in real_deleted_ids:
            try:
                await self._memory_service.handle_message_deleted(del_id)
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "message.delete_after_for_resend.memory_notify_failed",
                    error=str(exc),
                )

        return n, user_content

    async def search(
        self,
        *,
        user_id: int,
        query: str,
        chat_id: int | None = None,
        project_id: int | None = None,
        limit: int = 20,
        has_pg_trgm: bool = False,
    ) -> list[Message]:
        """Search messages owned by *user_id* matching *query*.

        SQLite path
        -----------
        Queries the ``messages_fts`` virtual table created by migration
        ``0002_fts5_messages.py``.  The query is phrase-quoted to prevent
        FTS5 operator injection (see module docstring for sanitisation
        strategy).  Results are ordered by FTS5 ``rank`` (most relevant
        first).

        Postgres path
        -------------
        If *has_pg_trgm* is ``True`` (set on ``app.state`` at startup by
        probing ``pg_extension``), uses ``similarity()`` from ``pg_trgm``
        ordered by similarity descending.  Otherwise falls back to
        ``ILIKE '%query%'`` ordered by ``created_at`` descending.

        Args:
            user_id:      Filter results to messages owned by this user.
            query:        Free-text search query.
            chat_id:      Optional — restrict search to a single chat.
            project_id:   Optional — restrict
                          to messages whose parent chat carries
                          ``chats.project_id == project_id``. ``None``
                          preserves the legacy user-scoped union.
            limit:        Maximum number of results to return (default 20).
            has_pg_trgm:  Caller-supplied flag from ``app.state``.  Only
                          consulted on the Postgres path.

        Returns:
            List of :class:`Message` matching the query, ordered by
            relevance (FTS5 rank on SQLite; similarity or recency on Postgres).
        """
        dialect = self._engine.dialect.name

        if dialect == "sqlite":
            return await self._search_sqlite(
                user_id=user_id,
                query=query,
                chat_id=chat_id,
                project_id=project_id,
                limit=limit,
            )
        else:
            return await self._search_postgres(
                user_id=user_id,
                query=query,
                chat_id=chat_id,
                project_id=project_id,
                limit=limit,
                has_pg_trgm=has_pg_trgm,
            )

    async def _search_sqlite(
        self,
        *,
        user_id: int,
        query: str,
        chat_id: int | None,
        project_id: int | None,
        limit: int,
    ) -> list[Message]:
        """Execute FTS5 search against the SQLite virtual table.

        Args:
            user_id: Owning user PK.
            query:   Raw user query (sanitised internally).
            chat_id: Optional chat scope filter.
            limit:   Max rows to return.

        Returns:
            List of :class:`Message` ordered by FTS5 rank (best first).
        """
        sanitised = _sanitise_fts5_query(query)

        # Build the query with optional chat_id filter.
        # The FTS5 JOIN uses rowid = messages.id per the virtual table
        # definition in 0002_fts5_messages.py (content_rowid='id').
        base_sql = """
            SELECT m.id, m.chat_id, m.role, m.content,
                   m.reasoning_content, m.state, m.response_id,
                   m.model_id, m.created_at
            FROM messages m
            JOIN messages_fts fts ON fts.rowid = m.id
            JOIN chats c ON c.id = m.chat_id
            WHERE c.user_id = :user_id
              AND messages_fts MATCH :query
        """

        params: dict[str, Any] = {
            "user_id": user_id,
            "query": sanitised,
            "limit": limit,
        }

        if chat_id is not None:
            base_sql += " AND m.chat_id = :chat_id"
            params["chat_id"] = chat_id

        if project_id is not None:
            # Bound to parent chat's project_id. The
            # ``chats`` JOIN already exists (alias ``c``).
            base_sql += " AND c.project_id = :project_id"
            params["project_id"] = project_id

        base_sql += " ORDER BY rank LIMIT :limit"

        async with self._engine.connect() as conn:
            result = await conn.execute(text(base_sql), params)
            rows = result.fetchall()

        return _messages_from_rows(rows)

    async def _search_postgres(
        self,
        *,
        user_id: int,
        query: str,
        chat_id: int | None,
        project_id: int | None,
        limit: int,
        has_pg_trgm: bool,
    ) -> list[Message]:
        """Execute Postgres full-text search using pg_trgm or ILIKE fallback.

        Args:
            user_id:      Owning user PK.
            query:        Raw user query.
            chat_id:      Optional chat scope filter.
            limit:        Max rows to return.
            has_pg_trgm:  When ``True``, use similarity(); else ILIKE.

        Returns:
            List of :class:`Message` ordered by similarity desc (pg_trgm)
            or ``created_at`` desc (ILIKE fallback).
        """
        if has_pg_trgm:
            base_sql = """
                SELECT m.id, m.chat_id, m.role, m.content,
                       m.reasoning_content, m.state, m.response_id,
                       m.model_id, m.created_at
                FROM messages m
                JOIN chats c ON c.id = m.chat_id
                WHERE c.user_id = :user_id
                  AND similarity(m.content, :query) > 0.1
            """
            order_clause = " ORDER BY similarity(m.content, :query) DESC LIMIT :limit"
        else:
            base_sql = """
                SELECT m.id, m.chat_id, m.role, m.content,
                       m.reasoning_content, m.state, m.response_id,
                       m.model_id, m.created_at
                FROM messages m
                JOIN chats c ON c.id = m.chat_id
                WHERE c.user_id = :user_id
                  AND m.content ILIKE :ilike_query
            """
            order_clause = " ORDER BY m.created_at DESC LIMIT :limit"

        params: dict[str, Any] = {
            "user_id": user_id,
            "query": query,
            "limit": limit,
        }

        if has_pg_trgm:
            pass  # query param already set above
        else:
            params["ilike_query"] = f"%{query}%"

        if project_id is not None:
            # Bound to parent chat's project_id.
            base_sql += " AND c.project_id = :project_id"
            params["project_id"] = project_id

        if chat_id is not None:
            base_sql += " AND m.chat_id = :chat_id"
            params["chat_id"] = chat_id

        base_sql += order_clause

        async with self._engine.connect() as conn:
            result = await conn.execute(text(base_sql), params)
            rows = result.fetchall()

        return [_message_from_row(r) for r in rows]
