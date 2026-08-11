# SPDX-License-Identifier: Apache-2.0
"""ProjectsService — CRUD for the `projects` table.

A project is a persistent container that owns chats, documents, and
memory_insights via nullable FK on each child table. All methods are
ownership-bounded by ``user_id`` — cross-user access returns None (404 at
the route) rather than raising.

Validation:
- ``name`` required, non-empty, ≤ 256 chars
- ``description`` optional (may be empty), ≤ 1024 chars
- ``system_prompt`` optional (may be empty), ≤ 16384 chars

Mutation semantics for ``update``: ``None`` means "don't touch this
field"; ``""`` (for description / system_prompt) is a real value that
clears the column. The legacy ``folders`` kwarg is accepted but silently
discarded (column dropped by migration 0023b). ``default_model_id`` /
``rag_threshold`` are nullable columns where NULL is itself meaningful,
so clearing them goes through the ``clear`` kwarg instead of an
empty-string sentinel.

Constructed once at app lifespan and attached to
``app.state.projects_service``; routes resolve it via DI.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import structlog
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from lmchat.db.schema import projects as projects_table
from lmchat.utils.clock import ensure_utc
from lmchat.utils.text_input_policy import (
    PROMPT_CONTENT_MAX_LENGTH,
    SHORT_FIELD_MAX_LENGTH,
    TextInputPolicyError,
    validate_text,
)

log = structlog.get_logger(__name__)

_DESCRIPTION_MAX_LENGTH: Final[int] = 1024


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ProjectsServiceError(Exception):
    """Base class for ProjectsService validation errors."""


class InvalidProjectFieldError(ProjectsServiceError):
    """A field failed validation (empty name, oversize, NUL bytes, etc.)."""


# ---------------------------------------------------------------------------
# Domain model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Project:
    """A row from the ``projects`` table.

    Fields mirror ``db/schema.py:projects`` and the wire response shape
    in ``routes/projects.py:ProjectResponse``.
    """

    id: int
    user_id: int
    name: str
    description: str
    system_prompt: str
    # NULL = no docs yet; pinned by the first document attach.
    embedding_model_id: str | None
    # Seed model for new chats in this project; NULL falls through to
    # the user's global default.
    default_model_id: str | None
    # Per-project RAG-mode threshold override, in tokens. NULL falls
    # back to rag_mode_resolver.compute_rag_threshold's formula.
    rag_threshold: int | None
    created_at: float
    updated_at: float
    # NULL = active; a unix epoch float = archived at that instant.
    archived_at: float | None = None
    # Rolling auto-summary. "" = not yet generated. summary_updated_at is
    # the last regeneration time; summary_message_watermark is the
    # project's message count at that regeneration (see
    # project_summary_service.should_refresh).
    summary: str = ""
    summary_updated_at: float | None = None
    summary_message_watermark: int = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_name(candidate: str, *, min_length: int = 3) -> str:
    """Validate the required ``name`` field; raise on policy failure.

    Args:
        candidate:  The raw project name from the caller.
        min_length: Minimum length after stripping (default 3). Pass a
                    different value for other name-like fields if needed.

    Raises:
        InvalidProjectFieldError: On any policy failure.
    """
    try:
        return validate_text(
            candidate,
            field="name",
            max_length=SHORT_FIELD_MAX_LENGTH,
            min_length=min_length,
            allow_newlines=False,
            allow_tabs=False,
        )
    except TextInputPolicyError as exc:
        raise InvalidProjectFieldError(str(exc)) from exc


def _normalize_optional_text(
    candidate: str, *, field: str, max_length: int
) -> str:
    """Validate an optional text field that may be empty.

    Empty string is a real value (cleared). Non-empty values go through
    ``validate_text`` for NUL / control / length checks.
    """
    if candidate == "":
        return ""
    try:
        return validate_text(
            candidate,
            field=field,
            max_length=max_length,
        )
    except TextInputPolicyError as exc:
        raise InvalidProjectFieldError(str(exc)) from exc


def _normalize_model_id(candidate: str) -> str:
    """Validate the ``default_model_id`` field; raise on policy failure.

    A model id is an opaque catalog key, not free-form prose — same
    length cap as ``name`` but without the higher ``min_length``.
    Whether the model is actually loaded/known is a runtime concern,
    not a write-time one.
    """
    try:
        return validate_text(
            candidate,
            field="default_model_id",
            max_length=SHORT_FIELD_MAX_LENGTH,
            allow_newlines=False,
            allow_tabs=False,
        )
    except TextInputPolicyError as exc:
        raise InvalidProjectFieldError(str(exc)) from exc


def _validate_rag_threshold(candidate: int) -> int:
    """Validate the ``rag_threshold`` field; raise on policy failure.

    Args:
        candidate: Proposed per-project RAG-mode threshold, in tokens.

    Raises:
        InvalidProjectFieldError: When negative. ``compute_rag_threshold``
            would clamp a negative override to 1 anyway, but rejecting it
            at write time surfaces the mistake to the admin instead of
            silently rewriting their input.
    """
    if candidate < 0:
        raise InvalidProjectFieldError(
            "rag_threshold must be a non-negative integer"
        )
    return candidate


def _validate_system_prompt_budget(system_prompt: str) -> None:
    """Write-time budget gate.

    Caps ``projects.system_prompt`` at 2000 estimated
    tokens. Validation happens at write time (POST / PATCH), NOT at
    runtime, so user-authored text is never silently truncated.

    Raises:
        InvalidProjectFieldError: When the token estimate exceeds
            :data:`PROJECT_PROMPT_TOKEN_BUDGET`. The message names
            the budget AND the estimate so the admin can trim
            without guessing.
    """
    from lmchat.services._token_budget import (  # noqa: PLC0415
        PROJECT_PROMPT_TOKEN_BUDGET,
        approx_token_count,
    )

    if not system_prompt:
        return
    estimated = approx_token_count(system_prompt)
    if estimated > PROJECT_PROMPT_TOKEN_BUDGET:
        raise InvalidProjectFieldError(
            f"system_prompt exceeds the "
            f"{PROJECT_PROMPT_TOKEN_BUDGET}-token budget "
            f"(estimated {estimated} tokens). Trim the prompt or "
            f"split across multiple projects."
        )


def _row_to_project(row: Any) -> Project:
    """Map a SQLAlchemy Row from a SELECT against ``projects`` to a Project."""
    # getattr(..., None) lets this work against an older row shape (test
    # fixtures, downgrade) without raising.
    embedding_model_id = getattr(row, "embedding_model_id", None)
    if embedding_model_id is not None:
        embedding_model_id = str(embedding_model_id) or None
    default_model_id = getattr(row, "default_model_id", None)
    if default_model_id is not None:
        default_model_id = str(default_model_id) or None
    rag_threshold = getattr(row, "rag_threshold", None)
    if rag_threshold is not None:
        rag_threshold = int(rag_threshold)
    # SQLite round-trips this column as a timezone-naive datetime even
    # though the stored value is UTC; ensure_utc() re-attaches UTC before
    # .timestamp() so the epoch isn't skewed by the host's local offset.
    archived_at_dt = ensure_utc(getattr(row, "archived_at", None))
    archived_at = archived_at_dt.timestamp() if archived_at_dt is not None else None
    # summary_updated_at: same naive-round-trip / ensure_utc() fix as
    # archived_at above.
    summary_updated_at_dt = ensure_utc(getattr(row, "summary_updated_at", None))
    summary_updated_at = (
        summary_updated_at_dt.timestamp() if summary_updated_at_dt is not None else None
    )
    return Project(
        id=int(row.id),
        user_id=int(row.user_id),
        name=str(row.name),
        description=str(row.description or ""),
        system_prompt=str(row.system_prompt or ""),
        embedding_model_id=embedding_model_id,
        default_model_id=default_model_id,
        rag_threshold=rag_threshold,
        created_at=float(row.created_at),
        updated_at=float(row.updated_at),
        archived_at=archived_at,
        summary=str(getattr(row, "summary", "") or ""),
        summary_updated_at=summary_updated_at,
        summary_message_watermark=int(getattr(row, "summary_message_watermark", 0) or 0),
    )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ProjectsService:
    """CRUD on the ``projects`` table.

    Args:
        engine: Async SQLAlchemy engine connected to the application DB.
    """

    def __init__(self, *, engine: AsyncEngine) -> None:
        self._engine = engine

    # ------------------------------------------------------------------
    # create
    # ------------------------------------------------------------------

    async def create(
        self,
        *,
        user_id: int,
        name: str,
        description: str = "",
        system_prompt: str = "",
        folders: list[str] | None = None,
    ) -> Project:
        """Create a new project owned by *user_id*.

        Args:
            folders: ACCEPTED FOR BACKWARD-COMPAT, IGNORED. Per-project
                folders were removed. The kwarg stays so older clients
                don't 400; the value is silently dropped.

        Raises:
            InvalidProjectFieldError: On any field validation failure.
        """
        # Accepted for backward-compat with older clients; nowhere to
        # persist it (column dropped).
        _ = folders
        clean_name = _normalize_name(name)
        clean_description = _normalize_optional_text(
            description,
            field="description",
            max_length=_DESCRIPTION_MAX_LENGTH,
        )
        clean_system_prompt = _normalize_optional_text(
            system_prompt,
            field="system_prompt",
            max_length=PROMPT_CONTENT_MAX_LENGTH,
        )
        # Write-time token budget check; routes already catch
        # InvalidProjectFieldError → 422 with the budget explanation.
        _validate_system_prompt_budget(clean_system_prompt)
        now = time.time()
        async with self._engine.begin() as conn:
            result = await conn.execute(
                insert(projects_table)
                .values(
                    user_id=user_id,
                    name=clean_name,
                    description=clean_description,
                    system_prompt=clean_system_prompt,
                    created_at=now,
                    updated_at=now,
                )
                .returning(*projects_table.c)
            )
            row = result.fetchone()
        # Don't use `assert` — python -O strips asserts, which would
        # turn this into a silent None-deref in _row_to_project.
        if row is None:
            raise RuntimeError(
                "INSERT projects … RETURNING returned no row"
            )
        log.info(
            "projects_service.created",
            user_id=user_id,
            project_id=int(row.id),
        )
        return _row_to_project(row)

    # ------------------------------------------------------------------
    # read paths
    # ------------------------------------------------------------------

    async def list_for_user(
        self, user_id: int, *, include_archived: bool = False
    ) -> list[Project]:
        """Return projects owned by *user_id*, newest-updated first.

        Args:
            user_id: The owning user.
            include_archived: When False (default), archived projects
                are filtered out (default sidebar/list behavior). Pass
                True for the all-projects landing page's "Archived"
                section.
        """
        query = select(projects_table).where(
            projects_table.c.user_id == user_id
        )
        if not include_archived:
            query = query.where(projects_table.c.archived_at.is_(None))
        query = query.order_by(projects_table.c.updated_at.desc())
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).fetchall()
        return [_row_to_project(r) for r in rows]

    async def get(
        self, *, user_id: int, project_id: int
    ) -> Project | None:
        """Return the project owned by *user_id* with *project_id*, or None.

        Ownership-bounded: a project owned by a different user returns
        None (the route layer maps that to 404 — never leak existence).
        """
        async with self._engine.connect() as conn:
            return await self.get_with_conn(
                conn, user_id=user_id, project_id=project_id
            )

    async def get_with_conn(
        self,
        conn: AsyncConnection,
        *,
        user_id: int,
        project_id: int,
    ) -> Project | None:
        """Connection-bearing variant of :meth:`get`.

        Callers that need the read to participate in an existing
        transaction (e.g. ``ChatService.set_project_id`` building a
        detach snapshot atomically with the project_id clear) call this
        directly with their own open ``AsyncConnection``. Same
        ownership-bounding contract as :meth:`get`.
        """
        row = (
            await conn.execute(
                select(projects_table).where(
                    projects_table.c.id == project_id,
                    projects_table.c.user_id == user_id,
                )
            )
        ).fetchone()
        if row is None:
            return None
        return _row_to_project(row)

    # ------------------------------------------------------------------
    # update
    # ------------------------------------------------------------------

    async def update(
        self,
        *,
        user_id: int,
        project_id: int,
        name: str | None = None,
        description: str | None = None,
        system_prompt: str | None = None,
        folders: list[str] | None = None,
        default_model_id: str | None = None,
        rag_threshold: int | None = None,
        clear: frozenset[str] | None = None,
    ) -> Project | None:
        """PATCH semantics — None means "don't touch this field."

        Empty string for ``description`` / ``system_prompt`` is a real
        value (clears the column). The ``folders`` kwarg is accepted for
        backward-compat but silently discarded.

        ``default_model_id`` / ``rag_threshold`` are nullable columns
        where NULL is itself meaningful ("fall through to the global
        default" / "use the formula"), distinct from "don't touch" —
        since ``None`` already means that, clearing them to NULL goes
        through *clear* instead (name the field there, same convention
        as the route layer's ``clear=`` form field).

        Returns the updated project, or None when no such project is
        owned by *user_id* (route maps to 404).

        Raises:
            InvalidProjectFieldError: On any field validation failure.
        """
        clear_fields = clear or frozenset()
        patch: dict[str, object] = {}
        if name is not None:
            patch["name"] = _normalize_name(name)
        if description is not None:
            patch["description"] = _normalize_optional_text(
                description,
                field="description",
                max_length=_DESCRIPTION_MAX_LENGTH,
            )
        if system_prompt is not None:
            cleaned_prompt = _normalize_optional_text(
                system_prompt,
                field="system_prompt",
                max_length=PROMPT_CONTENT_MAX_LENGTH,
            )
            # Write-time budget check.
            _validate_system_prompt_budget(cleaned_prompt)
            patch["system_prompt"] = cleaned_prompt
        # Accepted for backward-compat but silently discarded.
        if folders is not None:
            _ = folders
        # Clear-to-NULL wins over a same-call set (mirrors the route's
        # own clear-vs-value precedence).
        if "default_model_id" in clear_fields:
            patch["default_model_id"] = None
        elif default_model_id is not None:
            patch["default_model_id"] = _normalize_model_id(default_model_id)
        if "rag_threshold" in clear_fields:
            patch["rag_threshold"] = None
        elif rag_threshold is not None:
            patch["rag_threshold"] = _validate_rag_threshold(rag_threshold)
        if not patch:
            # No-op update: return the current row unchanged.
            return await self.get(user_id=user_id, project_id=project_id)
        patch["updated_at"] = time.time()

        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(projects_table)
                .where(
                    projects_table.c.id == project_id,
                    projects_table.c.user_id == user_id,
                )
                .values(**patch)
                .returning(*projects_table.c)
            )
            row = result.fetchone()
        if row is None:
            return None
        log.info(
            "projects_service.updated",
            user_id=user_id,
            project_id=project_id,
            fields=sorted(patch.keys()),
        )
        return _row_to_project(row)

    async def set_archived(
        self, *, user_id: int, project_id: int, archived: bool
    ) -> Project | None:
        """Archive or unarchive the caller's project.

        Soft — never touches children. Archiving only drops the
        project out of the default ``list_for_user`` filter; chats /
        documents attached to it are untouched and stay attached.

        Args:
            user_id: Must own the project.
            project_id: The project to archive/unarchive.
            archived: True sets ``archived_at`` to now; False clears
                it back to NULL.

        Returns:
            The updated project, or None when no such project is
            owned by *user_id* (route maps to 404).
        """
        archived_at = datetime.now(UTC) if archived else None
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(projects_table)
                .where(
                    projects_table.c.id == project_id,
                    projects_table.c.user_id == user_id,
                )
                .values(archived_at=archived_at, updated_at=time.time())
                .returning(*projects_table.c)
            )
            row = result.fetchone()
        if row is None:
            return None
        log.info(
            "projects_service.archived" if archived else "projects_service.unarchived",
            user_id=user_id,
            project_id=project_id,
        )
        return _row_to_project(row)

    async def set_summary(
        self,
        *,
        user_id: int,
        project_id: int,
        summary: str,
        message_watermark: int,
    ) -> Project | None:
        """Persist the rolling auto-summary for *project_id*.

        Called by ``project_summary_service.refresh_project_summary``
        after a fresh OOB regeneration — never by the user-facing PATCH.
        *message_watermark* records the project's message count at
        generation time so the throttled auto-refresh trigger knows how
        much new activity has accumulated since.

        Deliberately does NOT bump ``updated_at`` — that drives the
        project list's sort order, and a silent background regeneration
        reordering the sidebar would be a surprising side effect.
        ``updated_at`` stays reserved for explicit user edits and archive
        actions.

        Returns the updated project, or None when no such project is
        owned by *user_id* (route maps to 404).
        """
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(projects_table)
                .where(
                    projects_table.c.id == project_id,
                    projects_table.c.user_id == user_id,
                )
                .values(
                    summary=summary,
                    summary_updated_at=datetime.now(UTC),
                    summary_message_watermark=message_watermark,
                )
                .returning(*projects_table.c)
            )
            row = result.fetchone()
        if row is None:
            return None
        log.info(
            "projects_service.summary_refreshed",
            user_id=user_id,
            project_id=project_id,
            summary_len=len(summary),
            message_watermark=message_watermark,
        )
        return _row_to_project(row)

    # ------------------------------------------------------------------
    # delete
    # ------------------------------------------------------------------

    async def delete(self, *, user_id: int, project_id: int) -> bool:
        """Delete the project owned by *user_id* with *project_id*.

        Returns:
            True when a row was deleted; False on 404 (no such project,
            or wrong owner — the route maps either to 404 the same way).

        The ON DELETE SET NULL FK cascade on chats / documents /
        memory_insights leaves the children intact as un-projected.
        """
        async with self._engine.begin() as conn:
            try:
                result = await conn.execute(
                    delete(projects_table).where(
                        projects_table.c.id == project_id,
                        projects_table.c.user_id == user_id,
                    )
                )
            except IntegrityError:
                # No FK forces this today; if a future migration attaches
                # a CASCADE-incoming child that rejects deletion, log
                # clearly instead of a bare 500.
                log.exception(
                    "projects_service.delete_integrity_error",
                    user_id=user_id,
                    project_id=project_id,
                )
                raise
        deleted = result.rowcount > 0
        if deleted:
            log.info(
                "projects_service.deleted",
                user_id=user_id,
                project_id=project_id,
            )
        return deleted
