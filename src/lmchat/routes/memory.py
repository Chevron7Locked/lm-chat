# SPDX-License-Identifier: Apache-2.0
"""Memory routes for lm-chat — pin/unpin insights, list pins, reindex.

Routes
------
POST   /api/memory/pin              — pin a text insight (require_user).
DELETE /api/memory/pin/{insight_id} — unpin by ID (require_user).
GET    /api/memory/pins             — list user's pinned insights (require_user).
GET    /api/memory/auto             — list user's auto (distilled) memories (require_user).
POST   /api/memory/reindex          — kick a background reindex (require_admin).
GET    /api/memory/reindex/status   — poll reindex progress (require_admin).

Reindex background-task pattern
--------------------------------
The route handler defines a ``_run()`` closure that:
1. Marks the status holder as "running".
2. Calls ``await memory_service.reindex(embedding_model_id=...)`` which emits
   ``memory.reindex.start / .batch / .complete`` structured-log events.
3. Captures the final snapshot dict and updates the holder on completion.
4. On any exception the holder transitions to "failed".

Design decision: the holder is updated directly by
the closure rather than subscribing to structlog event emission.  This is
simpler and fits FastAPI lifecycle better — the holder is a plain Python
dataclass on ``app.state``; no multiprocessing or pub/sub required.  The
trade-off is that per-batch snapshots are NOT captured; only the start,
current-progress (updated after each batch is committed by reindex()), and
final snapshot are stored.  Per-batch snapshot would require wrapping or
monkey-patching structlog, which is disproportionately complex for this
use case.  The status endpoint reflects the last completed state — sufficient
for admin visibility.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Literal

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from lmchat.logging import get_logger
from lmchat.routes._dependencies import require_admin, require_user
from lmchat.services.auth_service import User
from lmchat.services.memory_service import (
    EMBEDDING_ERROR_REASON_PREFERRED_NOT_LOADED,
    HistoryNotFoundError,
    InsightNotFoundError,
    MemoryInsight,
    MemoryService,
    PinnedInsightsCapError,
    RefineUpstreamError,
)
from lmchat.utils.text_input_policy import (
    PIN_TEXT_MAX_LENGTH,
    TextInputPolicyError,
    validate_text,
)

if TYPE_CHECKING:
    pass

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# HTTP status constants
# ---------------------------------------------------------------------------

_HTTP_201: Final[int] = 201
_HTTP_202: Final[int] = 202
_HTTP_204: Final[int] = 204
_HTTP_400: Final[int] = 400
_HTTP_404: Final[int] = 404
_HTTP_409: Final[int] = 409
_HTTP_422: Final[int] = 422

# ---------------------------------------------------------------------------
# ReindexStatusHolder
# ---------------------------------------------------------------------------

ReindexState = Literal["idle", "running", "completed", "failed"]


class ReindexStatusSnapshot(BaseModel):
    """Point-in-time snapshot of a reindex operation.

    Attributes:
        state:             Current reindex state.
        embedding_model_id: Target embedding model key (absent when idle).
        processed:         Messages successfully processed so far.
        total:             Total messages to process.
        elapsed_s:         Elapsed seconds (monotonic) since start.
        failed:            Count of messages that failed embedding.
        started_at:        UTC timestamp when the reindex was kicked.
        completed_at:      UTC timestamp when the reindex finished (None if
                           still running or if it hasn't started).
    """

    model_config = ConfigDict(extra="forbid")

    state: ReindexState = "idle"
    embedding_model_id: str | None = None
    processed: int = 0
    total: int = 0
    elapsed_s: float = 0.0
    failed: int = 0
    started_at: datetime | None = None
    completed_at: datetime | None = None


class ReindexStatusHolder:
    """Thread-safe (asyncio-safe) holder for the latest reindex snapshot.

    Attached to ``app.state.reindex_status_holder`` at startup.  Updated
    by the reindex closure inside the POST /api/memory/reindex handler.

    Only one reindex can run at a time; the holder records the last one.
    """

    def __init__(self) -> None:
        self._snapshot: ReindexStatusSnapshot = ReindexStatusSnapshot()

    # ------------------------------------------------------------------
    # Write-path helpers (called from inside the reindex closure)
    # ------------------------------------------------------------------

    def start(
        self,
        *,
        embedding_model_id: str,
        total: int,
    ) -> None:
        """Mark reindex as running with initial totals.

        Args:
            embedding_model_id: The target embedding model key.
            total:              Total message count to process.
        """
        self._snapshot = ReindexStatusSnapshot(
            state="running",
            embedding_model_id=embedding_model_id,
            processed=0,
            total=total,
            elapsed_s=0.0,
            failed=0,
            started_at=datetime.now(UTC),
            completed_at=None,
        )

    def complete(
        self,
        *,
        snapshot_dict: dict[str, Any],
    ) -> None:
        """Record the final reindex completion snapshot.

        Args:
            snapshot_dict: The dict returned by ``memory_service.reindex()``.
                           Expected keys: ``embedding_model_id``, ``processed``,
                           ``total``, ``elapsed_s``, ``failed``.
        """
        self._snapshot = ReindexStatusSnapshot(
            state="completed",
            embedding_model_id=snapshot_dict.get("embedding_model_id"),
            processed=snapshot_dict.get("processed", 0),
            total=snapshot_dict.get("total", 0),
            elapsed_s=snapshot_dict.get("elapsed_s", 0.0),
            failed=snapshot_dict.get("failed", 0),
            started_at=self._snapshot.started_at,
            completed_at=datetime.now(UTC),
        )

    def fail(self, *, error: str) -> None:
        """Mark the reindex as failed.

        Args:
            error: A short error description (not propagated to the client;
                   stored for admin inspection via the status endpoint).
        """
        self._snapshot = ReindexStatusSnapshot(
            state="failed",
            embedding_model_id=self._snapshot.embedding_model_id,
            processed=self._snapshot.processed,
            total=self._snapshot.total,
            elapsed_s=self._snapshot.elapsed_s,
            failed=self._snapshot.failed,
            started_at=self._snapshot.started_at,
            completed_at=datetime.now(UTC),
        )
        log.warning(
            "memory.reindex.holder_failed",
            error=error,
            embedding_model_id=self._snapshot.embedding_model_id,
        )

    # ------------------------------------------------------------------
    # Read-path
    # ------------------------------------------------------------------

    @property
    def snapshot(self) -> ReindexStatusSnapshot:
        """Return the current snapshot (read-only)."""
        return self._snapshot

    @property
    def is_running(self) -> bool:
        """True when a reindex is in the ``running`` state."""
        return self._snapshot.state == "running"


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class ReindexStartResponse(BaseModel):
    """Response body for POST /api/memory/reindex.

    Attributes:
        ok:                 Always True on 202.
        embedding_model_id: The model that will be used for reindexing.
        task_status:        Always "started" on 202.
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool = True
    embedding_model_id: str
    task_status: Literal["started"] = "started"


# ---------------------------------------------------------------------------
# Dependency helpers (module-local; canonical versions live in _dependencies)
# ---------------------------------------------------------------------------


def _get_memory_service(request: Request) -> MemoryService:
    """Return ``app.state.memory_service``; raise ``RuntimeError`` if unset."""
    svc = getattr(request.app.state, "memory_service", None)
    if svc is None:
        raise RuntimeError(
            "app.state.memory_service is unset — lifespan did not run or "
            "no dependency_overrides entry registered."
        )
    return svc  # type: ignore[return-value]


def _get_projects_service(request: Request):  # noqa: ANN202
    """Return ``app.state.projects_service``; raise ``RuntimeError`` if unset.

    Routes that accept a ``project_id`` query param use this dep so
    they 404 on unknown / foreign IDs rather than silently returning
    an empty 200.
    """
    svc = getattr(request.app.state, "projects_service", None)
    if svc is None:
        raise RuntimeError(
            "app.state.projects_service is unset — lifespan did not run."
        )
    return svc


def _get_reindex_status_holder(request: Request) -> ReindexStatusHolder:
    """Return ``app.state.reindex_status_holder``; raise ``RuntimeError`` if unset."""
    holder = getattr(request.app.state, "reindex_status_holder", None)
    if holder is None:
        raise RuntimeError(
            "app.state.reindex_status_holder is unset — lifespan did not run or "
            "no dependency_overrides entry registered."
        )
    return holder  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router: APIRouter = APIRouter(prefix="/api/memory", tags=["memory"])


# ---------------------------------------------------------------------------
# POST /api/memory/pin
# ---------------------------------------------------------------------------


@router.post("/pin", response_model=MemoryInsight, status_code=_HTTP_201)
async def pin_insight(
    request: Request,
    text: str = Form(...),
    project_id: int | None = Form(default=None),
    user: User = Depends(require_user),
    memory_service: MemoryService = Depends(_get_memory_service),
) -> MemoryInsight:
    """Pin a text insight for the authenticated user.

    Enforces the per-user cap (``LM_CHAT_PINNED_INSIGHTS_CAP``).  Dedup:
    if the same normalized text is already pinned the existing row is
    returned (idempotent).

    Args:
        request:        FastAPI Request (used internally by dependencies).
        text:           Insight text to pin (form field).
        user:           Authenticated user from ``require_user``.
        memory_service: Injected ``MemoryService``.

    Returns:
        The new or existing :class:`~lmchat.services.memory_service.MemoryInsight`
        row.

    Raises:
        HTTPException: 400 if the user has reached the pinned-insights cap.
    """
    # Server-side text policy. The UI gates against empty, but the
    # API previously accepted whitespace-only, 500KB payloads, and
    # NUL bytes. Validate before the service call so we never persist
    # junk.
    try:
        text = validate_text(
            text,
            field="text",
            max_length=PIN_TEXT_MAX_LENGTH,
            allow_newlines=True,
            allow_tabs=True,
        )
    except TextInputPolicyError as exc:
        raise HTTPException(status_code=_HTTP_422, detail=str(exc)) from exc
    log.info(
        "memory.pin_insight.request",
        user_id=user.id,
        text_len=len(text),
    )
    # Ownership check when a project_id is supplied. ProjectsService
    # lives on app.state per the convention established in
    # routes/search.py.
    if project_id is not None:
        projects_svc = getattr(
            request.app.state, "projects_service", None
        )
        if projects_svc is None:
            raise RuntimeError(
                "app.state.projects_service is unset"
            )
        owned = await projects_svc.get(
            user_id=user.id, project_id=project_id
        )
        if owned is None:
            raise HTTPException(
                status_code=_HTTP_404, detail="project not found"
            )
    try:
        insight = await memory_service.pin_insight(
            user_id=user.id, text=text, project_id=project_id
        )
    except PinnedInsightsCapError as exc:
        raise HTTPException(
            status_code=_HTTP_400,
            detail="pinned insights cap exceeded",
        ) from exc
    return insight


# ---------------------------------------------------------------------------
# DELETE /api/memory/pin/{insight_id}
# ---------------------------------------------------------------------------


@router.delete("/pin/{insight_id}", status_code=_HTTP_204)
async def unpin_insight(
    insight_id: int,
    request: Request,
    user: User = Depends(require_user),
    memory_service: MemoryService = Depends(_get_memory_service),
) -> None:
    """Delete a pinned insight belonging to the authenticated user.

    Returns 204 on success.  Returns 404 if the insight does not exist or
    does not belong to this user.

    Ownership check: the route explicitly verifies that the insight belongs
    to the calling user before deleting.  ``memory_service.unpin_insight``
    deletes by PK without ownership enforcement (by design — it is a
    low-level primitive; the route is the authorization boundary).

    Args:
        insight_id:     PK of the insight to delete.
        request:        FastAPI Request (dependency access).
        user:           Authenticated user.
        memory_service: Injected ``MemoryService``.

    Raises:
        HTTPException: 404 if the insight is not found or not owned by user.
    """
    # Ownership is enforced by the service: ``unpin_insight`` deletes by
    # (id AND user_id) and raises InsightNotFoundError on rowcount 0, so a
    # foreign or missing id can never delete another user's row. This covers
    # BOTH manually-pinned insights and AUTO (distilled, pinned=False)
    # memories — the admin can remove an auto-saved memory from /memory
    # the same way they unpin a manual one. (The earlier list_pinned-only
    # ownership scan rejected auto-memory deletes with a spurious 404.)
    try:
        await memory_service.unpin_insight(insight_id, user_id=user.id)
    except InsightNotFoundError as exc:
        raise HTTPException(
            status_code=_HTTP_404,
            detail="insight not found",
        ) from exc
    log.info(
        "memory.unpin_insight.ok",
        user_id=user.id,
        insight_id=insight_id,
    )


# ---------------------------------------------------------------------------
# GET /api/memory/pins
# ---------------------------------------------------------------------------


@router.get("/pins", response_model=list[MemoryInsight])
async def list_pins(
    request: Request,
    project_id: int | None = None,
    user: User = Depends(require_user),
    memory_service: MemoryService = Depends(_get_memory_service),
    projects_service=Depends(_get_projects_service),
) -> list[MemoryInsight]:
    """Return pinned insights for the authenticated user, newest first.

    Args:
        request:          FastAPI Request.
        project_id:       When set, restrict to pins tagged with this
                          project_id. Default (omitted) preserves
                          existing behavior: every pin the user owns
                          regardless of project.
        user:             Authenticated user.
        memory_service:   Injected ``MemoryService``.
        projects_service: Injected ``ProjectsService`` for ownership
                          check on the optional ``project_id`` query
                          param (foreign IDs must 404, not silently
                          200-empty).

    Returns:
        List of :class:`~lmchat.services.memory_service.MemoryInsight`.

    Raises:
        HTTPException: 404 when ``project_id`` is set but the project
            is unknown or not owned by the caller.
    """
    if project_id is not None:
        project = await projects_service.get(
            user_id=user.id, project_id=project_id
        )
        if project is None:
            raise HTTPException(
                status_code=404, detail="project not found"
            )
    return await memory_service.list_pinned(
        user_id=user.id, project_id=project_id
    )


@router.get("/auto", response_model=list[MemoryInsight])
async def list_auto(
    request: Request,
    project_id: int | None = None,
    user: User = Depends(require_user),
    memory_service: MemoryService = Depends(_get_memory_service),
    projects_service=Depends(_get_projects_service),
) -> list[MemoryInsight]:
    """Return AUTO (distilled) insights for the authenticated user.

    These are the machine-extracted durable user-profile facts produced by
    the post-turn distillation pass (``pinned = False``, ``state = 'active'``)
    — automatically-saved long-term memory, distinct from insights the
    user pins by hand. They are surfaced on /memory alongside (and
    distinct from) admin-pinned insights.

    Args:
        request:          FastAPI Request.
        project_id:       When set, restrict to auto-memories tagged with this
                          project_id (404 if the project is unknown / not
                          owned). Default returns every auto-memory the user
                          owns.
        user:             Authenticated user.
        memory_service:   Injected ``MemoryService``.
        projects_service: Injected ``ProjectsService`` for the optional
                          project_id ownership check.

    Returns:
        List of :class:`~lmchat.services.memory_service.MemoryInsight`,
        newest first.

    Raises:
        HTTPException: 404 when ``project_id`` is set but the project is
            unknown or not owned by the caller.
    """
    if project_id is not None:
        project = await projects_service.get(
            user_id=user.id, project_id=project_id
        )
        if project is None:
            raise HTTPException(
                status_code=_HTTP_404, detail="project not found"
            )
    return await memory_service.list_auto(
        user_id=user.id, project_id=project_id
    )


# ---------------------------------------------------------------------------
# POST /api/memory/reindex
# ---------------------------------------------------------------------------


@router.post(
    "/reindex",
    response_model=ReindexStartResponse,
    status_code=_HTTP_202,
)
async def start_reindex(
    request: Request,
    embedding_model_id: str = Form(...),
    _admin: User = Depends(require_admin),
    memory_service: MemoryService = Depends(_get_memory_service),
    status_holder: ReindexStatusHolder = Depends(_get_reindex_status_holder),
) -> ReindexStartResponse:
    """Kick a background reindex of all message embeddings.

    Admin-only.  Accepts a form field ``embedding_model_id`` specifying
    the LM Studio embedding model to use.  The reindex runs as an
    ``asyncio.Task``; the route returns 202 immediately.

    Returns 409 Conflict if a reindex is already running.

    The task is stored on ``app.state.reindex_task`` so:
    - ``GET /api/memory/reindex/status`` can report progress.
    - The lifespan shutdown hook can cancel it gracefully.

    Args:
        request:            FastAPI Request.
        embedding_model_id: Target embedding model key (form field).
        _admin:             Admin user guard (not used directly).
        memory_service:     Injected ``MemoryService``.
        status_holder:      Injected ``ReindexStatusHolder``.

    Returns:
        :class:`ReindexStartResponse` with ``task_status="started"``.

    Raises:
        HTTPException: 409 if a reindex is already running.
    """
    # Check if a previous task is still running.
    existing_task: asyncio.Task[Any] | None = getattr(
        request.app.state, "reindex_task", None
    )
    if existing_task is not None and not existing_task.done():
        raise HTTPException(
            status_code=_HTTP_409,
            detail="a reindex is already running",
        )

    log.info(
        "memory.reindex.scheduled",
        embedding_model_id=embedding_model_id,
    )

    # Mark holder as running immediately (before the task starts) so the
    # status endpoint reflects "running" from the very first poll.
    status_holder.start(
        embedding_model_id=embedding_model_id,
        total=0,  # actual total is set by memory_service.reindex internally
    )

    async def _run() -> None:
        """Background reindex closure.

        Calls ``memory_service.reindex``, updates the status holder on
        completion or failure.
        """
        try:
            snapshot = await memory_service.reindex(
                embedding_model_id=embedding_model_id
            )
            status_holder.complete(snapshot_dict=snapshot)
            log.info(
                "memory.reindex.task_done",
                embedding_model_id=embedding_model_id,
                processed=snapshot.get("processed"),
                total=snapshot.get("total"),
                elapsed_s=snapshot.get("elapsed_s"),
            )
        except asyncio.CancelledError:
            log.info(
                "memory.reindex.task_cancelled",
                embedding_model_id=embedding_model_id,
            )
            status_holder.fail(error="task cancelled")
            raise
        except Exception as exc:  # noqa: BLE001
            log.error(
                "memory.reindex.task_error",
                embedding_model_id=embedding_model_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            status_holder.fail(error=str(exc))

    task = asyncio.create_task(_run(), name="memory_reindex")
    request.app.state.reindex_task = task

    return ReindexStartResponse(embedding_model_id=embedding_model_id)


# ---------------------------------------------------------------------------
# GET /api/memory/reindex/status
# ---------------------------------------------------------------------------


@router.get("/reindex/status", response_model=ReindexStatusSnapshot)
async def get_reindex_status(
    request: Request,
    _admin: User = Depends(require_admin),
    status_holder: ReindexStatusHolder = Depends(_get_reindex_status_holder),
) -> ReindexStatusSnapshot:
    """Return the latest reindex status snapshot.

    Admin-only.  Returns ``{"state": "idle", ...}`` if no reindex has run
    since startup.  Otherwise returns the last snapshot (running, completed,
    or failed) captured by the reindex closure.

    Args:
        request:        FastAPI Request.
        _admin:         Admin user guard.
        status_holder:  Injected ``ReindexStatusHolder``.

    Returns:
        The current :class:`ReindexStatusSnapshot`.
    """
    return status_holder.snapshot


# ---------------------------------------------------------------------------
# Memory edit + refine + restore
# ---------------------------------------------------------------------------


class RefineResponse(BaseModel):
    """Body for POST /api/memory/refine.

    Attributes:
        insights:   The new pinned-insight set after refine.
        history_id: PK of the ``memory_insights_history`` row written
                    before the destructive replace.  POST it back to
                    ``/api/memory/restore/{history_id}`` to undo.
        before_count: Number of insights pre-refine.
        after_count:  Number of insights post-refine.
    """

    model_config = ConfigDict(extra="forbid")

    insights: list[MemoryInsight]
    history_id: int
    before_count: int
    after_count: int


class EmbeddingStatus(BaseModel):
    """Visibility snapshot for the memory-indexing pipeline.

    Surfaces which embedding model the backend is using right now
    (auto-detected as the first loaded embedding model in LM Studio),
    how many messages have been indexed in total, and when the most
    recent indexed message was created. The Settings UI renders this
    so an admin can tell at a glance whether memory indexing is
    working — previously the embedding model was hidden behind a
    helper method with no UI surface.

    Adds the resolver sentinel so the Settings UI and the chat-level
    ``/api/chats/{id}/rag_mode`` endpoint draw from the same source of
    truth. ``embedding_status`` is one of ``"ok"`` (model loaded,
    retrieval will run) / ``"no_embedding_model"`` (no embedding model
    loaded) / ``"pinned_model_unavailable"`` (project-scoped OR a
    personally-preferred embedding model that isn't loaded — the two share
    the same shape: a specifically-configured model is unavailable, as
    opposed to nothing being loaded at all).

    ``active_model_error_reason`` preserves WHY
    ``active_model_id`` is None — the resolver used to collapse "preferred
    model not loaded" and "a generic resolver error" into the same bare
    ``None``, indistinguishable to the admin. ``write_failure_count`` /
    ``write_last_error`` surface the write-path (index
    on new messages) failure counter so repeated ``stream.memory_index_failed``
    log lines become visible here instead of log-only.
    """

    model_config = ConfigDict(extra="forbid")

    active_model_id: str | None = None
    active_model_error_reason: str | None = None
    loaded_embedding_models: list[str] = []
    total_indexed_messages: int = 0
    last_indexed_at: float | None = None
    models_in_use: dict[str, int] = {}
    embedding_status: str = "ok"
    write_failure_count: int = 0
    write_last_error: str | None = None


@router.get("/embedding/status", response_model=EmbeddingStatus)
async def get_embedding_status(
    request: Request,
    _user: User = Depends(require_user),
    memory_service: MemoryService = Depends(_get_memory_service),
) -> EmbeddingStatus:
    """Return the memory-indexing visibility snapshot for the calling user.

    Any authenticated user can read this (it doesn't expose other users'
    embeddings — only aggregate counts + the currently-detected model).

    Calls ``retrieval_service.resolve_embedding_model_status`` to get the
    sentinel code so the Settings UI and the chat-level
    ``/api/chats/{id}/rag_mode`` endpoint draw from one source of truth.

    ``active_model_id`` is aligned to the SAME resolver the index/recall
    path uses (``resolve_embedding_model_status``): when retrieval
    resolves to a loaded wire id, that id IS the active model.  Before
    this alignment the field came from ``embedding_status()``'s list
    order and could report a different model than the one actually
    embedding (e.g. ``bge-m3`` while indexing used ``nomic``).
    """
    snap = await memory_service.embedding_status()

    # Resolver sentinel + active wire id — user-scoped (project_id=None).
    embedding_status_code = "ok"
    resolved_active: str | None = None
    try:
        from lmchat.services.retrieval_service import (  # noqa: PLC0415
            resolve_embedding_model_status,
        )

        engine = getattr(request.app.state, "engine", None) or getattr(
            memory_service, "_engine", None
        )
        models_service = getattr(
            request.app.state, "models_service", None
        ) or getattr(memory_service, "_models_service", None)
        if engine is not None and models_service is not None:
            resolved_active, embedding_status_code = (
                await resolve_embedding_model_status(
                    project_id=None,
                    user_id=_user.id,
                    engine=engine,
                    models_service=models_service,
                )
            )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "memory.embedding_status.resolver_failed",
            error=str(exc),
        )
        # Fall back to inferring from active_model_id.
        if snap.get("active_model_id") is None:
            embedding_status_code = "no_embedding_model"

    # Align active_model_id with what retrieval actually resolved. The
    # resolver returns the loaded WIRE id (e.g. "...v1.5@q8_0") it embeds
    # under — the single source of truth for "which model is active". Only
    # override the snapshot value when the resolver produced a concrete id
    # (status "ok"); on "no_embedding_model" leave it None.
    if resolved_active is not None:
        snap["active_model_id"] = resolved_active
    elif embedding_status_code == "no_embedding_model":
        snap["active_model_id"] = None

    # A personally-preferred embedding model that isn't
    # currently loaded is a DIFFERENT failure than "nothing is loaded at
    # all" — the admin needs to load THEIR pinned model (or repick one in
    # Settings), not just load any embedder. resolve_embedding_model_status
    # can't make this distinction in the user-scoped path (project_id=None
    # means its own NoEmbeddingModelLoadedError handling collapses every
    # reason into "no_embedding_model"), but embedding_status()'s snapshot
    # preserved the reason — use it to refine the sentinel here.
    if (
        embedding_status_code == "no_embedding_model"
        and snap.get("active_model_error_reason")
        == EMBEDDING_ERROR_REASON_PREFERRED_NOT_LOADED
    ):
        embedding_status_code = "pinned_model_unavailable"

    snap["embedding_status"] = embedding_status_code
    return EmbeddingStatus(**snap)


@router.patch(
    "/insights/{insight_id}",
    response_model=MemoryInsight,
)
async def edit_insight_endpoint(
    insight_id: int,
    request: Request,
    content: str = Form(...),
    user: User = Depends(require_user),
    memory_service: MemoryService = Depends(_get_memory_service),
) -> MemoryInsight:
    """Edit the text body of a pinned insight.

    Args:
        insight_id:     PK of the insight to edit.
        request:        FastAPI Request.
        content:        Replacement insight text (form field).
        user:           Authenticated user.
        memory_service: Injected ``MemoryService``.

    Returns:
        The updated :class:`MemoryInsight`.

    Raises:
        HTTPException: 404 when the insight is missing or not owned.
        HTTPException: 400 when the body is empty after normalisation
            or duplicates another existing pinned insight.
    """
    try:
        return await memory_service.edit_insight(
            insight_id=insight_id,
            user_id=user.id,
            text=content,
        )
    except InsightNotFoundError as exc:
        raise HTTPException(status_code=_HTTP_404, detail="insight not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=_HTTP_400, detail=str(exc)) from exc


def _get_lmstudio_adapter(request: Request) -> Any:
    """Return ``app.state.lmstudio_adapter`` or raise ``RuntimeError``.

    Args:
        request: FastAPI Request.

    Returns:
        The application's :class:`LmstudioAdapter`.

    Raises:
        RuntimeError: When the lifespan did not register the adapter.
    """
    adapter = getattr(request.app.state, "lmstudio_adapter", None)
    if adapter is None:
        raise RuntimeError(
            "app.state.lmstudio_adapter is unset; the lifespan did not run"
        )
    return adapter


def _get_models_service(request: Request) -> Any:
    """Return ``app.state.models_service``."""
    svc = getattr(request.app.state, "models_service", None)
    if svc is None:
        raise RuntimeError(
            "app.state.models_service is unset; the lifespan did not run"
        )
    return svc


async def _lmstudio_refine_call(
    *,
    items: list[str],
    adapter: Any,
    models_service: Any,
) -> list[str]:
    """Default refine implementation — call LM Studio via the native surface.

    Builds a curator prompt, sends the items as a single user-turn payload,
    aggregates the streamed deltas into a complete text, and splits
    on newlines.

    Args:
        items:          Current pinned-insight texts.
        adapter:        :class:`LmstudioAdapter` from app.state.
        models_service: :class:`ModelsService` from app.state.

    Returns:
        List of refined insight strings (whitespace-trimmed).

    Raises:
        RuntimeError: When no chat-capable model is loaded.
    """
    from lmchat.lmstudio.types import (
        CanonicalChatRequest,
        CanonicalInputBlock,
    )

    # Find a chat-capable model.  Prefer the first non-embedding model.
    loaded = await models_service.list_loaded()
    chat_model: str | None = None
    for m in loaded:
        # Best-effort: skip obvious embedding models by id heuristics.
        ident = getattr(m, "id", None) or getattr(m, "key", None)
        if ident is None:
            continue
        ident_str = str(ident)
        if "embed" in ident_str.lower():
            continue
        chat_model = ident_str
        break
    if chat_model is None:
        raise RuntimeError(
            "no chat-capable model loaded for memory refine"
        )

    # System prompt avoids "You are a [X]" framing.
    system_prompt = (
        "Memory-curation pass. Given the list below, return a cleaned "
        "list deduplicating and merging near-duplicates without losing "
        "information. Output one item per line, no numbering, no prefixes."
    )

    numbered = "\n".join(items)
    req = CanonicalChatRequest(
        model=chat_model,
        input=[CanonicalInputBlock(type="text", content=numbered)],
        system_prompt=system_prompt,
        stream=True,
        store=False,
    )

    text_chunks: list[str] = []
    async for event in adapter.stream_chat(req, history=[]):
        if getattr(event, "type", None) == "content.delta":
            delta = getattr(event, "delta", None) or getattr(event, "content", None)
            if isinstance(delta, str):
                text_chunks.append(delta)
        elif getattr(event, "type", None) == "error":
            raise RuntimeError(
                f"LM Studio refine error: {getattr(event, 'message', 'unknown')}"
            )

    full = "".join(text_chunks)
    lines = [ln.strip() for ln in full.splitlines() if ln.strip() != ""]
    # Strip common list-prefix artefacts.
    stripped: list[str] = []
    for ln in lines:
        cleaned = ln
        # Strip leading number+dot ("1. " / "1) ").
        import re as _re

        cleaned = _re.sub(r"^\s*\d+[.)]\s*", "", cleaned)
        # Strip leading bullet marks.
        cleaned = _re.sub(r"^[-*•]\s*", "", cleaned)
        if cleaned != "":
            stripped.append(cleaned)
    return stripped


@router.post("/refine", response_model=RefineResponse)
async def refine_endpoint(
    request: Request,
    project_id: int | None = None,
    user: User = Depends(require_user),
    memory_service: MemoryService = Depends(_get_memory_service),
) -> RefineResponse:
    """Refine the user's pinned insights via LM Studio.

    Backs up the current set into ``memory_insights_history`` before
    the destructive replace; the returned ``history_id`` powers the
    "Undo" button.

    Args:
        request:        FastAPI Request.
        user:           Authenticated user.
        memory_service: Injected ``MemoryService``.

    Returns:
        :class:`RefineResponse` with the new insight list + history_id.

    Raises:
        HTTPException: 502 when the refine call fails or returns empty.
    """
    adapter = _get_lmstudio_adapter(request)
    models_service = _get_models_service(request)

    async def _curator(items: list[str]) -> list[str]:
        return await _lmstudio_refine_call(
            items=items,
            adapter=adapter,
            models_service=models_service,
        )

    # Override hook for tests.
    override = getattr(request.app.state, "memory_refine_callable", None)
    curator = override if override is not None else _curator

    # When scoped to a project, verify ownership and capture the
    # project-scoped before-count.
    if project_id is not None:
        projects_svc = getattr(
            request.app.state, "projects_service", None
        )
        if projects_svc is None:
            raise RuntimeError(
                "app.state.projects_service is unset"
            )
        owned = await projects_svc.get(
            user_id=user.id, project_id=project_id
        )
        if owned is None:
            raise HTTPException(
                status_code=_HTTP_404, detail="project not found"
            )

    # Capture before-count first (the service swallows the snapshot).
    pre = await memory_service.list_pinned(
        user_id=user.id, project_id=project_id
    )
    before_count = len(pre)

    try:
        result, history_id = await memory_service.refine(
            user_id=user.id,
            refine_callable=curator,
            project_id=project_id,
        )
    except RefineUpstreamError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return RefineResponse(
        insights=result,
        history_id=history_id,
        before_count=before_count,
        after_count=len(result),
    )


@router.post("/restore/{history_id}", response_model=list[MemoryInsight])
async def restore_endpoint(
    history_id: int,
    request: Request,
    project_id: int | None = None,
    user: User = Depends(require_user),
    memory_service: MemoryService = Depends(_get_memory_service),
) -> list[MemoryInsight]:
    """Roll the user's pinned insights back to a history snapshot.

    Args:
        history_id:     PK of the ``memory_insights_history`` row.
        request:        FastAPI Request.
        user:           Authenticated user.
        memory_service: Injected ``MemoryService``.

    Returns:
        The restored :class:`MemoryInsight` list.

    Raises:
        HTTPException: 404 when the history row is missing or not owned.
    """
    # When scoped to a project, verify ownership; the service then
    # partial-restores only that project's entries from the snapshot.
    if project_id is not None:
        projects_svc = getattr(
            request.app.state, "projects_service", None
        )
        if projects_svc is None:
            raise RuntimeError(
                "app.state.projects_service is unset"
            )
        owned = await projects_svc.get(
            user_id=user.id, project_id=project_id
        )
        if owned is None:
            raise HTTPException(
                status_code=_HTTP_404, detail="project not found"
            )
    try:
        return await memory_service.restore_from_history(
            user_id=user.id,
            history_id=history_id,
            project_id=project_id,
        )
    except HistoryNotFoundError as exc:
        raise HTTPException(status_code=_HTTP_404, detail="history not found") from exc
