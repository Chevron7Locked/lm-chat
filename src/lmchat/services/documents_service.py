# SPDX-License-Identifier: Apache-2.0
"""Document ingest service for lm-chat RAG pipeline.

Responsibilities
----------------
- ``upload_document`` — dedup via sha256; persist document row; chunk +
  embed in-process.
- ``chunk_and_embed`` — extract text by MIME type, chunk to ~500-token
  windows with 50-token overlap, embed via ``EmbeddingClient``, persist
  chunk rows.
- ``list_documents`` — user-scoped list of non-deleted documents.
- ``delete_document`` — soft-delete + cascade chunks.

Text extraction by MIME type
----------------------------
- ``text/plain``, ``text/markdown``: direct UTF-8 decode.
- ``application/pdf``: ``pypdf`` extraction; image-only PDFs raise
  ``ImageOnlyPdfError`` (route → HTTP 422).
- ``text/html``: tag stripping via ``beautifulsoup4`` (``lxml`` parser).
- ``application/epub+zip``: chapters read in OPF spine order (``zipfile``
  + ``xml.etree.ElementTree``), each stripped like ``text/html``. Falls
  back to every ``*.xhtml``/``*.html``/``*.htm`` entry (sorted by name)
  if the container/OPF can't be parsed.
- docx (``application/vnd.openxmlformats-officedocument.wordprocessingml.document``):
  paragraphs read from ``word/document.xml``; runs, tabs, and line breaks
  preserved.
- Malformed EPUB/DOCX or no extractable text: ``DocumentParseError``
  (route → HTTP 422).
- Anything else: ``UnsupportedMimeTypeError`` (route → HTTP 415).

Chunking algorithm
------------------
500-token windows (tiktoken ``cl100k_base``), 50-token overlap so
boundary-straddling sentences appear in both neighboring chunks.

Embedding
---------
``EmbeddingClient.embed_batch`` with the default embedding model from
``ModelsService``.

Dedup
-----
SHA-256 of raw file bytes. An existing non-deleted row for
``(user_id, sha256)`` is returned without re-inserting.
"""
from __future__ import annotations

import hashlib
import posixpath
import zipfile
from datetime import UTC, datetime
from io import BytesIO
from typing import Any, Final

import defusedxml.ElementTree as ET
import tiktoken
from defusedxml import DefusedXmlException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from lmchat.db.retry import with_write_retry
from lmchat.db.schema import document_chunks, documents, projects
from lmchat.embedding.client import EmbeddingClient
from lmchat.embedding.vector_math import pack_embedding as _pack_embedding
from lmchat.logging import get_logger
from lmchat.services.memory_service import (
    EMBEDDING_ERROR_REASON_PREFERRED_NOT_LOADED,
    NoEmbeddingModelLoadedError,
)
from lmchat.services.memory_service import (
    resolve_active_embedding_model_key as _resolve_embedding_key,
)
from lmchat.services.models_service import ModelsService
from lmchat.utils.text_hash import normalize_for_hash, text_hash

log = get_logger(__name__)

# Chunk size constants (in tokens).
_CHUNK_TOKENS: Final[int] = 500
_CHUNK_OVERLAP: Final[int] = 50

# cl100k_base covers GPT-4 / text-embedding-3 vocabulary; for local models
# the exact tokenizer may differ but counts stay consistent within a session.
_ENCODING = tiktoken.get_encoding("cl100k_base")

# Supported MIME types for text extraction.
_TEXT_PLAIN = "text/plain"
_TEXT_MARKDOWN = "text/markdown"
_APPLICATION_PDF = "application/pdf"
_TEXT_HTML = "text/html"
_APPLICATION_DOCX = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_APPLICATION_EPUB = "application/epub+zip"
_SUPPORTED_TYPES: Final[frozenset[str]] = frozenset({
    _TEXT_PLAIN,
    _TEXT_MARKDOWN,
    _APPLICATION_PDF,
    _TEXT_HTML,
    _APPLICATION_DOCX,
    _APPLICATION_EPUB,
})


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class DocumentsServiceError(Exception):
    """Base class for DocumentsService errors."""


class ImageOnlyPdfError(DocumentsServiceError):
    """Raised when a PDF has no extractable text (image-only / scanned).

    The route translates this to HTTP 422 with a user-facing message
    explaining that OCR is post-1.0 scope.
    """


class DocumentParseError(DocumentsServiceError):
    """Raised when an EPUB or DOCX container cannot be parsed or usefully read.

    Covers malformed containers declared as EPUB/DOCX (not a valid ZIP,
    missing required entries such as ``word/document.xml`` or
    ``META-INF/container.xml``, unparsable XML) as well as structurally
    valid containers that yield no extractable text.

    The route translates this to HTTP 422 with a user-facing message.
    """


class UnsupportedMimeTypeError(DocumentsServiceError):
    """Raised for MIME types that are not yet supported.

    The route translates this to HTTP 415.

    Args:
        mime_type: The unsupported MIME type string.
    """

    def __init__(self, mime_type: str) -> None:
        super().__init__(f"Unsupported MIME type: {mime_type!r}")
        self.mime_type = mime_type


class MimeTypeMismatchError(DocumentsServiceError):
    """Raised when file magic bytes do not match the declared content type.

    The route translates this to HTTP 415 with a specific message
    distinguishing it from an unsupported type.

    Args:
        content_type: The declared MIME type.
        expected_sig: Human-readable description of the expected signature.
    """

    def __init__(self, content_type: str, expected_sig: str) -> None:
        super().__init__(
            f"Content-Type {content_type!r} declared but file signature "
            f"does not match (expected {expected_sig})"
        )
        self.content_type = content_type
        self.expected_sig = expected_sig


class DocumentNotFoundError(DocumentsServiceError):
    """Raised when a document cannot be found (or access is denied)."""


class EmbeddingModelPinConflict(DocumentsServiceError):
    """Write-once-on-attach violation.

    Raised when attaching into a project pinned to embedding model A while
    the currently active embedding model is B. The admin must re-embed
    under the new model before the attach can proceed.

    Caught by the route layer and translated to a 409 with body
    ``{embedding_model_id, active_embedding_model_id, re_embed_url}``.
    """

    def __init__(
        self,
        *,
        project_id: int,
        pinned_model_id: str,
        active_model_id: str,
    ) -> None:
        super().__init__(
            f"project {project_id} is pinned to embedding model "
            f"{pinned_model_id!r}; active is {active_model_id!r}"
        )
        self.project_id = project_id
        self.pinned_model_id = pinned_model_id
        self.active_model_id = active_model_id


# ---------------------------------------------------------------------------
# Public Pydantic models
# ---------------------------------------------------------------------------


class Document(BaseModel):
    """One row from the ``documents`` table (non-deleted).

    Attributes:
        id:                 Row PK.
        user_id:            Owning user PK.
        title:              Display name (filename at upload time).
        mime_type:          MIME type string.
        byte_size:          Raw file size in bytes.
        chunk_count:        Number of chunks after processing (0 while in progress).
        embedding_model_id: Embedding model used for chunks.
        sha256:             Hex SHA-256 of the raw file bytes.
        uploaded_at:        Timestamp of the upload.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    title: str
    mime_type: str
    byte_size: int
    chunk_count: int
    embedding_model_id: str
    sha256: str
    uploaded_at: datetime
    # Optional project membership.
    project_id: int | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _sha256_hex(data: bytes) -> str:
    """Return the hex SHA-256 digest of *data*."""
    return hashlib.sha256(data).hexdigest()


# Re-exported under the module-private names so existing call sites are
# unaffected. See lmchat.utils.text_hash for the shared implementation —
# both this module and memory_service key their dedup rows off the same
# hash, so the algorithm lives in one place.
_normalize = normalize_for_hash
_text_hash = text_hash


# See lmchat.embedding.vector_math for the packed embedding storage format.


def _validate_magic_bytes(content_type: str, body_bytes: bytes) -> None:
    """Cross-check *body_bytes* magic bytes against the declared *content_type*.

    Text types have no magic byte and are skipped. HTML checks for a
    leading ``<`` (after stripping whitespace/BOM); epub/docx check for
    the ZIP local file header (``PK\x03\x04``) since both are ZIP
    containers.

    Args:
        content_type: Normalised MIME type string (no parameters).
        body_bytes:   Raw file bytes (may be empty).

    Raises:
        MimeTypeMismatchError: If the magic bytes do not match the declared
            content type.
    """
    mt = content_type.split(";")[0].strip().lower()

    if mt == _APPLICATION_PDF:
        # PDF files must start with the 5-byte signature ``%PDF-``.
        if not body_bytes.startswith(b"%PDF-"):
            raise MimeTypeMismatchError(
                content_type, "PDF magic bytes (%PDF-)"
            )
        return

    if mt == _TEXT_HTML:
        # HTML should start with ``<`` after stripping whitespace and the
        # UTF-8 BOM (lstrip only handles ASCII whitespace).
        stripped = body_bytes.lstrip()
        if stripped.startswith(b"\xef\xbb\xbf"):
            stripped = stripped[3:]
        if stripped and not stripped.startswith(b"<"):
            raise MimeTypeMismatchError(
                content_type, "HTML opening tag (<)"
            )
        return

    if mt in (_APPLICATION_EPUB, _APPLICATION_DOCX):
        # Both are ZIP containers; check the ZIP local file header.
        if not body_bytes.startswith(b"PK\x03\x04"):
            raise MimeTypeMismatchError(
                content_type, "ZIP magic bytes (PK\\x03\\x04)"
            )
        return

    # text/plain, text/markdown — no magic byte; other unsupported types
    # are rejected later by _extract_text.


def _extract_text(body_bytes: bytes, mime_type: str) -> str:
    """Extract plain text from *body_bytes* according to *mime_type*.

    Args:
        body_bytes: Raw file bytes.
        mime_type:  MIME type string (stripped of parameters).

    Returns:
        Extracted plain text string.

    Raises:
        ImageOnlyPdfError:      If the PDF has no extractable text.
        DocumentParseError:     If an EPUB/DOCX container cannot be parsed
            or yields no extractable text.
        UnsupportedMimeTypeError: If the MIME type is not supported.
    """
    mt = mime_type.split(";")[0].strip().lower()

    if mt in (_TEXT_PLAIN, _TEXT_MARKDOWN):
        return body_bytes.decode("utf-8", errors="replace")

    if mt == _TEXT_HTML:
        return _strip_html(body_bytes)

    if mt == _APPLICATION_PDF:
        from pypdf import PdfReader  # type: ignore[import-untyped]
        from pypdf.errors import PyPdfError  # type: ignore[import-untyped]

        # A corrupt/truncated PDF makes pypdf raise a PyPdfError subclass —
        # treat as bad input (422), not a server fault (mirrors the
        # BadZipFile handling in the epub/docx extractors).
        try:
            reader = PdfReader(BytesIO(body_bytes))
            pages_text: list[str] = []
            for page in reader.pages:
                pt = page.extract_text() or ""
                pages_text.append(pt)
        except PyPdfError as exc:
            raise DocumentParseError(
                "Could not parse PDF — the file is corrupt or not a valid PDF."
            ) from exc

        combined = "\n".join(pages_text).strip()
        if not combined:
            raise ImageOnlyPdfError(
                "This PDF appears to be image-only; OCR is post-1.0 scope."
            )
        return combined

    if mt == _APPLICATION_EPUB:
        return _extract_epub_text(body_bytes)

    if mt == _APPLICATION_DOCX:
        return _extract_docx_text(body_bytes)

    raise UnsupportedMimeTypeError(mime_type)


def _strip_html(html_bytes: bytes) -> str:
    """Strip tags from *html_bytes* and return visible text.

    Shared by the ``text/html`` upload path and EPUB chapter extraction
    (an EPUB chapter is XHTML, which BeautifulSoup's ``lxml`` parser
    handles identically to HTML).

    Args:
        html_bytes: Raw (X)HTML bytes.

    Returns:
        Whitespace-normalised visible text with script/style content
        removed.
    """
    from bs4 import BeautifulSoup  # type: ignore[import-untyped]

    soup = BeautifulSoup(html_bytes, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text(separator=" ", strip=True)


# ---------------------------------------------------------------------------
# EPUB extraction
# ---------------------------------------------------------------------------

# OPF/container XML namespaces (EPUB 2 and 3 both use these).
_EPUB_CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
_EPUB_OPF_NS = "http://www.idpf.org/2007/opf"
_EPUB_HTML_MEDIA_TYPES = frozenset({"application/xhtml+xml", "text/html"})


def _extract_epub_text(body_bytes: bytes) -> str:
    """Extract chapter text from an EPUB container, in reading order.

    Reads ``META-INF/container.xml`` to locate the OPF package document,
    Reads ``META-INF/container.xml`` to locate the OPF package, walks its
    ``<manifest>``/``<spine>`` for chapter order, and strips each XHTML
    chapter via :func:`_strip_html`. Falls back to extracting every
    ``*.xhtml``/``*.html``/``*.htm`` entry (sorted by name) when the
    container/OPF can't be parsed or the spine yields no chapters.

    Args:
        body_bytes: Raw EPUB (ZIP) bytes.

    Returns:
        Extracted plain text, chapters separated by ``"\n\n"``.

    Raises:
        DocumentParseError: If the ZIP cannot be opened, or no text can be
            extracted via either path.
    """
    try:
        with zipfile.ZipFile(BytesIO(body_bytes)) as zf:
            chapters = _epub_spine_chapters(zf)
            if not chapters:
                chapters = _epub_fallback_chapters(zf)
    except zipfile.BadZipFile as exc:
        raise DocumentParseError(
            "Could not parse EPUB — the file is not a valid ZIP container."
        ) from exc

    combined = "\n\n".join(c for c in chapters if c.strip()).strip()
    if not combined:
        raise DocumentParseError("Could not extract any text from this EPUB.")
    return combined


def _epub_spine_chapters(zf: zipfile.ZipFile) -> list[str]:
    """Return EPUB chapter text in OPF spine order, or ``[]`` on failure.

    Args:
        zf: Open EPUB ZIP archive.

    Returns:
        List of stripped chapter text, one entry per spine ``<itemref>``
        whose manifest media-type is XHTML/HTML. Empty if the container
        or OPF cannot be parsed, or the spine has no HTML chapters.
    """
    try:
        container_xml = zf.read("META-INF/container.xml")
        container_root = ET.fromstring(container_xml)
        rootfile = container_root.find(f".//{{{_EPUB_CONTAINER_NS}}}rootfile")
        if rootfile is None:
            return []
        opf_path = rootfile.get("full-path")
        if not opf_path:
            return []

        opf_bytes = zf.read(opf_path)
        opf_root = ET.fromstring(opf_bytes)

        manifest: dict[str, tuple[str, str]] = {}
        for item in opf_root.findall(
            f".//{{{_EPUB_OPF_NS}}}manifest/{{{_EPUB_OPF_NS}}}item"
        ):
            item_id = item.get("id")
            href = item.get("href")
            media_type = item.get("media-type", "")
            if item_id and href:
                manifest[item_id] = (href, media_type)

        opf_dir = opf_path.rsplit("/", 1)[0] if "/" in opf_path else ""

        chapters: list[str] = []
        for itemref in opf_root.findall(
            f".//{{{_EPUB_OPF_NS}}}spine/{{{_EPUB_OPF_NS}}}itemref"
        ):
            idref = itemref.get("idref")
            if not idref or idref not in manifest:
                continue
            href, media_type = manifest[idref]
            if media_type not in _EPUB_HTML_MEDIA_TYPES:
                continue
            entry_path = posixpath.normpath(
                f"{opf_dir}/{href}" if opf_dir else href
            )
            try:
                html_bytes = zf.read(entry_path)
            except KeyError:
                continue
            chapters.append(_strip_html(html_bytes))
        return chapters
    except (KeyError, ET.ParseError, DefusedXmlException):
        return []


def _epub_fallback_chapters(zf: zipfile.ZipFile) -> list[str]:
    """Extract every HTML-ish EPUB entry, sorted by name.

    Used when the container/OPF can't be parsed or the spine yields no
    chapters. Name-sort reading order is a heuristic, not guaranteed.

    Args:
        zf: Open EPUB ZIP archive.

    Returns:
        List of stripped chapter text, one per matching entry.
    """
    html_names = sorted(
        name
        for name in zf.namelist()
        if name.lower().endswith((".xhtml", ".html", ".htm"))
    )
    chapters: list[str] = []
    for name in html_names:
        try:
            chapters.append(_strip_html(zf.read(name)))
        except KeyError:
            continue
    return chapters


# ---------------------------------------------------------------------------
# DOCX extraction
# ---------------------------------------------------------------------------

_DOCX_NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_DOCX_TAG_T = f"{{{_DOCX_NS_W}}}t"
_DOCX_TAG_TAB = f"{{{_DOCX_NS_W}}}tab"
_DOCX_TAG_BR = f"{{{_DOCX_NS_W}}}br"
_DOCX_TAG_CR = f"{{{_DOCX_NS_W}}}cr"
_DOCX_TAG_BODY = f"{{{_DOCX_NS_W}}}body"
_DOCX_TAG_P = f"{{{_DOCX_NS_W}}}p"


def _extract_docx_text(body_bytes: bytes) -> str:
    """Extract paragraph text from a DOCX's ``word/document.xml``.

    Walks every ``<w:p>`` under ``<w:body>`` (including nested in tables)
    in document order; ``<w:tab>`` maps to a tab, ``<w:br>``/``<w:cr>`` to
    a newline. Paragraphs are joined with a single newline.

    Args:
        body_bytes: Raw DOCX (ZIP) bytes.

    Returns:
        Extracted plain text.

    Raises:
        DocumentParseError: If the ZIP cannot be opened, ``word/document.xml``
            is missing or malformed, or no extractable text results.
    """
    try:
        with zipfile.ZipFile(BytesIO(body_bytes)) as zf:
            document_xml = zf.read("word/document.xml")
    except zipfile.BadZipFile as exc:
        raise DocumentParseError(
            "Could not parse DOCX — the file is not a valid ZIP container."
        ) from exc
    except KeyError as exc:
        raise DocumentParseError(
            "Could not parse DOCX — word/document.xml is missing."
        ) from exc

    try:
        root = ET.fromstring(document_xml)
    except (ET.ParseError, DefusedXmlException) as exc:
        raise DocumentParseError(
            "Could not parse DOCX — word/document.xml is malformed."
        ) from exc

    body = root.find(_DOCX_TAG_BODY)
    if body is None:
        raise DocumentParseError("Could not extract any text from this DOCX.")

    paragraphs: list[str] = []
    for p in body.iter(_DOCX_TAG_P):
        runs: list[str] = []
        for node in p.iter():
            if node.tag == _DOCX_TAG_T:
                runs.append(node.text or "")
            elif node.tag == _DOCX_TAG_TAB:
                runs.append("\t")
            elif node.tag in (_DOCX_TAG_BR, _DOCX_TAG_CR):
                runs.append("\n")
        paragraphs.append("".join(runs))

    combined = "\n".join(paragraphs).strip()
    if not combined:
        raise DocumentParseError("Could not extract any text from this DOCX.")
    return combined


def _chunk_text(text: str) -> list[str]:
    """Split *text* into overlapping ~500-token chunks.

    Uses tiktoken ``cl100k_base`` for token counting. The final chunk
    may be shorter than ``_CHUNK_TOKENS``.

    Args:
        text: Plain text to chunk.

    Returns:
        List of chunk strings (may be empty if text is empty).
    """
    if not text.strip():
        return []

    tokens = _ENCODING.encode(text)
    chunks: list[str] = []
    start = 0

    while start < len(tokens):
        end = min(start + _CHUNK_TOKENS, len(tokens))
        chunk_tokens = tokens[start:end]
        chunk_text = _ENCODING.decode(chunk_tokens)
        chunks.append(chunk_text)

        if end == len(tokens):
            break
        # Advance by (CHUNK_TOKENS - OVERLAP) to create overlap.
        start += _CHUNK_TOKENS - _CHUNK_OVERLAP

    return chunks


# ---------------------------------------------------------------------------
# B1 — embedding-model pin enforcement (write-once-on-attach)
# ---------------------------------------------------------------------------


async def _resolve_active_embedding_model_id(
    models_service: ModelsService,
    engine: AsyncEngine | None = None,
) -> str | None:
    """Return the currently active embedding model's key, or None.

    Pin-enforcement path (no ``engine``): returns the FIRST loaded
    embedding model key — the pin gate asks "what is the admin loading
    RIGHT NOW?", distinct from "what is the globally preferred model?",
    so the stable resolver is intentionally not used here.

    When ``engine`` is passed, delegates to the stable resolver
    (:func:`~lmchat.services.memory_service.resolve_active_embedding_model_key`)
    so new-indexing paths (upload + re-embed) use the persisted preference
    and fail loud when it's unloaded.

    Returns:
        Catalog key string, or ``None`` when no embedding model is loaded.
    """
    if engine is not None:
        # New-indexing path: use the stable resolver (persist + fail-loud).
        try:
            return await _resolve_embedding_key(
                engine=engine,
                models_service=models_service,
                persist_default=True,
            )
        except NoEmbeddingModelLoadedError as exc:
            # Re-raise only "preferred set but not loaded" — the admin's
            # own explicit choice failing should fail loud immediately.
            # Matched on the stable exc.reason attribute, not message text.
            #
            # Returning None for other variants doesn't silently skip
            # embedding: chunk_and_embed re-resolves via this same
            # fail-loud resolver further down every upload, so it still
            # fails end-to-end. This swallow only lets
            # _enforce_embedding_pin_or_pin's "no active model" no-op path
            # run instead of raising before it.
            if exc.reason == EMBEDDING_ERROR_REASON_PREFERRED_NOT_LOADED:
                raise
            return None

    # Pin-enforcement path (no engine): first ACTUALLY-loaded wins. Without
    # the loaded_instance_ids guard, an unloaded catalog entry could win
    # the scan and pin the project to an unqueryable model. Sorted for
    # determinism (matches the stable resolver's lexicographic pick).
    loaded = await models_service.list_loaded()
    for m in sorted(loaded, key=lambda x: x.key):
        if m.type == "embedding" and m.loaded_instance_ids:
            return m.key
    return None


async def _enforce_embedding_pin_or_pin(
    *,
    project_id: int,
    user_id: int,
    engine: AsyncEngine,
    models_service: ModelsService,
    active_override: str | None = None,
) -> str | None:
    """Write-once-on-attach invariant.

    Called by both attach paths (``upload_document`` for new uploads,
    ``set_document_project_id`` for moves).

    * Project's ``embedding_model_id IS NULL`` (or empty) → first attach;
      pin it to the active embedding model via a compare-and-swap UPDATE.
    * Project's pin matches active model → no-op, allowed.
    * Project's pin mismatches active model → raise
      :class:`EmbeddingModelPinConflict` (route → 409 with
      ``{embedding_model_id, active_embedding_model_id, re_embed_url}``).
    * No active embedding model loaded → graceful skip (NULL fallback).

    Concurrency: the compare-and-swap UPDATE is atomic at the DB level, so
    two concurrent attaches on the same project see exactly one winner;
    the loser falls through to a SELECT to read what won and either
    no-ops or raises. What this does NOT close: the "active embedding
    model" is admin-controlled LM Studio runtime state, not transactional
    truth — we resolve ``active`` ONCE per attach so the pin and the
    chunk vectors stay internally consistent for THIS attach; a later
    admin swap doesn't retroactively re-pin.

    The HTTP call to LM Studio stays OUTSIDE ``engine.begin()`` so the DB
    writer lock isn't held during the network round trip.

    Args:
        project_id:      Target project to attach into. Caller verifies
                         ownership upstream.
        user_id:         Owning user PK (defense-in-depth).
        engine:          Async SQLAlchemy engine.
        models_service:  Resolves the currently active embedding model
                         when ``active_override`` is None.
        active_override: Caller-supplied active model id (resolved once
                         up the stack). When set, skips the internal
                         ``models_service.list_loaded`` call.

    Returns:
        The active embedding model id (``active_override`` if supplied,
        otherwise the resolved one). ``None`` when no embedding model is
        loaded — caller's downstream code must tolerate that.

    Raises:
        EmbeddingModelPinConflict: Mismatched active vs pinned model.
    """
    # Resolve active OUTSIDE the transaction (HTTP round trip must not hold
    # the DB writer lock). Do NOT pass engine here: the pin gate needs the
    # first-ACTUALLY-loaded model, not the persisted preference — routing
    # through the stable resolver would persist a preference as a side
    # effect and could raise before pin-conflict logic runs. The upload
    # path resolves the active model once up-stack and passes it as
    # active_override so pin and chunk embeddings land under the same model.
    active = (
        active_override
        if active_override is not None
        else await _resolve_active_embedding_model_id(models_service)
    )
    if active is None:
        # No embedding model loaded — let the attach proceed without a
        # pin; chunk_and_embed will raise when it tries to embed.
        return None

    # Compare-and-swap: UPDATE wins (sets the pin) or matches 0 rows; on
    # 0 rows, SELECT to decide no-op / conflict / missing row.
    async with engine.begin() as conn:
        update_result = await conn.execute(
            update(projects)
            .where(
                projects.c.id == project_id,
                projects.c.user_id == user_id,
                (projects.c.embedding_model_id.is_(None))
                | (projects.c.embedding_model_id == ""),
            )
            .values(embedding_model_id=active)
        )
        if update_result.rowcount > 0:
            log.info(
                "documents_service.embedding_pin_set",
                project_id=project_id,
                user_id=user_id,
                embedding_model_id=active,
            )
            return active

        pinned_row = (
            await conn.execute(
                select(projects.c.embedding_model_id).where(
                    projects.c.id == project_id,
                    projects.c.user_id == user_id,
                )
            )
        ).fetchone()

    pinned: str | None = (
        pinned_row.embedding_model_id if pinned_row is not None else None
    )

    if pinned is None or pinned == "":
        # Row not found (or owned by someone else); upstream ownership
        # checks would have raised already — treat as graceful no-op.
        return active

    if pinned != active:
        raise EmbeddingModelPinConflict(
            project_id=project_id,
            pinned_model_id=pinned,
            active_model_id=active,
        )
    return active


# ---------------------------------------------------------------------------
# Service functions
# ---------------------------------------------------------------------------


async def upload_document(
    *,
    user_id: int,
    filename: str,
    content_type: str,
    body_bytes: bytes,
    engine: AsyncEngine,
    embedding_client: EmbeddingClient,
    models_service: ModelsService,
    project_id: int | None = None,
) -> Document:
    """Upload a document, chunk and embed it, return the Document row.

    Dedup: if a non-deleted document with the same sha256 already exists
    for this user, the existing document is returned without re-processing.

    Args:
        user_id:          Owning user PK.
        filename:         Original filename (used as title).
        content_type:     MIME type string (may include parameters).
        body_bytes:       Raw file bytes.
        engine:           Async SQLAlchemy engine.
        embedding_client: Embedding client.
        models_service:   For resolving the default embedding model.

    Returns:
        The :class:`Document` row (new or existing dedup match).

    Raises:
        ImageOnlyPdfError:      If PDF has no extractable text.
        UnsupportedMimeTypeError: If MIME type is not supported.
    """
    sha = _sha256_hex(body_bytes)
    mime = content_type.split(";")[0].strip().lower()

    # Magic-byte validation before extraction — defense-in-depth against
    # renamed files (e.g. an .exe renamed to .pdf).
    _validate_magic_bytes(mime, body_bytes)

    # Validate extraction before writing to DB — no partial writes on a
    # bad MIME type or image-only PDF.
    _ = _extract_text(body_bytes, mime)

    # Enforce write-once-on-attach before any DB writes. Resolve
    # ``active`` ONCE and pass it to both the pin gate and chunk_and_embed
    # so the pin and chunk embeddings land under the same model id —
    # eliminates a double-resolve TOCTOU where a model swap mid-upload
    # could pin the project to model A but embed chunks under model B.
    resolved_active_embedding: str | None = (
        await _resolve_active_embedding_model_id(models_service, engine)
    )
    if project_id is not None:
        await _enforce_embedding_pin_or_pin(
            project_id=project_id,
            user_id=user_id,
            engine=engine,
            models_service=models_service,
            active_override=resolved_active_embedding,
        )

    # Dedup is scoped to (user_id, sha256, project_id) — the same bytes
    # re-uploaded into a DIFFERENT project must get their OWN row, never
    # short-circuit to another project's (would misattribute the doc and
    # leave this project's just-set pin dangling). unscoped=True always
    # yields a concrete scope clause, never the "no filter" case.
    from lmchat.db.scope import project_scope_clause

    dedup_project_clause = project_scope_clause(
        documents.c.project_id,
        project_id=project_id,
        unscoped=True,
    )
    assert dedup_project_clause is not None

    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(documents).where(
                    documents.c.user_id == user_id,
                    documents.c.sha256 == sha,
                    documents.c.deleted_at.is_(None),
                    dedup_project_clause,
                )
            )
        ).fetchone()

    if row is not None:
        log.info(
            "documents_service.upload_dedup",
            user_id=user_id,
            document_id=row.id,
            sha256=sha,
        )
        return Document.model_validate(row, from_attributes=True)

    # Insert the document row (chunk_count=0 until chunk_and_embed finishes).
    doc_id_holder: list[int] = []

    async def _insert_doc() -> None:
        async with engine.begin() as conn:
            values: dict[str, Any] = {
                "user_id": user_id,
                "title": filename,
                "mime_type": mime,
                "byte_size": len(body_bytes),
                "chunk_count": 0,
                "embedding_model_id": "",
                "sha256": sha,
                "deleted_at": None,
            }
            if project_id is not None:
                values["project_id"] = project_id
            result = await conn.execute(insert(documents).values(**values))
            pk = result.inserted_primary_key
            if pk is None:
                raise RuntimeError("INSERT into documents returned no PK")
            doc_id_holder.append(int(pk[0]))

    await with_write_retry(_insert_doc)
    doc_id = doc_id_holder[0]

    log.info(
        "documents_service.upload_created",
        user_id=user_id,
        document_id=doc_id,
        filename=filename,
        mime_type=mime,
        byte_size=len(body_bytes),
    )

    # Chunk and embed inline, passing the pre-resolved active model id so
    # chunks land under the same model the pin gate enforced.
    #
    # Compensating soft-delete on embed failure: the INSERT above and
    # chunk_and_embed run in separate transactions, so a raise here would
    # otherwise leave the row committed at chunk_count=0 forever AND still
    # matching the sha256 dedup predicate — re-uploads would return the
    # dead row instead of reprocessing. Soft-delete keeps it auditable
    # while making the dedup predicate skip it so a retry re-inserts cleanly.
    try:
        chunk_count = await chunk_and_embed(
            document_id=doc_id,
            body_bytes=body_bytes,
            mime_type=mime,
            engine=engine,
            embedding_client=embedding_client,
            models_service=models_service,
            active_model_id_override=resolved_active_embedding,
        )
    except Exception:
        compensate_at = datetime.now(UTC)

        async def _soft_delete_orphan() -> None:
            async with engine.begin() as conn:
                await conn.execute(
                    update(documents)
                    .where(documents.c.id == doc_id)
                    .values(deleted_at=compensate_at)
                )

        await with_write_retry(_soft_delete_orphan)
        log.warning(
            "documents_service.upload_embed_failed_soft_deleted",
            user_id=user_id,
            document_id=doc_id,
            sha256=sha,
        )
        raise

    # Fetch the committed row to return.
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(documents).where(documents.c.id == doc_id)
            )
        ).fetchone()

    if row is None:
        raise RuntimeError(f"documents row {doc_id!r} vanished after INSERT")

    log.info(
        "documents_service.upload_complete",
        user_id=user_id,
        document_id=doc_id,
        chunk_count=chunk_count,
    )
    return Document.model_validate(row, from_attributes=True)


async def chunk_and_embed(
    *,
    document_id: int,
    body_bytes: bytes,
    mime_type: str,
    engine: AsyncEngine,
    embedding_client: EmbeddingClient,
    models_service: ModelsService,
    active_model_id_override: str | None = None,
) -> int:
    """Extract text, chunk, embed, and persist chunks. Returns chunk count.

    Args:
        document_id:              PK of the document row (already
                                  inserted).
        body_bytes:               Raw file bytes.
        mime_type:                MIME type (already validated).
        engine:                   Async SQLAlchemy engine.
        embedding_client:         Embedding client.
        models_service:           For resolving the default embedding
                                  model when ``active_model_id_override``
                                  is None.
        active_model_id_override: Caller-supplied embedding model id
                                  (resolved once up the stack); skips the
                                  duplicate ``list_loaded()`` call so pin
                                  and chunks land under the same model
                                  even if the admin swaps loaded models
                                  mid-upload.

    Returns:
        Number of chunks inserted.

    Raises:
        RuntimeError:               If no embedding model is loaded.
        EmbeddingError (from client): On embedding failure.
    """
    # Prefer caller's override to avoid double-resolving; fall back to
    # the stable resolver (NoEmbeddingModelLoadedError propagates to the
    # route layer, which surfaces a clear error to the admin).
    if active_model_id_override is not None:
        model_id: str = active_model_id_override
    else:
        model_id = await _resolve_embedding_key(
            engine=engine,
            models_service=models_service,
            persist_default=True,
        )

    # Extract + chunk.
    text = _extract_text(body_bytes, mime_type)
    chunk_texts = _chunk_text(text)

    if not chunk_texts:
        # Empty document — update chunk_count=0, embedding_model_id.
        async def _update_empty() -> None:
            async with engine.begin() as conn:
                await conn.execute(
                    update(documents)
                    .where(documents.c.id == document_id)
                    .values(chunk_count=0, embedding_model_id=model_id)
                )

        await with_write_retry(_update_empty)
        return 0

    # Embed in one batch.
    vectors = await embedding_client.embed_batch(texts=chunk_texts, model_id=model_id)

    # Validate embedding dimensions — a mismatch indicates a misconfigured
    # or misbehaving embedding model; reject and log rather than storing
    # corrupt blobs.
    if vectors:
        expected_dim = len(vectors[0])
        for idx, vec in enumerate(vectors):
            if len(vec) != expected_dim:
                log.warning(
                    "documents_service.embedding_dim_mismatch",
                    document_id=document_id,
                    chunk_index=idx,
                    expected_dim=expected_dim,
                    actual_dim=len(vec),
                    model_id=model_id,
                )
                raise RuntimeError(
                    f"Embedding dimension mismatch at chunk {idx}: "
                    f"expected {expected_dim}, got {len(vec)} "
                    f"(model={model_id!r}). "
                    "Check that the embedding model is configured correctly."
                )

    # Build chunk rows.
    chunk_rows = []
    for ordinal, (chunk_text, vector) in enumerate(
        zip(chunk_texts, vectors, strict=False)
    ):
        normalized = _normalize(chunk_text)
        t_hash = _text_hash(normalized)
        blob = _pack_embedding(vector)
        chunk_rows.append({
            "document_id": document_id,
            "ordinal": ordinal,
            "text": chunk_text,
            "text_hash": t_hash,
            "embedding": blob,
            "embedding_model_id": model_id,
        })

    # Insert chunks + update chunk_count in a single transaction.
    async def _insert_chunks() -> None:
        async with engine.begin() as conn:
            for row in chunk_rows:
                await conn.execute(insert(document_chunks).values(**row))
            await conn.execute(
                update(documents)
                .where(documents.c.id == document_id)
                .values(chunk_count=len(chunk_rows), embedding_model_id=model_id)
            )

    await with_write_retry(_insert_chunks)

    log.info(
        "documents_service.chunks_embedded",
        document_id=document_id,
        chunk_count=len(chunk_rows),
        embedding_model_id=model_id,
    )
    return len(chunk_rows)


async def re_embed_project_documents(
    *,
    user_id: int,
    project_id: int,
    engine: AsyncEngine,
    embedding_client: EmbeddingClient,
    models_service: ModelsService,
) -> dict[str, int | str]:
    """Re-embed every document in *project_id* under the active model.

    Used when the admin swapped the active embedding model after
    documents were already attached — existing chunks are encoded under
    the old model and mis-cosine on retrieval until re-embedded.

    Mechanism: resolve the active embedding model (refuse if none
    loaded); UPDATE ``projects.embedding_model_id`` FIRST so an
    interleaved upload can't pin to the old model; then for each
    document, stream its chunks in ordinal order, re-embed the text
    under the new model, and UPDATE the ``embedding`` blob + the
    document's ``embedding_model_id``.

    Chunk *text* is unchanged — only the embedding bytes are rewritten.

    Args:
        user_id:          Owning user PK (defense-in-depth).
        project_id:       Target project. Ownership enforced upstream
                          by the route layer's ``ProjectsService.get``
                          call.
        engine:           Async SQLAlchemy engine.
        embedding_client: Embedding client.
        models_service:   Resolves the currently active embedding
                          model.

    Returns:
        ``{"documents_re_embedded": int, "chunks_re_embedded": int,
           "active_embedding_model_id": str}`` — the route layer
        bubbles this back to the admin so the UI can show
        "re-embedded N docs / M chunks under <model>".

    Raises:
        RuntimeError: If no embedding model is currently loaded.
    """
    active_model_id = await _resolve_active_embedding_model_id(
        models_service, engine
    )
    if active_model_id is None:
        raise RuntimeError(
            "No embedding model is currently loaded in LM Studio. "
            "Load an embedding model before re-embedding documents."
        )

    # Pin to the new model BEFORE rewriting chunks, or an interleaved
    # upload could still see the old pin.
    async with engine.begin() as conn:
        await conn.execute(
            update(projects)
            .where(
                projects.c.id == project_id,
                projects.c.user_id == user_id,
            )
            .values(embedding_model_id=active_model_id)
        )

    # Collect the document ids in this project.
    async with engine.connect() as conn:
        doc_rows = (
            await conn.execute(
                select(documents.c.id).where(
                    documents.c.user_id == user_id,
                    documents.c.project_id == project_id,
                    documents.c.deleted_at.is_(None),
                )
            )
        ).fetchall()
    doc_ids = [int(r.id) for r in doc_rows]

    chunks_re_embedded = 0
    for doc_id in doc_ids:
        async with engine.connect() as conn:
            chunk_rows = (
                await conn.execute(
                    select(
                        document_chunks.c.id,
                        document_chunks.c.text,
                    )
                    .where(document_chunks.c.document_id == doc_id)
                    .order_by(document_chunks.c.ordinal)
                )
            ).fetchall()
        if not chunk_rows:
            # Empty document — just update its model id pointer.
            async def _update_doc_only(doc_id: int = doc_id) -> None:
                async with engine.begin() as conn:
                    await conn.execute(
                        update(documents)
                        .where(documents.c.id == doc_id)
                        .values(embedding_model_id=active_model_id)
                    )

            await with_write_retry(_update_doc_only)
            continue

        chunk_texts = [str(r.text) for r in chunk_rows]
        chunk_ids = [int(r.id) for r in chunk_rows]
        vectors = await embedding_client.embed_batch(
            texts=chunk_texts, model_id=active_model_id
        )

        async def _update_chunks(
            doc_id: int = doc_id,
            chunk_ids: list[int] = chunk_ids,
            vectors: list = vectors,
        ) -> None:
            async with engine.begin() as conn:
                for chunk_id, vector in zip(
                    chunk_ids, vectors, strict=True
                ):
                    await conn.execute(
                        update(document_chunks)
                        .where(document_chunks.c.id == chunk_id)
                        .values(embedding=_pack_embedding(vector))
                    )
                await conn.execute(
                    update(documents)
                    .where(documents.c.id == doc_id)
                    .values(embedding_model_id=active_model_id)
                )

        await with_write_retry(_update_chunks)
        chunks_re_embedded += len(chunk_ids)

    log.info(
        "documents_service.project_re_embedded",
        user_id=user_id,
        project_id=project_id,
        documents_re_embedded=len(doc_ids),
        chunks_re_embedded=chunks_re_embedded,
        embedding_model_id=active_model_id,
    )
    return {
        "documents_re_embedded": len(doc_ids),
        "chunks_re_embedded": chunks_re_embedded,
        "active_embedding_model_id": active_model_id,
    }


async def _estimate_project_corpus_tokens(
    *,
    engine: AsyncEngine,
    user_id: int,
    project_id: int,
) -> int:
    """Estimate total chunk tokens across non-deleted documents in
    *project_id*. Used by the RAG-mode resolver for INLINE vs HYBRID
    branching, and surfaced via ``GET /api/chats/{id}/rag_mode``.

    Implementation: ``SUM(LENGTH(CAST(text AS BLOB))) / 4``. The
    cast-to-BLOB makes SQLite's ``LENGTH`` return UTF-8 byte count
    instead of codepoint count — matching
    :func:`lmchat.services._token_budget.approx_token_count`'s
    byte-based heuristic so CJK corpora aren't under-counted 2-4x.
    Tokenizer-grade accuracy isn't required for this guardrail.

    Args:
        engine:     Async SQLAlchemy engine.
        user_id:    Owning user PK (defense-in-depth).
        project_id: Target project. Required — un-projected docs are
                    spread across the user's whole library.

    Returns:
        Estimated token count (always ≥ 0). Empty project returns 0.
    """
    from sqlalchemy import cast as _cast
    from sqlalchemy import func as _func
    from sqlalchemy.types import LargeBinary as _LargeBinary

    async with engine.connect() as conn:
        result = await conn.execute(
            select(
                _func.coalesce(
                    _func.sum(
                        _func.length(
                            _cast(document_chunks.c.text, _LargeBinary)
                        )
                    ),
                    0,
                )
            )
            .select_from(
                document_chunks.join(
                    documents,
                    documents.c.id == document_chunks.c.document_id,
                )
            )
            .where(
                documents.c.user_id == user_id,
                documents.c.project_id == project_id,
                documents.c.deleted_at.is_(None),
            )
        )
        total_bytes = int(result.scalar_one() or 0)
    return total_bytes // 4


async def list_documents(
    *,
    user_id: int,
    engine: AsyncEngine,
    project_id: int | None = None,
    unscoped: bool = False,
) -> list[Document]:
    """Return non-deleted documents for *user_id*, newest first.

    Args:
        user_id: Owning user PK.
        engine:  Async SQLAlchemy engine.
        project_id: When non-None, restrict to
            documents in this project.
        unscoped: When True AND project_id is
            None, restrict to un-projected documents
            (``project_id IS NULL``). When False (default), no
            project filter is applied (existing behavior preserved).

    Returns:
        List of :class:`Document`, ordered by ``uploaded_at`` DESC.
    """
    from lmchat.db.scope import project_scope_clause

    stmt = (
        select(documents)
        .where(
            documents.c.user_id == user_id,
            documents.c.deleted_at.is_(None),
        )
        .order_by(documents.c.uploaded_at.desc())
    )
    project_clause = project_scope_clause(
        documents.c.project_id,
        project_id=project_id,
        unscoped=unscoped,
    )
    if project_clause is not None:
        stmt = stmt.where(project_clause)
    async with engine.connect() as conn:
        rows = (await conn.execute(stmt)).fetchall()
    return [Document.model_validate(r, from_attributes=True) for r in rows]


async def get_document(
    *,
    document_id: int,
    user_id: int,
    engine: AsyncEngine,
) -> Document | None:
    """Return the (non-deleted) document if owned by *user_id*, else None.

    Mirror of ``ChatService.get`` for the PATCH document route. Avoids
    the O(N) list+scan path the route was previously using.

    Args:
        document_id: PK of the document.
        user_id:     Must own the document.
        engine:      Async SQLAlchemy engine.

    Returns:
        The :class:`Document` row, or None when missing / not owned /
        soft-deleted.
    """
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(documents).where(
                    documents.c.id == document_id,
                    documents.c.user_id == user_id,
                    documents.c.deleted_at.is_(None),
                )
            )
        ).fetchone()
    if row is None:
        return None
    return Document.model_validate(row, from_attributes=True)


async def set_document_project_id(
    *,
    document_id: int,
    user_id: int,
    project_id: int | None,
    engine: AsyncEngine,
    models_service: ModelsService | None = None,
) -> None:
    """Move document to *project_id* (or detach when None).

    Project ownership is enforced upstream by the route layer (via
    ProjectsService.get); this method only enforces document ownership.

    When attaching AND ``models_service`` is supplied, enforces the
    write-once-on-attach embedding-model invariant. ``models_service``
    defaults to None for backward-compat with detach calls and tests
    that don't exercise the pin.

    Args:
        document_id:    PK of the document.
        user_id:        Must own the document.
        project_id:     Target project_id, or None to detach.
        engine:         Async SQLAlchemy engine.
        models_service: Required for attach to enforce the pin; optional
                        for detach.

    Raises:
        DocumentNotFoundError:     If the document is missing or not
                                   owned by *user_id*.
        EmbeddingModelPinConflict: If attaching into a project whose
                                   embedding-model pin differs from
                                   the active model.
    """
    from sqlalchemy import update as _update

    # Enforce write-once-on-attach for the attach case; detach is exempt,
    # and models_service=None preserves backward-compat for legacy
    # callers that don't supply it.
    if project_id is not None and models_service is not None:
        await _enforce_embedding_pin_or_pin(
            project_id=project_id,
            user_id=user_id,
            engine=engine,
            models_service=models_service,
        )

    async with engine.begin() as conn:
        result = await conn.execute(
            _update(documents)
            .where(
                documents.c.id == document_id,
                documents.c.user_id == user_id,
                documents.c.deleted_at.is_(None),
            )
            .values(project_id=project_id)
        )
    if result.rowcount == 0:
        raise DocumentNotFoundError(
            f"document_id {document_id!r} not found for user {user_id!r}"
        )
    log.info(
        "documents_service.project_id_set",
        document_id=document_id,
        user_id=user_id,
        project_id=project_id,
    )


async def delete_document(
    *,
    document_id: int,
    user_id: int,
    engine: AsyncEngine,
) -> None:
    """Soft-delete a document (sets deleted_at). Cascade chunks.

    Chunks are hard-deleted via the FK cascade; the document row is kept
    with ``deleted_at`` set for audit purposes.

    Args:
        document_id: PK of the document to delete.
        user_id:     Must own the document (enforces tenant isolation).
        engine:      Async SQLAlchemy engine.

    Raises:
        DocumentNotFoundError: If the document does not exist or is not
            owned by *user_id*.
    """
    # Verify ownership.
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(documents.c.id).where(
                    documents.c.id == document_id,
                    documents.c.user_id == user_id,
                    documents.c.deleted_at.is_(None),
                )
            )
        ).fetchone()

    if row is None:
        raise DocumentNotFoundError(
            f"document_id {document_id!r} not found for user {user_id!r}"
        )

    now = datetime.now(UTC)

    async def _soft_delete() -> None:
        async with engine.begin() as conn:
            # Hard-delete chunks via cascade; set deleted_at on document.
            await conn.execute(
                delete(document_chunks).where(
                    document_chunks.c.document_id == document_id
                )
            )
            await conn.execute(
                update(documents)
                .where(documents.c.id == document_id)
                .values(deleted_at=now)
            )

    await with_write_retry(_soft_delete)

    log.info(
        "documents_service.deleted",
        document_id=document_id,
        user_id=user_id,
    )


async def get_document_chunks(
    *,
    document_id: int,
    user_id: int,
    engine: AsyncEngine,
    full_text: bool = False,
) -> list[dict]:  # type: ignore[type-arg]
    """Return chunks for *document_id*, verifying ownership.

    Args:
        document_id: PK of the document.
        user_id:     Must own the document.
        engine:      Async SQLAlchemy engine.
        full_text:   When ``True`` each dict also carries the full
                     ``text`` value (used by the FOCUSED-mode bypass at
                     ``rag_service.augment_prompt``). Default ``False``
                     preserves the legacy UI-preview shape.

    Returns:
        List of dicts. Keys always include ``ordinal`` and
        ``text_preview`` (first 200 chars). When ``full_text=True``
        each dict ALSO carries ``text`` (the full chunk text).

    Raises:
        DocumentNotFoundError: If document not found or not owned by user.
    """
    # Verify ownership.
    async with engine.connect() as conn:
        doc_row = (
            await conn.execute(
                select(documents.c.id).where(
                    documents.c.id == document_id,
                    documents.c.user_id == user_id,
                    documents.c.deleted_at.is_(None),
                )
            )
        ).fetchone()

    if doc_row is None:
        raise DocumentNotFoundError(
            f"document_id {document_id!r} not found for user {user_id!r}"
        )

    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(document_chunks.c.ordinal, document_chunks.c.text)
                .where(document_chunks.c.document_id == document_id)
                .order_by(document_chunks.c.ordinal)
            )
        ).fetchall()

    if full_text:
        return [
            {
                "ordinal": r.ordinal,
                "text_preview": r.text[:200],
                "text": r.text,
            }
            for r in rows
        ]
    return [
        {"ordinal": r.ordinal, "text_preview": r.text[:200]}
        for r in rows
    ]


async def get_project_chunks(
    *,
    project_id: int,
    user_id: int,
    engine: AsyncEngine,
    full_text: bool = False,
) -> list[dict]:  # type: ignore[type-arg]
    """Return ALL chunks across *project_id*'s non-deleted documents.

    Companion to :func:`_estimate_project_corpus_tokens` — mirrors its
    exact ownership/soft-delete predicate so "does the corpus fit under
    the threshold" and "here is the corpus" never disagree about scope.
    Used by ``rag_service.augment_prompt`` to inject the whole project
    corpus when ``resolve_rag_mode`` selects ``RagMode.INLINE``.

    Args:
        project_id: Target project (required — un-projected chunks are
                    spread across the user's whole library and have no
                    single INLINE corpus).
        user_id:    Owning user PK (defense-in-depth).
        engine:     Async SQLAlchemy engine.
        full_text:  When ``True`` each dict carries the FULL ``text``
                    value under the ``text`` key (mirrors
                    ``get_document_chunks``'s ``full_text`` flag).
                    Default ``False`` returns previews only.

    Returns:
        List of dicts ordered by ``(document_id, ordinal)``. Keys
        always include ``document_id``, ``ordinal``, and
        ``text_preview`` (first 200 chars); ``text`` is added when
        ``full_text=True``. Empty project returns ``[]``.
    """
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(
                    document_chunks.c.document_id,
                    document_chunks.c.ordinal,
                    document_chunks.c.text,
                )
                .select_from(
                    document_chunks.join(
                        documents,
                        documents.c.id == document_chunks.c.document_id,
                    )
                )
                .where(
                    documents.c.user_id == user_id,
                    documents.c.project_id == project_id,
                    documents.c.deleted_at.is_(None),
                )
                .order_by(document_chunks.c.document_id, document_chunks.c.ordinal)
            )
        ).fetchall()

    if full_text:
        return [
            {
                "document_id": r.document_id,
                "ordinal": r.ordinal,
                "text_preview": r.text[:200],
                "text": r.text,
            }
            for r in rows
        ]
    return [
        {
            "document_id": r.document_id,
            "ordinal": r.ordinal,
            "text_preview": r.text[:200],
        }
        for r in rows
    ]
