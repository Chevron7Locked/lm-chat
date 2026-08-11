# SPDX-License-Identifier: Apache-2.0
"""Shared form-parsing helpers for v1 route handlers.

Centralizes the ``clear=`` comma-list parser introduced for the
PATCH /api/auth/profile route and reused by every child-
update route (PATCH /api/chats/{id}, PATCH /api/documents/{id},
PATCH /api/projects/{id}). The semantics:

- Empty / omitted ``clear=`` → no fields cleared.
- ``clear=a,b,c`` → {"a", "b", "c"}.
- Whitespace around items is stripped; empty tokens are coalesced
  (``clear=a, , a `` → ``{"a"}``).
- Unknown field names raise HTTP 422 with a per-route allowlist
  surfaced in the detail string.

Prior to this module each route had its own inline implementation
of the same parser shape, leaving the door open for subtle drift.
Centralizing the parser fixes that.
"""
from __future__ import annotations

from typing import Final

from fastapi import HTTPException

_HTTP_422: Final[int] = 422


def parse_clear(
    raw: str | None, *, allowed: frozenset[str]
) -> frozenset[str]:
    """Parse a ``clear=`` comma-list form value.

    Args:
        raw:     The raw form-field value, or None when the field
                 was omitted.
        allowed: The set of field names the caller's route permits
                 in ``clear=``. Any name outside this set raises 422.

    Returns:
        A frozen set of normalized field names to clear. Empty when
        ``raw`` is None / empty / contains only whitespace.

    Raises:
        HTTPException: 422 when ``raw`` contains any field name not
            in ``allowed``. The detail message names the unknown
            field(s) and the allowed set so the caller can fix the
            request without guessing.
    """
    items = {
        part.strip()
        for part in (raw or "").split(",")
        if part.strip()
    }
    unknown = items - allowed
    if unknown:
        raise HTTPException(
            status_code=_HTTP_422,
            detail=(
                f"clear= names unknown field(s): {sorted(unknown)}; "
                f"allowed: {sorted(allowed)}"
            ),
        )
    return frozenset(items)


def embedding_pin_conflict_response(exc: object) -> dict[str, object]:
    """409 body builder for an embedding-model pin conflict.

    Both attach paths (PATCH /api/documents/{id} and
    POST /api/projects/{id}/documents) catch
    :class:`~lmchat.services.documents_service.EmbeddingModelPinConflict`
    and respond 409 with the body this helper builds. The shape is
    fixed so the client banner can render the re-embed flow
    without a second round trip — the URL field gives the admin
    the exact page to click into.

    Args:
        exc: The
            :class:`~lmchat.services.documents_service.EmbeddingModelPinConflict`
            instance (typed as ``object`` here to avoid a service-layer
            import in this routes-layer module).

    Returns:
        Dict body for the 409 detail:
        ``{embedding_model_id, active_embedding_model_id, re_embed_url}``.
    """
    return {
        "embedding_model_id": getattr(exc, "pinned_model_id", None),
        "active_embedding_model_id": getattr(exc, "active_model_id", None),
        "re_embed_url": (
            f"/project/{getattr(exc, 'project_id', 0)}#documents"
        ),
    }
