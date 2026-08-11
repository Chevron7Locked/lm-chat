# SPDX-License-Identifier: Apache-2.0
"""Documents routes for lm-chat RAG pipeline.

Endpoints
---------
POST /api/documents
    Multipart file upload. Returns {id, filename, chunk_count}.
GET /api/documents
    List user's non-deleted documents. Returns list[DocumentResponse].
DELETE /api/documents/{document_id}
    Soft-delete a document. Returns 204.
GET /api/documents/{document_id}/chunks
    Return chunk preview list (ordinal + first 200 chars). Owner-only.

Error mapping
-------------
413 — upload exceeds LM_CHAT_DOCUMENT_MAX_BYTES (default 50 MB).
415 — unsupported content type, or content type does not match the file's
      magic bytes.
422 — image-only PDF (no extractable text; OCR is post-1.0), or a
      malformed/empty-text EPUB or DOCX container.
404 — document not found or not owned by the caller.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel

from lmchat.config import get_settings
from lmchat.routes._dependencies import (
    get_embedding_client_dep,
    get_engine_dep,
    get_models_service_dep,
    require_user,
)
from lmchat.services.auth_service import User
from lmchat.services.documents_service import (
    Document,
    DocumentNotFoundError,
    DocumentParseError,
    EmbeddingModelPinConflict,
    ImageOnlyPdfError,
    MimeTypeMismatchError,
    UnsupportedMimeTypeError,
    delete_document,
    get_document,
    get_document_chunks,
    list_documents,
    set_document_project_id,
    upload_document,
)
from lmchat.services.memory_service import NoEmbeddingModelLoadedError

router = APIRouter(prefix="/api/documents", tags=["documents"])

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Multipart overhead allowance (boundary, field headers, trailing CRLF).
# A file just under max_bytes shouldn't be false-rejected because of
# multipart framing bytes.
_MULTIPART_OVERHEAD: int = 8192

# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class DocumentResponse(BaseModel):
    """Wire shape for a single document in list/create responses.

    Matches the ``Document`` service model shape, projected for the API.
    """

    id: int
    title: str
    mime_type: str
    byte_size: int
    chunk_count: int
    embedding_model_id: str
    sha256: str
    uploaded_at: str
    # Optional project membership.
    project_id: int | None = None


class UploadResponse(BaseModel):
    """Response body for POST /api/documents."""

    id: int
    filename: str
    chunk_count: int


class ChunkPreview(BaseModel):
    """One chunk preview entry for GET /api/documents/{id}/chunks."""

    ordinal: int
    text_preview: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sanitize_filename(raw: str | None) -> str:
    """Return a safe filename, stripping path components and unsafe characters.

    Prevents path traversal attacks (``../../etc/passwd``, backslash
    variants) and NUL bytes.  Caps the name at 255 characters so it fits
    all common filesystems.

    Args:
        raw: The raw filename from the upload (may be None or adversarial).

    Returns:
        A safe filename string, never empty (falls back to ``"upload"``).
    """
    if not raw:
        return "upload"
    # Strip directory components (both UNIX and Windows separators).
    base = raw.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    # Replace anything that is not alphanumeric, dot, dash, underscore,
    # or space with an underscore.
    safe = re.sub(r"[^a-zA-Z0-9._\- ]", "_", base)
    # Cap at 255 chars to fit common filesystems.
    return safe[:255] or "upload"


def _doc_to_response(doc: Document) -> DocumentResponse:
    return DocumentResponse(
        id=doc.id,
        title=doc.title,
        mime_type=doc.mime_type,
        byte_size=doc.byte_size,
        chunk_count=doc.chunk_count,
        embedding_model_id=doc.embedding_model_id,
        sha256=doc.sha256,
        uploaded_at=doc.uploaded_at.isoformat(),
        project_id=doc.project_id,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=UploadResponse,
    status_code=201,
    responses={400: {"description": "Invalid upload — malformed multipart body or missing file"}},
)
async def upload_document_route(
    request: Request,
    file: UploadFile,
    project_id: int | None = None,
    user: User = Depends(require_user),
    engine: object = Depends(get_engine_dep),
    embedding_client: object = Depends(get_embedding_client_dep),
    models_service: object = Depends(get_models_service_dep),
) -> UploadResponse:
    """Upload a document and trigger chunking + embedding.

    Accepts ``multipart/form-data`` with a ``file`` field.

    Returns ``{id, filename, chunk_count}`` on success.

    Args:
        request:          FastAPI request (for settings access).
        file:             The uploaded file (FastAPI UploadFile).
        user:             Authenticated user.
        engine:           Async SQLAlchemy engine.
        embedding_client: EmbeddingClient.
        models_service:   ModelsService.

    Returns:
        :class:`UploadResponse` with the document id, filename, and chunk count.

    Raises:
        HTTPException: 413 if file exceeds size limit.
        HTTPException: 415 if MIME type is not supported.
        HTTPException: 422 if PDF is image-only, or an EPUB/DOCX
            container is malformed or has no extractable text.
    """
    settings = get_settings()
    max_bytes: int = settings.lm_chat_document_max_bytes

    # Reject oversized uploads before reading the body.
    # First check: declared Content-Length on the request itself.
    # This catches clients that declare a huge upload at the HTTP
    # request level (non-multipart or raw uploads).
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_len = int(declared)
        except ValueError:
            declared_len = None
        if declared_len is not None and declared_len > max_bytes + _MULTIPART_OVERHEAD:
            raise HTTPException(
                status_code=413,
                detail=f"Declared upload size {declared_len} exceeds limit {max_bytes}",
            )

    # Second check: multipart part Content-Length (via file.headers).
    # Starlette 1.1.0's MultiPartParser hardcodes UploadFile(size=0)
    # (see Starlette #1234), so file.size is always 0 for multipart
    # uploads.  However, the parser DOES populate file.headers with
    # the part headers, including Content-Length.  We check that here
    # to catch clients that declare a huge file in a multipart part
    # but send a tiny body.
    part_declared = file.headers.get("content-length") if hasattr(file, "headers") else None
    if part_declared is not None:
        try:
            part_declared_len = int(part_declared)
        except ValueError:
            part_declared_len = None
        if part_declared_len is not None and part_declared_len > max_bytes + _MULTIPART_OVERHEAD:
            raise HTTPException(
                status_code=413,
                detail=f"Declared part size {part_declared_len} exceeds limit {max_bytes}",
            )

    # Third check: UploadFile.size (populated by Starlette from the
    # multipart part Content-Length, when available — currently
    # hardcoded to 0 in Starlette 1.1.0).
    if file.size is not None and file.size > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File size {file.size} exceeds limit {max_bytes}",
        )

    if file.size is None:
        # Streamed read: accumulate with running-total check.
        chunks: list[bytes] = []
        total = 0
        chunk = await file.read(65_536)
        while chunk:
            total += len(chunk)
            if total > max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"File size exceeds limit {max_bytes}",
                )
            chunks.append(chunk)
            chunk = await file.read(65_536)
        body_bytes = b"".join(chunks)
    else:
        body_bytes = await file.read()
        if len(body_bytes) > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"File size {len(body_bytes)} exceeds limit {max_bytes}",
            )

    # Sanitize filename to prevent path-traversal attacks.
    filename = _sanitize_filename(file.filename)
    content_type = file.content_type or "application/octet-stream"

    # When ?project_id= is set, the project must
    # be owned by the caller. Unknown / cross-user IDs return 404 BEFORE any DB
    # write, mirroring the chat-route pattern.
    if project_id is not None:
        projects_svc = getattr(
            request.app.state, "projects_service", None
        )
        if projects_svc is None:
            raise RuntimeError(
                "app.state.projects_service is unset"
            )
        owned = await projects_svc.get(
            user_id=user.id,  # type: ignore[attr-defined]
            project_id=project_id,
        )
        if owned is None:
            raise HTTPException(
                status_code=404, detail="project not found"
            )

    try:
        doc = await upload_document(
            user_id=user.id,  # type: ignore[attr-defined]
            filename=filename,
            content_type=content_type,
            body_bytes=body_bytes,
            engine=engine,  # type: ignore[arg-type]
            embedding_client=embedding_client,  # type: ignore[arg-type]
            models_service=models_service,  # type: ignore[arg-type]
            project_id=project_id,
        )
    except MimeTypeMismatchError:
        raise HTTPException(
            status_code=415,
            detail="content type does not match file signature",
        )
    except UnsupportedMimeTypeError as exc:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported content type: {exc.mime_type}",
        )
    except ImageOnlyPdfError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc),
        )
    except DocumentParseError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc),
        )
    except EmbeddingModelPinConflict as exc:
        # Write-once-on-attach violation on a project upload. 409 + the
        # re-embed body so the UI banner renders the flow without a
        # second round trip.
        from lmchat.routes._form_utils import (  # noqa: PLC0415
            embedding_pin_conflict_response,
        )

        raise HTTPException(
            status_code=409,
            detail=embedding_pin_conflict_response(exc),
        )
    except NoEmbeddingModelLoadedError as exc:
        # Preferred model set-but-not-loaded (or no embedder loaded
        # at all). Surface as 503 with the actionable message so the FE can
        # display the LM Studio embedding-model hint. Ordered BEFORE
        # RuntimeError because NoEmbeddingModelLoadedError inherits from
        # MemoryServiceError(Exception), not RuntimeError.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return UploadResponse(
        id=doc.id,
        filename=doc.title,
        chunk_count=doc.chunk_count,
    )


@router.get("", response_model=list[DocumentResponse])
async def list_documents_route(
    project_id: int | None = None,
    unscoped: bool = False,
    user: User = Depends(require_user),
    engine: object = Depends(get_engine_dep),
) -> list[DocumentResponse]:
    """List non-deleted documents for the authenticated user.

    Returns ``list[DocumentResponse]`` — no envelope per Invariant 3.

    Args:
        project_id: When set, restrict to documents in this project.
        unscoped:   When True AND project_id
                    is None, restrict to un-projected documents
                    (``project_id IS NULL``). Default False
                    preserves existing behavior (user-scoped union).
        user:       Authenticated user.
        engine:     Async SQLAlchemy engine.

    Returns:
        Plain list of :class:`DocumentResponse`.
    """
    docs = await list_documents(
        user_id=user.id,  # type: ignore[attr-defined]
        engine=engine,  # type: ignore[arg-type]
        project_id=project_id,
        unscoped=unscoped,
    )
    return [_doc_to_response(d) for d in docs]


@router.delete("/{document_id}", status_code=204)
async def delete_document_route(
    document_id: int,
    user: User = Depends(require_user),
    engine: object = Depends(get_engine_dep),
) -> None:
    """Soft-delete a document. Returns 204 on success.

    Args:
        document_id: PK of the document to delete.
        user:        Authenticated user (must own the document).
        engine:      Async SQLAlchemy engine.

    Raises:
        HTTPException: 404 if document not found or not owned by user.
    """
    try:
        await delete_document(
            document_id=document_id,
            user_id=user.id,  # type: ignore[attr-defined]
            engine=engine,  # type: ignore[arg-type]
        )
    except DocumentNotFoundError:
        raise HTTPException(status_code=404, detail="Document not found")


@router.patch("/{document_id}", response_model=DocumentResponse)
async def patch_document_route(
    document_id: int,
    request: Request,
    project_id: int | None = Form(default=None),
    clear: str | None = Form(default=None),
    user: User = Depends(require_user),
    engine: object = Depends(get_engine_dep),
    models_service: object = Depends(get_models_service_dep),
) -> DocumentResponse:
    """Move a document into a project or detach it.

    Field semantics mirror the chat PATCH:
    - ``project_id`` set → move into that project (404 on unknown
      / cross-user project).
    - ``clear=project_id`` → detach (set to NULL).
    - Both omitted → no-op (200 with current state).

    Ownership of the target project is enforced via
    ``ProjectsService.get`` BEFORE the document is updated; ownership
    of the document itself is enforced by ``set_document_project_id``
    via its WHERE clause.

    Raises:
        HTTPException: 404 if the document OR (when set) the target
            project is not found / not owned by the caller.
        HTTPException: 422 on unknown ``clear=`` field name.
    """
    # Shared parser — one parser, three routes.
    from lmchat.routes._form_utils import parse_clear  # noqa: PLC0415

    clear_set = parse_clear(clear, allowed=frozenset({"project_id"}))

    target_project_id: int | None = None
    will_mutate = False
    if "project_id" in clear_set:
        target_project_id = None
        will_mutate = True
    elif project_id is not None:
        # Ownership check on the target project.
        projects_svc = getattr(
            request.app.state, "projects_service", None
        )
        if projects_svc is None:
            raise RuntimeError(
                "app.state.projects_service is unset"
            )
        owned = await projects_svc.get(
            user_id=user.id,  # type: ignore[attr-defined]
            project_id=project_id,
        )
        if owned is None:
            raise HTTPException(
                status_code=404, detail="project not found"
            )
        target_project_id = project_id
        will_mutate = True

    if will_mutate:
        try:
            await set_document_project_id(
                document_id=document_id,
                user_id=user.id,  # type: ignore[attr-defined]
                project_id=target_project_id,
                engine=engine,  # type: ignore[arg-type]
                models_service=models_service,  # type: ignore[arg-type]
            )
        except DocumentNotFoundError:
            raise HTTPException(
                status_code=404, detail="Document not found"
            )
        except EmbeddingModelPinConflict as exc:
            # Translate to 409 with the standard re-embed body shape so
            # the UI banner renders the re-embed flow without a second
            # round trip.
            from lmchat.routes._form_utils import (
                embedding_pin_conflict_response,
            )

            raise HTTPException(
                status_code=409,
                detail=embedding_pin_conflict_response(exc),
            )

    # Return the fresh document row (single-row fetch instead of the
    # O(N) list+scan the route previously used).
    fresh = await get_document(
        document_id=document_id,
        user_id=user.id,  # type: ignore[attr-defined]
        engine=engine,  # type: ignore[arg-type]
    )
    if fresh is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return _doc_to_response(fresh)


@router.get("/{document_id}/chunks", response_model=list[ChunkPreview])
async def get_chunks_route(
    document_id: int,
    user: User = Depends(require_user),
    engine: object = Depends(get_engine_dep),
) -> list[ChunkPreview]:
    """Return chunk previews for a document.

    Returns ``list[ChunkPreview]`` — no envelope per Invariant 3.

    Args:
        document_id: PK of the document.
        user:        Authenticated user (must own the document).
        engine:      Async SQLAlchemy engine.

    Returns:
        List of :class:`ChunkPreview` ordered by chunk ordinal.

    Raises:
        HTTPException: 404 if document not found or not owned by user.
    """
    try:
        chunks = await get_document_chunks(
            document_id=document_id,
            user_id=user.id,  # type: ignore[attr-defined]
            engine=engine,  # type: ignore[arg-type]
        )
    except DocumentNotFoundError:
        raise HTTPException(status_code=404, detail="Document not found")

    return [ChunkPreview(ordinal=c["ordinal"], text_preview=c["text_preview"]) for c in chunks]
