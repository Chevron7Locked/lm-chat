# SPDX-License-Identifier: Apache-2.0
"""Folder CRUD routes for lm-chat (feature-parity closure).

Endpoints
---------
GET    /api/folders          — list visible folders for the caller.
POST   /api/folders          — add a folder name to ``user_prefs``.
PATCH  /api/folders/{name}   — rename across prefs + chats atomically.
DELETE /api/folders/{name}   — remove from prefs + unfolder matching chats.

All endpoints require ``Depends(require_user)`` and scope by ``user.id``.
The catalogue is admin-private: there is no cross-user enumeration.

Wire contract follows the v1 form-encoded convention for mutations.
"""
from __future__ import annotations

from typing import Final

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from lmchat.logging import get_logger
from lmchat.routes._dependencies import require_user
from lmchat.services.auth_service import User
from lmchat.services.folder_service import (
    FolderConflictError,
    FolderService,
    InvalidFolderNameError,
)

log = get_logger(__name__)


_HTTP_201: Final[int] = 201
_HTTP_400: Final[int] = 400
_HTTP_404: Final[int] = 404
_HTTP_409: Final[int] = 409
_HTTP_422: Final[int] = 422


def _get_folder_service(request: Request) -> FolderService:
    """Return ``app.state.folder_service``; raise on missing dependency.

    Args:
        request: Incoming FastAPI Request.

    Returns:
        Singleton :class:`FolderService`.

    Raises:
        RuntimeError: When the lifespan did not register the service.
    """
    svc = getattr(request.app.state, "folder_service", None)
    if svc is None:
        raise RuntimeError(
            "app.state.folder_service is unset — the FastAPI lifespan did "
            "not run, and no dependency_overrides entry exists for "
            "get_folder_service.  Tests bypassing the lifespan must "
            "register an override; production code paths must use the lifespan."
        )
    return svc  # type: ignore[return-value]


router: APIRouter = APIRouter(prefix="/api/folders", tags=["folders"])


@router.get("", response_model=list[str])
async def list_folders(
    request: Request,
    project_id: int | None = None,
    user: User = Depends(require_user),
    svc: FolderService = Depends(_get_folder_service),
) -> list[str]:
    """Return the admin's folder list.

    Source switches on *project_id*:
    - project_id is None → union of ``user_prefs.folders`` + distinct
      ``chats.folder`` for un-projected behavior (existing).
    - project_id is set → **HTTP 410 GONE** with code
      ``FOLDER_API_REMOVED_FOR_PROJECTS`` (per-project folders
      removed in v1.0). Older frontend builds receive a structured 410
      instead of a 500.

    Args:
        request:    FastAPI Request (dependency access).
        project_id: Project scope.
        user:       Authenticated user.
        svc:        Injected ``FolderService``.

    Returns:
        Sorted, de-duplicated folder name list.
    """
    if project_id is None:
        return await svc.list_folders(user_id=user.id)
    # Per-project folders were removed. The route signature stays so older
    # frontend builds get a structured 410 instead of a 500 when they
    # send the query param.
    raise HTTPException(
        status_code=410,
        detail={
            "detail": "project folders removed; see project page chat list",
            "code": "FOLDER_API_REMOVED_FOR_PROJECTS",
        },
    )


@router.post("", response_model=list[str], status_code=_HTTP_201)
async def add_folder(
    request: Request,
    name: str = Form(...),
    project_id: int | None = None,
    user: User = Depends(require_user),
    svc: FolderService = Depends(_get_folder_service),
) -> list[str]:
    """Add *name* to the admin's folder catalogue.

    Idempotent: adding an existing name is a no-op.

    When *project_id* is set, returns **HTTP 410 GONE** with code
    ``FOLDER_API_REMOVED_FOR_PROJECTS`` (per-project folders
    removed in v1.0).

    Raises:
        HTTPException: 400 on validation failure.
    """
    log.info(
        "folders.add.request",
        user_id=user.id,
        project_id=project_id,
        name=name,
    )
    if project_id is not None:
        # Per-project folders were removed — 410 GONE.
        raise HTTPException(
            status_code=410,
            detail={
                "detail": "project folders removed; see project page chat list",
                "code": "FOLDER_API_REMOVED_FOR_PROJECTS",
            },
        )
    try:
        return await svc.add_folder(user_id=user.id, name=name)
    except InvalidFolderNameError as exc:
        raise HTTPException(status_code=_HTTP_400, detail=str(exc)) from exc


@router.patch(
    "/{folder_name:path}",
    response_model=list[str],
    responses={
        400: {"description": "Invalid folder name"},
        404: {"description": "Folder not found"},
        409: {"description": "Target folder name already exists"},
    },
)
async def rename_folder(
    folder_name: str,
    request: Request,
    new_name: str = Form(...),
    project_id: int | None = None,
    user: User = Depends(require_user),
    svc: FolderService = Depends(_get_folder_service),
) -> list[str]:
    """Rename *folder_name* → *new_name* across prefs + chats.

    When *project_id* is set, returns **HTTP 410 GONE** with code
    ``FOLDER_API_REMOVED_FOR_PROJECTS`` (per-project folders
    removed in v1.0).

    Raises:
        HTTPException: 400 on validation; 409 on target-name collision.
    """
    # FastAPI's {folder_name:path} converter already URL-decodes the
    # segment once; a second unquote() here double-decoded any folder
    # whose real name itself contains a percent-encoded-looking
    # sequence (e.g. a folder literally named "A%2Fb", sent correctly
    # as "A%252Fb"), silently mis-targeting a different folder.
    decoded = folder_name
    log.info(
        "folders.rename.request",
        user_id=user.id,
        project_id=project_id,
        old=decoded,
        new=new_name,
    )
    if project_id is not None:
        # Per-project folders were removed — 410 GONE.
        raise HTTPException(
            status_code=410,
            detail={
                "detail": "project folders removed; see project page chat list",
                "code": "FOLDER_API_REMOVED_FOR_PROJECTS",
            },
        )
    try:
        return await svc.rename_folder(
            user_id=user.id,
            old_name=decoded,
            new_name=new_name,
        )
    except InvalidFolderNameError as exc:
        raise HTTPException(status_code=_HTTP_400, detail=str(exc)) from exc
    except FolderConflictError as exc:
        raise HTTPException(status_code=_HTTP_409, detail=str(exc)) from exc


@router.delete("/{folder_name:path}", response_model=list[str])
async def delete_folder(
    folder_name: str,
    request: Request,
    project_id: int | None = None,
    user: User = Depends(require_user),
    svc: FolderService = Depends(_get_folder_service),
) -> list[str]:
    """Remove *folder_name* from the catalogue + unfolder matching chats.

    When *project_id* is set, returns **HTTP 410 GONE** with code
    ``FOLDER_API_REMOVED_FOR_PROJECTS`` (per-project folders
    removed in v1.0).

    Raises:
        HTTPException: 400 on validation failure.
    """
    # See rename_folder above: {folder_name:path} already decodes
    # once — do not unquote() again.
    decoded = folder_name
    log.info(
        "folders.delete.request",
        user_id=user.id,
        project_id=project_id,
        name=decoded,
    )
    if project_id is not None:
        # Per-project folders were removed — 410 GONE.
        raise HTTPException(
            status_code=410,
            detail={
                "detail": "project folders removed; see project page chat list",
                "code": "FOLDER_API_REMOVED_FOR_PROJECTS",
            },
        )
    try:
        return await svc.delete_folder(user_id=user.id, name=decoded)
    except InvalidFolderNameError as exc:
        raise HTTPException(status_code=_HTTP_400, detail=str(exc)) from exc
