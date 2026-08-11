# SPDX-License-Identifier: Apache-2.0
"""Prompt library routes for lm-chat.

Endpoints
---------
GET    /api/prompts          — list user's prompts (Invariant 3: list[Prompt])
POST   /api/prompts          — create a prompt (form-encoded contract)
PATCH  /api/prompts/{id}     — update name/content (form-encoded)
DELETE /api/prompts/{id}     — delete a prompt (204)

All routes:
- Require ``Depends(require_user)`` — 401 on unauthenticated.
- Return 404 (never 403) on cross-user access.
- POST/PATCH return 409 on name conflict.
"""
from __future__ import annotations

from typing import Final

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from lmchat.logging import get_logger
from lmchat.routes._dependencies import require_user
from lmchat.services.auth_service import User
from lmchat.services.prompt_library_service import (
    Prompt,
    PromptLibraryService,
    PromptNameConflictError,
    PromptNotFoundError,
)
from lmchat.utils.text_input_policy import (
    PROMPT_CONTENT_MAX_LENGTH,
    PROMPT_NAME_MAX_LENGTH,
    TextInputPolicyError,
    validate_text,
)

log = get_logger(__name__)

_HTTP_201: Final[int] = 201
_HTTP_204: Final[int] = 204
_HTTP_404: Final[int] = 404
_HTTP_409: Final[int] = 409
_HTTP_422: Final[int] = 422

router = APIRouter(prefix="/api/prompts", tags=["prompts"])


def _get_prompt_service(request: Request) -> PromptLibraryService:
    """Return ``app.state.prompt_library_service``; raise ``RuntimeError`` if unset."""
    svc: PromptLibraryService | None = getattr(
        request.app.state, "prompt_library_service", None
    )
    if svc is None:
        raise RuntimeError(
            "app.state.prompt_library_service is not set — "
            "PromptLibraryService must be initialised in the app lifespan."
        )
    return svc


@router.get("", response_model=list[Prompt])
async def list_prompts(
    request: Request,
    user: User = Depends(require_user),
    svc: PromptLibraryService = Depends(_get_prompt_service),
) -> list[Prompt]:
    """Return all prompt presets for the authenticated user.

    Invariant 3: returns ``list[Prompt]`` directly (no envelope).

    Args:
        request: FastAPI Request.
        user:    Authenticated user.
        svc:     Injected ``PromptLibraryService``.

    Returns:
        List of :class:`Prompt`, sorted by name.
    """
    return await svc.list_for_user(user.id)


@router.post("", response_model=Prompt, status_code=_HTTP_201)
async def create_prompt(
    request: Request,
    # Caps come from the shared text_input_policy so the FE/BE stay
    # aligned (prior caps were 128/32768 vs frontend's 16384 — silent
    # acceptance of NUL bytes and 32k blobs).
    name: str = Form(..., min_length=1, max_length=PROMPT_NAME_MAX_LENGTH),
    content: str = Form(..., min_length=1, max_length=PROMPT_CONTENT_MAX_LENGTH),
    user: User = Depends(require_user),
    svc: PromptLibraryService = Depends(_get_prompt_service),
) -> Prompt:
    """Create a new prompt preset.

    ``content`` is capped at 32 768 characters (32 KB).  Larger values return
    422 Unprocessable Entity.  This prevents storing arbitrarily large blobs in
    the prompt table before per-user quota infrastructure lands.

    Args:
        request: FastAPI Request.
        name:    Short display label (form field, unique per user).
        content: Full prompt text (form field; max 32 768 chars).
        user:    Authenticated user.
        svc:     Injected ``PromptLibraryService``.

    Returns:
        The created :class:`Prompt` (201).

    Raises:
        HTTPException: 409 if a prompt with *name* already exists.
    """
    # NUL/control reject + whitespace trim from the shared validator;
    # length cap was already enforced by Form() but redundant validation
    # is cheap and self-documenting.
    try:
        name = validate_text(
            name,
            field="name",
            max_length=PROMPT_NAME_MAX_LENGTH,
            allow_newlines=False,
            allow_tabs=False,
        )
        content = validate_text(
            content,
            field="content",
            max_length=PROMPT_CONTENT_MAX_LENGTH,
            allow_newlines=True,
            allow_tabs=True,
        )
    except TextInputPolicyError as exc:
        raise HTTPException(status_code=_HTTP_422, detail=str(exc)) from exc

    try:
        prompt = await svc.create(user_id=user.id, name=name, content=content)
    except PromptNameConflictError as exc:
        raise HTTPException(
            status_code=_HTTP_409,
            detail=f"prompt named {name!r} already exists",
        ) from exc
    return prompt


@router.patch("/{prompt_id}", response_model=Prompt)
async def update_prompt(
    prompt_id: int,
    request: Request,
    name: str | None = Form(default=None, max_length=PROMPT_NAME_MAX_LENGTH),
    content: str | None = Form(default=None, max_length=PROMPT_CONTENT_MAX_LENGTH),
    user: User = Depends(require_user),
    svc: PromptLibraryService = Depends(_get_prompt_service),
) -> Prompt:
    """Update a prompt's name and/or content.

    Args:
        prompt_id: PK of the prompt to update.
        request:   FastAPI Request.
        name:      New name (optional form field).
        content:   New content (optional form field).
        user:      Authenticated user.
        svc:       Injected ``PromptLibraryService``.

    Returns:
        The updated :class:`Prompt` (200).

    Raises:
        HTTPException: 404 if the prompt is not found or not owned by user.
        HTTPException: 409 if the new name conflicts with an existing prompt.
    """
    # Validate any provided field. None means "leave unchanged"; an
    # empty string after strip is rejected by validate_text. Patches
    # therefore can't blank a prompt to whitespace.
    try:
        if name is not None:
            name = validate_text(
                name,
                field="name",
                max_length=PROMPT_NAME_MAX_LENGTH,
                allow_newlines=False,
                allow_tabs=False,
            )
        if content is not None:
            content = validate_text(
                content,
                field="content",
                max_length=PROMPT_CONTENT_MAX_LENGTH,
                allow_newlines=True,
                allow_tabs=True,
            )
    except TextInputPolicyError as exc:
        raise HTTPException(status_code=_HTTP_422, detail=str(exc)) from exc

    try:
        prompt = await svc.update(
            prompt_id,
            user_id=user.id,
            name=name,
            content=content,
        )
    except PromptNotFoundError as exc:
        raise HTTPException(
            status_code=_HTTP_404, detail="prompt not found"
        ) from exc
    except PromptNameConflictError as exc:
        raise HTTPException(
            status_code=_HTTP_409,
            detail=f"prompt named {name!r} already exists",
        ) from exc
    return prompt


@router.delete("/{prompt_id}", status_code=_HTTP_204)
async def delete_prompt(
    prompt_id: int,
    request: Request,
    user: User = Depends(require_user),
    svc: PromptLibraryService = Depends(_get_prompt_service),
) -> None:
    """Delete a prompt preset.

    Args:
        prompt_id: PK of the prompt to delete.
        request:   FastAPI Request.
        user:      Authenticated user.
        svc:       Injected ``PromptLibraryService``.

    Returns:
        204 No Content on success.

    Raises:
        HTTPException: 404 if the prompt is not found or not owned by user.
    """
    try:
        await svc.delete(prompt_id, user_id=user.id)
    except PromptNotFoundError as exc:
        raise HTTPException(
            status_code=_HTTP_404, detail="prompt not found"
        ) from exc
