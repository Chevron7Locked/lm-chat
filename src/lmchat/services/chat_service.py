# SPDX-License-Identifier: Apache-2.0
"""Chat lifecycle service for lm-chat.

Responsibilities:
- CRUD: create / list_for_user / get / rename / move_to_folder / pin / delete.
- fork(): snapshot a chat's messages up to a given message_id.
- compact(): head-trim with invariant preservation (system prompt, latest user
  message, tool-call pairs) using tiktoken for token counting.
- Per-chat asyncio.Lock for compact() + streaming-service serialization.
- Post-commit memory_service.handle_message_deleted notifications.
- Audit log rows for every chat lifecycle event.
"""
from __future__ import annotations

import asyncio
import re
import secrets
from datetime import datetime
from typing import Any, Final

import httpx
import tiktoken
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from lmchat.db.retry import with_write_retry
from lmchat.db.schema import chat_shares, chats, compactions, messages, sub_sessions
from lmchat.db.scope import project_scope_clause
from lmchat.lmstudio.oob_text import oob_message_text
from lmchat.logging import get_logger
from lmchat.services._stream_state import PersistState
from lmchat.services.audit_service import AuditEvent, write_audit_event
from lmchat.services.bg_aux import bg_aux_slot
from lmchat.services.memory_service import MemoryService
from lmchat.services.message_service import Message
from lmchat.services.models_service import (
    ModelsService,
    resolve_background_model_id,
)
from lmchat.services.substance_fold import _SALVAGE_PREFIX
from lmchat.utils.text_input_policy import DEFAULT_MAX_LENGTH

log = get_logger(__name__)

_DEFAULT_ENCODING: Final[str] = "cl100k_base"

# 10% safety margin for both the invariant-minimum check and the drop-until target.
_SAFETY_MARGIN: Final[float] = 0.10

# ---------------------------------------------------------------------------
# Auto-title generation
# ---------------------------------------------------------------------------

# "Auto-assigned" titles eligible for replacement; anything else is user-set
# and never overwritten (manual rename collision guard).
_AUTO_TITLE_DEFAULT_VALUES: Final[frozenset[str]] = frozenset(
    {"", "New Chat", "Incognito Chat"}
)

_AUTO_TITLE_SYSTEM_PROMPT: Final[str] = (
    "You generate a concise 3-6 word title for a conversation. "
    "Output ONLY the title — no quotes, no explanation, no trailing punctuation."
)

# The instruction appended after the transcript in the title-generation user
# message. Kept as a named constant so _TITLE_INSTRUCTION_MARKERS below (and its
# drift-guard test) stay coupled to the exact wording sent to the model.
_AUTO_TITLE_USER_INSTRUCTION: Final[str] = (
    "Write a concise 3-6 word title for this conversation. Output ONLY the title."
)

# 4 = up to 2 user/assistant turn pairs fed into the title generator.
_AUTO_TITLE_MAX_HISTORY_MESSAGES: Final[int] = 4

_AUTO_TITLE_MAX_CHARS: Final[int] = 80

# Distinctive phrases from the two title-generation prompts above
# (_AUTO_TITLE_SYSTEM_PROMPT / _AUTO_TITLE_USER_INSTRUCTION). A reasoning or
# background model sometimes echoes the instruction verbatim instead of emitting
# a title; a "title" containing any of these is an echo, not a title. If either
# prompt's wording changes, update these markers — the drift-guard test in
# test_autotitle_sanitiser.py feeds both live prompts through the sanitiser and
# goes red if an echo would no longer be caught.
_TITLE_INSTRUCTION_MARKERS: Final[tuple[str, ...]] = (
    "output only the title",
    "word title for this conversation",
    "concise 3-6 word",
    "no quotes, no explanation",
)

# Reasoning models spend ~1k tokens deliberating before the title; a tiny cap
# truncates mid-think and returns empty content, so 1024 leaves room for both.
_AUTO_TITLE_MAX_TOKENS: Final[int] = 1024


# ---------------------------------------------------------------------------
# Hybrid compaction — summarize + archive
# ---------------------------------------------------------------------------

# Fraction of target_tokens budgeted for the generated summary's max_tokens.
_COMPACTION_SUMMARY_BUDGET_RATIO: Final[float] = 0.25

# Floor so a tiny target_tokens doesn't starve the summary call entirely.
_COMPACTION_SUMMARY_MIN_TOKENS: Final[int] = 64

# Extra budget on top of the summary-output budget so a reasoning model can
# finish reasoning before it emits the summary — otherwise a small
# target_tokens truncates it mid-reasoning and content comes back empty.
_COMPACTION_SUMMARY_REASONING_HEADROOM: Final[int] = 2048

# Per-archived-message content cap fed into the summary prompt (mirrors
# generate_title's guard so one huge pasted document can't blow the context).
_COMPACTION_SUMMARY_MSG_CHAR_CAP: Final[int] = 2_000

# Ceiling on contiguous runs a single compact() call will summarize + archive.
# Retained tool-call pairs scattered through history can split the archive set
# into many runs, and every run costs a full summarizer LLM call, so an
# unbounded run count means unbounded upstream calls. Runs are built
# oldest-first, so the cap keeps the highest-value (earliest) archives; excess
# runs stay live and are picked up by a later /compact call.
_COMPACTION_MAX_RUNS_PER_CALL: Final[int] = 6

_COMPACTION_SUMMARY_SYSTEM_PROMPT: Final[str] = (
    "You maintain a factual running summary of an ongoing conversation whose "
    "oldest turns are about to be archived. Condense the given turns into a "
    "concise, factual summary covering: decisions made, constraints "
    "established, entities/names introduced, and any open threads or "
    "unresolved questions. Write in third person, plain prose — no bullet "
    "points, no headers, no meta-commentary about summarizing. Output ONLY "
    "the summary text."
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ChatServiceError(Exception):
    """Base class for all ChatService errors."""


class ChatNotFoundError(ChatServiceError):
    """Chat missing OR not owned by the requesting user.

    Doesn't distinguish "missing" from "not yours" — HTTP 404 only,
    never 403 (which would leak existence).
    """


class CompactTooLowError(ChatServiceError):
    """target_tokens is below the invariant-preserved message floor.

    Maps to HTTP 422 at the route layer.
    """


class CompactionSummaryError(ChatServiceError):
    """The LLM summarization call failed during compaction.

    Fail policy = ABORT (unlike title generation's best-effort fallback or the
    OOB distill/followups calls' fail-soft ``[]``): archiving a span without a
    real summary loses context with no way back. Raised before any DB write;
    maps to HTTP 502 at the route layer.
    """


class ChatNotShareableError(ChatServiceError):
    """Share endpoint refused to issue a token for an incognito chat.

    Maps to HTTP 403 — the chat exists and is owned by the caller, but its
    privacy posture (incognito) forbids public sharing.
    """


class TitleGenerationError(ChatServiceError):
    """Auto-title generation failed (upstream LM Studio error or bad output).

    Best-effort background operation; maps to HTTP 502 so the client can
    swallow it silently.
    """


# ---------------------------------------------------------------------------
# Public Pydantic models
# ---------------------------------------------------------------------------


class Chat(BaseModel):
    """Pydantic projection of one row from the ``chats`` table.

    ``settings`` is a JSON blob (migration 0003). Keys in use:
    - ``rag_enabled: bool``
    - ``reasoning_effort: str | None``
    - ``ab_compare: dict`` — ``{enabled: bool, model_a?: str, model_b?: str}``

    Incognito fields:
    - ``incognito: bool`` — when True, memory write paths short-circuit
      (no embeddings, no insights, no activations).
    - ``incognito_expires_at: float | None`` — UNIX epoch seconds; past due
      and the periodic incognito purge DELETEs this chat.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    title: str
    folder: str | None
    pinned: bool
    created_at: datetime
    updated_at: datetime
    settings: dict = {}  # type: ignore[assignment]
    display_order: int = 0
    # None = no model selected yet (new-chat default).
    model_id: str | None = None
    incognito: bool = False
    incognito_expires_at: float | None = None
    # None = un-projected (default; legacy rows). Set via PATCH /api/chats/{id}
    # or the project-scoped create route.
    project_id: int | None = None


class CompactResult(BaseModel):
    """Result from ``ChatService.compact()``.

    ``removed_message_ids`` / ``remaining_token_count`` / ``original_token_count``
    predate hybrid compaction — "removed" now means "archived" (rows are never
    deleted). The hybrid fields are ``None`` / ``0`` / ``[]`` on a no-op.

    Protected/retained messages (e.g. an interior assistant+tool pair) can
    split a single compact() call's archive set into a non-contiguous run of
    spans, so more than one ``compactions`` row may be written — one per
    contiguous run, each summarized and anchored independently to preserve
    chronological order. ``compaction_id`` / ``summary`` report only the most
    recent (highest-anchor) row for backward compat; ``compaction_ids`` has
    the full set (oldest-anchor first); full per-span detail lives in
    :meth:`list_compactions`.

    Attributes:
        compaction_id:       PK of the most recent row this call wrote, or
                             ``None`` on a no-op.
        summary:             Summary for that row, or ``None`` on a no-op.
        archived_count:      ``len(removed_message_ids)``.
        summary_token_count: Summed token count across every row this call
                             wrote.
        compaction_ids:      PKs of every row this call wrote, oldest-anchor
                             first. ``compaction_id`` is always
                             ``compaction_ids[-1]`` when set.
    """

    chat_id: int
    removed_message_ids: list[int]
    remaining_token_count: int
    original_token_count: int
    compaction_id: int | None = None
    summary: str | None = None
    archived_count: int = 0
    summary_token_count: int = 0
    compaction_ids: list[int] = []


class Compaction(BaseModel):
    """Pydantic projection of one row from the ``compactions`` table.

    ``anchor_msg_id`` is a display-position id (oldest archived id at
    archive time), not a membership range. ``archived_count`` is derived
    from live ``messages.compaction_id`` membership, not stored — it
    defaults to 0 except where :meth:`ChatService.list_compactions`
    populates the real value.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    chat_id: int
    summary: str
    summary_model_id: str | None
    anchor_msg_id: int
    original_token_count: int
    summary_token_count: int
    created_at: datetime
    archived_count: int = 0


class ReorderRequest(BaseModel):
    """Request body for ``PATCH /api/chats/reorder``."""

    chat_id: int
    folder: str | None  # None = ungrouped
    display_order: int  # target position, 0-based


class ChatShare(BaseModel):
    """Pydantic projection of one row from the ``chat_shares`` table.

    Returned by :meth:`ChatService.create_share` and
    :meth:`ChatService.get_share_by_token`; the route layer wraps it into a
    JSON envelope with the relative URL (``/share/{token}``).
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    chat_id: int
    token: str
    created_at: datetime


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_encoding(hint: str) -> tiktoken.Encoding:
    """Return a tiktoken Encoding for *hint*, falling back to cl100k_base on KeyError."""
    try:
        return tiktoken.encoding_for_model(hint)
    except KeyError:
        log.warning(
            "tokenizer.fallback",
            model_hint=hint,
            fallback=_DEFAULT_ENCODING,
        )
        return tiktoken.get_encoding(_DEFAULT_ENCODING)


def _count_tokens(enc: tiktoken.Encoding, text: str) -> int:
    """Count tokens in *text* using *enc*."""
    return len(enc.encode(text))


def _salvage_title_from_reasoning(reasoning: str) -> str:
    """Recover a chat title from a reasoning model's ``reasoning_content``.

    When ``content`` is empty (the model reasoned past the budget), the
    intended title is usually the last quoted string in the reasoning tail.
    Returns it when plausible (2-80 chars), else ``""`` so the caller falls
    back to the safe user-message title rather than risk garbage.
    """
    import re as _re  # noqa: PLC0415

    quoted = _re.findall(r'"([^"\n]{2,80})"', reasoning)
    if quoted:
        candidate = quoted[-1].strip()
        if candidate:
            return candidate
    return ""


def _sanitise_generated_title(raw: str) -> str:
    """Strip whitespace, quotes, and trailing punctuation from a model-emitted title."""
    s = raw.strip()
    if not s:
        return ""

    # Strip a followups marker the model echoed back.
    s = re.sub(r"<!--followups:?\s*\[.*?\]\s*-->", "", s, flags=re.DOTALL).strip()

    # Strip markdown code-fence markers (model may parrot conversation code).
    s = re.sub(r"```[a-zA-Z0-9]*", "", s).strip()

    s = re.sub(r"\s+", " ", s)  # titles are single-line

    # Drop common "Title:" / "Here is the title:" prefixes.
    s = re.sub(
        r"^(?:title|here(?:'s| is) (?:the |a )?title|chat title)\s*[:\-]\s*",
        "",
        s,
        flags=re.IGNORECASE,
    )

    # Strip matched wrapping quotes (straight + smart).
    quote_pairs = [
        ('"', '"'),
        ("'", "'"),
        ("“", "”"),
        ("‘", "’"),
        ("`", "`"),
    ]
    for opener, closer in quote_pairs:
        if len(s) >= 2 and s.startswith(opener) and s.endswith(closer):
            s = s[1:-1]
            break

    s = re.sub(r"[.!?,;:\s]+$", "", s)
    s = s.strip()

    # Reject an echo of the title-generation instruction: the model emitted the
    # prompt itself instead of a title. Returning "" makes generate_title fall
    # back to the first user message.
    if any(marker in s.lower() for marker in _TITLE_INSTRUCTION_MARKERS):
        return ""

    if len(s) > _AUTO_TITLE_MAX_CHARS:
        # Cut on the last space in the cap window, then mark truncation.
        head = s[: _AUTO_TITLE_MAX_CHARS - 1]
        last_space = head.rfind(" ")
        if last_space > _AUTO_TITLE_MAX_CHARS // 2:
            head = head[:last_space]
        s = head.rstrip() + "…"

    return s


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ChatService:
    """Chat lifecycle service — CRUD, fork, and compaction.

    Injected via ``app.state.chat_service``; constructed once at lifespan
    start. ``chat_locks`` is shared with the streaming service so both hold
    the same per-chat mutex.
    """

    def __init__(
        self,
        *,
        engine: AsyncEngine,
        memory_service: MemoryService,
        models_service: ModelsService,
        chat_locks: dict[int, asyncio.Lock],
        aux_model_timeout_sec: float = 900.0,
    ) -> None:
        self._engine = engine
        self._memory_service = memory_service
        self._models_service = models_service
        self._chat_locks = chat_locks
        # Wall-clock budget for background aux calls (auto-title, compaction
        # summary); mirrors settings.lm_chat_aux_model_timeout_sec.
        self._aux_model_timeout_sec = aux_model_timeout_sec
        # Per-(user_id, folder) lock for reorder + update_settings mutations;
        # folder=None covers pinned/ungrouped chats.
        self._folder_locks: dict[tuple[int, str | None], asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_chat_row(self, chat_id: int, *, user_id: int) -> Any:  # noqa: ANN401
        """Fetch the chats row for *chat_id* owned by *user_id*.

        Args:
            chat_id: PK of the chat.
            user_id: Must match the chat's ``user_id`` column.

        Returns:
            SQLAlchemy Row for the chat.

        Raises:
            ChatNotFoundError: If the row does not exist or is owned by
                               a different user.
        """
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(chats).where(
                    chats.c.id == chat_id,
                    chats.c.user_id == user_id,
                )
            )
            row = result.fetchone()
        if row is None:
            raise ChatNotFoundError(
                f"chat {chat_id!r} not found for user {user_id!r}"
            )
        return row

    async def _delete_messages_with_notification(
        self, message_ids: list[int]
    ) -> None:
        """Call memory_service.handle_message_deleted for each id.

        Wrapped in try/except per id so a transient memory-service failure
        doesn't propagate after the transaction has already committed.
        """
        for mid in message_ids:
            try:
                await self._memory_service.handle_message_deleted(mid)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "memory.handle_message_deleted.failed",
                    message_id=mid,
                    error=str(exc),
                )

    async def _write_audit(
        self,
        *,
        event: AuditEvent,
        user_id: int | None,
        detail: dict[str, Any] | None,
    ) -> None:
        """Write an audit log row, swallowing failures so the caller is not 500'd."""
        try:
            await write_audit_event(
                user_id=user_id,
                event=event,
                ip=None,
                user_agent=None,
                detail=detail,
                engine=self._engine,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "audit.write_failed",
                audit_event=event,
                user_id=user_id,
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # Public API — CRUD
    # ------------------------------------------------------------------

    async def create(
        self,
        *,
        user_id: int,
        title: str,
        incognito: bool = False,
        incognito_ttl_seconds: int | None = None,
        project_id: int | None = None,
        model_id: str | None = None,
    ) -> Chat:
        """Insert a new chat row and return it as a :class:`Chat`.

        Args:
            incognito:              When True, marks incognito at creation
                                    time — memory writes short-circuit and
                                    the chat is scheduled for TTL purge.
            incognito_ttl_seconds:  Override the default TTL
                                    (settings.lm_chat_incognito_ttl_seconds).
            model_id:               Seeds ``chats.model_id`` on insert (e.g.
                                    from ``projects.default_model_id``);
                                    None preserves legacy NULL-on-insert.

        Returns:
            The newly created :class:`Chat`.
        """
        expires_at: float | None = None
        if incognito:
            from time import time

            from lmchat.config import get_settings

            ttl = (
                incognito_ttl_seconds
                if incognito_ttl_seconds is not None
                else get_settings().lm_chat_incognito_ttl_seconds
            )
            expires_at = time() + float(ttl)

        async def _insert() -> int:
            async with self._engine.begin() as conn:
                values: dict[str, Any] = {"user_id": user_id, "title": title}
                if incognito:
                    values["incognito"] = 1
                    values["incognito_expires_at"] = expires_at
                if project_id is not None:
                    values["project_id"] = project_id
                if model_id is not None:
                    values["model_id"] = model_id
                result = await conn.execute(insert(chats).values(**values))
                pk = result.inserted_primary_key
                if pk is None:
                    raise RuntimeError("INSERT into chats returned no PK")
                return int(pk[0])

        new_id = await with_write_retry(_insert)

        async with self._engine.connect() as conn:
            row = (
                await conn.execute(select(chats).where(chats.c.id == new_id))
            ).fetchone()

        if row is None:
            raise RuntimeError(f"chats row {new_id!r} not found after INSERT")

        chat = Chat.model_validate(row, from_attributes=True)

        log.info(
            "chat.created",
            chat_id=chat.id,
            user_id=user_id,
            title=title,
            incognito=incognito,
        )
        await self._write_audit(
            event="chat.created",
            user_id=user_id,
            detail={
                "chat_id": chat.id,
                "title": title,
                "incognito": incognito,
            },
        )
        return chat

    async def list_for_user(
        self,
        user_id: int,
        *,
        folder: str | None = None,
        project_id: int | None = None,
        unscoped: bool = False,
    ) -> list[Chat]:
        """Return chats for *user_id*, optionally filtered.

        Args:
            folder:     Restrict to this folder if given.
            project_id: Restrict to this project if given.
            unscoped:   When True and project_id is None, restrict to
                       ``project_id IS NULL`` (legacy un-projected set).
                       Default False applies no project filter (backward
                       compat).

        Returns:
            List of :class:`Chat`, newest first.
        """
        stmt = (
            select(chats)
            .where(chats.c.user_id == user_id)
            .order_by(chats.c.created_at.desc())
        )
        if folder is not None:
            stmt = stmt.where(chats.c.folder == folder)
        project_clause = project_scope_clause(
            chats.c.project_id,
            project_id=project_id,
            unscoped=unscoped,
        )
        if project_clause is not None:
            stmt = stmt.where(project_clause)

        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).fetchall()

        return [Chat.model_validate(r, from_attributes=True) for r in rows]

    async def get(self, chat_id: int, *, user_id: int) -> Chat:
        """Return the :class:`Chat` for *chat_id* owned by *user_id*.

        Args:
            chat_id: PK of the chat.
            user_id: Must be the owning user.

        Returns:
            The :class:`Chat`.

        Raises:
            ChatNotFoundError: If missing or owned by another user.
        """
        row = await self._get_chat_row(chat_id, user_id=user_id)
        return Chat.model_validate(row, from_attributes=True)

    async def set_project_id(
        self,
        chat_id: int,
        *,
        user_id: int,
        project_id: int | None,
        projects_service: Any | None = None,
    ) -> None:
        """Move *chat_id* into project *project_id* (or detach when None).

        On a true detach (``project_id=None`` and the chat WAS in a project),
        snapshots the project's identity into
        ``chats.detached_from_project_meta`` in the same transaction as the
        ``project_id`` clear:

            {"project_id": int, "name": str,
             "detached_at": float, "system_prompt_hash": str}

        Stores a hash, not the full prompt text, so the chat history can
        still render a "Detached from X on Y" separator after the project
        is deleted.

        Args:
            project_id:       Target project_id, or None to detach.
            projects_service: Required to build the detach snapshot; None is
                              permitted for the ATTACH case and legacy tests.

        Raises:
            ChatNotFoundError: If the chat is missing or owned by another
                               user.

        Note: project ownership is enforced upstream by the route layer
        (``_require_owned_project``); the FK alone would CASCADE-SET-NULL on
        project deletion, and a hard 404 is wanted instead.
        """
        from hashlib import sha256
        from time import time

        # Holders so the post-transaction log can see what happened inside
        # the atomic block.
        prior_holder: list[int | None] = [None]
        wrote_meta_holder: list[bool] = [False]

        async def _atomic_set() -> None:
            async with self._engine.begin() as conn:
                # Ownership check + prior_project_id capture, transactional
                # with the UPDATE below — no stale-read window.
                chat_result = await conn.execute(
                    select(chats).where(
                        chats.c.id == chat_id,
                        chats.c.user_id == user_id,
                    )
                )
                chat_row = chat_result.fetchone()
                if chat_row is None:
                    raise ChatNotFoundError(
                        f"chat {chat_id!r} not found for user {user_id!r}"
                    )
                prior_project_id: int | None = chat_row.project_id
                prior_holder[0] = prior_project_id

                # Snapshot only on a true detach (project_id=None from a
                # prior project); a move A→B skips it since the new
                # project_id stays discoverable.
                detached_meta: dict[str, Any] | None = None
                if project_id is None and prior_project_id is not None:
                    if projects_service is None:
                        # Best-effort: UI just shows a less informative
                        # timeline without the snapshot.
                        log.warning(
                            "chat.detach_snapshot_skipped_no_projects_service",
                            chat_id=chat_id,
                            user_id=user_id,
                            prior_project_id=prior_project_id,
                        )
                    else:
                        prior = await projects_service.get_with_conn(
                            conn,
                            user_id=user_id,
                            project_id=prior_project_id,
                        )
                        if prior is not None:
                            sp = getattr(prior, "system_prompt", "") or ""
                            detached_meta = {
                                "project_id": int(prior_project_id),
                                "name": str(getattr(prior, "name", "")),
                                "detached_at": time(),
                                "system_prompt_hash": (
                                    "sha256:"
                                    + sha256(sp.encode("utf-8")).hexdigest()
                                ),
                            }
                        else:
                            # FK→SET NULL already nuked the project row;
                            # snapshot with what we still know.
                            log.warning(
                                "chat.detach_snapshot_project_gone",
                                chat_id=chat_id,
                                user_id=user_id,
                                prior_project_id=prior_project_id,
                            )
                            detached_meta = {
                                "project_id": int(prior_project_id),
                                "name": "",
                                "detached_at": time(),
                                "system_prompt_hash": "",
                            }
                wrote_meta_holder[0] = detached_meta is not None

                # Only include detached_from_project_meta when built, so
                # other moves leave the column untouched.
                values: dict[str, Any] = {"project_id": project_id}
                if detached_meta is not None:
                    values["detached_from_project_meta"] = detached_meta
                await conn.execute(
                    update(chats)
                    .where(
                        chats.c.id == chat_id, chats.c.user_id == user_id
                    )
                    .values(**values)
                )

        await with_write_retry(_atomic_set)

        log.info(
            "chat.project_id_set",
            chat_id=chat_id,
            user_id=user_id,
            project_id=project_id,
            prior_project_id=prior_holder[0],
            wrote_detach_meta=wrote_meta_holder[0],
        )

    async def rename(self, chat_id: int, *, user_id: int, title: str) -> None:
        """Update the title of *chat_id*.

        Args:
            chat_id: PK of the chat to rename.
            user_id: Must be the owning user.
            title:   New title string.

        Raises:
            ChatNotFoundError: If missing or owned by another user.
        """
        await self._get_chat_row(chat_id, user_id=user_id)

        async def _update() -> None:
            async with self._engine.begin() as conn:
                await conn.execute(
                    update(chats)
                    .where(chats.c.id == chat_id, chats.c.user_id == user_id)
                    .values(title=title)
                )

        await with_write_retry(_update)

        log.info(
            "chat.renamed",
            chat_id=chat_id,
            user_id=user_id,
            title=title,
        )
        await self._write_audit(
            event="chat.renamed",
            user_id=user_id,
            detail={"chat_id": chat_id, "title": title},
        )


    async def generate_title(
        self,
        chat_id: int,
        *,
        user_id: int,
        http_client: httpx.AsyncClient,
        base_url: str,
        fallback_model_id: str | None = None,
    ) -> str:
        """Auto-generate a concise title for *chat_id* from its early turns.

        Triggered by the frontend after the second assistant message lands.
        Idempotent: if the chat already has a user-set title (i.e. not in
        :data:`_AUTO_TITLE_DEFAULT_VALUES`), that title is returned as-is
        without calling LM Studio — once the user renames a chat, this
        method becomes a permanent no-op for it.

        Args:
            fallback_model_id: Model to use if the chat's most-recent
                               assistant message has no ``model_id`` (e.g.
                               legacy rows). Typically the admin's
                               default-loaded model.

        Returns:
            The newly-generated title (or the existing one if user-set).

        Raises:
            ChatNotFoundError:     Chat missing OR not owned by *user_id*.
            TitleGenerationError:  No early messages yet, no model available
                                   to call, upstream LM Studio failure, or
                                   the model returned an unusable response.
        """
        # Doing the ownership + existing-title check in one query keeps the
        # idempotent path single-round-trip.
        row = await self._get_chat_row(chat_id, user_id=user_id)
        existing_title: str = str(row.title) if row.title is not None else ""
        if existing_title not in _AUTO_TITLE_DEFAULT_VALUES:
            log.info(
                "chat.generate_title.skip_user_set",
                chat_id=chat_id,
                user_id=user_id,
                existing_title=existing_title,
            )
            return existing_title

        # ASC by id gives conversation order; capped at _AUTO_TITLE_MAX_HISTORY_MESSAGES.
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(
                    messages.c.role,
                    messages.c.content,
                    messages.c.model_id,
                )
                .where(messages.c.chat_id == chat_id)
                .where(messages.c.role.in_(("user", "assistant")))
                .where(messages.c.state == "final")
                .order_by(messages.c.id.asc())
                .limit(_AUTO_TITLE_MAX_HISTORY_MESSAGES)
            )
            history_rows = result.fetchall()

        if not history_rows:
            raise TitleGenerationError(
                f"chat {chat_id!r} has no early messages to title from"
            )

        # Prefer the most-recent assistant model_id (same family that produced
        # the conversation), else the caller-supplied fallback.
        model_id: str | None = None
        for r in reversed(history_rows):
            if r.role == "assistant" and r.model_id:
                model_id = str(r.model_id)
                break
        if not model_id:
            model_id = fallback_model_id
        if not model_id:
            raise TitleGenerationError(
                f"chat {chat_id!r} has no model to call for title generation"
            )

        # Compat, not native: no response chain / tool use to continue here.
        # The transcript must NOT end on an assistant turn, or the model
        # continues that turn (echoing the answer) instead of titling it.
        first_user_text = ""
        convo_lines: list[str] = []
        for r in history_rows:
            content = str(r.content) if r.content is not None else ""
            if not content:
                continue
            # Strip the hidden followups marker so the title model can't echo it.
            content = re.sub(
                r"<!--followups:?\s*\[.*?\]\s*-->", "", content, flags=re.DOTALL
            ).strip()
            # Drop substance_fold's salvage prefix + reasoning tail — feeding
            # that to the title model leaks reasoning prose into the title.
            salvage_idx = content.find(_SALVAGE_PREFIX)
            if salvage_idx >= 0:
                content = content[:salvage_idx].strip()
                if not content:
                    continue
            # Defensively cap so a long pasted document doesn't blow the context.
            if len(content) > 2_000:
                content = content[:2_000]
            role = str(r.role)
            if role == "user" and not first_user_text:
                first_user_text = content
            convo_lines.append(f"{'User' if role == 'user' else 'Reply'}: {content}")

        # No assistant reply survived salvage-strip — same as an
        # unmaterialized chat; the FE swallows this and keeps the default title.
        if not any(line.startswith("Reply:") for line in convo_lines):
            raise TitleGenerationError(
                f"chat {chat_id!r} has no assistant content available for"
                " title generation (all turns were salvage-only)"
            )

        chat_payload_messages: list[dict[str, str]] = [
            {"role": "system", "content": _AUTO_TITLE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Conversation:\n\n"
                    + "\n".join(convo_lines)
                    + "\n\n"
                    + _AUTO_TITLE_USER_INSTRUCTION
                ),
            },
        ]

        # Use the admin-pinned background-tasks model so title generation
        # doesn't compete with the user's next turn (fail-soft to chat model).
        model_id = await resolve_background_model_id(
            engine=self._engine,
            models_service=self._models_service,
            chat_model_id=model_id,
        )

        # Resolve the stable model key to the current loaded_instance_id;
        # legacy rows may still carry an old instance id.
        wire_model_id = model_id
        try:
            # Best-effort: fall back to any loaded LLM when the pinned model
            # has idled out. If nothing is loaded, keep the raw value — LM
            # Studio's 400 surfaces as TitleGenerationError below.
            resolved = await self._models_service.resolve_to_loaded_or_fallback(
                model_id
            )
            if resolved.wire_id is not None:
                wire_model_id = resolved.wire_id
        except Exception:  # noqa: BLE001
            pass  # Non-fatal; LM Studio will surface the error.

        request_body: dict[str, Any] = {
            "model": wire_model_id,
            "messages": chat_payload_messages,
            "stream": False,
            "max_tokens": _AUTO_TITLE_MAX_TOKENS,
            # Low temp -> stable, deterministic-ish titles.
            "temperature": 0.3,
        }

        # Best-effort: any upstream failure (timeouts are common with
        # reasoning models) degrades to a title from the first user message
        # rather than leaving the chat at "New Chat"; never surfaced.
        url = f"{base_url.rstrip('/')}/v1/chat/completions"
        raw_title = ""
        try:
            # Serializes against auto-memory distillation + followups so they
            # don't contend on the one background model (see bg_aux.py).
            async with bg_aux_slot():
                response = await http_client.post(
                    url,
                    json=request_body,
                    timeout=self._aux_model_timeout_sec,
                )
            if response.status_code == 200:
                payload = response.json()
                choices = payload.get("choices") or []
                if isinstance(choices, list) and choices:
                    message = choices[0].get("message") or {}
                    raw_title = str(message.get("content") or "")
                    if not raw_title.strip():
                        # Reasoning model may leave content empty with the
                        # title in reasoning_content; recover a clean quoted
                        # title from the tail, else fall back below.
                        raw_title = _salvage_title_from_reasoning(
                            str(message.get("reasoning_content") or "")
                        )
            else:
                log.warning(
                    "chat.generate_title.upstream_status_error",
                    chat_id=chat_id,
                    user_id=user_id,
                    model_id=model_id,
                    status_code=response.status_code,
                    body_preview=response.text[:200],
                )
        except (httpx.HTTPError, ValueError, AttributeError, IndexError, TypeError) as exc:
            # Timeout / transport / non-JSON / malformed shape — all non-fatal.
            log.warning(
                "chat.generate_title.upstream_error",
                chat_id=chat_id,
                user_id=user_id,
                model_id=model_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )

        title = _sanitise_generated_title(raw_title)
        if not title:
            # No usable LLM title — fall back to the first user message.
            title = _sanitise_generated_title(first_user_text)
        if not title:
            raise TitleGenerationError(
                "no usable title: upstream produced nothing and there is no"
                " user message to fall back to"
            )

        async def _update() -> None:
            async with self._engine.begin() as conn:
                await conn.execute(
                    update(chats)
                    .where(chats.c.id == chat_id, chats.c.user_id == user_id)
                    .values(title=title)
                )

        await with_write_retry(_update)

        log.info(
            "chat.title_generated",
            chat_id=chat_id,
            user_id=user_id,
            model_id=model_id,
            title=title,
            history_count=len(history_rows),
        )
        # Audited as chat.renamed — a rename from the persistence layer's view.
        await self._write_audit(
            event="chat.renamed",
            user_id=user_id,
            detail={
                "chat_id": chat_id,
                "title": title,
                "auto_generated": True,
                "model_id": model_id,
            },
        )

        return title

    async def move_to_folder(
        self, chat_id: int, *, user_id: int, folder: str | None
    ) -> None:
        """Set the folder for *chat_id* atomically.

        Args:
            chat_id: PK of the chat.
            user_id: Must be the owning user.
            folder:  New folder value, or ``None`` to remove from all folders.

        Raises:
            ChatNotFoundError: If missing or owned by another user.
        """
        await self._get_chat_row(chat_id, user_id=user_id)

        async def _update() -> None:
            async with self._engine.begin() as conn:
                await conn.execute(
                    update(chats)
                    .where(chats.c.id == chat_id, chats.c.user_id == user_id)
                    .values(folder=folder)
                )

        await with_write_retry(_update)

        log.info(
            "chat.moved",
            chat_id=chat_id,
            user_id=user_id,
            folder=folder,
        )
        await self._write_audit(
            event="chat.moved",
            user_id=user_id,
            detail={"chat_id": chat_id, "folder": folder},
        )

    async def set_model_id(
        self, chat_id: int, *, user_id: int, model_id: str
    ) -> None:
        """Persist the user's per-chat model selection.

        Args:
            chat_id: PK of the chat.
            user_id: Must be the owning user.
            model_id: LM Studio model identifier. Empty string is rejected;
                the frontend cascade keeps the chat with no model when the
                user hasn't picked one yet.

        Raises:
            ChatNotFoundError: If missing or owned by another user.
            ValueError: If model_id is empty.
        """
        if model_id == "":
            raise ValueError("model_id must not be empty")
        await self._get_chat_row(chat_id, user_id=user_id)

        async def _update() -> None:
            async with self._engine.begin() as conn:
                await conn.execute(
                    update(chats)
                    .where(chats.c.id == chat_id, chats.c.user_id == user_id)
                    .values(model_id=model_id)
                )

        await with_write_retry(_update)

        log.info(
            "chat.model_id_set",
            chat_id=chat_id,
            user_id=user_id,
            model_id=model_id,
        )

    async def clear_model_id(self, chat_id: int, *, user_id: int) -> None:
        """Reset the per-chat model override back to "Auto".

        Sets ``chats.model_id`` to NULL so the frontend picker shows "Auto"
        and the send path falls through to the user's default model. Symmetric
        with :meth:`set_model_id`; reached via the PATCH ``clear=model_id``
        path (the flat ``model_id=""`` param is intentionally ignored so an
        implicit default is never persisted as an explicit pin).

        Args:
            chat_id: PK of the chat.
            user_id: Must be the owning user.

        Raises:
            ChatNotFoundError: If missing or owned by another user.
        """
        await self._get_chat_row(chat_id, user_id=user_id)

        async def _update() -> None:
            async with self._engine.begin() as conn:
                await conn.execute(
                    update(chats)
                    .where(chats.c.id == chat_id, chats.c.user_id == user_id)
                    .values(model_id=None)
                )

        await with_write_retry(_update)

        log.info(
            "chat.model_id_cleared",
            chat_id=chat_id,
            user_id=user_id,
        )

    async def pin(self, chat_id: int, *, user_id: int, pinned: bool) -> None:
        """Set the pinned flag on *chat_id*.

        Args:
            chat_id: PK of the chat.
            user_id: Must be the owning user.
            pinned:  ``True`` to pin, ``False`` to unpin.

        Raises:
            ChatNotFoundError: If missing or owned by another user.
        """
        await self._get_chat_row(chat_id, user_id=user_id)

        async def _update() -> None:
            async with self._engine.begin() as conn:
                await conn.execute(
                    update(chats)
                    .where(chats.c.id == chat_id, chats.c.user_id == user_id)
                    .values(pinned=pinned)
                )

        await with_write_retry(_update)

        log.info(
            "chat.pinned",
            chat_id=chat_id,
            user_id=user_id,
            pinned=pinned,
        )
        await self._write_audit(
            event="chat.pinned",
            user_id=user_id,
            detail={"chat_id": chat_id, "pinned": pinned},
        )

    async def update_settings(
        self, chat_id: int, *, user_id: int, settings: dict  # type: ignore[type-arg]
    ) -> Chat:
        """Merge *settings* into the chat's settings JSON blob.

        Shallow merge: ``existing | settings`` — keys in *settings* override
        matching keys in the stored value; other keys are preserved. Legacy
        keys are validated inline below; the full merged shape is also
        validated via :class:`~lmchat.models.chat_settings.ChatSettings` so
        future drift surfaces as a typed error rather than silent data loss.

        Returns:
            The updated :class:`Chat`.

        Raises:
            ChatNotFoundError: If missing or owned by another user.
            ValueError: If any settings value fails validation.
        """
        import json

        from pydantic import ValidationError

        from lmchat.models.chat_settings import ChatSettings

        # ------------------------------------------------------------------
        # Per-key validation before touching the DB.
        # ------------------------------------------------------------------

        # Unknown keys are forwarded as-is (forward-compat); we WARN rather
        # than reject so typos are visible without breaking integrations.
        _KNOWN_SETTINGS_KEYS: set[str] = {
            # Legacy chat-settings keys
            "reasoning_effort",
            "rag_enabled",
            "ab_compare",
            # Per-chat rail keys
            "system_prompt",
            "temperature",
            "top_p",
            "top_k",
            "min_p",
            "repeat_penalty",
            "max_tokens",
            "reasoning",
            "self_consistency_enabled",
            "chain_of_verification_enabled",
            "stateless",
            "repeat_warning_cut_k",
            # Forward-compat
            "active_preset",
        }
        unknown_keys = set(settings.keys()) - _KNOWN_SETTINGS_KEYS
        if unknown_keys:
            log.warning(
                "chat.settings_unknown_keys",
                chat_id=chat_id,
                user_id=user_id,
                unknown_keys=sorted(unknown_keys),
            )

        _VALID_REASONING = {"off", "low", "medium", "high"}

        if "reasoning_effort" in settings:
            val = settings["reasoning_effort"]
            if val is not None and val not in _VALID_REASONING:
                raise ValueError(
                    f"reasoning_effort must be one of {sorted(_VALID_REASONING)} or null, "
                    f"got {val!r}"
                )

        if "rag_enabled" in settings:
            val = settings["rag_enabled"]
            if not isinstance(val, bool):
                raise ValueError(
                    f"rag_enabled must be bool, got {type(val).__name__!r}"
                )

        if "ab_compare" in settings:
            val = settings["ab_compare"]
            if not isinstance(val, dict):
                raise ValueError(
                    f"ab_compare must be a dict, got {type(val).__name__!r}"
                )
            if "enabled" not in val or not isinstance(val["enabled"], bool):
                raise ValueError(
                    "ab_compare.enabled is required and must be bool"
                )
            for _k in ("model_a", "model_b"):
                if _k in val and val[_k] is not None and not isinstance(val[_k], str):
                    raise ValueError(
                        f"ab_compare.{_k} must be a string or null"
                    )

        # Catches range/type errors on the per-chat rail keys before merging
        # into the stored blob; unknown keys pass through (extra='allow').
        try:
            ChatSettings.model_validate(settings)
        except ValidationError as exc:
            raise ValueError(str(exc)) from exc

        # Per-chat lock prevents concurrent PATCHes from a lost-update race.
        lock = self._chat_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            row = await self._get_chat_row(chat_id, user_id=user_id)
            existing_locked: dict = {}  # type: ignore[type-arg]
            if row.settings:
                if isinstance(row.settings, dict):
                    existing_locked = row.settings
                elif isinstance(row.settings, str):
                    try:
                        existing_locked = json.loads(row.settings)
                    except (ValueError, TypeError):
                        existing_locked = {}

            merged = {**existing_locked, **settings}

            async def _update() -> None:
                async with self._engine.begin() as conn:
                    await conn.execute(
                        update(chats)
                        .where(chats.c.id == chat_id, chats.c.user_id == user_id)
                        .values(settings=merged)
                    )

            await with_write_retry(_update)

        log.info(
            "chat.settings_updated",
            chat_id=chat_id,
            user_id=user_id,
            settings_keys=list(settings.keys()),
        )

        return await self.get(chat_id, user_id=user_id)

    async def reorder(
        self,
        *,
        chat_id: int,
        user_id: int,
        folder: str | None,
        display_order: int,
    ) -> None:
        """Move *chat_id* to *folder* and assign *display_order*.

        Other chats in the target folder owned by *user_id* are re-indexed
        under a per-folder asyncio.Lock to keep ``display_order`` gap-free.

        Args:
            folder:        Target folder (None = ungrouped/pinned section).
            display_order: Target position index (0-based).

        Raises:
            ChatNotFoundError: If chat_id not found or not owned by user.
        """
        await self._get_chat_row(chat_id, user_id=user_id)

        folder_key = (user_id, folder)
        lock = self._folder_locks.setdefault(folder_key, asyncio.Lock())

        async with lock:
            # All chats in the target folder except the moving one.
            stmt = (
                select(chats.c.id, chats.c.display_order)
                .where(
                    chats.c.user_id == user_id,
                    chats.c.folder == folder,
                    chats.c.id != chat_id,
                )
                .order_by(chats.c.display_order)
            )
            async with self._engine.connect() as conn:
                rows = (await conn.execute(stmt)).fetchall()

            other_ids = [r[0] for r in rows]
            clamped = max(0, min(display_order, len(other_ids)))
            ordered_ids = other_ids[:clamped] + [chat_id] + other_ids[clamped:]

            async def _reorder_tx() -> None:
                async with self._engine.begin() as conn:
                    for idx, cid in enumerate(ordered_ids):
                        values: dict[str, object] = {"display_order": idx}  # type: ignore[type-arg]
                        if cid == chat_id:
                            values["folder"] = folder
                        await conn.execute(
                            update(chats)
                            .where(chats.c.id == cid, chats.c.user_id == user_id)
                            .values(**values)
                        )

            await with_write_retry(_reorder_tx)

        log.info(
            "chat.reordered",
            chat_id=chat_id,
            user_id=user_id,
            folder=folder,
            display_order=display_order,
        )
        await self._write_audit(
            event="chat.reordered",
            user_id=user_id,
            detail={"chat_id": chat_id, "folder": folder, "display_order": display_order},
        )

    async def delete(self, chat_id: int, *, user_id: int) -> None:
        """Delete *chat_id* and all its descendant rows (FK cascade drops
        messages + message_embeddings). Post-commit, notifies memory_service
        per deleted message and pops the chat's lock to prevent unbounded
        growth of the lock dict.

        Raises:
            ChatNotFoundError: If missing or owned by another user.
        """
        await self._get_chat_row(chat_id, user_id=user_id)

        lock = self._chat_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            # Collect message_ids before the CASCADE wipes them.
            async with self._engine.connect() as conn:
                result = await conn.execute(
                    select(messages.c.id).where(messages.c.chat_id == chat_id)
                )
                message_ids = [row[0] for row in result.fetchall()]

            async def _delete() -> None:
                async with self._engine.begin() as conn:
                    await conn.execute(
                        delete(chats).where(
                            chats.c.id == chat_id,
                            chats.c.user_id == user_id,
                        )
                    )

            await with_write_retry(_delete)

        # Pop after releasing so the streaming service doesn't race on a deleted chat.
        self._chat_locks.pop(chat_id, None)

        await self._delete_messages_with_notification(message_ids)

        log.info(
            "chat.deleted",
            chat_id=chat_id,
            user_id=user_id,
            message_count=len(message_ids),
        )
        await self._write_audit(
            event="chat.deleted",
            user_id=user_id,
            detail={
                "chat_id": chat_id,
                "user_id": user_id,
                "message_count": len(message_ids),
            },
        )

    async def clear_messages(self, chat_id: int, *, user_id: int) -> int:
        """Delete every message in *chat_id* but keep the chat shell.

        Backs the ``/clear`` slash command: empties history while preserving
        the chat row (title, folder, settings, project link) and its lock.
        Mirrors :meth:`delete` but targets ``messages`` rows, not ``chats``.
        The LM Studio ``response_id`` chain lives on message rows, so a
        cleared chat naturally starts fresh on the next turn.

        Also deletes the chat's ``sub_sessions`` rows (D8, migration 0045)
        — cascades to ``sub_session_messages`` via ``ON DELETE CASCADE`` —
        so "clear chat" doesn't silently retain old sub-session history
        while wiping the main thread.

        Returns:
            The number of messages removed.

        Raises:
            ChatNotFoundError: If missing or owned by another user.
        """
        await self._get_chat_row(chat_id, user_id=user_id)

        lock = self._chat_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            # Collect message_ids before the DELETE wipes them.
            async with self._engine.connect() as conn:
                result = await conn.execute(
                    select(messages.c.id).where(messages.c.chat_id == chat_id)
                )
                message_ids = [row[0] for row in result.fetchall()]

            async def _clear() -> None:
                async with self._engine.begin() as conn:
                    await conn.execute(
                        delete(messages).where(messages.c.chat_id == chat_id)
                    )
                    # Clearing also drops the chat's compactions rows — an
                    # archived span with nothing left to reference is moot.
                    await conn.execute(
                        delete(compactions).where(compactions.c.chat_id == chat_id)
                    )
                    # And the chat's durable sub-sessions (D8) — cascades to
                    # sub_session_messages automatically via the FK.
                    await conn.execute(
                        delete(sub_sessions).where(sub_sessions.c.chat_id == chat_id)
                    )

            await with_write_retry(_clear)

        # Chat row + lock are intentionally retained, unlike delete().
        await self._delete_messages_with_notification(message_ids)

        log.info(
            "chat.cleared",
            chat_id=chat_id,
            user_id=user_id,
            message_count=len(message_ids),
        )
        await self._write_audit(
            event="chat.cleared",
            user_id=user_id,
            detail={
                "chat_id": chat_id,
                "user_id": user_id,
                "message_count": len(message_ids),
            },
        )
        return len(message_ids)

    # ------------------------------------------------------------------
    # Incognito mode helpers
    # ------------------------------------------------------------------

    

    async def is_shareable(self, chat_id: int, *, user_id: int) -> bool:
        """Privacy hook for chat export + share.

        Returns False when the chat is incognito ("share endpoint MUST
        refuse to share incognito chats").  Returns True otherwise.

        Args:
            chat_id: PK of the chat.
            user_id: Must be the owning user (cross-user → ChatNotFoundError).

        Returns:
            True iff the chat is owned by *user_id* and is NOT incognito.

        Raises:
            ChatNotFoundError: If missing or owned by another user.
        """
        row = await self._get_chat_row(chat_id, user_id=user_id)
        return not bool(int(row.incognito))

    # ------------------------------------------------------------------
    # Chat share lifecycle
    # ------------------------------------------------------------------

    async def create_share(self, chat_id: int, *, user_id: int) -> ChatShare:
        """Mint a public share token for *chat_id*.

        Enforces that incognito chats are unshareable. Idempotent — an
        already-active share is returned rather than issuing a new token, so
        re-clicking "Share" doesn't break an in-flight public URL.

        Returns:
            The :class:`ChatShare` row.

        Raises:
            ChatNotFoundError:      If the chat is missing or owned by
                                    another user.
            ChatNotShareableError:  If the chat is incognito.
        """
        if not await self.is_shareable(chat_id, user_id=user_id):
            log.info(
                "chat.share.refused_incognito",
                chat_id=chat_id,
                user_id=user_id,
            )
            raise ChatNotShareableError(
                f"chat {chat_id!r} is incognito and cannot be shared"
            )

        # Idempotent: return the existing row if a share is already active.
        async with self._engine.connect() as conn:
            existing = await conn.execute(
                select(chat_shares).where(chat_shares.c.chat_id == chat_id)
            )
            row = existing.fetchone()
        if row is not None:
            return ChatShare.model_validate(row, from_attributes=True)

        # Fresh token; 24-byte collision risk is cryptographically negligible.
        async def _insert() -> Any:  # noqa: ANN401
            token = secrets.token_urlsafe(24)
            async with self._engine.begin() as conn:
                result = await conn.execute(
                    insert(chat_shares)
                    .values(chat_id=chat_id, token=token)
                    .returning(chat_shares)
                )
                return result.fetchone()

        inserted = await with_write_retry(_insert)
        log.info(
            "chat.shared",
            chat_id=chat_id,
            user_id=user_id,
        )
        return ChatShare.model_validate(inserted, from_attributes=True)

    async def delete_share(self, chat_id: int, *, user_id: int) -> bool:
        """Revoke the active share for *chat_id*.

        Returns True when a row was deleted, False when there was no active
        share to revoke. Cross-user access raises ``ChatNotFoundError``
        (existence-leak prevention) regardless of whether a share exists.

        Raises:
            ChatNotFoundError: If the chat is missing or owned by another
                               user.
        """
        await self._get_chat_row(chat_id, user_id=user_id)

        async def _delete() -> int:
            async with self._engine.begin() as conn:
                result = await conn.execute(
                    delete(chat_shares).where(chat_shares.c.chat_id == chat_id)
                )
                return result.rowcount or 0

        deleted = await with_write_retry(_delete)
        if deleted:
            log.info(
                "chat.share.revoked",
                chat_id=chat_id,
                user_id=user_id,
            )
        return bool(deleted)

    async def get_share_by_token(self, token: str) -> ChatShare | None:
        """Resolve a share token to its row, or None when not found.

        Used by the unauthenticated public share view at
        ``GET /api/share/{token}``.  Returns None on miss so the route
        layer can return 404 without disclosing whether the chat exists.

        Args:
            token: The URL-safe public token.

        Returns:
            The :class:`ChatShare`, or None if no row matches.
        """
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(chat_shares).where(chat_shares.c.token == token)
            )
            row = result.fetchone()
        if row is None:
            return None
        return ChatShare.model_validate(row, from_attributes=True)

    async def get_chat_unscoped(self, chat_id: int) -> Chat | None:
        """Fetch a chat row WITHOUT an ownership filter.

        Public-share-only entry point. The share-token-based public view at
        ``GET /api/share/{token}`` has no authenticated user, so it cannot
        scope by ``user_id``. The incognito check stays at the route
        layer (this method does NOT filter on ``chats.incognito``).

        Args:
            chat_id: PK of the chat.

        Returns:
            The :class:`Chat`, or None when no row matches.
        """
        async with self._engine.connect() as conn:
            result = await conn.execute(select(chats).where(chats.c.id == chat_id))
            row = result.fetchone()
        if row is None:
            return None
        return Chat.model_validate(row, from_attributes=True)

    async def list_messages_unscoped(self, chat_id: int) -> list[Any]:
        """Return all messages for ``chat_id`` WITHOUT an ownership filter.

        Public-share-only entry point — same rationale as
        :meth:`get_chat_unscoped`. Rows come back oldest-first.

        Args:
            chat_id: PK of the chat.

        Returns:
            A list of SQLAlchemy ``Row`` objects with the message columns
            (id, role, content, reasoning_content, created_at, ...).
        """
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(messages)
                .where(messages.c.chat_id == chat_id)
                .order_by(messages.c.created_at.asc(), messages.c.id.asc())
            )
            return list(result.fetchall())

    async def get_active_share(self, chat_id: int, *, user_id: int) -> ChatShare | None:
        """Return the active share for *chat_id*, or None when not shared.

        Used by the chat-detail UI so it can render the "Share" button
        with the existing public URL when one is already active.

        Args:
            chat_id: PK of the chat.
            user_id: Must be the owning user.

        Returns:
            The :class:`ChatShare`, or None if no share is active.

        Raises:
            ChatNotFoundError: If the chat is missing or owned by another
                               user.
        """
        await self._get_chat_row(chat_id, user_id=user_id)
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(chat_shares).where(chat_shares.c.chat_id == chat_id)
            )
            row = result.fetchone()
        if row is None:
            return None
        return ChatShare.model_validate(row, from_attributes=True)

    async def set_incognito(
        self,
        chat_id: int,
        *,
        user_id: int,
        incognito: bool,
        incognito_ttl_seconds: int | None = None,
    ) -> Chat:
        """Set the incognito flag on a chat, ONLY when no messages exist.

        Once messages exist the flag is IMMUTABLE: toggling it later would
        either retroactively promote already-persisted content into the
        incognito guarantee (false sense of privacy) or demote incognito
        content out of it (silent privacy regression), so both directions
        are refused by rejecting any change once messages exist.

        Args:
            incognito:             New flag value.
            incognito_ttl_seconds: Override TTL when flipping ON.

        Returns:
            The updated :class:`Chat`.

        Raises:
            ChatNotFoundError: If missing or owned by another user.
            ValueError:        If at least one message exists on the chat.
        """
        await self._get_chat_row(chat_id, user_id=user_id)

        async with self._engine.connect() as conn:
            msg_count_row = await conn.execute(
                select(messages.c.id)
                .where(messages.c.chat_id == chat_id)
                .limit(1)
            )
            has_messages = msg_count_row.fetchone() is not None

        if has_messages:
            raise ValueError(
                "incognito flag is immutable once messages exist on a chat "
                "(P13i privacy invariant). Create a new chat instead of "
                "toggling incognito mid-conversation."
            )

        expires_at: float | None = None
        if incognito:
            from time import time

            from lmchat.config import get_settings

            ttl = (
                incognito_ttl_seconds
                if incognito_ttl_seconds is not None
                else get_settings().lm_chat_incognito_ttl_seconds
            )
            expires_at = time() + float(ttl)

        async def _update() -> None:
            async with self._engine.begin() as conn:
                await conn.execute(
                    update(chats)
                    .where(chats.c.id == chat_id, chats.c.user_id == user_id)
                    .values(
                        incognito=1 if incognito else 0,
                        incognito_expires_at=expires_at,
                    )
                )

        await with_write_retry(_update)

        log.info(
            "chat.incognito_set",
            chat_id=chat_id,
            user_id=user_id,
            incognito=incognito,
            expires_at=expires_at,
        )
        await self._write_audit(
            event="chat.incognito_set",
            user_id=user_id,
            detail={"chat_id": chat_id, "incognito": incognito},
        )

        return await self.get(chat_id, user_id=user_id)

    async def purge_user_incognito(self, *, user_id: int) -> int:
        """Delete every incognito chat owned by *user_id*.

        Called by the logout endpoint (logout-sweep) so an admin
        clearing their session knows their incognito sessions are gone.

        Returns:
            Number of chats deleted.
        """
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(chats.c.id).where(
                    chats.c.user_id == user_id,
                    chats.c.incognito == 1,
                )
            )
            chat_ids = [int(r[0]) for r in result.fetchall()]

        if not chat_ids:
            return 0

        # Reuse delete() (not a bulk DELETE) so each chat still gets its
        # message-deleted notifications + audit log entry.
        deleted = 0
        for cid in chat_ids:
            try:
                await self.delete(cid, user_id=user_id)
                deleted += 1
            except ChatNotFoundError:
                # Raced with TTL purge — fine, count it as already gone.
                continue

        log.info(
            "chat.incognito_logout_sweep",
            user_id=user_id,
            deleted_count=deleted,
        )
        return deleted

    

    # ------------------------------------------------------------------
    # Public API — fork
    # ------------------------------------------------------------------

    async def fork(
        self, chat_id: int, *, user_id: int, at_message_id: int
    ) -> Chat:
        """Create a new chat that is a snapshot of *chat_id* up to *at_message_id*.

        Messages keep their original ``created_at``; the new chat's is now.
        The source chat's archived compaction spans are remapped onto the
        fork rather than dropped: for every ``compactions`` row referenced by
        a copied message, a new row is inserted on the fork chat (a source
        span that falls entirely after ``at_message_id`` hadn't happened yet
        at the forked point, so it's skipped), copied messages are
        repointed at the new row via an old-id → new-id map, and each new
        row's ``anchor_msg_id`` is remapped the same way (falling back to the
        old anchor id if that message itself wasn't copied).

        Args:
            at_message_id: Copy messages with id ≤ this value.

        Returns:
            The newly created forked :class:`Chat`.

        Raises:
            ChatNotFoundError: If the source chat is missing or not owned by user.
        """
        source_row = await self._get_chat_row(chat_id, user_id=user_id)
        source_title: str = source_row.title

        async with self._engine.connect() as conn:
            msg_rows = (
                await conn.execute(
                    select(messages)
                    .where(
                        messages.c.chat_id == chat_id,
                        messages.c.id <= at_message_id,
                    )
                    .order_by(messages.c.id)
                )
            ).fetchall()

        async def _insert_chat() -> int:
            async with self._engine.begin() as conn:
                result = await conn.execute(
                    insert(chats).values(
                        user_id=user_id,
                        title=f"{source_title} (fork)",
                    )
                )
                pk = result.inserted_primary_key
                if pk is None:
                    raise RuntimeError("INSERT into chats (fork) returned no PK")
                return int(pk[0])

        new_chat_id = await with_write_retry(_insert_chat)

        # Copy messages preserving original created_at; capture the
        # old_msg_id -> new_msg_id map for the compaction remap below.
        old_to_new_msg_id: dict[int, int] = {}
        if msg_rows:

            async def _insert_messages() -> None:
                async with self._engine.begin() as conn:
                    for msg in msg_rows:
                        result = await conn.execute(
                            insert(messages).values(
                                chat_id=new_chat_id,
                                role=msg.role,
                                content=msg.content,
                                reasoning_content=msg.reasoning_content,
                                state=msg.state,
                                response_id=msg.response_id,
                                model_id=msg.model_id,
                                created_at=msg.created_at,
                                # compaction_id intentionally omitted here —
                                # remapped in a second pass below once the
                                # new compactions rows exist.
                            )
                        )
                        pk = result.inserted_primary_key
                        if pk is None:
                            raise RuntimeError(
                                "INSERT into messages (fork) returned no PK"
                            )
                        old_to_new_msg_id[msg.id] = int(pk[0])

            await with_write_retry(_insert_messages)

            # Only spans with at least one copied member are relevant.
            old_compaction_ids = {
                msg.compaction_id for msg in msg_rows if msg.compaction_id is not None
            }
            if old_compaction_ids:
                async with self._engine.connect() as conn:
                    compaction_rows = (
                        await conn.execute(
                            select(compactions).where(
                                compactions.c.id.in_(old_compaction_ids)
                            )
                        )
                    ).fetchall()

                old_to_new_compaction_id: dict[int, int] = {}

                async def _insert_compactions() -> None:
                    async with self._engine.begin() as conn:
                        for crow in compaction_rows:
                            new_anchor = old_to_new_msg_id.get(
                                crow.anchor_msg_id, crow.anchor_msg_id
                            )
                            result = await conn.execute(
                                insert(compactions).values(
                                    chat_id=new_chat_id,
                                    summary=crow.summary,
                                    summary_model_id=crow.summary_model_id,
                                    anchor_msg_id=new_anchor,
                                    original_token_count=crow.original_token_count,
                                    summary_token_count=crow.summary_token_count,
                                )
                            )
                            pk = result.inserted_primary_key
                            if pk is None:
                                raise RuntimeError(
                                    "INSERT into compactions (fork) returned no PK"
                                )
                            old_to_new_compaction_id[crow.id] = int(pk[0])

                await with_write_retry(_insert_compactions)

                # Group copied messages by their NEW compaction id so each
                # span needs only one UPDATE statement.
                new_msg_ids_by_new_cid: dict[int, list[int]] = {}
                for msg in msg_rows:
                    if msg.compaction_id is None:
                        continue
                    new_cid = old_to_new_compaction_id.get(msg.compaction_id)
                    if new_cid is None:
                        continue
                    new_msg_ids_by_new_cid.setdefault(new_cid, []).append(
                        old_to_new_msg_id[msg.id]
                    )

                if new_msg_ids_by_new_cid:

                    async def _remap_messages() -> None:
                        async with self._engine.begin() as conn:
                            for new_cid, new_mids in new_msg_ids_by_new_cid.items():
                                await conn.execute(
                                    update(messages)
                                    .where(messages.c.id.in_(new_mids))
                                    .values(compaction_id=new_cid)
                                )

                    await with_write_retry(_remap_messages)

        async with self._engine.connect() as conn:
            new_row = (
                await conn.execute(
                    select(chats).where(chats.c.id == new_chat_id)
                )
            ).fetchone()

        if new_row is None:
            raise RuntimeError(
                f"chats row {new_chat_id!r} not found after fork INSERT"
            )

        new_chat = Chat.model_validate(new_row, from_attributes=True)

        log.info(
            "chat.forked",
            parent_chat_id=chat_id,
            new_chat_id=new_chat_id,
            at_message_id=at_message_id,
        )
        await self._write_audit(
            event="chat.forked",
            user_id=user_id,
            detail={
                "parent_chat_id": chat_id,
                "new_chat_id": new_chat_id,
                "at_message_id": at_message_id,
            },
        )
        return new_chat

    # ------------------------------------------------------------------
    # Public API — compact
    # ------------------------------------------------------------------

    async def compact(
        self,
        chat_id: int,
        *,
        user_id: int,
        target_tokens: int,
        http_client: httpx.AsyncClient,
        base_url: str,
    ) -> CompactResult:
        """Summarize + archive *chat_id*'s oldest span to fit within *target_tokens*.

        Hybrid compaction: fetches active messages (``compaction_id IS
        NULL``) oldest-first, identifies invariant-protected messages
        (system prompt, latest user message, tool-call pairs), and validates
        invariant_tokens + 10% margin ≤ target_tokens before walking
        oldest-first to collect archive candidates down to target_tokens ×
        0.9. Protected messages are skipped, so the archive set may be
        non-contiguous; it's split into contiguous runs, capped at
        :data:`_COMPACTION_MAX_RUNS_PER_CALL`, and each kept run is
        summarized via the LLM (fail policy = ABORT — any upstream failure
        raises :class:`CompactionSummaryError` and nothing is archived).
        One ``compactions`` row is inserted per run and that run's messages'
        ``compaction_id`` updated in the same transaction (archived, not
        deleted — ``message_embeddings`` are never cascade-dropped).

        Args:
            target_tokens: Upper bound on the remaining token count (after
                           the 10% safety margin).
            http_client:   Shared ``httpx.AsyncClient`` (threaded the same
                           way :meth:`generate_title` does).
            base_url:      LM Studio base URL (no trailing slash).

        Returns:
            :class:`CompactResult` (``compaction_ids`` covers every row
            written this call).

        Raises:
            ChatNotFoundError:      If missing or not owned by user.
            CompactTooLowError:     If target_tokens is below the invariant
                                    minimum.
            CompactionSummaryError: If the summary call fails/times out —
                                    nothing is archived in that case.
        """
        chat_row = await self._get_chat_row(chat_id, user_id=user_id)

        lock = self._chat_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            return await self._compact_under_lock(
                chat_id=chat_id,
                user_id=user_id,
                target_tokens=target_tokens,
                chat_row=chat_row,
                http_client=http_client,
                base_url=base_url,
            )

    async def _compact_under_lock(
        self,
        *,
        chat_id: int,
        user_id: int,
        target_tokens: int,
        chat_row: Any,
        http_client: httpx.AsyncClient,
        base_url: str,
    ) -> CompactResult:
        """Perform compaction while holding the per-chat lock.

        Not part of the public API; extracted for readability.
        """
        # Active, committed messages oldest-first: already-archived rows are
        # never re-selected, and only state=FINAL is eligible (a draft/
        # pending/aborted row isn't settled content and would double-count
        # once it finalizes).
        async with self._engine.connect() as conn:
            msg_rows = (
                await conn.execute(
                    select(messages)
                    .where(
                        messages.c.chat_id == chat_id,
                        messages.c.compaction_id.is_(None),
                        messages.c.state == PersistState.FINAL.value,
                    )
                    .order_by(messages.c.id)
                )
            ).fetchall()

        if not msg_rows:
            return CompactResult(
                chat_id=chat_id,
                removed_message_ids=[],
                remaining_token_count=0,
                original_token_count=0,
            )

        # Resolve tokenizer hint from the latest message that has a model_id.
        hint = _DEFAULT_ENCODING
        for msg in reversed(msg_rows):
            if msg.model_id:
                hint = msg.model_id
                break

        # Called for forward-compat (a future tokenizer_hint field); KeyError
        # is non-fatal, _resolve_encoding handles the fallback + WARN.
        try:
            await self._models_service.get_capabilities(hint)
        except KeyError:
            pass  # _resolve_encoding will WARN and fall back to cl100k_base

        enc = _resolve_encoding(hint)

        token_counts: list[int] = [
            _count_tokens(enc, msg.content) for msg in msg_rows
        ]
        original_token_count = sum(token_counts)

        # ------------------------------------------------------------------
        # Identify invariant-protected message indices.
        # ------------------------------------------------------------------

        # 1. First system-role message index (if present).
        system_idx: int | None = None
        for i, msg in enumerate(msg_rows):
            if msg.role == "system":
                system_idx = i
                break

        # 2. Latest user-role message index.
        latest_user_idx: int | None = None
        for i in range(len(msg_rows) - 1, -1, -1):
            if msg_rows[i].role == "user":
                latest_user_idx = i
                break

        # 3. Tool-call pairs — collect response_ids referenced by tool-role
        #    messages so assistant+tool pairs can be kept atomic. response_id
        #    doubles as the tool_call_id correlation key (the only id column
        #    on messages).
        tool_call_ids_in_tool_msgs: set[str] = set()
        for msg in msg_rows:
            if msg.role == "tool":
                if msg.response_id:
                    tool_call_ids_in_tool_msgs.add(msg.response_id)

        protected_indices: set[int] = set()
        if system_idx is not None:
            protected_indices.add(system_idx)
        if latest_user_idx is not None:
            protected_indices.add(latest_user_idx)

        # Keep assistant+tool pairs: if a tool message references a
        # response_id that an assistant message also references, keep both.
        for i, msg in enumerate(msg_rows):
            if msg.role == "assistant" and msg.response_id in tool_call_ids_in_tool_msgs:
                protected_indices.add(i)
            if msg.role == "tool" and msg.response_id:
                protected_indices.add(i)
                for j, amsg in enumerate(msg_rows):
                    if amsg.role == "assistant" and amsg.response_id == msg.response_id:
                        protected_indices.add(j)

        # ------------------------------------------------------------------
        # Validate: invariant token sum + 10% margin vs target_tokens.
        # ------------------------------------------------------------------
        invariant_token_sum = sum(
            token_counts[i] for i in protected_indices
        )
        invariant_minimum = invariant_token_sum * (1 + _SAFETY_MARGIN)

        if invariant_minimum > target_tokens:
            raise CompactTooLowError(
                f"target_tokens={target_tokens} is below the invariant minimum "
                f"({invariant_minimum:.0f} tokens including 10% safety margin). "
                f"Invariant-protected messages require {invariant_token_sum} tokens."
            )

        # ------------------------------------------------------------------
        # Collect archive candidates (oldest-first walk). Protected messages
        # are skipped, so the archive set may be non-contiguous by id —
        # membership is the `compaction_id` FK, not a range.
        # ------------------------------------------------------------------
        drop_indices: set[int] = set()
        remaining_tokens = original_token_count
        target_with_margin = int(target_tokens * (1 - _SAFETY_MARGIN))

        for i in range(len(msg_rows)):
            if remaining_tokens <= target_with_margin:
                break
            if i in protected_indices:
                continue
            drop_indices.add(i)
            remaining_tokens -= token_counts[i]

        drop_ids = [msg_rows[i].id for i in sorted(drop_indices)]

        if not drop_ids:
            return CompactResult(
                chat_id=chat_id,
                removed_message_ids=[],
                remaining_token_count=original_token_count,
                original_token_count=original_token_count,
            )

        # Split into contiguous runs. A retained message (system prompt,
        # latest user message, an interior assistant+tool pair) creates a
        # gap, so archived content can fall both before AND after a retained
        # span — a single summary anchored at the overall min(id) would
        # invert order. Summarizing + anchoring each run separately keeps
        # every summary at its correct chronological position;
        # StreamingService._load_replay_history's merge already flushes N
        # independent compaction rows in anchor_msg_id order.
        sorted_drop = sorted(drop_indices)
        runs: list[list[int]] = []
        for idx in sorted_drop:
            if runs and idx == runs[-1][-1] + 1:
                runs[-1].append(idx)
            else:
                runs.append([idx])

        # Bound the per-call cost: scattered retained tool-call pairs can
        # split the archive set into many runs, and each run costs a full
        # summarizer LLM call with a floor of _COMPACTION_SUMMARY_MIN_TOKENS
        # regardless of content — so an unbounded run count means unbounded
        # upstream calls. `runs` is oldest-first, so capping keeps the
        # highest-value (earliest) archives; excess runs are left LIVE and
        # picked up by a later /compact call. drop_ids / archived_token_count
        # / remaining_tokens below are recomputed from the KEPT runs only.
        if len(runs) > _COMPACTION_MAX_RUNS_PER_CALL:
            runs = runs[:_COMPACTION_MAX_RUNS_PER_CALL]

        kept_indices = {i for run in runs for i in run}
        drop_ids = [msg_rows[i].id for i in sorted(kept_indices)]
        archived_token_count = sum(token_counts[i] for i in kept_indices)
        remaining_tokens = original_token_count - archived_token_count

        summary_budget_total = max(
            int(target_tokens * _COMPACTION_SUMMARY_BUDGET_RATIO),
            _COMPACTION_SUMMARY_MIN_TOKENS,
        )

        # Deliberately decoupled from `hint`: hint may fall back to
        # "cl100k_base", which isn't a valid LM Studio model id and would
        # 400 if handed to _run_llm_distill. Resolution order: latest
        # message's model_id, then the chat's model_id, then any loaded
        # non-embedding model, else CompactionSummaryError.
        summary_model_id: str | None = None
        for msg in reversed(msg_rows):
            if msg.model_id:
                summary_model_id = msg.model_id
                break
        if summary_model_id is None and chat_row.model_id:
            summary_model_id = chat_row.model_id
        if summary_model_id is None:
            for model in await self._models_service.list_loaded():
                is_embedding = (
                    getattr(model, "type", None) == "embedding"
                    or "embed" in (model.key or "").lower()
                )
                if not is_embedding:
                    summary_model_id = model.key
                    break
        if summary_model_id is None:
            raise CompactionSummaryError(
                "no LM Studio model available to generate the compaction summary"
            )

        # Resolve to the loaded instance id before calling — a catalog key
        # that isn't the loaded instance name 400s and aborts the whole
        # compaction. compactions rows keep summary_model_id (catalog key)
        # for display.
        _summary_res = await self._models_service.resolve_to_loaded_or_fallback(
            summary_model_id
        )
        _summary_wire_id = _summary_res.wire_id or summary_model_id

        # Summarize every run BEFORE touching the DB (ABORT policy): none of
        # the segments are written until the transaction below, so a failure
        # partway through leaves nothing archived.
        segments: list[dict[str, Any]] = []
        for run in runs:
            run_drop_ids = [msg_rows[i].id for i in run]
            run_archive_rows = [msg_rows[i] for i in run]
            run_archived_tokens = sum(token_counts[i] for i in run)
            run_share = (
                run_archived_tokens / archived_token_count
                if archived_token_count
                else 1.0
            )
            run_summary_budget = max(
                int(summary_budget_total * run_share),
                _COMPACTION_SUMMARY_MIN_TOKENS,
            )

            run_summary = await self._run_llm_distill(
                http_client=http_client,
                base_url=base_url,
                model_id=_summary_wire_id,
                archive_rows=run_archive_rows,
                max_tokens=run_summary_budget,
            )
            # Defensive clamp so a large target_tokens can't yield a summary
            # bigger than the archive it replaces (mirrors project_summary_service).
            run_summary = run_summary[:DEFAULT_MAX_LENGTH]
            segments.append(
                {
                    "drop_ids": run_drop_ids,
                    "anchor_msg_id": run_drop_ids[0],
                    "summary": run_summary,
                    "archived_token_count": run_archived_tokens,
                    "summary_token_count": _count_tokens(enc, run_summary),
                }
            )

        # One compactions row per run, then that run's messages' compaction_id
        # is UPDATEd — never DELETEd, so message_embeddings keep participating
        # in semantic recall.
        async def _archive() -> list[int]:
            new_ids: list[int] = []
            async with self._engine.begin() as conn:
                for seg in segments:
                    result = await conn.execute(
                        insert(compactions).values(
                            chat_id=chat_id,
                            summary=seg["summary"],
                            summary_model_id=summary_model_id,
                            anchor_msg_id=seg["anchor_msg_id"],
                            original_token_count=seg["archived_token_count"],
                            summary_token_count=seg["summary_token_count"],
                        )
                    )
                    pk = result.inserted_primary_key
                    if pk is None:
                        raise RuntimeError("INSERT into compactions returned no PK")
                    new_compaction_id = int(pk[0])
                    new_ids.append(new_compaction_id)

                    await conn.execute(
                        update(messages)
                        .where(messages.c.id.in_(seg["drop_ids"]))
                        .values(compaction_id=new_compaction_id)
                    )
            return new_ids

        new_compaction_ids = await with_write_retry(_archive)

        # Archived rows are never deleted, so the delete-notification
        # contract doesn't apply here.

        total_summary_token_count = sum(
            seg["summary_token_count"] for seg in segments
        )

        log.info(
            "chat.compacted",
            chat_id=chat_id,
            user_id=user_id,
            archived_count=len(drop_ids),
            remaining_tokens=remaining_tokens,
            compaction_ids=new_compaction_ids,
            archived_token_count=archived_token_count,
            summary_token_count=total_summary_token_count,
            span_count=len(segments),
        )
        await self._write_audit(
            event="chat.compacted",
            user_id=user_id,
            detail={
                "chat_id": chat_id,
                "archived_count": len(drop_ids),
                "remaining_tokens": remaining_tokens,
                "compaction_ids": new_compaction_ids,
                "archived_token_count": archived_token_count,
                "summary_token_count": total_summary_token_count,
                "span_count": len(segments),
            },
        )

        # compaction_id/summary report the most recent (highest-anchor) span
        # for backward compat; compaction_ids has the full set (see class docstring).
        return CompactResult(
            chat_id=chat_id,
            removed_message_ids=drop_ids,
            remaining_token_count=remaining_tokens,
            original_token_count=original_token_count,
            compaction_id=new_compaction_ids[-1],
            summary=segments[-1]["summary"],
            archived_count=len(drop_ids),
            summary_token_count=total_summary_token_count,
            compaction_ids=new_compaction_ids,
        )

    async def _run_llm_distill(
        self,
        *,
        http_client: httpx.AsyncClient,
        base_url: str,
        model_id: str,
        archive_rows: list[Any],
        max_tokens: int,
    ) -> str:
        """Summarize an archive set via LM Studio's ``/v1/chat/completions``.

        Unlike title generation (best-effort) or the streaming service's OOB
        followups/distill calls (fail-soft, return ``[]``), this has a hard
        ABORT fail policy: any upstream failure or unusable response raises
        :class:`CompactionSummaryError` so the caller never archives a span
        with no real summary.

        Returns:
            The generated summary text (non-empty, stripped).

        Raises:
            CompactionSummaryError: On any upstream failure, non-200
                                    status, unusable payload, or empty
                                    content.
        """
        convo_lines: list[str] = []
        for row in archive_rows:
            content = str(row.content) if row.content is not None else ""
            if not content:
                continue
            if len(content) > _COMPACTION_SUMMARY_MSG_CHAR_CAP:
                content = content[:_COMPACTION_SUMMARY_MSG_CHAR_CAP]
            convo_lines.append(f"{str(row.role).upper()}: {content}")

        if not convo_lines:
            raise CompactionSummaryError(
                "archive set has no summarizable content"
            )

        request_messages: list[dict[str, str]] = [
            {"role": "system", "content": _COMPACTION_SUMMARY_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Conversation turns to archive:\n\n"
                    + "\n".join(convo_lines)
                    # /no_think (Qwen) skips reasoning so a local thinking
                    # model doesn't blow the timeout / starve the budget.
                    + "\n\nWrite the running summary now. /no_think"
                ),
            },
        ]

        request_body: dict[str, Any] = {
            "model": model_id,
            "messages": request_messages,
            "stream": False,
            # Summary-output budget plus reasoning headroom so a reasoning
            # model can finish thinking before it emits the summary.
            "max_tokens": (
                max(max_tokens, _COMPACTION_SUMMARY_MIN_TOKENS)
                + _COMPACTION_SUMMARY_REASONING_HEADROOM
            ),
            "temperature": 0.2,  # factual summary, not creative
            # Models that honor this skip reasoning entirely; models that
            # ignore it still work via the reasoning headroom above, with
            # oob_message_text salvaging reasoning_content as a fallback.
            "thinking": {"type": "disabled"},
        }

        url = f"{base_url.rstrip('/')}/v1/chat/completions"
        try:
            response = await http_client.post(
                url, json=request_body, timeout=self._aux_model_timeout_sec
            )
        except httpx.HTTPError as exc:
            raise CompactionSummaryError(f"summary call failed: {exc}") from exc

        if response.status_code != 200:
            raise CompactionSummaryError(
                f"summary call returned HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )

        try:
            payload = response.json()
            choices = payload.get("choices") or []
            message = choices[0].get("message") or {}
            # Falls back to reasoning_content on reasoning models (see oob_text.py).
            summary = oob_message_text(message)
        except (ValueError, AttributeError, IndexError, TypeError) as exc:
            raise CompactionSummaryError(
                f"summary call returned an unusable payload: {exc}"
            ) from exc

        if not summary:
            raise CompactionSummaryError("summary call returned empty content")

        return summary

    # ------------------------------------------------------------------
    # Public API — compaction recall
    # ------------------------------------------------------------------

    async def list_compactions(
        self, chat_id: int, *, user_id: int
    ) -> list[Compaction]:
        """Return every compaction span for *chat_id*, oldest first.

        ``archived_count`` is derived per-span as the live count of
        ``messages`` rows whose ``compaction_id`` equals the span's id (not a
        cached number that could drift) — one LEFT OUTER JOIN + GROUP BY
        rather than an N+1 per-row count.

        Returns:
            List of :class:`Compaction`, ordered by ``anchor_msg_id`` ascending.

        Raises:
            ChatNotFoundError: If missing or not owned by user.
        """
        await self._get_chat_row(chat_id, user_id=user_id)

        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(compactions, func.count(messages.c.id).label("archived_count"))
                    .select_from(compactions)
                    .outerjoin(
                        messages, messages.c.compaction_id == compactions.c.id
                    )
                    .where(compactions.c.chat_id == chat_id)
                    .group_by(compactions.c.id)
                    .order_by(compactions.c.anchor_msg_id.asc())
                )
            ).fetchall()

        return [Compaction.model_validate(r, from_attributes=True) for r in rows]

    async def get_compaction_messages(
        self, chat_id: int, compaction_id: int, *, user_id: int
    ) -> list[Message]:
        """Return the archived message set for one compaction span, id-ordered.

        Args:
            chat_id:       PK of the chat (ownership scope).
            compaction_id: PK of the ``compactions`` row.
            user_id:       Must own the chat.

        Returns:
            The archived :class:`Message` rows, ordered by id ascending.

        Raises:
            ChatNotFoundError: If the chat is missing/not owned, OR the
                               compaction does not belong to this chat —
                               both surface as 404 so existence never leaks.
        """
        await self._get_chat_row(chat_id, user_id=user_id)

        async with self._engine.connect() as conn:
            compaction_row = (
                await conn.execute(
                    select(compactions).where(
                        compactions.c.id == compaction_id,
                        compactions.c.chat_id == chat_id,
                    )
                )
            ).fetchone()
            if compaction_row is None:
                raise ChatNotFoundError(
                    f"compaction {compaction_id!r} not found on chat {chat_id!r}"
                )

            msg_rows = (
                await conn.execute(
                    select(messages)
                    .where(messages.c.compaction_id == compaction_id)
                    .order_by(messages.c.id.asc())
                )
            ).fetchall()

        return [Message.model_validate(r, from_attributes=True) for r in msg_rows]
