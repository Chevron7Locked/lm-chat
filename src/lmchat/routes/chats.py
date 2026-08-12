# SPDX-License-Identifier: Apache-2.0
"""Chat CRUD + fork + compact + message-append routes for lm-chat.

All routes:
- Require ``Depends(require_user)`` — unauthenticated requests → 401.
- Return 404 (never 403) on cross-user access, so resource existence is
  not leaked.
- Use typed Pydantic response models — no ``dict[str, object]`` returns.

Error mapping
-------------
``ChatNotFoundError``   → HTTP 404
``CompactTooLowError``  → HTTP 422
``ValueError``          → HTTP 422 (invalid role on message append)
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from lmchat.logging import get_logger
from lmchat.metrics import STREAMS_SALVAGED
from lmchat.routes._dependencies import (
    get_chat_service_dep,
    get_engine_dep,
    get_integrations_service_dep,
    get_message_service_dep,
    get_models_service_dep,
    get_streaming_service_dep,
    require_user,
)
from lmchat.routes.projects import ProjectResponse
from lmchat.services.auth_service import User
from lmchat.services.integrations_service import IntegrationsService
from lmchat.services.streaming_service import (
    StreamingService,
    _grammar_degrade_eligible,
    _grammar_degrade_warning,
)

if TYPE_CHECKING:
    from lmchat.services.lmstudio_streaming_client import LmstudioStreamingClient
from lmchat.services.chat_service import (
    Chat,
    ChatNotFoundError,
    ChatNotShareableError,
    ChatService,
    ChatShare,
    Compaction,
    CompactionSummaryError,
    CompactResult,
    CompactTooLowError,
    TitleGenerationError,
)
from lmchat.services.lmstudio_streaming_client import StreamingClientUpstreamError
from lmchat.services.message_service import (
    EditNotAllowedError,
    Message,
    MessageNotFoundError,
    MessageService,
)
from lmchat.services.prompt_assembly import serialize_prior_turns
from lmchat.utils.lru_counter import LruCappedCounter
from lmchat.utils.task_lifetime import spawn_background_task

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Sub-session cross-turn MTP counter
# ---------------------------------------------------------------------------
# Persists cumulative tool-round counts across HTTP turns of the same
# sub-session.  Each sub_session_stream POST is an independent HTTP request;
# the FE sends the full message history but not the prior round count.
# We key on chat_id (int) via the same LruCappedCounter primitive
# StreamingService._tool_round_counts uses, with its own (lower) cap.
#
# Not keyed on (chat_id, panel) — the chat_id alone is sufficient because
# each chat has at most one active sub-session panel at a time.
_SUB_SESSION_TOOL_ROUNDS_LRU_CAP: Final[int] = 512
_sub_session_tool_rounds = LruCappedCounter(_SUB_SESSION_TOOL_ROUNDS_LRU_CAP)


def _sub_session_increment_tool_round(chat_id: int) -> None:
    """Increment and LRU-cap the sub-session tool-round counter for *chat_id*."""
    _sub_session_tool_rounds.increment(chat_id)


def _sub_session_get_tool_rounds(chat_id: int) -> int:
    """Return the persisted cumulative tool-round count for *chat_id* (0 if unknown)."""
    return _sub_session_tool_rounds.get(chat_id, 0)


def _sub_session_reset_tool_rounds(chat_id: int) -> None:
    """Reset the MTP tool-round counter for *chat_id*.

    Called in two places:
    1. When an ``mtp_suspected`` error event is observed inside
       ``_sub_session_sse`` — mirrors the main-pump reset in
       ``streaming_service.py`` so each retry starts a fresh detection window.
    2. Directly in tests to isolate per-test state.

    NOT called automatically on sub-session close; the counter accumulates
    across turns until an mtp_suspected event or LRU eviction resets it.
    """
    _sub_session_tool_rounds.reset(chat_id)


# ---------------------------------------------------------------------------
# HTTP status constants
# ---------------------------------------------------------------------------

_HTTP_201: Final[int] = 201
_HTTP_204: Final[int] = 204
_HTTP_400: Final[int] = 400
_HTTP_403: Final[int] = 403
_HTTP_404: Final[int] = 404
_HTTP_409: Final[int] = 409
_HTTP_422: Final[int] = 422
_HTTP_502: Final[int] = 502

# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class ChatResponse(BaseModel):
    """JSON projection of one ``chats`` row.

    Mirrors :class:`~lmchat.services.chat_service.Chat` exactly so the
    route layer never constructs dicts.

    Attributes:
        id:         Row PK.
        user_id:    Owning user PK.
        title:      Chat display title.
        folder:     Optional folder name.
        pinned:     Whether the chat is pinned.
        created_at: Row creation timestamp.
        updated_at: Last-updated timestamp.
        settings:   Per-chat settings JSON blob. Keys:
                    ``rag_enabled: bool``, ``reasoning_effort: str | None``.
        incognito:  Incognito mode flag.
        incognito_expires_at: UNIX epoch seconds; null when
                    incognito=False.
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
    # DnD sort order within each folder / pinned section.
    display_order: int = 0
    # Per-chat model persistence: the model selected in this chat's composer.
    # None means no model selected (new-chat default: "Select a model…" placeholder).
    model_id: str | None = None
    # Incognito mode.
    incognito: bool = False
    incognito_expires_at: float | None = None
    # Optional project membership. None ↔ the
    # chat is un-projected (legacy / default).
    project_id: int | None = None


class ChatWithMessagesResponse(BaseModel):
    """Chat row plus its ordered message list with pagination metadata.

    Attributes:
        id:         Row PK.
        user_id:    Owning user PK.
        title:      Chat display title.
        folder:     Optional folder name.
        pinned:     Whether the chat is pinned.
        created_at: Row creation timestamp.
        updated_at: Last-updated timestamp.
        messages:   Ordered list of messages (oldest first, up to limit).
        has_more:   True when older messages exist beyond this page.
        incognito:  Incognito mode flag.
        incognito_expires_at: UNIX epoch seconds; null when
                    incognito=False.
        project_id: Owning project PK, or null when the chat
                    is not attached to a project.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    title: str
    folder: str | None
    pinned: bool
    created_at: datetime
    updated_at: datetime
    messages: list[Message]
    # Cursor pagination metadata.
    has_more: bool = False
    # Per-chat model persistence: the model selected in this chat's composer.
    # None means no model selected (new-chat default: "Select a model…" placeholder).
    model_id: str | None = None
    # Incognito mode.
    incognito: bool = False
    incognito_expires_at: float | None = None
    # Owning project PK (null when un-projected). Populated from
    # chats.project_id so API consumers see the project linkage on the detail
    # endpoint (the field existed on ChatResponse but the detail response never
    # set it — a project-linked chat reported project_id: null).
    project_id: int | None = None


class CompactResultResponse(BaseModel):
    """Result of a compaction operation.

    Mirrors :class:`~lmchat.services.chat_service.CompactResult`.

    Attributes:
        chat_id:              PK of the compacted chat.
        removed_message_ids:  IDs of messages that were archived (kept name
                              for backward compat — rows are archived, not
                              deleted).
        remaining_token_count: Token count of the still-active messages
                              after compaction.
        original_token_count:  Token count of the active messages before
                              this compaction call.
        compaction_id:        PK of the new ``compactions`` row, or ``None``
                              on a no-op (nothing needed archiving).
        summary:               The generated running summary, or ``None``
                              on a no-op.
        archived_count:        ``len(removed_message_ids)``.
        summary_token_count:   Token count of the generated summary.
        compaction_ids:        PKs of every ``compactions`` row this call
                              wrote, oldest-anchor first — ``[]`` on a
                              no-op. A single call may write more than one
                              row (see ``CompactResult``'s docstring); the
                              full per-span detail is available via
                              ``GET /api/chats/{id}/compactions``.
    """

    model_config = ConfigDict(from_attributes=True)

    chat_id: int
    removed_message_ids: list[int]
    remaining_token_count: int
    original_token_count: int
    compaction_id: int | None = None
    summary: str | None = None
    archived_count: int = 0
    summary_token_count: int = 0
    compaction_ids: list[int] = []


class CompactionResponse(BaseModel):
    """One compaction span, for the recall endpoints.

    Mirrors :class:`~lmchat.services.chat_service.Compaction`.

    Attributes:
        id:                    PK of the compactions row.
        chat_id:               Parent chat PK.
        summary:                The generated running summary of the
                               archived span.
        summary_model_id:      Model that produced the summary (nullable).
        anchor_msg_id:         Display-position id (oldest archived id at
                               archive time); not a membership range.
        original_token_count:  Token count of the archived span before
                               summarization.
        summary_token_count:   Token count of the generated summary.
        created_at:            Row creation timestamp.
        archived_count:        Number of messages currently archived under
                               this span (live membership count, derived by
                               :meth:`ChatService.list_compactions`).
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


class GenerateTitleResponse(BaseModel):
    """Result of an auto-title generation call.

    The endpoint is idempotent: if the chat already
    has a user-set title, the existing title is returned unchanged.

    Attributes:
        title: The final title persisted on the chat row.  Equal to the
               pre-existing title when no generation occurred.
    """

    model_config = ConfigDict(from_attributes=True)

    title: str


# ---------------------------------------------------------------------------
# Module-local dependency helpers
# (canonical versions live in _dependencies.py; these helpers keep the
#  router independent of circular-import concerns at module load time)
# ---------------------------------------------------------------------------


def _get_chat_service(request: Request) -> ChatService:
    """Return ``app.state.chat_service``; raise ``RuntimeError`` if unset."""
    return get_chat_service_dep(request)


def _get_message_service(request: Request) -> MessageService:
    """Return ``app.state.message_service``; raise ``RuntimeError`` if unset."""
    return get_message_service_dep(request)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router: APIRouter = APIRouter(prefix="/api/chats", tags=["chats"])


# ---------------------------------------------------------------------------
# POST /api/chats — create a new chat
# ---------------------------------------------------------------------------


@router.post("", response_model=ChatResponse, status_code=_HTTP_201)
async def create_chat(
    request: Request,
    title: str = Form(...),
    incognito: bool = Form(default=False),
    user: User = Depends(require_user),
    chat_service: ChatService = Depends(_get_chat_service),
    streaming_service: StreamingService = Depends(get_streaming_service_dep),
) -> ChatResponse:
    """Create a new chat for the authenticated user.

    Args:
        request:             FastAPI Request (dependency access).
        title:               Chat display title (form field).
        incognito:           When True, the chat is marked incognito. Memory
                             write paths short-circuit and the chat is
                             scheduled for TTL purge or logout-sweep.
                             Default False.
        user:                Authenticated user from ``require_user``.
        chat_service:        Injected ``ChatService``.
        streaming_service:   Injected ``StreamingService`` (for counter reset).

    Returns:
        The newly created :class:`ChatResponse` (201).
    """
    log.info(
        "chats.create.request",
        user_id=user.id,
        title=title,
        incognito=incognito,
    )
    chat: Chat = await chat_service.create(
        user_id=user.id,
        title=title,
        incognito=incognito,
    )
    streaming_service.reset_counter(chat.id)
    return ChatResponse.model_validate(chat.model_dump())


# ---------------------------------------------------------------------------
# GET /api/chats — list chats for user
# ---------------------------------------------------------------------------


@router.get("", response_model=list[ChatResponse])
async def list_chats(
    request: Request,
    folder: str | None = None,
    project_id: int | None = None,
    unscoped: bool = False,
    user: User = Depends(require_user),
    chat_service: ChatService = Depends(_get_chat_service),
) -> list[ChatResponse]:
    """Return chats for the authenticated user, optionally filtered.

    Args:
        request:      FastAPI Request.
        folder:       Optional folder filter (query param).
        project_id:   When set, restrict to chats in this project.
        unscoped:     When True AND project_id
                      is None, restrict to un-projected chats
                      (``project_id IS NULL``). When False (default),
                      returns every chat the user owns regardless of
                      project_id (existing behavior preserved).
        user:         Authenticated user.
        chat_service: Injected ``ChatService``.

    Returns:
        List of :class:`ChatResponse` (newest first).
    """
    chats: list[Chat] = await chat_service.list_for_user(
        user.id,
        folder=folder,
        project_id=project_id,
        unscoped=unscoped,
    )
    return [ChatResponse.model_validate(c.model_dump()) for c in chats]


# ---------------------------------------------------------------------------
# PATCH /api/chats/reorder — move a chat to a folder at a given position
# ---------------------------------------------------------------------------


@router.patch("/reorder", status_code=200, response_model=None)
async def reorder_chats(
    request: Request,
    chat_id: int = Form(...),
    folder: str | None = Form(default=None),
    # Explicit "clear the folder" sentinel. Before this, moving a
    # chat OUT of a folder relied on the caller omitting `folder` entirely
    # and FastAPI's Form default happening to already be None — functionally
    # correct, but an implicit contract with no way to express "clear" that
    # doesn't collide with "leave unspecified". `clear_folder=true`
    # overrides whatever `folder` carries and forces the target to the
    # ungrouped/pinned bucket (folder=None), same convention as passing
    # `folder=None` directly to ``ChatService.reorder``.
    clear_folder: bool = Form(default=False),
    display_order: int = Form(..., ge=0),
    user: User = Depends(require_user),
    chat_service: ChatService = Depends(_get_chat_service),
) -> dict:  # type: ignore[type-arg]
    """Move a chat to a different folder at a specific display_order position.

    All other chats in the target folder are renumbered atomically under a
    per-folder asyncio.Lock.

    Expects form-encoded body with ``chat_id``, ``folder`` (optional),
    ``clear_folder`` (optional), and ``display_order`` (all
    mutating routes use application/x-www-form-urlencoded).

    Args:
        request:       FastAPI Request.
        chat_id:       PK of the chat to move (form field).
        folder:        Target folder (form field; omit for ungrouped).
        clear_folder:  When true, explicitly clears the chat's folder
                       (ungrouped/pinned bucket) regardless of ``folder``.
        display_order: Target position (0-based) within the folder (form field).
        user:          Authenticated user.
        chat_service:  Injected ``ChatService``.

    Returns:
        ``{"ok": true}`` on success.

    Raises:
        HTTPException: 404 if the chat is not found or not owned by the user.
    """
    target_folder = None if clear_folder else folder
    try:
        await chat_service.reorder(
            chat_id=chat_id,
            user_id=user.id,
            folder=target_folder,
            display_order=display_order,
        )
    except ChatNotFoundError as exc:
        raise HTTPException(status_code=_HTTP_404, detail="chat not found") from exc

    return {"ok": True}


# ---------------------------------------------------------------------------
# GET /api/chats/{chat_id} — read chat with messages
# ---------------------------------------------------------------------------


@router.get("/{chat_id}", response_model=ChatWithMessagesResponse)
async def get_chat(
    chat_id: int,
    request: Request,
    before_id: int | None = None,
    since_id: int | None = None,
    limit: int = 200,
    user: User = Depends(require_user),
    chat_service: ChatService = Depends(_get_chat_service),
    message_service: MessageService = Depends(_get_message_service),
) -> ChatWithMessagesResponse:
    """Return a single chat including its ordered messages.

    Cross-user access returns 404 to avoid leaking resource existence.

    Supports cursor-based pagination.  Default page size is 200
    messages.  Provide ``before_id`` to load older messages; provide
    ``since_id`` to poll for new messages.

    Args:
        chat_id:         PK of the chat to fetch.
        request:         FastAPI Request.
        before_id:       Cursor — return messages with id < before_id.
        since_id:        Cursor — return messages with id > since_id.
        limit:           Page size (default 200, capped at 500).
        user:            Authenticated user.
        chat_service:    Injected ``ChatService``.
        message_service: Injected ``MessageService``.

    Returns:
        :class:`ChatWithMessagesResponse` (200) with ``has_more`` flag.

    Raises:
        HTTPException: 404 if the chat does not exist or is not owned by the user.
    """
    try:
        chat: Chat = await chat_service.get(chat_id, user_id=user.id)
    except ChatNotFoundError as exc:
        raise HTTPException(status_code=_HTTP_404, detail="chat not found") from exc

    # Cap limit to prevent abuse while honouring caller's preference.
    effective_limit = min(max(limit, 1), 500)

    # Delegate to the MessageService for the chat's message list — the route no
    # longer reaches into ``chat_service._engine`` and the message
    # retrieval goes through the canonical service surface.
    msg_list, has_more = await message_service.list_for_chat(
        chat_id,
        user_id=user.id,
        limit=effective_limit,
        before_id=before_id,
        since_id=since_id,
    )

    return ChatWithMessagesResponse(
        id=chat.id,
        user_id=chat.user_id,
        title=chat.title,
        folder=chat.folder,
        pinned=chat.pinned,
        created_at=chat.created_at,
        updated_at=chat.updated_at,
        messages=msg_list,
        has_more=has_more,
        model_id=chat.model_id,
        incognito=chat.incognito,
        incognito_expires_at=chat.incognito_expires_at,
        project_id=chat.project_id,
    )


# ---------------------------------------------------------------------------
# PATCH /api/chats/{chat_id} — rename / move-to-folder / pin
# ---------------------------------------------------------------------------


@router.patch("/{chat_id}", response_model=ChatResponse)
async def patch_chat(
    chat_id: int,
    request: Request,
    title: str | None = Form(default=None),
    folder: str | None = Form(default=None),
    pinned: bool | None = Form(default=None),
    model_id: str | None = Form(default=None),
    rag_enabled: bool | None = Form(default=None),
    reasoning_effort: str | None = Form(default=None),
    ab_compare: str | None = Form(default=None),
    # Flat form params — mirror useChats.ts ``useUpdateChat`` which sends
    # ab_compare_enabled / ab_compare_model_a / ab_compare_model_b as separate
    # fields rather than a JSON blob.  FastAPI silently drops undeclared Form
    # fields, so these MUST be declared here.  The handler merges them into the
    # ``settings_patch["ab_compare"]`` dict (see below); the JSON-blob path is
    # kept for the e2e-live test that PATCHes with ``form: { ab_compare: ... }``.
    ab_compare_enabled: bool | None = Form(default=None),
    ab_compare_model_a: str | None = Form(default=None),
    ab_compare_model_b: str | None = Form(default=None),
    incognito: bool | None = Form(default=None),
    # Per-chat rail fields — must mirror useChats.ts
    # ``useUpdateChat``. Without these declarations FastAPI silently drops
    # the form fields and the Pydantic gate never sees them.
    system_prompt: str | None = Form(default=None),
    temperature: float | None = Form(default=None),
    top_p: float | None = Form(default=None),
    top_k: int | None = Form(default=None),
    min_p: float | None = Form(default=None),
    repeat_penalty: float | None = Form(default=None),
    max_tokens: int | None = Form(default=None),
    reasoning: str | None = Form(default=None),
    self_consistency_enabled: bool | None = Form(default=None),
    chain_of_verification_enabled: bool | None = Form(default=None),
    stateless: bool | None = Form(default=None),
    active_preset: str | None = Form(default=None),
    # Move/detach a chat between projects.
    project_id: int | None = Form(default=None),
    # Comma-separated list of fields to explicitly NULL. Required because
    # FastAPI's Form(default=None) coerces empty form fields to None and
    # we can't distinguish "omit" from "clear" otherwise. Clearable fields:
    # ``project_id`` (detach a chat from its current project) and ``model_id``
    # (reset the per-chat model override back to "Auto" — the flat model_id
    # param below only SETS a non-empty pin, so clearing must come through here).
    clear: str | None = Form(default=None),
    # Chat provider selection.  Sets ``chat.settings.provider`` so the
    # streaming layer routes to the chosen provider.  Must match a registered
    # provider slug (or "lmstudio").  An empty string is accepted as
    # "lmstudio" (clears the cloud-provider override).
    provider: str | None = Form(default=None),
    user: User = Depends(require_user),
    chat_service: ChatService = Depends(_get_chat_service),
) -> ChatResponse:
    """Update title, folder, pinned flag, settings, or incognito on a chat.

    All fields are optional — only the provided ones are applied.
    Cross-user access returns 404.

    Privacy invariant: the ``incognito`` flag is immutable once
    messages exist on the chat. PATCH with ``incognito`` after a message
    has been sent returns 422.  See
    ``ChatService.set_incognito`` for the rationale.

    Args:
        chat_id:          PK of the chat to update.
        request:          FastAPI Request.
        title:            New title (optional form field).
        folder:           New folder value (optional form field; empty string
                          is treated as a folder name, not as "remove folder").
        pinned:           New pinned flag (optional form field).
        rag_enabled:      Toggle RAG augmentation for this chat.
        reasoning_effort: Per-chat reasoning level
                          (``"off"``, ``"low"``, ``"medium"``, ``"high"``,
                          or ``""`` to clear the override).
        ab_compare:         JSON-encoded A/B compare settings dict
                            ``{"enabled": bool, "model_a"?: str, "model_b"?: str}``.
        ab_compare_enabled: Flat form: toggle A/B compare on/off.
        ab_compare_model_a: Flat form: model A identifier.
        ab_compare_model_b: Flat form: model B identifier.
        incognito:        Toggle incognito mode. Rejected with 422
                          when messages already exist on the chat (immutable
                          privacy invariant).
        system_prompt:    Per-chat system prompt override; ``""`` clears.
        temperature:      Sampler temperature (0-2).
        top_p:            Nucleus sampling threshold (0-1).
        top_k:            Top-K filter (>=1 when set).
        min_p:            Min-P filter (0-1).
        repeat_penalty:   Token repetition penalty (0-5).
        max_tokens:       Max output token cap.
        reasoning:        Alias for reasoning_effort surfaced in the
                          v0.5.x right-rail UI (kept distinct so the rail
                          can present it under its UI name; ``""`` clears).
        self_consistency_enabled: Opt-in to SC orchestration.
        chain_of_verification_enabled: Opt-in to CoVe orchestration.
        stateless:        When True, the LM Studio request sets
                          ``store=False`` upstream.
        active_preset:    Forward-compat: name of the currently-active
                          preset.
        user:             Authenticated user.
        chat_service:     Injected ``ChatService``.

    Returns:
        The updated :class:`ChatResponse` (200).

    Raises:
        HTTPException: 404 if chat not found or not owned by user.
        HTTPException: 422 if ``ab_compare`` is not valid JSON or fails schema.
        HTTPException: 422 if ``incognito`` is sent on a chat that already has
                       messages (privacy invariant).
        HTTPException: 422 if any settings field fails ChatSettings
                       validation (range / type errors).
    """
    import json as _json

    try:
        if title is not None:
            await chat_service.rename(chat_id, user_id=user.id, title=title)
        if folder is not None:
            await chat_service.move_to_folder(chat_id, user_id=user.id, folder=folder)
        if pinned is not None:
            await chat_service.pin(chat_id, user_id=user.id, pinned=pinned)
        if model_id is not None and model_id != "":
            await chat_service.set_model_id(chat_id, user_id=user.id, model_id=model_id)
        # Incognito toggle. Must apply BEFORE the settings merge so
        # the response Chat reflects the latest value, and so a single
        # request that toggles incognito + updates settings is atomic
        # from the caller's perspective.
        if incognito is not None:
            try:
                await chat_service.set_incognito(chat_id, user_id=user.id, incognito=incognito)
            except ValueError as exc:
                # Immutable-once-messages-exist invariant.
                raise HTTPException(status_code=_HTTP_422, detail=str(exc)) from exc
        # Settings update (merge into existing JSON blob).
        # Collect all settings keys changed in this request and merge once.
        settings_patch: dict = {}  # type: ignore[type-arg]
        if rag_enabled is not None:
            settings_patch["rag_enabled"] = rag_enabled
        if reasoning_effort is not None:
            # Empty string clears the per-chat override (falls through to
            # the global default in the frontend).
            settings_patch["reasoning_effort"] = reasoning_effort if reasoning_effort else None
        if ab_compare is not None:
            try:
                ab_compare_parsed = _json.loads(ab_compare)
            except (ValueError, TypeError) as exc:
                raise HTTPException(
                    status_code=_HTTP_422,
                    detail=f"ab_compare must be valid JSON: {exc}",
                ) from exc
            settings_patch["ab_compare"] = ab_compare_parsed
        # Flat params — merge on top of the JSON-blob path (if both
        # arrive, these win).  To preserve model_a / model_b when only
        # ab_compare_enabled is sent (e.g. auto-off after pane commit), we
        # read the chat's persisted ab_compare dict as the base.  If the
        # JSON-blob path already staged a value, start from that instead.
        if (
            ab_compare_enabled is not None
            or ab_compare_model_a is not None
            or ab_compare_model_b is not None
        ):
            # Use the JSON-blob base if it was already staged; otherwise
            # fetch the live chat row so we can preserve the existing values.
            if "ab_compare" in settings_patch and isinstance(settings_patch["ab_compare"], dict):
                existing_ab: dict = dict(settings_patch["ab_compare"])  # type: ignore[type-arg]
            else:
                existing_chat_row = await chat_service.get(chat_id, user_id=user.id)
                _raw_ab = (existing_chat_row.settings or {}).get("ab_compare")
                existing_ab = dict(_raw_ab) if isinstance(_raw_ab, dict) else {}
            if ab_compare_enabled is not None:
                existing_ab["enabled"] = ab_compare_enabled
            if ab_compare_model_a is not None:
                existing_ab["model_a"] = ab_compare_model_a
            if ab_compare_model_b is not None:
                existing_ab["model_b"] = ab_compare_model_b
            settings_patch["ab_compare"] = existing_ab
        # Per-chat rail fields. Empty string on text/select fields
        # explicitly clears the override (sends None); on numeric fields the
        # frontend coerces "" → omits the field, so we only see a real value
        # here. The explicit-null clear path lives in the frontend rail
        # (persistNumber now sends None on empty string).
        if system_prompt is not None:
            settings_patch["system_prompt"] = system_prompt if system_prompt else None
        if temperature is not None:
            settings_patch["temperature"] = temperature
        if top_p is not None:
            settings_patch["top_p"] = top_p
        if top_k is not None:
            settings_patch["top_k"] = top_k
        if min_p is not None:
            settings_patch["min_p"] = min_p
        if repeat_penalty is not None:
            settings_patch["repeat_penalty"] = repeat_penalty
        if max_tokens is not None:
            settings_patch["max_tokens"] = max_tokens
        if reasoning is not None:
            # Empty string clears the per-chat override (matches the
            # reasoning_effort field behaviour above; ChatSettings'
            # field_validator also coerces "" → None defensively).
            settings_patch["reasoning"] = reasoning if reasoning else None
        if self_consistency_enabled is not None:
            settings_patch["self_consistency_enabled"] = self_consistency_enabled
        if chain_of_verification_enabled is not None:
            settings_patch["chain_of_verification_enabled"] = chain_of_verification_enabled
        if stateless is not None:
            settings_patch["stateless"] = stateless
        if active_preset is not None:
            settings_patch["active_preset"] = active_preset if active_preset else None
        # W2-BE: provider selection.  FastAPI coerces an empty form field to
        # None (same as omitting the field), so we only reach here when a
        # non-None slug was submitted.  An explicit empty string would be
        # treated as "lmstudio" for safety, but in practice it arrives as None.
        if provider is not None:
            effective_provider = provider if provider else "lmstudio"
            # Validate against the registry so an unknown slug is rejected now
            # rather than silently accepted and failing at stream time.
            provider_registry = getattr(request.app.state, "provider_registry", None)
            if provider_registry is not None:
                if provider_registry.get(effective_provider) is None:
                    raise HTTPException(
                        status_code=_HTTP_400,
                        detail=(
                            f"Unknown provider {effective_provider!r}. "
                            "Configure it via the admin providers API first."
                        ),
                    )
            settings_patch["provider"] = effective_provider
        if settings_patch:
            try:
                chat: Chat = await chat_service.update_settings(
                    chat_id, user_id=user.id, settings=settings_patch
                )
            except ValueError as exc:
                raise HTTPException(status_code=_HTTP_422, detail=str(exc)) from exc
        else:
            chat = await chat_service.get(chat_id, user_id=user.id)

        # project_id move/detach. Ownership of
        # the target project enforced via ProjectsService.get(); 404
        # on unknown / cross-user project_id rather than silent FK
        # rejection. Detach via ``clear=project_id``. The shared
        # ``parse_clear`` helper centralizes the comma-list parsing
        # so this route's accept-set never drifts from the projects
        # / documents routes' parsers (shared parser — one parser, three routes).
        from lmchat.routes._form_utils import parse_clear  # noqa: PLC0415

        clear_set = parse_clear(clear, allowed=frozenset({"project_id", "model_id"}))
        if "model_id" in clear_set:
            # Reset the per-chat model override back to "Auto" (NULL model_id).
            # The flat ``model_id`` form param only SETS a non-empty pin
            # (guarded above), so an explicit reset must arrive via clear=.
            await chat_service.clear_model_id(chat_id, user_id=user.id)
            chat = await chat_service.get(chat_id, user_id=user.id)
        if "project_id" in clear_set:
            # Pass ``projects_service`` so the service can capture the
            # detach snapshot (project name + system_prompt_hash) in
            # the same transaction as the project_id clear.
            projects_svc = getattr(request.app.state, "projects_service", None)
            await chat_service.set_project_id(
                chat_id,
                user_id=user.id,
                project_id=None,
                projects_service=projects_svc,
            )
            chat = await chat_service.get(chat_id, user_id=user.id)
        elif project_id is not None:
            projects_svc = getattr(request.app.state, "projects_service", None)
            if projects_svc is None:
                raise RuntimeError("app.state.projects_service is unset")
            owned = await projects_svc.get(user_id=user.id, project_id=project_id)
            if owned is None:
                raise HTTPException(
                    status_code=_HTTP_404,
                    detail="project not found",
                )
            # Attach path: no detach snapshot to capture; passing
            # ``projects_service`` is harmless (service skips the
            # snapshot logic when ``project_id`` is non-None).
            await chat_service.set_project_id(
                chat_id,
                user_id=user.id,
                project_id=project_id,
                projects_service=projects_svc,
            )
            chat = await chat_service.get(chat_id, user_id=user.id)
    except ChatNotFoundError as exc:
        raise HTTPException(status_code=_HTTP_404, detail="chat not found") from exc

    return ChatResponse.model_validate(chat.model_dump())


# ---------------------------------------------------------------------------
# DELETE /api/chats/{chat_id}
# ---------------------------------------------------------------------------


@router.get("/{chat_id}/rag_mode")
async def get_chat_rag_mode(
    chat_id: int,
    request: Request,
    user: User = Depends(require_user),
    chat_service: ChatService = Depends(_get_chat_service),
) -> dict[str, Any]:
    """Return the RAG-mode the resolver would pick for *chat_id*.

    Surfaces the
    ``rag_mode_resolver.resolve_rag_mode`` decision so the frontend
    can render the RAG-mode badge (INLINE / HYBRID / FOCUSED) +
    show the admin the active project corpus size + threshold.
    Read-only; does NOT mutate any chat state.

    Args:
        chat_id:      PK of the chat (must be owned by *user*).
        request:      FastAPI Request (for app.state).
        user:         Authenticated user.
        chat_service: Injected ``ChatService``.

    Returns:
        JSON body::

            {
              "mode": "inline" | "hybrid" | "focused",
              "project_corpus_tokens": int | null,
              "threshold_tokens": int | null,
              "focused_document_id": int | null,
              "embedding_status": "ok" | "pinned_model_unavailable"
                                  | "no_embedding_model",
              "embedding_model_pinned": str | null,
              "embedding_model_active": str | null
            }

        The three ``embedding_*`` fields surface the resolver's status
        sentinel so the badge UI can warn when retrieval will silently
        skip (project pinned a model that isn't loaded, or no
        embedding model is loaded at all).

    Raises:
        HTTPException: 404 if chat is missing or not owned by *user*.
    """
    # Lazy imports — avoid circular deps with services package.
    from lmchat.services.documents_service import (  # noqa: PLC0415
        _estimate_project_corpus_tokens,
    )
    from lmchat.services.rag_mode_resolver import (  # noqa: PLC0415
        resolve_rag_mode,
    )
    from lmchat.services.rag_service import (  # noqa: PLC0415
        _resolve_chat_ctx_window,
    )
    from lmchat.services.retrieval_service import (  # noqa: PLC0415
        EMBED_STATUS_OK,
        resolve_embedding_model_status,
    )

    try:
        chat = await chat_service.get(chat_id, user_id=user.id)
    except ChatNotFoundError as exc:
        raise HTTPException(status_code=_HTTP_404, detail="chat not found") from exc

    project_id = getattr(chat, "project_id", None)
    chat_settings = chat.settings if getattr(chat, "settings", None) is not None else {}

    # Embedding-model status sentinel — surface "pinned model not loaded"
    # instead of silently degrading.
    embedding_status: str = EMBED_STATUS_OK
    embedding_model_pinned: str | None = None
    embedding_model_active: str | None = None

    # ctx_window resolution: resolve the active model's REAL context window
    # (0 = unknown), reusing augment_prompt's _resolve_chat_ctx_window so this
    # diagnostic reports the same RAG mode retrieval will actually pick. The
    # old hardcoded 131K made the badge disagree with runtime once INLINE
    # became real.
    models_service = getattr(request.app.state, "models_service", None)
    ctx_window = 0
    if models_service is not None:
        ctx_window = await _resolve_chat_ctx_window(
            chat_model_id=getattr(chat, "model_id", None),
            models_service=models_service,
        )

    # Per-project override + corpus size only when scoped.
    override: int | None = None
    corpus_tokens: int | None = None
    if project_id is not None:
        # Reuse ProjectsService.get for ownership + read of
        # rag_threshold; covers the cross-user 404 implicitly.
        projects_svc = getattr(request.app.state, "projects_service", None)
        if projects_svc is not None:
            project = await projects_svc.get(user_id=user.id, project_id=project_id)
            if project is not None:
                override = getattr(project, "rag_threshold", None)
        # Only estimate the corpus when ctx_window is known (> 0): the INLINE
        # fit test is meaningless at ctx_window <= 0, and augment_prompt gates
        # the estimate the same way — so an unknown-model chat resolves to
        # HYBRID identically here and at runtime.
        engine = getattr(request.app.state, "engine", None) or getattr(
            chat_service, "_engine", None
        )
        if engine is not None and ctx_window > 0:
            try:
                corpus_tokens = await _estimate_project_corpus_tokens(
                    engine=engine,
                    user_id=user.id,
                    project_id=project_id,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "chats.rag_mode.corpus_estimate_failed",
                    chat_id=chat_id,
                    error=str(exc),
                )

    decision = resolve_rag_mode(
        project_id=project_id,
        chat_settings=chat_settings if isinstance(chat_settings, dict) else None,
        ctx_window=ctx_window,
        project_corpus_tokens=corpus_tokens,
        project_rag_threshold_override=override,
    )

    # Resolve the embedding-status sentinel so the UI can warn about
    # pinned-model unavailability.
    engine = getattr(request.app.state, "engine", None) or getattr(chat_service, "_engine", None)
    models_service = getattr(request.app.state, "models_service", None)
    if engine is not None and models_service is not None:
        try:
            resolved_active, embedding_status = await resolve_embedding_model_status(
                project_id=project_id,
                user_id=user.id,
                engine=engine,
                models_service=models_service,
            )
            embedding_model_active = resolved_active
            # When pinned-unavailable, surface the pinned id too so
            # the UI can name it in its message.
            if project_id is not None and embedding_status != EMBED_STATUS_OK:
                # Re-read the pin (cheap; already in connection cache).
                from sqlalchemy import select as _select  # noqa: PLC0415

                from lmchat.db.schema import projects as _projects  # noqa: PLC0415

                async with engine.connect() as conn:
                    row = (
                        await conn.execute(
                            _select(_projects.c.embedding_model_id).where(
                                _projects.c.id == project_id
                            )
                        )
                    ).fetchone()
                if row is not None:
                    raw = row.embedding_model_id
                    if raw:
                        embedding_model_pinned = str(raw)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "chats.rag_mode.embedding_status_failed",
                chat_id=chat_id,
                error=str(exc),
            )

    return {
        "mode": decision.mode.value,
        "project_corpus_tokens": decision.project_corpus_tokens,
        "threshold_tokens": decision.threshold_tokens,
        "focused_document_id": decision.focused_document_id,
        "embedding_status": embedding_status,
        "embedding_model_pinned": embedding_model_pinned,
        "embedding_model_active": embedding_model_active,
    }


@router.delete("/{chat_id}", status_code=_HTTP_204)
async def delete_chat(
    chat_id: int,
    request: Request,
    user: User = Depends(require_user),
    chat_service: ChatService = Depends(_get_chat_service),
    streaming_service: StreamingService = Depends(get_streaming_service_dep),
) -> None:
    """Delete a chat and all its messages (cascade).

    Cross-user access returns 404.

    Args:
        chat_id:             PK of the chat to delete.
        request:             FastAPI Request.
        user:                Authenticated user.
        chat_service:        Injected ``ChatService``.
        streaming_service:   Injected ``StreamingService`` (for counter reset).

    Returns:
        204 No Content on success.

    Raises:
        HTTPException: 404 if chat not found or not owned by user.
    """
    try:
        await chat_service.delete(chat_id, user_id=user.id)
        streaming_service.reset_counter(chat_id)
    except ChatNotFoundError as exc:
        raise HTTPException(status_code=_HTTP_404, detail="chat not found") from exc


# ---------------------------------------------------------------------------
# DELETE /api/chats/{chat_id}/messages — clear history (/clear)
# ---------------------------------------------------------------------------


class ClearMessagesResponse(BaseModel):
    """Result of clearing a chat's message history (/clear)."""

    cleared: int


@router.delete("/{chat_id}/messages", response_model=ClearMessagesResponse)
async def clear_chat_messages(
    chat_id: int,
    request: Request,
    user: User = Depends(require_user),
    chat_service: ChatService = Depends(_get_chat_service),
    streaming_service: StreamingService = Depends(get_streaming_service_dep),
) -> ClearMessagesResponse:
    """Clear a chat's message history, keeping the chat shell (/clear).

    Deletes every message in the chat (FK cascade drops embeddings)
    but preserves the chat row — title, folder, per-chat settings, and project
    link survive. The next turn starts a fresh LM Studio conversation because
    the ``response_id`` chain lived on the deleted message rows.

    Cross-user access returns 404 (does not leak resource existence).

    Args:
        chat_id:           PK of the chat to clear.
        request:           FastAPI Request.
        user:              Authenticated user.
        chat_service:      Injected ``ChatService``.
        streaming_service: Injected ``StreamingService`` (for counter reset).

    Returns:
        200 with the number of messages removed.

    Raises:
        HTTPException: 404 if the chat does not exist or is not owned by user.
    """
    try:
        cleared = await chat_service.clear_messages(chat_id, user_id=user.id)
        streaming_service.reset_counter(chat_id)
    except ChatNotFoundError as exc:
        raise HTTPException(status_code=_HTTP_404, detail="chat not found") from exc
    return ClearMessagesResponse(cleared=cleared)


# ---------------------------------------------------------------------------
# POST /api/chats/{chat_id}/fork
# ---------------------------------------------------------------------------


@router.post("/{chat_id}/fork", response_model=ChatResponse, status_code=_HTTP_201)
async def fork_chat(
    chat_id: int,
    request: Request,
    at_message_id: int = Form(...),
    user: User = Depends(require_user),
    chat_service: ChatService = Depends(_get_chat_service),
) -> ChatResponse:
    """Create a new chat that is a snapshot of ``chat_id`` up to ``at_message_id``.

    Messages are copied with their original timestamps.  The fork is owned
    by the requesting user (same owner as the source).

    Args:
        chat_id:        PK of the source chat.
        request:        FastAPI Request.
        at_message_id:  Copy messages with id ≤ this value (form field).
        user:           Authenticated user.
        chat_service:   Injected ``ChatService``.

    Returns:
        The new :class:`ChatResponse` (201).

    Raises:
        HTTPException: 404 if source chat not found or not owned by user.
    """
    log.info(
        "chats.fork.request",
        chat_id=chat_id,
        user_id=user.id,
        at_message_id=at_message_id,
    )
    try:
        forked: Chat = await chat_service.fork(
            chat_id, user_id=user.id, at_message_id=at_message_id
        )
    except ChatNotFoundError as exc:
        raise HTTPException(status_code=_HTTP_404, detail="chat not found") from exc

    return ChatResponse.model_validate(forked.model_dump())


# ---------------------------------------------------------------------------
# POST /api/chats/{chat_id}/promote-to-project
# ---------------------------------------------------------------------------


def _parse_document_ids(raw: str | None) -> list[int]:
    """Parse the ``document_ids`` comma-list form field.

    Accepted shapes:
    - omitted / empty string → ``[]`` (no documents to move)
    - ``"1, 2, 3"`` → ``[1, 2, 3]`` (order preserved, duplicates dropped)

    Raises:
        HTTPException: 422 on a non-integer token.
    """
    if raw is None or raw.strip() == "":
        return []
    ids: list[int] = []
    for part in raw.split(","):
        token = part.strip()
        if token == "":
            continue
        try:
            ids.append(int(token))
        except ValueError as exc:
            raise HTTPException(
                status_code=_HTTP_422,
                detail=(
                    "document_ids must be a comma-separated list of "
                    f"integers (bad token {token!r})"
                ),
            ) from exc
    # De-dup while preserving first-seen order.
    return list(dict.fromkeys(ids))


class PromoteToProjectResponse(ProjectResponse):
    """``POST /api/projects`` shape plus the count of documents carried over."""

    moved_document_count: int


@router.post(
    "/{chat_id}/promote-to-project",
    response_model=PromoteToProjectResponse,
    status_code=_HTTP_201,
)
async def promote_chat_to_project(
    chat_id: int,
    request: Request,
    name: str | None = Form(default=None),
    system_prompt: str = Form(default=""),
    document_ids: str | None = Form(default=None),
    user: User = Depends(require_user),
    chat_service: ChatService = Depends(_get_chat_service),
) -> PromoteToProjectResponse:
    """Turn ``chat_id`` into a new project, carrying selected documents.

    The chat's message history, compactions, and embeddings are all
    ``chat_id``-scoped and travel for free the moment ``chats.project_id``
    is set — this route's real work is (1) creating the project,
    (2) the FK flip on the chat, and (3) an FK flip per selected
    document (``set_document_project_id`` — no re-embed).

    Guards (checked BEFORE any write, so a rejected request never
    creates a half-migrated chat):

    * 404 — the chat is missing or not owned by the caller.
    * 409 — the chat already belongs to a project.
    * 422 — the chat is incognito (incognito chats have no durable
      memory writes and shouldn't seed a project).
    * 404 — any ``document_ids`` entry is missing / not owned by the
      caller.
    * 409 — any ``document_ids`` entry already belongs to a project
      (never silently steal it from where it is).
    * 409 — the selected documents span more than one embedding
      model (the new project can only pin to one vector space).

    Sub-sessions: there is no backend marker for "this chat currently
    has a sub-session open" — that's ephemeral frontend state
    (``subSessionStore``), not a persisted chat column, so this route
    has nothing to gate on. The frontend hides the promote action
    while a sub-session is open (the same guard the sidebar's
    move-to-project affordance uses).

    Embedding-model pin: when documents are supplied and they share
    ONE non-empty ``embedding_model_id``, the new project is pinned to
    THAT model — not whatever happens to be loaded in LM Studio right
    now — so retrieval stays in the documents' existing vector space.
    Reuses ``_enforce_embedding_pin_or_pin``'s compare-and-swap UPDATE
    via ``active_override``; safe here because the project is brand
    new (nothing else could have raced its pin yet). Documents that
    haven't finished embedding (``embedding_model_id == ""``) are
    excluded from the span check — they don't constrain a vector
    space yet.

    Ordering / fail-closed note: every guard above runs before the
    project is created, so a rejected request leaves nothing to clean
    up. Once the project exists, the chat move and the document moves
    reuse the SAME validated (ownership-checked, un-projected) rows
    gathered during the guard phase, so a failure between them would
    mean a concurrent mutation raced this request — the same
    documented TOCTOU disposition as
    ``routes/projects.py::_require_owned_project``.

    Args:
        chat_id:       PK of the chat to promote.
        request:       FastAPI Request (app.state access).
        name:          New project name; defaults to the chat's title.
        system_prompt: New project's custom instructions; default "".
        document_ids:  Comma-separated document PKs to move into the
                       new project. Omitted/empty moves none.
        user:          Authenticated user.
        chat_service:  Injected ``ChatService``.

    Returns:
        The created project (``POST /api/projects`` shape) plus
        ``moved_document_count``.
    """
    # Lazy imports — avoid circular deps with the services package (same
    # convention as get_chat_rag_mode's documents_service import above).
    from lmchat.services.documents_service import (  # noqa: PLC0415
        DocumentNotFoundError,
        _enforce_embedding_pin_or_pin,
        get_document,
        set_document_project_id,
    )
    from lmchat.services.projects_service import (  # noqa: PLC0415
        InvalidProjectFieldError,
    )

    try:
        chat = await chat_service.get(chat_id, user_id=user.id)
    except ChatNotFoundError as exc:
        raise HTTPException(status_code=_HTTP_404, detail="chat not found") from exc

    if chat.project_id is not None:
        raise HTTPException(
            status_code=_HTTP_409, detail="chat is already in a project"
        )
    if chat.incognito:
        raise HTTPException(
            status_code=_HTTP_422,
            detail="incognito chats cannot be promoted to a project",
        )

    doc_ids = _parse_document_ids(document_ids)

    engine = get_engine_dep(request)
    models_service = get_models_service_dep(request)
    projects_svc = getattr(request.app.state, "projects_service", None)
    if projects_svc is None:
        raise RuntimeError("app.state.projects_service is unset")

    # Validate every document BEFORE any write — ownership, existence,
    # and un-projected state all checked up front so the mutation phase
    # below only touches rows already proven safe to move.
    docs = []
    for doc_id in doc_ids:
        doc = await get_document(document_id=doc_id, user_id=user.id, engine=engine)
        if doc is None:
            raise HTTPException(
                status_code=_HTTP_404, detail=f"document {doc_id} not found"
            )
        if doc.project_id is not None:
            raise HTTPException(
                status_code=_HTTP_409,
                detail=f"document {doc_id} already belongs to a project",
            )
        docs.append(doc)

    # Span-check only the docs that HAVE an embedding model — one still
    # chunking (embedding_model_id == "") doesn't constrain a vector
    # space yet.
    embedded_models = {d.embedding_model_id for d in docs if d.embedding_model_id}
    if len(embedded_models) > 1:
        raise HTTPException(
            status_code=_HTTP_409,
            detail=(
                "selected documents use different embedding models; "
                "bring documents that share one model, or re-embed first"
            ),
        )
    shared_embedding_model_id = next(iter(embedded_models), None)

    log.info(
        "chats.promote_to_project.request",
        chat_id=chat_id,
        user_id=user.id,
        document_count=len(docs),
    )

    try:
        project = await projects_svc.create(
            user_id=user.id,
            name=name if name not in (None, "") else chat.title,
            system_prompt=system_prompt,
        )
    except InvalidProjectFieldError as exc:
        raise HTTPException(status_code=_HTTP_422, detail=str(exc)) from exc

    if shared_embedding_model_id is not None:
        # Pin the brand-new project to the documents' existing vector
        # space instead of letting the CAS default to whatever's
        # currently loaded in LM Studio.
        await _enforce_embedding_pin_or_pin(
            project_id=project.id,
            user_id=user.id,
            engine=engine,
            models_service=models_service,
            active_override=shared_embedding_model_id,
        )

    await chat_service.set_project_id(
        chat_id,
        user_id=user.id,
        project_id=project.id,
        projects_service=projects_svc,
    )

    moved = 0
    for doc in docs:
        try:
            await set_document_project_id(
                document_id=doc.id,
                user_id=user.id,
                project_id=project.id,
                engine=engine,
                # models_service intentionally omitted (defaults to None):
                # the pin was already set above from the documents' OWN
                # embedding model, so the write-once CAS against
                # whatever's currently loaded must not run again here.
            )
        except DocumentNotFoundError as exc:
            raise HTTPException(
                status_code=_HTTP_404,
                detail=f"document {doc.id} not found",
            ) from exc
        moved += 1

    fresh_project = await projects_svc.get(user_id=user.id, project_id=project.id)
    if fresh_project is None:
        raise RuntimeError(
            f"project {project.id!r} vanished immediately after creation"
        )
    return PromoteToProjectResponse(
        **ProjectResponse.from_project(fresh_project).model_dump(),
        moved_document_count=moved,
    )


# ---------------------------------------------------------------------------
# POST /api/chats/{chat_id}/compact
# ---------------------------------------------------------------------------


@router.post("/{chat_id}/compact", response_model=CompactResultResponse)
async def compact_chat(
    chat_id: int,
    request: Request,
    target_tokens: int = Form(...),
    user: User = Depends(require_user),
    chat_service: ChatService = Depends(_get_chat_service),
) -> CompactResultResponse:
    """Summarize + archive a chat's oldest message span (hybrid compaction).

    Preserves system prompt, latest user message, and tool-call pairs as
    active messages. The archived span is summarized by the LM Studio
    model that produced the conversation and archived (``compaction_id``
    set) rather than deleted — the rows and any ``message_embeddings``
    stay in the DB for recall. Returns 422 if ``target_tokens`` is below
    the invariant-minimum floor, and 502 if the summary call fails (nothing
    is archived in that case — fail policy is ABORT, never a partial
    archive without a summary).

    Args:
        chat_id:       PK of the chat to compact.
        request:       FastAPI Request (used to reach ``app.state.http_client``
                      and ``app.state.lmstudio_adapter``).
        target_tokens: Upper bound on the remaining token count (form field).
        user:          Authenticated user.
        chat_service:  Injected ``ChatService``.

    Returns:
        :class:`CompactResultResponse` (200).

    Raises:
        HTTPException: 404 if chat not found or not owned by user.
        HTTPException: 422 if ``target_tokens`` is below the invariant minimum.
        HTTPException: 502 if the upstream summary call fails or times out.
    """
    log.info(
        "chats.compact.request",
        chat_id=chat_id,
        user_id=user.id,
        target_tokens=target_tokens,
    )

    # Pull the shared httpx client + LIVE rewired adapter URL from app.state
    # — same pattern as generate_chat_title (see that route's docstring for
    # why this must NOT be http_client.base_url).
    http_client = request.app.state.http_client
    base_url: str = request.app.state.lmstudio_adapter._base_url

    try:
        result: CompactResult = await chat_service.compact(
            chat_id,
            user_id=user.id,
            target_tokens=target_tokens,
            http_client=http_client,
            base_url=base_url,
        )
    except ChatNotFoundError as exc:
        raise HTTPException(status_code=_HTTP_404, detail="chat not found") from exc
    except CompactTooLowError as exc:
        raise HTTPException(status_code=_HTTP_422, detail=str(exc)) from exc
    except CompactionSummaryError as exc:
        raise HTTPException(status_code=_HTTP_502, detail=str(exc)) from exc

    return CompactResultResponse.model_validate(result.model_dump())


# ---------------------------------------------------------------------------
# GET /api/chats/{chat_id}/compactions — recall
# ---------------------------------------------------------------------------


@router.get(
    "/{chat_id}/compactions",
    response_model=list[CompactionResponse],
)
async def list_chat_compactions(
    chat_id: int,
    user: User = Depends(require_user),
    chat_service: ChatService = Depends(_get_chat_service),
) -> list[CompactionResponse]:
    """List every compaction span for ``chat_id``, oldest first.

    Read-only; owner-authed. Backs the FE's collapsed compaction tabs.

    Args:
        chat_id:      PK of the chat.
        user:         Authenticated user.
        chat_service: Injected ``ChatService``.

    Returns:
        List of :class:`CompactionResponse` (200), ordered by
        ``anchor_msg_id`` ascending.

    Raises:
        HTTPException: 404 if chat not found or not owned by user.
    """
    try:
        spans: list[Compaction] = await chat_service.list_compactions(
            chat_id, user_id=user.id
        )
    except ChatNotFoundError as exc:
        raise HTTPException(status_code=_HTTP_404, detail="chat not found") from exc

    return [CompactionResponse.model_validate(s.model_dump()) for s in spans]


@router.get(
    "/{chat_id}/compactions/{compaction_id}/messages",
    response_model=list[Message],
)
async def list_compaction_messages(
    chat_id: int,
    compaction_id: int,
    user: User = Depends(require_user),
    chat_service: ChatService = Depends(_get_chat_service),
) -> list[Message]:
    """Return the archived message set for one compaction span, id-ordered.

    Read-only; owner-authed. Backs the FE's expand-to-recall interaction.

    Args:
        chat_id:       PK of the chat.
        compaction_id: PK of the ``compactions`` row.
        user:          Authenticated user.
        chat_service:  Injected ``ChatService``.

    Returns:
        List of :class:`~lmchat.services.message_service.Message` (200),
        ordered by id ascending.

    Raises:
        HTTPException: 404 if the chat or the compaction is not found (or
                       not owned by user) — existence never leaks.
    """
    try:
        return await chat_service.get_compaction_messages(
            chat_id, compaction_id, user_id=user.id
        )
    except ChatNotFoundError as exc:
        raise HTTPException(status_code=_HTTP_404, detail="chat not found") from exc


# ---------------------------------------------------------------------------
# POST /api/chats/{chat_id}/generate-title — auto-generate a chat title
# ---------------------------------------------------------------------------


@router.post("/{chat_id}/generate-title", response_model=GenerateTitleResponse)
async def generate_chat_title(
    chat_id: int,
    request: Request,
    user: User = Depends(require_user),
    chat_service: ChatService = Depends(_get_chat_service),
) -> GenerateTitleResponse:
    """Auto-generate a concise title for ``chat_id`` from its first turns.

    The frontend calls this after the second assistant
    message lands and only when the current title is still default
    (``"New Chat"`` / ``"Incognito Chat"`` / ``""``).  The endpoint is
    idempotent: if the chat already has a user-set title, the existing
    title is returned without a model call.

    Pulls the chat's earliest user/assistant messages, sends them to LM
    Studio's ``/v1/chat/completions`` with a short titling system prompt,
    and persists the sanitised result to ``chats.title``.

    Args:
        chat_id:      PK of the chat to title.
        request:      FastAPI Request (used to reach ``app.state.http_client``
                      and the live adapter base URL).
        user:         Authenticated user.
        chat_service: Injected :class:`ChatService`.

    Returns:
        :class:`GenerateTitleResponse` with the final ``title`` (200).

    Raises:
        HTTPException: 404 if chat not found or not owned by user.
        HTTPException: 502 if upstream LM Studio fails or returns an
                       unusable response.  Frontend swallows this silently
                       and leaves the chat at its current title (best-effort
                       UX nicety).
    """
    log.info(
        "chats.generate_title.request",
        chat_id=chat_id,
        user_id=user.id,
    )

    # Pull the shared httpx client + base URL from app.state.  These are
    # attached by the lifespan so they are always present in normal
    # operation; missing attributes are programmer errors and surface as
    # 500s via the route's standard exception path.
    http_client = request.app.state.http_client
    # Read the LIVE rewired adapter URL, not the boot-frozen env URL.
    # This URL changes whenever an admin saves a new base_url via the Settings
    # UI (rewire_singletons mutation — httpx ignores
    # client.base_url when an absolute URL is passed to post(), so reading
    # http_client.base_url here would silently re-introduce that bug).
    base_url: str = request.app.state.lmstudio_adapter._base_url

    # Resolve a fallback model id from the cached loaded-model list.  This
    # is used only if the chat has no prior assistant message with a
    # model_id (e.g. a fresh chat where the assistant turn just landed
    # without persisting its model_id, or a legacy row).  We pick the
    # first loaded model from ModelsService.list_loaded() — same heuristic
    # the Composer uses on the frontend.
    fallback_model_id: str | None = None
    try:
        loaded = await request.app.state.models_service.list_loaded()
        if loaded:
            fallback_model_id = str(loaded[0].key)
    except Exception as exc:  # noqa: BLE001
        # Don't fail the call if the cache lookup blows up — generate_title
        # will simply raise its own TitleGenerationError if no model_id is
        # found anywhere.
        log.warning(
            "chats.generate_title.fallback_model_lookup_failed",
            chat_id=chat_id,
            user_id=user.id,
            error=str(exc),
        )

    try:
        title = await chat_service.generate_title(
            chat_id,
            user_id=user.id,
            http_client=http_client,
            base_url=base_url,
            fallback_model_id=fallback_model_id,
        )
    except ChatNotFoundError as exc:
        raise HTTPException(status_code=_HTTP_404, detail="chat not found") from exc
    except TitleGenerationError as exc:
        raise HTTPException(status_code=_HTTP_502, detail=str(exc)) from exc

    return GenerateTitleResponse(title=title)


# ---------------------------------------------------------------------------
# POST /api/chats/{chat_id}/messages — append a message
# ---------------------------------------------------------------------------


@router.post("/{chat_id}/messages", response_model=Message, status_code=_HTTP_201)
async def append_message(
    chat_id: int,
    request: Request,
    role: str = Form(...),
    content: str = Form(...),
    reasoning_content: str | None = Form(default=None),
    model_id: str | None = Form(default=None),
    user: User = Depends(require_user),
    message_service: MessageService = Depends(_get_message_service),
) -> Message:
    """Append a message to a chat.

    The message is written with ``state='final'``.  The streaming service
    uses ``state='draft'`` internally and is not routed through this endpoint.

    Cross-user access to the target chat returns 404.

    Args:
        chat_id:           PK of the target chat.
        request:           FastAPI Request.
        role:              Message role (``user`` | ``assistant`` | ``system`` |
                           ``tool``).
        content:           Message text body (form field).
        reasoning_content: Optional chain-of-thought text (form field).
        model_id:          Producing model key (form field; nullable).
        user:              Authenticated user.
        message_service:   Injected ``MessageService``.

    Returns:
        The inserted :class:`~lmchat.services.message_service.Message` (201).

    Raises:
        HTTPException: 404 if chat not found or not owned by user.
        HTTPException: 422 if the role is invalid.
    """
    log.info(
        "chats.append_message.request",
        chat_id=chat_id,
        user_id=user.id,
        role=role,
    )
    try:
        msg: Message = await message_service.append(
            chat_id=chat_id,
            user_id=user.id,
            role=role,
            content=content,
            reasoning_content=reasoning_content,
            model_id=model_id,
        )
    except MessageNotFoundError as exc:
        raise HTTPException(status_code=_HTTP_404, detail="chat not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=_HTTP_422, detail=str(exc)) from exc

    return msg


# ---------------------------------------------------------------------------
# Chat share endpoints
# ---------------------------------------------------------------------------


class ChatShareResponse(BaseModel):
    """JSON projection of one ``chat_shares`` row plus the public URL.

    Attributes:
        token:      URL-safe public token (``secrets.token_urlsafe(24)``).
        url:        Relative SPA path to the public read-only view.  The
                    client renders this with ``window.location.origin`` to
                    produce the absolute URL.
        chat_id:    FK back to the chat being shared.
        created_at: Row creation timestamp.
    """

    model_config = ConfigDict(from_attributes=True)

    token: str
    url: str
    chat_id: int
    created_at: datetime


def _share_to_response(share: ChatShare) -> ChatShareResponse:
    """Build a :class:`ChatShareResponse` from a :class:`ChatShare`."""
    return ChatShareResponse(
        token=share.token,
        url=f"/share/{share.token}",
        chat_id=share.chat_id,
        created_at=share.created_at,
    )


@router.post(
    "/{chat_id}/share",
    response_model=ChatShareResponse,
    status_code=_HTTP_201,
)
async def share_chat(
    chat_id: int,
    request: Request,
    user: User = Depends(require_user),
    chat_service: ChatService = Depends(_get_chat_service),
) -> ChatShareResponse:
    """Create (or return the existing) public share token for a chat.

    Idempotent.  Re-POSTing returns the existing token without churning
    the URL — admin-friendly when a user double-clicks "Share".

    Privacy invariant: incognito chats cannot be shared.
    The endpoint returns 403 in that case; the chat is intentionally
    treated as "exists but private" (not 404) because the caller is the
    chat owner and there's no existence-leak benefit to hiding it.

    Args:
        chat_id:      PK of the chat to share.
        request:      FastAPI Request.
        user:         Authenticated user.
        chat_service: Injected ``ChatService``.

    Returns:
        The :class:`ChatShareResponse` (201).

    Raises:
        HTTPException: 404 if the chat is missing or owned by another user.
        HTTPException: 403 if the chat is incognito.
    """
    log.info(
        "chats.share.request",
        chat_id=chat_id,
        user_id=user.id,
    )
    try:
        share = await chat_service.create_share(chat_id, user_id=user.id)
    except ChatNotFoundError as exc:
        raise HTTPException(status_code=_HTTP_404, detail="chat not found") from exc
    except ChatNotShareableError as exc:
        raise HTTPException(
            status_code=_HTTP_403,
            detail="incognito chats cannot be shared",
        ) from exc
    return _share_to_response(share)


@router.get(
    "/{chat_id}/share",
    response_model=ChatShareResponse | None,
)
async def get_chat_share(
    chat_id: int,
    request: Request,
    user: User = Depends(require_user),
    chat_service: ChatService = Depends(_get_chat_service),
) -> ChatShareResponse | None:
    """Return the active share for *chat_id*, or null when not shared.

    The chat detail view calls this on mount to know whether to render
    the "Share" button as "Create link" vs "Copy existing link / Stop
    sharing".

    Args:
        chat_id:      PK of the chat.
        request:      FastAPI Request.
        user:         Authenticated user.
        chat_service: Injected ``ChatService``.

    Returns:
        The :class:`ChatShareResponse`, or ``None`` when no share exists.

    Raises:
        HTTPException: 404 if the chat is missing or owned by another user.
    """
    try:
        share = await chat_service.get_active_share(chat_id, user_id=user.id)
    except ChatNotFoundError as exc:
        raise HTTPException(status_code=_HTTP_404, detail="chat not found") from exc
    if share is None:
        return None
    return _share_to_response(share)


@router.delete("/{chat_id}/share", status_code=_HTTP_204)
async def delete_chat_share(
    chat_id: int,
    request: Request,
    user: User = Depends(require_user),
    chat_service: ChatService = Depends(_get_chat_service),
) -> None:
    """Revoke any active share for *chat_id*.

    Idempotent: returns 204 whether or not a row existed (the caller
    just wants the chat to be un-shared; that's true whether the row
    was already absent or just got deleted).

    Args:
        chat_id:      PK of the chat.
        request:      FastAPI Request.
        user:         Authenticated user.
        chat_service: Injected ``ChatService``.

    Returns:
        204 No Content on success.

    Raises:
        HTTPException: 404 if the chat is missing or owned by another user.
    """
    try:
        deleted = await chat_service.delete_share(chat_id, user_id=user.id)
    except ChatNotFoundError as exc:
        raise HTTPException(status_code=_HTTP_404, detail="chat not found") from exc
    log.info(
        "chats.share.delete",
        chat_id=chat_id,
        user_id=user.id,
        deleted=deleted,
    )


# ---------------------------------------------------------------------------
# Regenerate assistant message
# ---------------------------------------------------------------------------


class RegenerateConfirmDetail(BaseModel):
    """Body returned with HTTP 412 when ``?confirm=true`` is absent.

    Surfaces the destructive-action confirmation context to the UI so the
    admin can decide whether to re-issue with ``confirm=true``.

    Attributes:
        code:              Stable client-side discriminator.
        subsequent_count:  Number of messages that will be removed,
                           including the target assistant turn.
        chat_id:           Parent chat PK.
        message_id:        Target assistant message PK.
    """

    model_config = ConfigDict(extra="forbid")

    code: str = "confirm_required"
    subsequent_count: int
    chat_id: int
    message_id: int


@router.post(
    "/{chat_id}/messages/{message_id}/regenerate",
    response_model=None,
    status_code=200,
)
async def regenerate_message(
    chat_id: int,
    message_id: int,
    request: Request,
    confirm: bool = False,
    user: User = Depends(require_user),
    chat_service: ChatService = Depends(_get_chat_service),
    message_service: MessageService = Depends(_get_message_service),
) -> dict:  # type: ignore[type-arg]
    """Regenerate the assistant turn at *message_id* **or** resend the
    user prompt at *message_id*.

    Accepts both assistant-role and user-role messages:

    * **Assistant** (existing behaviour): deletes the boundary assistant
      turn, its triggering user prompt, and all later messages.  The
      frontend resubmits the prior user prompt content as a fresh turn.
    * **User** (Resend): deletes every message AFTER the boundary user
      turn (the user message itself is kept).  The frontend resubmits
      the kept user message content as a fresh turn.

    Two-step confirmation contract:

    * Without ``?confirm=true`` the endpoint returns HTTP 412 with a
      :class:`RegenerateConfirmDetail` body so the UI can render a
      confirm modal.  No mutation occurs.
    * With ``?confirm=true`` the endpoint performs the delete and returns
      a 200 envelope with the number of deleted rows and the user content
      to resubmit.  The client follows up with the standard
      POST /api/chat/stream.

    Special case: when the target is a user message and there are zero
    messages after it (nothing to delete), the 412 confirm gate is
    skipped and the endpoint auto-confirms — callers do not need to
    present a modal in this edge case.

    Args:
        chat_id:         Parent chat PK.
        message_id:      Target message PK (assistant or user role).
        request:         FastAPI Request.
        confirm:         When True, proceed with the destructive delete.
        user:            Authenticated user.
        chat_service:    Injected ``ChatService`` (ownership check).
        message_service: Injected ``MessageService``.

    Returns:
        On success, ``{"deleted": <int>, "chat_id": <int>,
        "prior_user_content": <str|null>}``.

    Raises:
        HTTPException: 403 when the chat is owned by another user.
        HTTPException: 404 when chat or message is missing.
        HTTPException: 412 when ``confirm`` is False (with subsequent_count).
        HTTPException: 422 when the role is not regeneratable.
    """
    # 1. Verify chat ownership separately so we can return a precise 403.
    try:
        await chat_service.get(chat_id, user_id=user.id)
    except ChatNotFoundError as exc:
        # When ChatService.get raises ChatNotFoundError it covers both
        # "missing" and "cross-user"; tighten the response with a dedicated
        # existence probe so the UI can distinguish them.
        async with chat_service._engine.connect() as _conn:  # noqa: SLF001
            from sqlalchemy import select as _select

            from lmchat.db.schema import chats as _chats

            exists = (
                await _conn.execute(_select(_chats.c.user_id).where(_chats.c.id == chat_id))
            ).fetchone()
        if exists is not None and int(exists[0]) != int(user.id):
            raise HTTPException(status_code=_HTTP_403, detail="chat not owned by you") from exc
        raise HTTPException(status_code=_HTTP_404, detail="chat not found") from exc

    log.info(
        "chats.regenerate.request",
        chat_id=chat_id,
        message_id=message_id,
        user_id=user.id,
        confirm=confirm,
    )

    # Probe the message role so we can dispatch to the correct boundary
    # logic.  This is a lightweight read that also validates ownership and
    # existence, so we surface 404 / 403 before any mutation.
    try:
        msg_role = await message_service.get_message_role(
            chat_id=chat_id, message_id=message_id, user_id=user.id
        )
    except MessageNotFoundError as exc:
        raise HTTPException(status_code=_HTTP_404, detail="message not found") from exc

    is_user_role = msg_role == "user"

    if not confirm:
        # Count rows that would be removed.
        # - Assistant path (existing): count inclusive of the boundary (the
        #   assistant message itself + everything after, including the prior
        #   user prompt which the service also deletes).
        # - User path (Resend): count exclusive of the boundary — only
        #   messages AFTER the user message.  This is the confirm-modal's
        #   "how much history would this affect" count; the boundary user
        #   message itself is also deleted at delete time (it gets replayed
        #   as a fresh turn, not left in place — see
        #   delete_from_user_message_for_resend).  When count is 0 there is
        #   nothing else to confirm; skip the 412 gate and fall through to
        #   the delete path directly.
        try:
            if is_user_role:
                subsequent_count = await message_service.count_messages_after(
                    chat_id=chat_id, message_id=message_id, user_id=user.id
                )
            else:
                subsequent_count = await message_service.count_messages_from(
                    chat_id=chat_id, message_id=message_id, user_id=user.id
                )
        except MessageNotFoundError as exc:
            raise HTTPException(status_code=_HTTP_404, detail="message not found") from exc

        # For a user-role resend with nothing after it, auto-confirm so the
        # caller doesn't have to go through the confirm modal.
        if is_user_role and subsequent_count == 0:
            # Fall through to the confirm=True path below.
            confirm = True
        else:
            # subsequent_count == 0 on the ASSISTANT path is the common case
            # (regen the LATEST assistant turn) — still legitimate.  The UI
            # can render a softer prompt (or auto-confirm) when nothing else
            # would be deleted.
            detail = RegenerateConfirmDetail(
                subsequent_count=subsequent_count,
                chat_id=chat_id,
                message_id=message_id,
            )
            raise HTTPException(status_code=412, detail=detail.model_dump())

    # confirm is True (either passed explicitly or auto-set above for the
    # user-resend case where nothing SUBSEQUENT would be deleted).
    try:
        if is_user_role:
            n, prior_user_content = await message_service.delete_from_user_message_for_resend(
                chat_id=chat_id, message_id=message_id, user_id=user.id
            )
        else:
            n, prior_user_content = await message_service.delete_assistant_turn_for_regenerate(
                chat_id=chat_id, message_id=message_id, user_id=user.id
            )
    except MessageNotFoundError as exc:
        raise HTTPException(status_code=_HTTP_404, detail="message not found") from exc
    except EditNotAllowedError as exc:
        raise HTTPException(status_code=_HTTP_422, detail=str(exc)) from exc

    return {
        "deleted": n,
        "chat_id": chat_id,
        "prior_user_content": prior_user_content,
    }


# ─── Sub-session streaming ────────────────────────────────────────────────────

_SUB_SESSION_FINALIZE_PROMPT: Final[str] = (
    "Based on everything above, write a clear, comprehensive summary document "
    "suitable for the main conversation context. Format it as a concise "
    "deliverable that captures the key findings, decisions, or output from "
    "this session. Use markdown where appropriate."
)


async def _sub_session_sse(
    *,
    lm_client: LmstudioStreamingClient,
    model_id: str,
    system_prompt: str,
    messages: list[dict[str, str]],
    integrations: list[str] | None = None,
    prior_tool_rounds: int = 0,
    chat_id: int | None = None,
    on_final: Callable[[str, str], None] | None = None,
) -> AsyncIterator[bytes]:
    """Bridge a sub-session through the canonical LM Studio pipeline.

    Delegates upstream I/O to :class:`LmstudioStreamingClient` — the public
    primitive built for non-chat reuse (see
    ``lmstudio_streaming_client.py`` §"Public reuse seam"). The raw httpx
    block this helper used to wrap has been removed; canonical events are
    translated into the ``sub.*`` SSE events the frontend consumes.

    Event mapping
    -------------
    - ``message.delta``                → ``sub.delta`` (``{delta: str}``)
    - ``reasoning.start``              → ``sub.reasoning.start``
    - ``reasoning.delta``              → ``sub.reasoning.delta``
        (``{delta: str}``)
    - ``reasoning.end``                → ``sub.reasoning.end``
    - ``chat.end`` (or stream exhaust) → ``sub.complete``
        (``{final_content: str}``)
    - ``error``                        → ``sub.error``
        (``{code, message}`` — canonical error contract)

    The sub-session has clean-context lifetime: no DB writes, no chat
    history hydration, no memory ingestion — only the system prompt and
    the provided messages reach the model. :class:`StreamingService` is
    intentionally not used; its persistence model is wrong here.

    ``on_final`` is fired exactly once, at whichever ``sub.complete``-style
    terminal resolves final content, with ``(content, kind)`` where ``kind``
    is one of ``"complete"``, ``"partial"``, or ``"capped"``. ``sub.error``
    terminals (and a graceful no-answer cap) do NOT invoke it — the deleted
    byte-scan wrapper only fired on non-empty ``sub.complete`` frames, and this
    preserves that. Lets the caller run distillation structurally instead of
    byte-scanning frames.
    """
    from lmchat.lmstudio.types import (
        CanonicalChatRequest,
        CanonicalInputBlock,
    )

    def _fire_on_final(content: str, kind: str) -> None:
        # Callback boundary: a caller-supplied on_final must NEVER corrupt the
        # SSE stream. The deleted byte-scan wrapper swallowed all distillation
        # errors (try/except -> _final=""); preserve that guarantee generically
        # so a raising callback degrades to a logged no-op instead of surfacing
        # a spurious sub.error to the client.
        if on_final is None:
            return
        try:
            on_final(content, kind)
        except Exception as _exc:  # noqa: BLE001
            log.warning(
                "sub_session.on_final_callback_error",
                kind=kind,
                error=str(_exc),
                error_type=type(_exc).__name__,
            )

    # Build a canonical request. The sub-session uses the compat-style
    # conversation shape: a system prompt plus an alternating turn list.
    # On the canonical surface the *current* turn lives in `input`; prior
    # turns ride along inside `system_prompt` so the model sees them as
    # context without LM Studio expecting a server-side history chain.
    history_pairs: list[tuple[str, str]] = []
    current_user_text = ""
    for idx, msg in enumerate(messages):
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if idx == len(messages) - 1 and role == "user":
            current_user_text = content
        else:
            history_pairs.append((role, content))

    composed_system = system_prompt + serialize_prior_turns(history_pairs)

    _original_integrations: list[str] = list(integrations or [])

    request = CanonicalChatRequest(
        model=model_id,
        system_prompt=composed_system,
        input=[CanonicalInputBlock(type="text", content=current_user_text)],
        store=False,
        integrations=_original_integrations,
    )

    def _build_toolless_request() -> CanonicalChatRequest:
        """Rebuild the sub-session request with integrations stripped.

        Used by the grammar-parse degrade (below): when LM Studio rejects the
        tool turn because one offered MCP schema can't be parsed by its grammar
        generator, we retry the SAME sub-session tool-less.  ``store`` stays
        False and the composed history already lives in ``system_prompt``, so
        the only change is dropping every integration.

        Returns:
            A fresh tool-less :class:`CanonicalChatRequest`.
        """
        return request.model_copy(update={"integrations": []})

    accumulated: list[str] = []
    # Accumulate reasoning text server-side so substance_fold can salvage
    # reasoning-only completions into final_content (mirror of streaming_service
    # §1.1). Without this, a /research turn where the model parks the answer
    # in reasoning_content emits sub.complete with final_content="" and the
    # user sees "thinking, then it stopped" (confirmed 2026-06-12).
    accumulated_reasoning: list[str] = []
    # Per-event-type tally — feeds the MTP tool-round gate and the
    # user-facing error/diagnostic messages below.
    _event_tally: dict[str, int] = {}
    # MTP gate for sub-sessions.
    # Start from the persisted prior-turn count so the MTP gate accumulates
    # correctly across multi-turn sub-sessions.  A sub-session with ≥20
    # tool rounds across multiple turns (each an independent HTTP request)
    # will correctly trigger the MTP-suspected warning on a 500 or
    # tool_format_generation_error — fixing the prior no-op where 0 was
    # always passed regardless of how many prior turns had completed.
    cumulative_tool_rounds: int = prior_tool_rounds
    # streaming-3: per-HTTP-request tool-round counter (distinct from the
    # cross-turn ``cumulative_tool_rounds`` above) — feeds the proactive
    # tool-loop cap via the shared main-pump policy (`_decide_loop_cut`).
    turn_tool_rounds: int = 0
    try:
        # -- Grammar-parse degrade retry loop (2026-07-04, sub-session parity).
        # LM Studio's grammar generator rejects the turn (400 "failed to parse
        # grammar") when ANY offered MCP tool schema is malformed. On the native
        # surface that arrives as chat.start + a bare exhaust (no chat.end, no
        # error event); the exhausted branch below probes for the real message
        # and, when it is a grammar failure and the turn had integrations, warns
        # + retries this SAME sub-session tool-less by rebinding _active_request
        # and ``continue``-ing this loop. Degrade-once via _grammar_degraded.
        # Mirror of streaming_service.stream_chat's grammar degrade.
        _active_request = request
        _grammar_degraded = False
        while True:
            _grammar_retry = False
            _stream_iter = lm_client.stream(
                request=_active_request, cumulative_tool_rounds=cumulative_tool_rounds
            )
            async for event in _stream_iter:
                etype = event.type
                _event_tally[etype] = _event_tally.get(etype, 0) + 1
                if etype == "message.delta" and event.content:
                    accumulated.append(event.content)
                    yield (
                        b"event: sub.delta\ndata: "
                        + json.dumps({"delta": event.content}).encode()
                        + b"\n\n"
                    )
                elif etype == "reasoning.start":
                    yield b"event: sub.reasoning.start\ndata: {}\n\n"
                elif etype == "reasoning.delta" and event.content:
                    accumulated_reasoning.append(event.content)
                    yield (
                        b"event: sub.reasoning.delta\ndata: "
                        + json.dumps({"delta": event.content}).encode()
                        + b"\n\n"
                    )
                elif etype == "reasoning.end":
                    yield b"event: sub.reasoning.end\ndata: {}\n\n"
                elif etype == "chat.start":
                    # Bridge liveness event so FE knows streaming started.
                    yield b"event: sub.processing.start\ndata: {}\n\n"
                elif etype in (
                    "prompt_processing.start",
                    "prompt_processing.progress",
                    "prompt_processing.end",
                ):
                    # Bridge prompt-processing progress for FE liveness.
                    pp_payload: dict[str, object] = {}
                    if event.progress is not None:
                        pp_payload["progress"] = event.progress
                    sub_etype = etype.replace("prompt_processing.", "sub.processing.")
                    yield (
                        b"event: "
                        + sub_etype.encode()
                        + b"\ndata: "
                        + json.dumps(pp_payload).encode()
                        + b"\n\n"
                    )
                elif etype in (
                    "tool_call.start",
                    "tool_call.name",
                    "tool_call.arguments",
                    "tool_call.success",
                ):
                    # Bridge tool_call events through to the sub-session FE so
                    # the user can see what the model is actually doing. Maps
                    # 1:1 with the canonical event types (rename ``tool_call``
                    # → ``sub.tool_call`` to keep namespacing consistent with
                    # sub.delta / sub.reasoning.*).
                    # Count completed tool rounds for MTP gate.
                    # Also persist to the cross-turn registry so subsequent HTTP
                    # turns of the same sub-session start from the correct offset.
                    if etype == "tool_call.success":
                        cumulative_tool_rounds += 1
                        turn_tool_rounds += 1
                        if chat_id is not None:
                            _sub_session_increment_tool_round(chat_id)
                    tc = event.tool_call
                    payload: dict[str, object] = {}
                    if tc is not None:
                        payload = tc.model_dump()
                    yield (
                        b"event: sub."
                        + etype.encode()
                        + b"\ndata: "
                        + json.dumps(payload).encode()
                        + b"\n\n"
                    )
                elif etype == "tool_call.failure":
                    # Bridge failure detail (code, tool, message, output)
                    # into sub.tool_call.failure so the FE receives the full context.
                    # Count failures toward MTP gate too.
                    cumulative_tool_rounds += 1
                    turn_tool_rounds += 1
                    if chat_id is not None:
                        _sub_session_increment_tool_round(chat_id)
                    tc = event.tool_call
                    tc_payload: dict[str, object] = {}
                    if tc is not None:
                        tc_payload = tc.model_dump()
                    if event.error:
                        tc_payload["error"] = event.error
                    yield (
                        b"event: sub.tool_call.failure\ndata: "
                        + json.dumps(tc_payload).encode()
                        + b"\n\n"
                    )
                elif etype == "error":
                    err = event.error or {}
                    err_type = err.get("type") or err.get("code")
                    accumulated_chars = sum(len(s) for s in accumulated)
                    log.warning(
                        "sub_session.stream.error_event",
                        error_raw=err,
                        accumulated_chars=accumulated_chars,
                        event_tally=_event_tally,
                    )

                    # Belt-and-suspenders grammar-degrade on a YIELDED error.
                    # On the native surface the grammar-parse rejection normally
                    # arrives via the exhausted-branch probe (below), NOT as an
                    # error event. But if a future LM Studio build emits it as an
                    # early error frame, degrade here too — warn + retry tool-less
                    # — instead of surfacing a fatal sub.error. Gated on: no
                    # content yet, integrations present, not already degraded.
                    _err_msg_be = str(err.get("message") or err.get("error") or "")
                    # is_native_path=True: the sub-session grammar-degrade is the
                    # LM Studio path (a cloud sub-session never emits grammar-parse
                    # errors, so the shared _is_grammar_parse_error check gates it
                    # out regardless). content_emitted is the inverse of the
                    # original inline guard (`accumulated_chars == 0`) — the helper
                    # checks `not content_emitted`, i.e. no content streamed yet.
                    if _grammar_degrade_eligible(
                        is_native_path=True,
                        has_integrations=bool(_original_integrations),
                        content_emitted=accumulated_chars > 0,
                        already_degraded=_grammar_degraded,
                        error_detail=_err_msg_be,
                    ):
                        _grammar_degraded = True
                        _grammar_retry = True
                        log.warning(
                            "sub_session.stream.grammar_degrade.triggered_on_error_event",
                            integrations=_original_integrations,
                            error_message=_err_msg_be[:200],
                        )
                        STREAMS_SALVAGED.labels(reason="grammar_degrade").inc()
                        yield (
                            b"event: sub.warning\ndata: "
                            + json.dumps(
                                {
                                    "code": "tool_schema_parse_failed",
                                    "message": _grammar_degrade_warning(
                                        _original_integrations
                                    ),
                                }
                            ).encode()
                            + b"\n\n"
                        )
                        _active_request = _build_toolless_request()
                        _event_tally = {}
                        # Clean slate for the tool-less retry (symmetry with the
                        # _event_tally reset): the retry advertises no tools, so
                        # this stays 0, but never carry a stale per-turn count.
                        turn_tool_rounds = 0
                        # Break the async-for; the exhausted branch sees
                        # _grammar_retry and re-enters the outer while.
                        break

                    # `tool_format_generation_error` = LM Studio rejected a
                    # malformed tool-call generation from the model. Small
                    # reasoning models regularly hit this on the 3rd+ tool
                    # round when their structured-output discipline degrades.
                    # If the model HAS already produced answer content, deliver
                    # it as a partial completion + a soft warning rather than
                    # discarding it with a generic error (confirmed 2026-06-06:
                    # a 438-char partial answer was silently discarded).
                    if (
                        err_type in ("tool_format_generation_error", "tool_call_malformed")
                        and accumulated_chars > 0
                    ):
                        yield (
                            b"event: sub.complete\ndata: "
                            + json.dumps(
                                {
                                    "final_content": "".join(accumulated),
                                    "truncated": True,
                                    "truncation_reason": "tool_format_generation_error",
                                    "truncation_hint": (
                                        "The model couldn't format the next tool "
                                        "call cleanly. This is a known limit of "
                                        "small reasoning models on multi-round "
                                        "tool chains — try a larger model (122b, "
                                        "35b-a3b) for the full chain."
                                    ),
                                }
                            ).encode()
                            + b"\n\n"
                        )
                        _fire_on_final("".join(accumulated), "partial")
                        return

                    # Hard error path — no partial content, or a non-tool error.
                    # Build a human-readable message that survives empty LM Studio
                    # ``message`` fields.  For tool_format_generation_error we
                    # surface the partial tool-call tally so the user knows the
                    # stream made progress before failing.
                    err_code = err.get("code") or err_type or "upstream_error"
                    raw_msg = err.get("message") or err.get("error") or err.get("output") or ""
                    # Always override message for known tool-error types to
                    # include the tally progress — _normalize_error_event
                    # always supplies a non-empty default message, so the original
                    # `if not raw_msg` guard was dead code.
                    if err_type in ("tool_format_generation_error", "tool_call_malformed"):
                        tc_done = _event_tally.get("tool_call.success", 0)
                        tc_started = _event_tally.get("tool_call.start", 0)
                        tc_failed = _event_tally.get("tool_call.failure", 0)
                        failed_suffix = f", {tc_failed} failed" if tc_failed else ""
                        raw_msg = (
                            f"Tool format generation error — completed "
                            f"{tc_done}/{tc_started} tool calls{failed_suffix} before the "
                            "model produced a malformed call"
                        )
                    elif not raw_msg:
                        raw_msg = f"{err_type or 'upstream error'} (no message)"
                    # Mirror main-pump MTP reset.
                    # When mtp_suspected fires in a sub-session, reset the cross-turn
                    # counter for this chat_id.  Without the reset, every subsequent
                    # 500 or tool_format_generation_error on ANY future sub-session
                    # for this chat fires the MTP-suspected warning (counter never
                    # clears until the 512-entry LRU evicts it).  The main pump
                    # already does this (streaming_service.py:~1649); sub-sessions
                    # must mirror the same semantics.
                    if err_code == "mtp_suspected" and chat_id is not None:
                        _sub_session_reset_tool_rounds(chat_id)
                    yield (
                        b"event: sub.error\ndata: "
                        + json.dumps(
                            {
                                "code": err_code,
                                "message": raw_msg,
                            }
                        ).encode()
                        + b"\n\n"
                    )
                    return
                elif etype == "chat.end":
                    _chat_end_chars = sum(len(s) for s in accumulated)
                    _chat_end_reasoning_chars = sum(len(s) for s in accumulated_reasoning)
                    log.info(
                        "sub_session.stream.chat_end",
                        accumulated_chars=_chat_end_chars,
                        reasoning_chars=_chat_end_reasoning_chars,
                        event_tally=_event_tally,
                    )
                    # Tool-aware terminal decision (shared policy with the main
                    # stream — resolve_terminal_content). ORDERING FIX: a
                    # tool-using /research turn that produced no answer must yield
                    # the graceful no_final_content error, NOT a raw-reasoning
                    # dump. The old code ran substance_fold FIRST and returned,
                    # which SHADOWED the graceful-no-answer guard below so a turn
                    # with >240 chars of reasoning surfaced the raw chain instead
                    # of the error.
                    from lmchat.services.substance_fold import (  # noqa: PLC0415
                        resolve_terminal_content,
                    )

                    _joined_content = "".join(accumulated)
                    _joined_reasoning = "".join(accumulated_reasoning) or None
                    _terminal = resolve_terminal_content(
                        _joined_content,
                        _joined_reasoning,
                        had_tool_calls=_event_tally.get("tool_call.start", 0) > 0,
                    )
                    # Tools ran but produced no answer — structured error
                    # the FE renders (larger-model / rephrase hint), instead of a
                    # silent empty sub.complete OR a raw-reasoning dump.
                    if _terminal.kind == "graceful":
                        log.warning(
                            "sub_session.stream.tool_turn_no_final_answer",
                            reasoning_len=_chat_end_reasoning_chars,
                            tally=_event_tally,
                        )
                        yield (
                            b"event: sub.error\ndata: "
                            + json.dumps(
                                {
                                    "code": "no_final_content",
                                    "message": (
                                        "The model completed tool calls but produced no "
                                        "final answer. This may indicate a reasoning loop "
                                        "or a model that only outputs tool calls without "
                                        "summarising results. Try a larger model or "
                                        "rephrase the question."
                                    ),
                                    "tally": _event_tally,
                                }
                            ).encode()
                            + b"\n\n"
                        )
                        return
                    if _terminal.kind == "salvaged":
                        log.warning(
                            "sub_session.stream.content_starvation_salvaged",
                            base_len=len(_joined_content),
                            reasoning_len=_chat_end_reasoning_chars,
                        )
                    yield (
                        b"event: sub.complete\ndata: "
                        + json.dumps({"final_content": _terminal.content}).encode()
                        + b"\n\n"
                    )
                    _fire_on_final(_terminal.content, "complete")
                    return
                # streaming-3: proactive per-turn tool-loop cap. Shares the
                # main pump's decision (_decide_loop_cut) — no second threshold.
                # Fires only on tool_call.success/.failure (the events that grew
                # turn_tool_rounds); the frame for this event was already yielded
                # above, so the FE sees the round before we abort.
                if etype in ("tool_call.success", "tool_call.failure"):
                    from lmchat.services.streaming_service import (  # noqa: PLC0415
                        _MAX_TOOL_ROUNDS_PER_TURN,
                        _decide_loop_cut,
                    )

                    # early_cut_reason / consecutive_identical_rounds are the
                    # main pump's client-advisory + repeat-detection signals,
                    # which the sub-session does not track — so ONLY the absolute
                    # per-turn backstop (_MAX_TOOL_ROUNDS_PER_TURN) can fire here.
                    # Deliberate: streaming-3's remit is a hard runaway cap where
                    # there was none, not full repeat-loop parity. (Early
                    # identical-round detection for sub-sessions is a separate
                    # enhancement.)
                    _cut = _decide_loop_cut(
                        early_cut_reason=None,
                        event_type=etype,
                        consecutive_identical_rounds=0,
                        turn_tool_rounds=turn_tool_rounds,
                    )
                    if _cut.should_cut:
                        # ``_decide_loop_cut`` guarantees cut_reason / effective_cut
                        # are non-None whenever should_cut is True (mirrors the
                        # main pump's narrowing in streaming_service.py).
                        assert _cut.cut_reason is not None
                        assert _cut.effective_cut is not None
                        _cut_reason: str = _cut.cut_reason
                        _effective_cut: str = _cut.effective_cut
                        with suppress(Exception):
                            await _stream_iter.aclose()  # type: ignore[attr-defined]
                        from lmchat.services.substance_fold import (  # noqa: PLC0415
                            resolve_terminal_content,
                        )

                        _cap_terminal = resolve_terminal_content(
                            "".join(accumulated),
                            "".join(accumulated_reasoning) or None,
                            had_tool_calls=True,
                            tool_rounds=turn_tool_rounds,
                            loop_cut_reason=_effective_cut,
                        )
                        log.warning(
                            "sub_session.stream.tool_loop_cap_hit",
                            chat_id=chat_id,
                            turn_tool_rounds=turn_tool_rounds,
                            cap=_MAX_TOOL_ROUNDS_PER_TURN,
                            cut_reason=_cut_reason,
                        )
                        STREAMS_SALVAGED.labels(reason=_cut_reason).inc()
                        yield (
                            b"event: sub.warning\ndata: "
                            + json.dumps(
                                {
                                    "code": "tool_loop_cap",
                                    "message": (
                                        "Stopped a runaway tool-call loop after "
                                        f"{turn_tool_rounds} rounds. The model kept "
                                        "calling tools without answering."
                                    ),
                                }
                            ).encode()
                            + b"\n\n"
                        )
                        yield (
                            b"event: sub.complete\ndata: "
                            + json.dumps({"final_content": _cap_terminal.content}).encode()
                            + b"\n\n"
                        )
                        # Distill ONLY a genuinely salvaged answer. A "graceful"
                        # terminal carries a system no-answer hint, not user
                        # content — distilling it would pollute memory. This also
                        # mirrors the main pump, which never distills its
                        # tool_loop_cap terminal (with_distill_and_summary=False).
                        if _cap_terminal.kind != "graceful":
                            _fire_on_final(_cap_terminal.content, "capped")
                        return
            # The belt-and-suspenders error-event degrade (above) ``break``s out
            # of the async-for with _grammar_retry already armed; re-enter the
            # outer while immediately (the warning was emitted + _active_request
            # rebuilt tool-less) without re-probing.
            if _grammar_retry:
                continue

            # Generator exhausted without an explicit chat.end.
            _exhausted_chars = sum(len(s) for s in accumulated)
            log.warning(
                "sub_session.stream.generator_exhausted_no_chat_end",
                accumulated_chars=_exhausted_chars,
                reasoning_chars=sum(len(s) for s in accumulated_reasoning),
                event_tally=_event_tally,
                degraded=_grammar_degraded,
            )

            # -- Grammar-parse degrade + real-error surfacing (sub-session
            # parity, 2026-07-04). LM Studio collapses a grammar-parse rejection
            # into a bare chat.start + exhaust (no chat.end, no error event) —
            # the exact firecrawl-bad-schema symptom (WITH firecrawl: empty
            # death; WITHOUT: works). On an EMPTY exhaust with integrations,
            # probe (non-streaming re-issue) to recover the real message:
            #   - grammar failure + not-yet-degraded → warn + retry tool-less
            #     (degrade-once via _grammar_degraded).
            #   - anything else (non-grammar, OR a retry that also failed) →
            #     surface the probed error as sub.error instead of a silent
            #     empty sub.complete, so the user sees the real failure.
            if _exhausted_chars == 0 and _original_integrations:
                _probed: str | None = None
                try:
                    _probed = await lm_client.probe_for_error(_active_request)
                    if _probed is not None:
                        log.info(
                            "sub_session.stream.exhausted_error_probed",
                            detail=_probed[:200],
                        )
                except Exception as _probe_exc:  # noqa: BLE001
                    log.warning(
                        "sub_session.stream.exhausted_error_probe_failed",
                        error=str(_probe_exc),
                    )

                # Retry tool-less ONLY on a grammar failure we haven't already
                # degraded from (degrade-once).
                if _grammar_degrade_eligible(
                    is_native_path=True,
                    has_integrations=bool(_original_integrations),
                    content_emitted=_exhausted_chars > 0,
                    already_degraded=_grammar_degraded,
                    error_detail=_probed or "",
                ):
                    _grammar_degraded = True
                    log.warning(
                        "sub_session.stream.grammar_degrade.triggered",
                        integrations=_original_integrations,
                        error_message=(_probed or "")[:200],
                    )
                    STREAMS_SALVAGED.labels(reason="grammar_degrade").inc()
                    # The culprit isn't named by LM Studio, so drop ALL tools for
                    # the retry and list what was active so the user can identify
                    # the bad one.
                    yield (
                        b"event: sub.warning\ndata: "
                        + json.dumps(
                            {
                                "code": "tool_schema_parse_failed",
                                "message": _grammar_degrade_warning(
                                    _original_integrations
                                ),
                            }
                        ).encode()
                        + b"\n\n"
                    )
                    # Rebuild tool-less + reset the per-attempt tally, then re-enter
                    # the loop against the fresh stream (same accumulate/emit logic).
                    _active_request = _build_toolless_request()
                    _event_tally = {}
                    # Clean slate for the tool-less retry (symmetry with the
                    # _event_tally reset): the retry advertises no tools, so this
                    # stays 0, but never carry a stale per-turn count.
                    turn_tool_rounds = 0
                    continue

                # Not retrying: surface the REAL error (grammar-after-degrade, a
                # non-grammar failure, or a retry that also exhausted) instead of
                # a silent empty sub.complete.
                if _probed:
                    yield (
                        b"event: sub.error\ndata: "
                        + json.dumps(
                            {
                                "code": "upstream_error",
                                "message": _probed,
                                "tally": _event_tally,
                            }
                        ).encode()
                        + b"\n\n"
                    )
                    return

            # Mirror the chat.end terminal decision so a stream that exhausts
            # mid-flight resolves the same way (tool-aware: a tool turn with no
            # answer gets the graceful message, not a raw-reasoning dump).
            from lmchat.services.substance_fold import (  # noqa: PLC0415
                resolve_terminal_content,
            )

            _exhausted_content = "".join(accumulated)
            _exhausted_reasoning = "".join(accumulated_reasoning) or None
            _exhausted_terminal = resolve_terminal_content(
                _exhausted_content,
                _exhausted_reasoning,
                had_tool_calls=_event_tally.get("tool_call.start", 0) > 0,
            )
            yield (
                b"event: sub.complete\ndata: "
                + json.dumps({"final_content": _exhausted_terminal.content}).encode()
                + b"\n\n"
            )
            _fire_on_final(_exhausted_terminal.content, "complete")
            # Normal (non-degrade) terminal — leave the retry loop.
            break
    except StreamingClientUpstreamError as exc:
        # Unwrap and preserve structured fields from upstream error events.
        accumulated_chars = sum(len(s) for s in accumulated)
        err = exc.event.error or {}
        err_code = err.get("code") or "upstream_error"
        err_message = err.get("message") or str(exc)
        log.warning(
            "sub_session.stream_error.upstream",
            error_code=err_code,
            accumulated_chars=accumulated_chars,
            event_tally=_event_tally,
        )
        yield (
            b"event: sub.error\ndata: "
            + json.dumps(
                {
                    "code": err_code,
                    "message": err_message,
                    "hint": err.get("hint"),
                    "tally": _event_tally,
                    "accumulated_chars": accumulated_chars,
                    "truncated": accumulated_chars > 0,
                }
            ).encode()
            + b"\n\n"
        )
    except httpx.HTTPError as exc:
        # httpx transport errors get a stable code.
        accumulated_chars = sum(len(s) for s in accumulated)
        log.warning(
            "sub_session.stream_error.connection",
            error=str(exc),
            error_type=type(exc).__name__,
            accumulated_chars=accumulated_chars,
        )
        yield (
            b"event: sub.error\ndata: "
            + json.dumps(
                {
                    "code": "upstream_connection_lost",
                    "message": f"Connection to LM Studio lost: {exc}",
                    "tally": _event_tally,
                    "accumulated_chars": accumulated_chars,
                    "truncated": accumulated_chars > 0,
                }
            ).encode()
            + b"\n\n"
        )
    except Exception as exc:  # noqa: BLE001
        accumulated_chars = sum(len(s) for s in accumulated)
        log.warning(
            "sub_session.stream_error",
            error=str(exc),
            error_type=type(exc).__name__,
            accumulated_chars=accumulated_chars,
        )
        yield (
            b"event: sub.error\ndata: "
            + json.dumps(
                {
                    "code": "stream_error",
                    "message": str(exc),
                    "tally": _event_tally,
                    "accumulated_chars": accumulated_chars,
                    "truncated": accumulated_chars > 0,
                }
            ).encode()
            + b"\n\n"
        )


@router.post("/{chat_id}/sub-session/stream")
async def sub_session_stream(
    chat_id: int,
    model_id: str = Form(...),
    system_prompt: str = Form(...),
    messages_json: str = Form(...),
    project_id: str | None = Form(None),
    integrations: str | None = Form(None),
    provider: str | None = Form(default=None),
    request: Request = None,  # type: ignore[assignment]
    user: User = Depends(require_user),
    integrations_service: IntegrationsService = Depends(get_integrations_service_dep),
) -> StreamingResponse:
    """Stream a sub-session completion with clean context (no chat history).

    The sub-session uses ONLY [system_prompt, ...messages_json] — the main
    chat history is never loaded.  Nothing is persisted to the DB.

    Used by the frontend slash-command sub-session panel so that each mode
    (/research, /code, etc.) operates in a clean, isolated context.

    Sub-sessions cannot be projected. The ``project_id`` Form field is
    accepted in the signature ONLY so the rejection is explicit + diagnosable
    — sending one yields 400 with ``code=sub_session_not_projectable`` rather
    than a silent ignore. The parent chat's project membership is intentionally
    irrelevant; project context flows into persistent main-chat turns via
    the project-prompt hoist at ``streaming_service.py:836-862``, never
    into the ephemeral sub-session pipeline.
    """
    if project_id is not None and project_id != "":
        raise HTTPException(
            status_code=_HTTP_400,
            detail={
                "code": "sub_session_not_projectable",
                "message": (
                    "Sub-sessions are clean-context (no DB writes, no "
                    "history hydration); they intentionally cannot be "
                    "scoped to a project. The parent chat's project "
                    "membership applies to persistent main-chat turns "
                    "but not to this isolated stream."
                ),
            },
        )

    # Defense in depth — the FE guards against empty model_id but the
    # silent-failure mode (FastAPI 422 from `Form(...)` on empty multipart
    # strings → swallowed by the FE before 2026-06-06 fix) was severe
    # enough that this route deserves a structured 400 too. Symmetric
    # with the projectable guard above.
    if not model_id or not model_id.strip():
        raise HTTPException(
            status_code=_HTTP_400,
            detail={
                "code": "no_model_selected",
                "message": (
                    "Select a model in the top bar before starting a "
                    "sub-session. The request reached the server with "
                    "an empty model_id."
                ),
            },
        )

    # Verify chat ownership (session belongs to this user even though we
    # don't load its history).
    async with request.app.state.streaming_service._engine.connect() as conn:
        from sqlalchemy import select as _select

        from lmchat.db.schema import chats as _chats

        row = (
            await conn.execute(
                _select(_chats.c.id).where(
                    _chats.c.id == chat_id,
                    _chats.c.user_id == user.id,
                )
            )
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=_HTTP_404, detail="chat not found")

    try:
        messages: list[dict[str, str]] = json.loads(messages_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=_HTTP_422, detail="invalid messages_json") from exc

    # Parse integrations form field — JSON-encoded list of integration ids
    # (e.g. ``["mcp/context7", "mcp/deepwiki"]``). Empty / missing → no
    # tools. Per the canonical chat surface, integration ids are validated
    # downstream by LM Studio at request time.
    #
    # Security note: ids land in a markdown code block via
    # ``buildSubSessionSystemPrompt`` (subSession.ts). A user bypassing
    # the FE could craft an id with control chars or backticks and break
    # the block → prompt injection. Defense: enforce the same allowlist
    # the admin curator uses (``IntegrationSetEntry._validate_value``):
    # ``[A-Za-z0-9_\-./:]{1,256}`` only, no control chars, no path
    # traversal, no absolute paths. Anything failing the allowlist is
    # silently dropped rather than 422'ing the whole sub-session — the
    # FE seeds only validated admin values, so a drop here only fires
    # under bypass.
    import re as _re_integrations

    _INTEGRATION_ID_RE = _re_integrations.compile(r"[A-Za-z0-9_\-./:]{1,256}")

    def _is_valid_integration_id(v: str) -> bool:
        if not v or not v.strip():
            return False
        if len(v) > 256:
            return False
        # Block control bytes first (null, newline, CR, tab).
        if any(ch in v for ch in ("\x00", "\n", "\r", "\t")):
            return False
        if not _INTEGRATION_ID_RE.fullmatch(v):
            return False
        # Block absolute paths and traversal sequences.
        if v.startswith("/") or "../" in v:
            return False
        return True

    # Sub-session parity: resolve admin-default integrations when
    # the FE omits the field entirely (None).  Explicit "[]" → stays empty;
    # explicit JSON list → used as-is after validation.  Mirrors the same
    # logic in chat_stream (streaming.py) which guards the main chat surface.
    _raw_integrations_absent = integrations is None

    integrations_list: list[str] = []
    integrations_dropped: list[str] = []
    if integrations:
        try:
            parsed = json.loads(integrations)
            if isinstance(parsed, list):
                for x in parsed:
                    candidate = str(x)
                    if _is_valid_integration_id(candidate):
                        integrations_list.append(candidate)
                    else:
                        integrations_dropped.append(candidate[:64])
        except json.JSONDecodeError:
            # Non-JSON value is non-fatal — proceed with no tools rather
            # than 422'ing the whole sub-session.
            log.warning("sub_session.integrations_json_invalid", raw=integrations[:200])
    if integrations_dropped:
        log.warning(
            "sub_session.integrations_id_rejected",
            count=len(integrations_dropped),
            sample=integrations_dropped[:5],
            hint="FE bypass suspected — only admin-curated ids should reach this route",
        )

    # Admin-defaults: when the field was absent (not an explicit empty list),
    # fall back to the admin-configured defaults — same contract as chat_stream.
    # Sub-session parity: wrap list_available() so a transient DB error
    # degrades to no-tools rather than 500-ing the sub-session start.
    if _raw_integrations_absent:
        try:
            _entries = await integrations_service.list_available()
            integrations_list = [e.value for e in _entries if e.enabled_by_default]
            log.info(
                "sub_session.integrations_admin_defaults_applied",
                count=len(integrations_list),
                ids=integrations_list,
            )
        except Exception:  # noqa: BLE001
            integrations_list = []
            log.warning(
                "sub_session.integrations_default_lookup_failed",
                hint="Falling back to no-tools for this sub-session; transient DB error.",
            )
    elif integrations_list:
        # Sub-session parity with chat_stream: drop ids no longer in the
        # catalog (e.g. a removed MCP server lingering in the FE's cached
        # per-chat selection) so an unknown id can't crash the sub-session at
        # the LM Studio layer ("Cannot find plugin handle for plugin: mcp/...").
        try:
            _avail = {e.value for e in await integrations_service.list_available()}
            # Guard: only filter against a NON-EMPTY catalog — an empty result
            # (unconfigured install / transient DB error) must never drop the
            # user's explicit selection.
            if _avail:
                _kept = [i for i in integrations_list if i in _avail]
                if len(_kept) != len(integrations_list):
                    log.warning(
                        "sub_session.integrations_dropped_unavailable",
                        dropped=[i for i in integrations_list if i not in _avail],
                        kept=_kept,
                    )
                    integrations_list = _kept
        except Exception:  # noqa: BLE001
            pass  # best-effort; can't validate on a transient DB error

    # Diagnostic — log what the FE actually sent to distinguish between
    # FE not sending vs BE not forwarding integrations.
    log.info(
        "sub_session.stream.received",
        chat_id=chat_id,
        model_id=model_id,
        integrations_raw=(integrations or "")[:200],
        integrations_parsed=integrations_list,
        msg_count=len(messages),
    )

    lm_client: LmstudioStreamingClient = request.app.state.lm_streaming_client

    # Sub-session cloud-provider routing.
    # Resolve the effective provider FIRST — before any LM Studio model
    # resolution — so the cloud path never touches the LM Studio loaded-instance
    # lookup (which would either 422 or substitute a local model id in place of
    # the cloud model id, corrupting the request sent to the cloud provider).
    # When provider is absent / "lmstudio" / unresolved, fall back to the
    # default lm_client — keeping the existing LM Studio path byte-identical.
    from lmchat.services.lmstudio_streaming_client import (  # noqa: PLC0415
        LmstudioStreamingClient as _LmstudioStreamingClient,
    )

    _effective_provider = (provider or "").strip()
    _dispatch_client = lm_client  # default: LM Studio path unchanged
    _is_cloud = False
    if _effective_provider and _effective_provider != "lmstudio":
        _registry = getattr(request.app.state, "provider_registry", None)
        if _registry is not None:
            _resolved = _registry.get(_effective_provider)
            if _resolved is not None:
                # B3: when cloud + MCP integrations, wrap in the agentic loop
                # (single source of truth in mcp/agentic.py).
                # cloud-without-integrations and LM Studio paths are untouched.
                from lmchat.mcp.agentic import maybe_wrap_agentic  # noqa: PLC0415

                _effective_resolved = await maybe_wrap_agentic(
                    _resolved,
                    integrations_list,
                    request.app.state,
                    log_ctx={"site": "sub_session", "chat_id": chat_id},
                )
                _dispatch_client = _LmstudioStreamingClient(adapter=_effective_resolved)  # type: ignore[arg-type]
                _is_cloud = True
                log.info(
                    "sub_session.provider_routing",
                    chat_id=chat_id,
                    provider=_effective_provider,
                    model_id=model_id,
                )
            else:
                log.warning(
                    "sub_session.provider_unknown_fallback_lmstudio",
                    chat_id=chat_id,
                    provider=_effective_provider,
                )
    else:
        # openai_compat parity (mirrors streaming_service.py's
        # _resolve_provider_and_context_mode lmstudio branch, applied here
        # exactly like the cloud branch above): native endpoint mode (the
        # default) leaves this a no-op — _dispatch_client stays lm_client,
        # chain path byte-identical. openai_compat re-presents the SAME
        # live LM Studio adapter as an OpenAICompatProvider and wraps it in
        # maybe_wrap_agentic so mcp/* integrations AND the app-executed
        # web_search tool reach the sub-session, same as a cloud provider
        # would above. _is_cloud stays False — the model still needs the
        # LOADED-instance wire-id resolution below (openai_compat routes by
        # loaded label, not the catalog key); only a real cloud model id
        # skips that.
        _registry = getattr(request.app.state, "provider_registry", None)
        if _registry is not None:
            from lmchat.services.lm_studio_overrides_service import (  # noqa: PLC0415
                resolve_lm_studio_endpoint_mode,
            )

            _endpoint_mode = await resolve_lm_studio_endpoint_mode(
                engine=get_engine_dep(request)
            )
            if _endpoint_mode == "openai_compat":
                _lmstudio_native = _registry.get("lmstudio")
                if _lmstudio_native is not None:
                    _compat = _lmstudio_native.as_openai_compat_provider()  # type: ignore[attr-defined]

                    # Increment 4 parity: app-executed web_search, gated
                    # behind openai_compat exactly like the main path.
                    _builtin_registry_arg = None
                    _builtin_ctx_arg = None
                    _web_search_service = getattr(
                        request.app.state, "web_search_service", None
                    )
                    if _web_search_service is not None:
                        from lmchat.services.builtin_tools import (  # noqa: PLC0415
                            BUILTIN_TOOL_REGISTRY,
                            BuiltinToolContext,
                        )

                        _builtin_registry_arg = BUILTIN_TOOL_REGISTRY
                        _builtin_ctx_arg = BuiltinToolContext(
                            web_search_service=_web_search_service
                        )

                    from lmchat.mcp.agentic import maybe_wrap_agentic  # noqa: PLC0415

                    _wrapped = await maybe_wrap_agentic(
                        _compat,
                        integrations_list,
                        request.app.state,
                        log_ctx={"site": "sub_session", "chat_id": chat_id},
                        builtin_registry=_builtin_registry_arg,
                        builtin_ctx=_builtin_ctx_arg,
                    )
                    _dispatch_client = _LmstudioStreamingClient(adapter=_wrapped)  # type: ignore[arg-type]
                    log.info(
                        "sub_session.lmstudio_openai_compat_dispatch",
                        chat_id=chat_id,
                        model_id=model_id,
                    )

    # Resolve the stored model key to a LOADED loaded_instance_id, falling back
    # to another loaded LLM when the pinned model has idled out of LM Studio.
    # chats.model_id (and what the FE sends) is the stable key; shipping it raw
    # when the model is unloaded (and JIT is disabled) hard-errors the whole
    # sub-session — this stranded finished /research runs (2026-06-17).
    #
    # This block is SKIPPED for cloud providers: the cloud model id (e.g.
    # "openai/gpt-4o-mini") must reach the cloud provider verbatim; running it
    # through LM Studio resolution would either 422 ("no model loaded") or
    # substitute a local loaded-instance id — both corrupt the cloud request.
    # Mirrors how streaming_service.py gates resolution behind
    # context_mode=="chain" for the main replay path.
    wire_model_id = model_id
    if not _is_cloud:
        try:
            _models_svc = request.app.state.models_service
            _res = await _models_svc.resolve_to_loaded_or_fallback(model_id)
            if _res.wire_id is None:
                raise HTTPException(
                    status_code=_HTTP_422,
                    detail=(
                        "No language model is loaded in LM Studio. Load a model "
                        "(or enable JIT loading) and try again."
                    ),
                )
            wire_model_id = _res.wire_id
            if _res.substituted:
                log.info(
                    "sub_session.model_substituted",
                    chat_id=chat_id,
                    requested=model_id,
                    used=_res.fallback_key,
                )
            elif wire_model_id != model_id:
                log.info(
                    "sub_session.model_id_resolved",
                    chat_id=chat_id,
                    stored_key=model_id,
                    wire_id=wire_model_id,
                )
        except HTTPException:
            raise
        except Exception as _exc:  # noqa: BLE001
            log.warning(
                "sub_session.model_resolve_failed",
                model_id=model_id,
                error=str(_exc),
            )
            # Fall through with the raw value — LM Studio will surface a clear 400.

    # Load the persisted cross-turn tool-round count so the
    # MTP gate inside LmstudioAdapter sees the correct cumulative value.
    prior_rounds = _sub_session_get_tool_rounds(chat_id)

    # Sub-session auto-memory distillation (opt-in, default OFF).
    # When BOTH lm_chat_memory_distillation_enabled (master) AND
    # lm_chat_subsession_memory_distillation_enabled are True, wrap the
    # SSE generator so that the first sub.complete event triggers a
    # fire-and-forget _safe_distill_memory call — identical to the
    # main-chat path in streaming_service.py:3519.  All bytes pass through
    # unchanged; the distillation task can never affect the stream.
    #
    # user_text: the last user-role message in the messages list (what the
    # model was responding to).  Falls back to "" if the list is empty or
    # the last entry isn't a user turn — matches the main-chat contract
    # where user_text is the raw prompt text.
    _distill_user_text: str = ""
    for _m in reversed(messages):
        if _m.get("role") == "user":
            _distill_user_text = _m.get("content") or ""
            break

    # Use the app-settings resolvers so admin overrides take effect
    # without a redeploy.  Falls back to config defaults on any error.
    _engine = getattr(request.app.state, "engine", None) or getattr(
        integrations_service, "_engine", None
    )
    _distill_enabled = False
    if _engine is not None:
        try:
            from lmchat.services.app_settings_service import (  # noqa: PLC0415
                resolve_memory_distillation_enabled,
                resolve_subsession_memory_distillation_enabled,
            )

            _master = await resolve_memory_distillation_enabled(_engine)
            _sub = await resolve_subsession_memory_distillation_enabled(_engine)
            _distill_enabled = _master and _sub
        except Exception:  # noqa: BLE001
            _distill_enabled = False
    else:
        # No engine available — fall back to config defaults (safe: default is
        # master=True, sub=False → overall False, matching today's behaviour).
        from lmchat.config import get_settings as _get_settings  # noqa: PLC0415

        _distill_enabled = (
            _get_settings().lm_chat_memory_distillation_enabled
            and _get_settings().lm_chat_subsession_memory_distillation_enabled
        )

    def _on_final(content: str, kind: str) -> None:
        # Structural replacement for the old sub.complete byte-scan. Distill
        # only when there is a resolved answer (complete/partial/capped) — the
        # old wrapper fired on any non-empty sub.complete final_content, never
        # on sub.error, which this preserves exactly.
        if kind == "error" or not content or not _distill_enabled:
            return
        _ss_svc = getattr(request.app.state, "streaming_service", None)
        _distill_fn = getattr(_ss_svc, "_safe_distill_memory", None)
        if _distill_fn is not None:
            # spawn_background_task holds a strong ref so the distill task
            # can't be GC'd mid-flight (bare create_task() is only weakly
            # referenced by the loop).
            spawn_background_task(
                _distill_fn(
                    user_id=user.id,
                    chat_id=chat_id,
                    model_id=wire_model_id,
                    user_text=_distill_user_text,
                    assistant_answer=content,
                    project_id=None,
                ),
                name=f"sub_session_memory_distill_{chat_id}",
            )

    _sse_inner = _sub_session_sse(
        lm_client=_dispatch_client,
        model_id=wire_model_id,
        system_prompt=system_prompt,
        messages=messages,
        integrations=integrations_list,
        prior_tool_rounds=prior_rounds,
        chat_id=chat_id,
        on_final=_on_final,
    )

    return StreamingResponse(
        _sse_inner,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{chat_id}/sub-session/finalize")
async def sub_session_finalize(
    chat_id: int,
    model_id: str = Form(...),
    system_prompt: str = Form(...),
    messages_json: str = Form(...),
    project_id: str | None = Form(None),
    provider: str | None = Form(default=None),
    request: Request = None,  # type: ignore[assignment]
    user: User = Depends(require_user),
    message_service: object = Depends(_get_message_service),
) -> StreamingResponse:
    """Generate a summary of the sub-session and stream it back.

    Appends the standard finalization prompt to the sub-session messages,
    streams the model's summary response.  The frontend saves the final
    content and POSTs it to /inject-message to add it to the main thread.

    Same project_id reject as /stream — the finalize stream is part of the
    same ephemeral pipeline so it inherits the not-projectable contract.
    """
    if project_id is not None and project_id != "":
        raise HTTPException(
            status_code=_HTTP_400,
            detail={
                "code": "sub_session_not_projectable",
                "message": (
                    "Sub-sessions are clean-context; the finalize stream "
                    "shares that contract. The parent chat's project "
                    "membership applies to persistent main-chat turns "
                    "only."
                ),
            },
        )

    # Symmetric guard with /sub-session/stream — empty model_id would
    # otherwise be a generic 422 from `Form(...)`.
    if not model_id or not model_id.strip():
        raise HTTPException(
            status_code=_HTTP_400,
            detail={
                "code": "no_model_selected",
                "message": ("Select a model in the top bar before finalizing a sub-session."),
            },
        )

    # Ownership check (reuses same pattern as /sub-session/stream).
    async with request.app.state.streaming_service._engine.connect() as conn:
        from sqlalchemy import select as _select

        from lmchat.db.schema import chats as _chats

        row = (
            await conn.execute(
                _select(_chats.c.id).where(
                    _chats.c.id == chat_id,
                    _chats.c.user_id == user.id,
                )
            )
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=_HTTP_404, detail="chat not found")

    try:
        messages: list[dict[str, str]] = json.loads(messages_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=_HTTP_422, detail="invalid messages_json") from exc

    # Append the finalization directive.
    finalize_messages = [*messages, {"role": "user", "content": _SUB_SESSION_FINALIZE_PROMPT}]

    lm_client: LmstudioStreamingClient = request.app.state.lm_streaming_client

    # Cloud-provider routing for finalize (mirror of the equivalent
    # block in sub_session_stream). Resolve the effective provider FIRST — the
    # cloud model id (e.g. "openai/gpt-4o-mini") must reach the cloud provider
    # verbatim; running it through LM Studio's loaded-instance resolution below
    # would 422 ("no model loaded") or substitute a LOCAL loaded-instance id,
    # both of which corrupt the cloud request (this stranded cloud /research
    # finalize entirely). The finalize summary is tool-less, so this ONLY
    # routes the provider — no agentic/tool wrapping (unlike the stream path).
    _effective_provider = (provider or "").strip()
    _dispatch_client = lm_client  # default: LM Studio path unchanged
    _is_cloud = False
    if _effective_provider and _effective_provider != "lmstudio":
        _registry = getattr(request.app.state, "provider_registry", None)
        if _registry is not None:
            _resolved = _registry.get(_effective_provider)
            if _resolved is not None:
                from lmchat.services.lmstudio_streaming_client import (  # noqa: PLC0415
                    LmstudioStreamingClient as _LmstudioStreamingClient,
                )

                _dispatch_client = _LmstudioStreamingClient(adapter=_resolved)  # type: ignore[arg-type]
                _is_cloud = True
                log.info(
                    "sub_session_finalize.provider_routing",
                    chat_id=chat_id,
                    provider=_effective_provider,
                    model_id=model_id,
                )
            else:
                log.warning(
                    "sub_session_finalize.provider_unknown_fallback_lmstudio",
                    chat_id=chat_id,
                    provider=_effective_provider,
                )

    # Resolve stored model key → a LOADED loaded_instance_id, falling back to
    # another loaded LLM when the pinned model has idled out (same pattern as
    # sub_session_stream). Summarization is model-agnostic, so a finished
    # research run is never stranded just because its model unloaded.
    #
    # SKIPPED for cloud providers: the cloud model id must reach the cloud
    # provider verbatim; LM Studio resolution would 422 or substitute a local
    # loaded-instance id. Mirrors sub_session_stream + the main replay path.
    wire_model_id = model_id
    if not _is_cloud:
        try:
            _models_svc = request.app.state.models_service
            _res = await _models_svc.resolve_to_loaded_or_fallback(model_id)
            if _res.wire_id is None:
                raise HTTPException(
                    status_code=_HTTP_422,
                    detail=(
                        "No language model is loaded in LM Studio. Load a model "
                        "(or enable JIT loading) and try again."
                    ),
                )
            wire_model_id = _res.wire_id
            if _res.substituted:
                log.info(
                    "sub_session_finalize.model_substituted",
                    chat_id=chat_id,
                    requested=model_id,
                    used=_res.fallback_key,
                )
            elif wire_model_id != model_id:
                log.info(
                    "sub_session_finalize.model_id_resolved",
                    chat_id=chat_id,
                    stored_key=model_id,
                    wire_id=wire_model_id,
                )
        except HTTPException:
            raise
        except Exception as _exc:  # noqa: BLE001
            log.warning(
                "sub_session_finalize.model_resolve_failed",
                model_id=model_id,
                error=str(_exc),
            )

    return StreamingResponse(
        _sub_session_sse(
            lm_client=_dispatch_client,
            model_id=wire_model_id,
            system_prompt=system_prompt,
            messages=finalize_messages,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


class InjectMessageRequest(BaseModel):
    """Body for POST /api/chats/{id}/inject-message."""

    model_config = ConfigDict(strict=True)
    content: str
    model_id: str | None = None


@router.post(
    "/{chat_id}/inject-message",
    status_code=201,
    responses={
        _HTTP_400: {"description": "Malformed request body — unparseable JSON"},
    },
)
async def inject_message(
    chat_id: int,
    body: InjectMessageRequest,
    user: User = Depends(require_user),
    message_service: object = Depends(_get_message_service),
) -> dict[str, object]:
    """Inject a pre-generated assistant message into a chat's main thread.

    Used by the sub-session frontend to add the finalized summary to the
    main chat context after the sub-session completes.
    """
    from lmchat.services.message_service import MessageNotFoundError

    svc: MessageService = message_service  # type: ignore[assignment]
    try:
        msg = await svc.append(
            chat_id=chat_id,
            user_id=user.id,
            role="assistant",
            content=body.content,
            model_id=body.model_id,
        )
    except MessageNotFoundError as exc:
        raise HTTPException(status_code=_HTTP_404, detail="chat not found") from exc

    return {"id": msg.id, "chat_id": chat_id, "role": "assistant", "content": body.content}
