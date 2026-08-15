# SPDX-License-Identifier: Apache-2.0
"""Projects CRUD routes.

Endpoints
---------
POST   /api/projects             — create a new project for the caller.
GET    /api/projects             — list every project owned by the caller.
GET    /api/projects/{id}        — fetch one project (ownership-bounded).
PATCH  /api/projects/{id}        — update name / description /
                                   system_prompt / default_model_id /
                                   rag_threshold.
DELETE /api/projects/{id}        — delete; ON DELETE SET NULL cascades
                                   on chats / documents / memory_insights
                                   (children survive as un-projected).
POST   /api/projects/{id}/archive   — soft-archive; children
                                       untouched, drops out of the
                                       default sidebar/list.
POST   /api/projects/{id}/unarchive — reverse of the above.
GET    /api/projects/{id}/knowledge-stats — KB capacity meter numbers.
GET    /api/projects/{id}/export    — portable JSON backup bundle.
POST   /api/projects/{id}/regenerate-summary — regenerate the rolling
                                       auto-summary.

All endpoints require ``Depends(require_user)`` and scope by ``user.id``
through ``ProjectsService`` (the service returns ``None`` on cross-user
access; the route maps that to 404 without leaking project existence).

Wire contract follows the v1 form-encoded convention for mutations,
mirroring ``routes/auth.py`` PATCH /profile. PATCH supports a ``clear=``
comma-separated list naming fields to clear ("description",
"system_prompt", "default_model_id", "rag_threshold") — empty form
fields are ambiguous with omitted ones under FastAPI's Form handling.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

from fastapi import (  # noqa: F401
    APIRouter,
    Depends,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from pydantic import BaseModel

from lmchat.logging import get_logger
from lmchat.routes._dependencies import require_user
from lmchat.routes._form_utils import parse_clear
from lmchat.services.auth_service import User
from lmchat.services.projects_service import (
    InvalidProjectFieldError,
    Project,
    ProjectsService,
)

log = get_logger(__name__)


_HTTP_201: Final[int] = 201
_HTTP_204: Final[int] = 204
_HTTP_404: Final[int] = 404
_HTTP_422: Final[int] = 422

_CLEARABLE_FIELDS: Final[frozenset[str]] = frozenset(
    # ``default_model_id`` / ``rag_threshold`` are both nullable columns
    # where NULL is itself meaningful ("use the global default" / "use
    # the formula"), so they clear via this list rather than an
    # empty-string sentinel.
    {"description", "system_prompt", "default_model_id", "rag_threshold"}
)


# ---------------------------------------------------------------------------
# DI
# ---------------------------------------------------------------------------


def _get_projects_service(request: Request) -> ProjectsService:
    """Resolve ``app.state.projects_service`` or raise.

    Tests bypassing the lifespan must override this dependency.
    """
    svc = getattr(request.app.state, "projects_service", None)
    if svc is None:
        raise RuntimeError(
            "app.state.projects_service is unset — the FastAPI lifespan "
            "did not run, and no dependency_overrides entry exists for "
            "_get_projects_service."
        )
    return svc  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Wire models
# ---------------------------------------------------------------------------


class ProjectResponse(BaseModel):
    """Wire shape returned to the frontend.

    Mirrors :class:`Project` exactly — fields kept in lockstep so the
    response model never silently drops a column added to the schema.
    """

    id: int
    user_id: int
    name: str
    description: str
    system_prompt: str
    created_at: float
    updated_at: float
    # Embedding + default-model surfaces.
    embedding_model_id: str | None = None
    default_model_id: str | None = None
    # RAG-mode threshold override surface.
    rag_threshold: int | None = None
    # NULL = active project.
    archived_at: float | None = None
    # Rolling auto-summary. "" = none generated
    # yet; summary_updated_at is None until the first regeneration.
    summary: str = ""
    summary_updated_at: float | None = None

    @classmethod
    def from_project(cls, p: Project) -> ProjectResponse:
        return cls(
            id=p.id,
            user_id=p.user_id,
            name=p.name,
            description=p.description,
            system_prompt=p.system_prompt,
            created_at=p.created_at,
            updated_at=p.updated_at,
            embedding_model_id=getattr(p, "embedding_model_id", None),
            default_model_id=getattr(p, "default_model_id", None),
            rag_threshold=getattr(p, "rag_threshold", None),
            archived_at=getattr(p, "archived_at", None),
            summary=getattr(p, "summary", "") or "",
            summary_updated_at=getattr(p, "summary_updated_at", None),
        )


class KnowledgeStatsResponse(BaseModel):
    """Wire shape for ``GET /api/projects/{id}/knowledge-stats``.

    Powers the Documents tab's KB capacity meter: "~X tokens of
    knowledge · Y% of the inline threshold."
    """

    corpus_tokens: int
    threshold: int
    ctx_window: int


class ExportedMessage(BaseModel):
    """One message inside a ``ProjectExportResponse`` chat."""

    role: str
    content: str
    reasoning_content: str | None
    created_at: str  # ISO 8601


class ExportedChat(BaseModel):
    """One chat inside a ``ProjectExportResponse`` bundle."""

    id: int
    title: str
    created_at: str  # ISO 8601
    messages: list[ExportedMessage]


class ExportedDocument(BaseModel):
    """One document inside a ``ProjectExportResponse`` bundle.

    ``text`` is the full extracted text, reconstructed from
    ``document_chunks`` in ordinal order — NEVER the embedding vectors.
    """

    id: int
    title: str
    mime_type: str
    byte_size: int
    sha256: str
    uploaded_at: str  # ISO 8601
    text: str


class ProjectExportBundle(BaseModel):
    """The project fields carried by ``ProjectExportResponse``."""

    name: str
    description: str
    system_prompt: str
    default_model_id: str | None
    rag_threshold: int | None
    embedding_model_id: str | None


class ProjectExportResponse(BaseModel):
    """Wire shape for ``GET /api/projects/{id}/export``.

    A portable backup/handoff artifact — NOT a multi-user sharing
    surface (this is a single-admin app). Never includes raw embedding
    vectors; documents carry re-extracted text, not chunk embeddings.
    """

    exported_at: str  # ISO 8601
    project: ProjectExportBundle
    documents: list[ExportedDocument]
    chats: list[ExportedChat]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_clear(raw: str | None) -> frozenset[str]:
    """Parse the ``clear=`` comma-list for the project PATCH route.

    Delegates to the shared parser in
    ``routes/_form_utils.py`` so this module's implementation is the
    SAME as the chat / document PATCH route parsers — no per-route
    drift.
    """
    return parse_clear(raw, allowed=_CLEARABLE_FIELDS)


async def _export_all_messages(
    message_service: Any, chat_id: int, user_id: int
) -> list[Any]:
    """Fetch every message in *chat_id*, oldest-first (for export).

    ``MessageService.list_for_chat`` is paginated (the ``GET
    /api/chats/{id}`` route caps it at 500/page) — export needs the
    FULL history, so this loops the ``before_id`` cursor until
    ``has_more`` is False rather than trusting a single oversized limit.
    """
    all_messages: list[Any] = []
    before_id: int | None = None
    while True:
        page, has_more = await message_service.list_for_chat(
            chat_id, user_id=user_id, limit=500, before_id=before_id
        )
        if not page:
            break
        all_messages = page + all_messages
        if not has_more:
            break
        before_id = page[0].id
    return all_messages


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


router: APIRouter = APIRouter(prefix="/api/projects", tags=["projects"])


@router.post("", response_model=ProjectResponse, status_code=_HTTP_201)
async def create_project(
    name: str = Form(...),
    description: str = Form(default=""),
    system_prompt: str = Form(default=""),
    user: User = Depends(require_user),
    svc: ProjectsService = Depends(_get_projects_service),
) -> ProjectResponse:
    """Create a new project owned by the caller."""
    log.info("projects.create.request", user_id=user.id, name=name)
    try:
        project = await svc.create(
            user_id=user.id,
            name=name,
            description=description,
            system_prompt=system_prompt,
        )
    except InvalidProjectFieldError as exc:
        raise HTTPException(status_code=_HTTP_422, detail=str(exc)) from exc
    return ProjectResponse.from_project(project)


@router.get("", response_model=list[ProjectResponse])
async def list_projects(
    include_archived: bool = False,
    user: User = Depends(require_user),
    svc: ProjectsService = Depends(_get_projects_service),
) -> list[ProjectResponse]:
    """List projects owned by the caller.

    ``include_archived`` defaults to False, matching the
    sidebar's default view; pass True to also fetch archived projects
    (the all-projects landing page's "Archived" section).
    """
    projects = await svc.list_for_user(
        user_id=user.id, include_archived=include_archived
    )
    return [ProjectResponse.from_project(p) for p in projects]


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(
    project_id: int,
    user: User = Depends(require_user),
    svc: ProjectsService = Depends(_get_projects_service),
) -> ProjectResponse:
    """Fetch one project; 404 if not owned by the caller."""
    project = await svc.get(user_id=user.id, project_id=project_id)
    if project is None:
        raise HTTPException(status_code=_HTTP_404, detail="project not found")
    return ProjectResponse.from_project(project)


@router.patch("/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: int,
    name: str | None = Form(default=None),
    description: str | None = Form(default=None),
    system_prompt: str | None = Form(default=None),
    default_model_id: str | None = Form(default=None),
    rag_threshold: int | None = Form(default=None),
    clear: str | None = Form(default=None),
    user: User = Depends(require_user),
    svc: ProjectsService = Depends(_get_projects_service),
) -> ProjectResponse:
    """PATCH project fields.

    Field semantics:
        - Omitted form field → leave the column untouched.
        - Non-empty form field → set the column to the new value.
        - To clear ``description`` / ``system_prompt`` /
          ``default_model_id`` / ``rag_threshold``, name the field
          in the ``clear=`` comma-separated list. (Empty form fields
          are ambiguous with omitted ones under FastAPI's Form
          handling — this matches the auth profile route.)

    Returns the updated project; 404 if not owned by the caller.
    """
    clear_set = _parse_clear(clear)
    # FastAPI's Form(default=None) coerces explicit empty form fields
    # to None — so at this layer, "" and "omitted" are indistinguishable.
    # Treat both as "don't touch" for every field. To clear a clearable
    # field, use the `clear=` list. `name` cannot be cleared (it's
    # required-non-empty per the service contract).
    name_param: str | None = name if name not in (None, "") else None
    description_param: str | None = (
        "" if "description" in clear_set else (
            description if description not in (None, "") else None
        )
    )
    system_prompt_param: str | None = (
        "" if "system_prompt" in clear_set else (
            system_prompt if system_prompt not in (None, "") else None
        )
    )
    default_model_id_param: str | None = (
        default_model_id if default_model_id not in (None, "") else None
    )

    try:
        project = await svc.update(
            user_id=user.id,
            project_id=project_id,
            name=name_param,
            description=description_param,
            system_prompt=system_prompt_param,
            default_model_id=default_model_id_param,
            rag_threshold=rag_threshold,
            clear=clear_set,
        )
    except InvalidProjectFieldError as exc:
        raise HTTPException(status_code=_HTTP_422, detail=str(exc)) from exc
    if project is None:
        raise HTTPException(status_code=_HTTP_404, detail="project not found")
    return ProjectResponse.from_project(project)


@router.delete("/{project_id}", status_code=_HTTP_204)
async def delete_project(
    project_id: int,
    user: User = Depends(require_user),
    svc: ProjectsService = Depends(_get_projects_service),
) -> Response:
    """Delete the caller's project.

    ON DELETE SET NULL on the three child FKs (chats / documents /
    memory_insights) leaves the children intact as un-projected.
    """
    deleted = await svc.delete(user_id=user.id, project_id=project_id)
    if not deleted:
        raise HTTPException(status_code=_HTTP_404, detail="project not found")
    return Response(status_code=_HTTP_204)


@router.post("/{project_id}/archive", response_model=ProjectResponse)
async def archive_project(
    project_id: int,
    user: User = Depends(require_user),
    svc: ProjectsService = Depends(_get_projects_service),
) -> ProjectResponse:
    """Archive the caller's project.

    Soft — chats / documents attached to the project are untouched;
    the project just drops out of the default sidebar/list until
    unarchived. 404 if not owned by the caller.
    """
    project = await svc.set_archived(
        user_id=user.id, project_id=project_id, archived=True
    )
    if project is None:
        raise HTTPException(status_code=_HTTP_404, detail="project not found")
    return ProjectResponse.from_project(project)


@router.post("/{project_id}/unarchive", response_model=ProjectResponse)
async def unarchive_project(
    project_id: int,
    user: User = Depends(require_user),
    svc: ProjectsService = Depends(_get_projects_service),
) -> ProjectResponse:
    """Unarchive the caller's project. 404 if not owned."""
    project = await svc.set_archived(
        user_id=user.id, project_id=project_id, archived=False
    )
    if project is None:
        raise HTTPException(status_code=_HTTP_404, detail="project not found")
    return ProjectResponse.from_project(project)


@router.post("/{project_id}/regenerate-summary", response_model=ProjectResponse)
async def regenerate_project_summary(
    request: Request,
    project_id: int,
    user: User = Depends(require_user),
    svc: ProjectsService = Depends(_get_projects_service),
) -> ProjectResponse:
    """Regenerate the rolling auto-summary for *project_id*.

    Runs the OOB summarizer synchronously (an explicit user action, so
    unlike the throttled post-turn auto-refresh, this always
    regenerates regardless of how little has changed). Gathers a bounded
    slice of the project's recent conversation content across all its
    chats, folds it together with the existing summary via the
    admin-pinned background model, and persists the result.

    Fail-soft: ``project_summary_service.refresh_project_summary`` never
    raises — if the OOB call fails or no model is loaded, the project's
    existing summary (unchanged) is returned rather than an error.

    Returns 404 when the project is unknown or not owned by the caller.
    """
    from lmchat.routes._dependencies import get_engine_dep  # noqa: PLC0415
    from lmchat.services.project_summary_service import (  # noqa: PLC0415
        refresh_project_summary,
    )

    lm_client = getattr(request.app.state, "lm_streaming_client", None)
    if lm_client is None:
        raise RuntimeError("app.state.lm_streaming_client is unset")
    models_service = getattr(request.app.state, "models_service", None)

    project = await refresh_project_summary(
        engine=get_engine_dep(request),
        projects_service=svc,
        lm_client=lm_client,
        models_service=models_service,
        user_id=user.id,
        project_id=project_id,
    )
    if project is None:
        raise HTTPException(status_code=_HTTP_404, detail="project not found")
    return ProjectResponse.from_project(project)


@router.get(
    "/{project_id}/knowledge-stats", response_model=KnowledgeStatsResponse
)
async def get_project_knowledge_stats(
    request: Request,
    project_id: int,
    user: User = Depends(require_user),
    svc: ProjectsService = Depends(_get_projects_service),
) -> KnowledgeStatsResponse:
    """KB capacity meter numbers for *project_id*.

    Reuses the same estimator + threshold formula as the per-chat
    ``GET /api/chats/{id}/rag_mode`` route
    (``documents_service._estimate_project_corpus_tokens`` +
    ``rag_mode_resolver.compute_rag_threshold``) so the Documents tab's
    meter and the RAG-mode badge never disagree about the numbers.

    Returns 404 when the project is unknown or not owned by the caller.
    """
    from lmchat.routes._dependencies import get_engine_dep  # noqa: PLC0415
    from lmchat.services.documents_service import (  # noqa: PLC0415
        _estimate_project_corpus_tokens,
    )
    from lmchat.services.rag_mode_resolver import (  # noqa: PLC0415
        compute_rag_threshold,
    )

    project = await svc.get(user_id=user.id, project_id=project_id)
    if project is None:
        raise HTTPException(status_code=_HTTP_404, detail="project not found")

    engine = get_engine_dep(request)
    corpus_tokens = await _estimate_project_corpus_tokens(
        engine=engine, user_id=user.id, project_id=project_id
    )
    # ctx_window is unknown at this layer without a live chat's resolved
    # model — same 131K anchor as get_chat_rag_mode / rag_service's
    # project-level fallback.
    ctx_window = 131_000
    threshold = compute_rag_threshold(
        ctx_window=ctx_window, override=project.rag_threshold
    )
    return KnowledgeStatsResponse(
        corpus_tokens=corpus_tokens,
        threshold=threshold,
        ctx_window=ctx_window,
    )


@router.get("/{project_id}/export", response_model=ProjectExportResponse)
async def export_project(
    request: Request,
    project_id: int,
    user: User = Depends(require_user),
    svc: ProjectsService = Depends(_get_projects_service),
) -> ProjectExportResponse:
    """Portable JSON backup/handoff bundle for *project_id*.

    NOT a multi-user sharing surface — this is a single-admin app; the
    bundle is meant for local backup or migrating a project elsewhere.
    Includes:

    - the project itself (name / description / system_prompt /
      default_model_id / rag_threshold / embedding_model_id)
    - its documents, with FULL extracted text reconstructed from
      ``document_chunks`` (never the embedding vectors)
    - its chats, each with every message (role / content /
      reasoning_content / created_at), oldest-first

    Returns 404 when the project is unknown or not owned by the caller.
    """
    from lmchat.routes._dependencies import get_engine_dep  # noqa: PLC0415
    from lmchat.services.documents_service import (  # noqa: PLC0415
        get_document_chunks,
        list_documents,
    )

    project = await svc.get(user_id=user.id, project_id=project_id)
    if project is None:
        raise HTTPException(status_code=_HTTP_404, detail="project not found")

    engine = get_engine_dep(request)

    docs = await list_documents(
        user_id=user.id, engine=engine, project_id=project_id
    )
    exported_documents: list[ExportedDocument] = []
    for doc in docs:
        chunks = await get_document_chunks(
            document_id=doc.id,
            user_id=user.id,
            engine=engine,
            full_text=True,
        )
        text = "\n\n".join(str(c["text"]) for c in chunks)
        exported_documents.append(
            ExportedDocument(
                id=doc.id,
                title=doc.title,
                mime_type=doc.mime_type,
                byte_size=doc.byte_size,
                sha256=doc.sha256,
                uploaded_at=doc.uploaded_at.isoformat(),
                text=text,
            )
        )

    chat_service = getattr(request.app.state, "chat_service", None)
    message_service = getattr(request.app.state, "message_service", None)
    if chat_service is None or message_service is None:
        raise RuntimeError(
            "app.state.chat_service / message_service is unset"
        )

    chats = await chat_service.list_for_user(user.id, project_id=project_id)
    exported_chats: list[ExportedChat] = []
    for chat in chats:
        msgs = await _export_all_messages(message_service, chat.id, user.id)
        exported_chats.append(
            ExportedChat(
                id=chat.id,
                title=chat.title,
                created_at=chat.created_at.isoformat(),
                messages=[
                    ExportedMessage(
                        role=m.role,
                        content=m.content,
                        reasoning_content=m.reasoning_content,
                        created_at=m.created_at.isoformat(),
                    )
                    for m in msgs
                ],
            )
        )

    return ProjectExportResponse(
        exported_at=datetime.now(UTC).isoformat(),
        project=ProjectExportBundle(
            name=project.name,
            description=project.description,
            system_prompt=project.system_prompt,
            default_model_id=project.default_model_id,
            rag_threshold=project.rag_threshold,
            embedding_model_id=project.embedding_model_id,
        ),
        documents=exported_documents,
        chats=exported_chats,
    )


# ---------------------------------------------------------------------------
# Project-scoped child collections
# ---------------------------------------------------------------------------
#
# These routes create a chat / upload a document INSIDE a project, so the
# child row carries the project's ``project_id`` from the moment it's
# persisted (rather than being created un-projected then moved via
# ``PATCH /api/chats/{id}`` or ``PATCH /api/documents/{id}``).
#
# Ownership is enforced via ``_require_owned_project`` before any write —
# unknown or cross-user ``project_id`` always returns 404, never 200-empty
# or 201-with-detached-child.


async def _require_owned_project(
    *, user: User, project_id: int, svc: ProjectsService
) -> None:
    """Reject the request with 404 unless the project is owned by *user*.

    The helper lives here rather than in ``ProjectsService`` so the
    route layer owns the HTTP semantics; the service stays HTTP-agnostic.

    **TOCTOU note (documented disposition):**
    There is a small window between this check and the downstream
    write where a concurrent ``DELETE /api/projects/{project_id}``
    could remove the project. The downstream write would then either
    (a) succeed and the FK's ``ON DELETE SET NULL`` cascade flips the
    just-written ``project_id`` back to NULL — yielding an
    un-projected child, which IS the documented behavior for
    project deletion (chats / docs / insights survive), OR (b) the
    DB layer raises a foreign-key violation if isolation puts the
    INSERT and the DELETE in the wrong order. Either outcome is
    acceptable for v1.0: no cross-user leak is possible (the
    DELETE was issued by the SAME owner) and the orphaned-row
    semantics match a normal project delete. A future v1.x can
    wrap the check + write in a single transaction if the rare
    self-race becomes a UX issue.
    """
    if (
        await svc.get(user_id=user.id, project_id=project_id)
        is None
    ):
        raise HTTPException(
            status_code=_HTTP_404, detail="project not found"
        )


@router.post(
    "/{project_id}/chats",
    status_code=_HTTP_201,
)
async def create_chat_in_project(
    request: Request,
    project_id: int,
    # Optional + defaulted (mirrors the main POST /api/chats flow): a blank
    # title from the "New chat" box previously 422'd with a raw error toast
    # instead of just creating an untitled chat.
    title: str | None = Form(default=None),
    incognito: bool = Form(default=False),
    user: User = Depends(require_user),
    svc: ProjectsService = Depends(_get_projects_service),
):  # noqa: ANN201  # ChatResponse imported lazily to avoid a cycle
    """Create a chat inside *project_id*.

    Equivalent to ``POST /api/chats`` followed by ``PATCH /api/chats/{id}``
    with the project_id move, but in a single round-trip and with the
    child row carrying the project_id from the first INSERT.

    Returns the ``ChatResponse``-shaped body of the created chat
    (matches the contract of ``POST /api/chats``
    exactly so the frontend ``useChats`` hook gets the same shape from
    both code paths).

    Returns 404 when the project is unknown or not owned by the
    caller.
    """
    # Lazy import — routes/chats.py imports from routes/projects.py
    # transitively via routes/_dependencies.py, so eager `from
    # lmchat.routes.chats import ChatResponse` would form a cycle.
    from lmchat.routes.chats import ChatResponse  # noqa: PLC0415

    # Fetch the project so we can
    # read ``default_model_id`` and seed the new chat's ``model_id``
    # from it. ``svc.get`` doubles as the ownership check (None → 404)
    # so we replace the ``_require_owned_project`` call site here
    # rather than do two SELECTs against the same row.
    project = await svc.get(user_id=user.id, project_id=project_id)
    if project is None:
        raise HTTPException(
            status_code=_HTTP_404, detail="project not found"
        )
    chat_svc = getattr(
        request.app.state, "chat_service", None
    )
    streaming_svc = getattr(
        request.app.state, "streaming_service", None
    )
    if chat_svc is None or streaming_svc is None:
        raise RuntimeError(
            "app.state.chat_service / streaming_service is unset"
        )
    _title = (title or "").strip() or "New Chat"
    chat = await chat_svc.create(
        user_id=user.id,
        title=_title,
        incognito=incognito,
        project_id=project_id,
        # NULL falls through to the user's global default at
        # stream time; existing behavior preserved.
        model_id=getattr(project, "default_model_id", None),
    )
    streaming_svc.reset_counter(chat.id)
    return ChatResponse.model_validate(chat.model_dump())


# POST /api/projects/{id}/documents
#
# To avoid duplicating the
# multipart body-handling pipeline from POST /api/documents (size
# limits, magic-byte validation, MIME detection, dedup), this route
# is a thin shim that calls into the existing upload route with the
# project_id query param. Frontend can use either URL shape; both
# end up at the same code path with the same ownership check.


@router.post("/{project_id}/re-embed")
async def re_embed_project_route(
    request: Request,
    project_id: int,
    user: User = Depends(require_user),
    svc: ProjectsService = Depends(_get_projects_service),
) -> dict[str, int | str]:
    """Re-embed every document in *project_id* under the active model.

    Used when the admin swapped the active embedding model after
    documents were already attached — the existing chunks are encoded
    under the OLD model and retrieval mis-cosines until they're
    re-embedded. This route rewrites the chunk embeddings and updates
    the project's pin.

    Returns ``{"documents_re_embedded": int, "chunks_re_embedded": int,
    "active_embedding_model_id": str}``.

    Raises:
        HTTPException: 404 when the project is unknown or not owned
            by the caller. 503 when no embedding model is currently
            loaded in LM Studio.
    """
    from lmchat.routes._dependencies import (  # noqa: PLC0415
        get_embedding_client_dep,
        get_engine_dep,
        get_models_service_dep,
    )
    from lmchat.services.documents_service import (  # noqa: PLC0415
        re_embed_project_documents,
    )

    await _require_owned_project(
        user=user, project_id=project_id, svc=svc
    )
    engine = get_engine_dep(request)
    embedding_client = get_embedding_client_dep(request)
    models_service = get_models_service_dep(request)
    try:
        return await re_embed_project_documents(
            user_id=user.id,
            project_id=project_id,
            engine=engine,
            embedding_client=embedding_client,
            models_service=models_service,
        )
    except RuntimeError as exc:
        # Surface "no embedding model loaded" as 503 so the FE can
        # display the LM Studio model-loading hint rather than a
        # generic 500.
        raise HTTPException(
            status_code=503,
            detail=str(exc),
        ) from exc


@router.post(
    "/{project_id}/documents",
    status_code=201,
    responses={400: {"description": "Invalid upload — malformed multipart body or missing file"}},
)
async def upload_document_to_project(
    request: Request,
    project_id: int,
    file: UploadFile,  # noqa: F821  # forward-ref to lazy import below
    user: User = Depends(require_user),
    svc: ProjectsService = Depends(_get_projects_service),
):  # noqa: ANN201
    """Upload a document into *project_id* (dedicated route shim).

    Delegates the multipart body handling to
    ``upload_document_route`` so the size limit, MIME validation,
    and dedup logic live ONCE. Returns the same ``UploadResponse``
    shape as the existing route.
    """
    from fastapi import UploadFile as _UploadFile  # noqa: PLC0415

    # Lazy import — routes/documents.py would form a cycle.
    from lmchat.routes._dependencies import (  # noqa: PLC0415
        get_embedding_client_dep,
        get_engine_dep,
        get_models_service_dep,
    )
    from lmchat.routes.documents import (  # noqa: PLC0415
        upload_document_route,
    )

    _ = _UploadFile  # keep the type hint alive
    await _require_owned_project(
        user=user, project_id=project_id, svc=svc
    )
    # Resolve the same deps the existing route's signature expects.
    engine = get_engine_dep(request)
    embedding_client = get_embedding_client_dep(request)
    models_service = get_models_service_dep(request)
    return await upload_document_route(
        request=request,
        file=file,
        project_id=project_id,
        user=user,
        engine=engine,
        embedding_client=embedding_client,
        models_service=models_service,
    )
