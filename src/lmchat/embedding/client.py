# SPDX-License-Identifier: Apache-2.0
"""LM Studio embedding-model client for lm-chat.

This module is the single integration point for LM Studio's
``/api/v1/embeddings`` endpoint.  The client is stateless and
pure on its injected ``httpx.AsyncClient``; all connection-pool
tuning and auth headers are owned by the caller (``app.py``
lifespan).

Wire-shape reference (harness-verified 2026-05-18):

    POST /api/v1/embeddings
    {"model": "<model-id>", "input": "<text>" | ["<text>", ...]}

Response follows the OpenAI-compat shape:

    {"data": [{"embedding": [...], "index": 0}, ...], ...}

The ``index`` field may not be in the same order as the input list.
:meth:`EmbeddingClient.embed_batch` sorts by ``index`` before returning.

Timeout: reuses ``CHAT_TIMEOUT`` and ``CHAT_LIMITS`` from
``lmstudio_adapter`` — embedding calls on a warm model take
~50–300 ms; cold-start can be longer.  The 600 s read timeout is
the same ceiling used for streaming generations and is acceptable
for embeddings too.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Final

import httpx

from lmchat.embedding.errors import (
    EmbeddingResponseError,
    EmbeddingUpstreamError,
)
from lmchat.logging import get_logger

log = get_logger(__name__)

# Maximum characters of the raw response body to include in error messages.
# Prevents leaking large payloads into structured logs.
_MAX_BODY_IN_ERROR: Final[int] = 200

# Endpoint path appended to base_url.
# LM Studio only exposes the OpenAI-compat /v1/embeddings path; the native
# /api/v1/embeddings path does not exist and returns 404.
_EMBEDDINGS_PATH: Final[str] = "/v1/embeddings"


class EmbeddingClient:
    """Client for LM Studio's ``/v1/embeddings`` endpoint.

    Thread-safety: ``httpx.AsyncClient`` is safe for concurrent use;
    this class adds no shared mutable state.

    Args:
        http_client: Shared ``httpx.AsyncClient`` with auth headers
            pre-loaded by the caller and sized by ``CHAT_LIMITS``.
        base_url:    LM Studio base URL, e.g. ``"http://localhost:1234"``.
    """

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient,
        base_url: str,
        wire_id_resolver: Callable[[str], Awaitable[str | None]] | None = None,
    ) -> None:
        self._http = http_client
        self._base_url = base_url.rstrip("/")
        self._endpoint = f"{self._base_url}{_EMBEDDINGS_PATH}"
        # Optional async resolver mapping a configured/stored embedding key to
        # its LOADED INSTANCE wire id (the @quant-suffixed identifier LM Studio
        # accepts when JIT is disabled). Injected from app.py as
        # ``ModelsService.resolve_embedding_wire_id``. When the resolver returns
        # None (no matching loaded instance), the original model_id is kept so
        # the upstream POST surfaces the same 400/EmbeddingError as before and
        # callers degrade exactly as today. Defaulting to None keeps the client
        # usable standalone (tests / tools) without a ModelsService.
        self._wire_id_resolver = wire_id_resolver

    async def _resolve_wire_id(self, model_id: str) -> str:
        """Resolve *model_id* to a loaded instance wire id via the injected
        resolver; return *model_id* unchanged when no resolver is wired or no
        loaded instance matches (preserves legacy behavior + error surface)."""
        if self._wire_id_resolver is None:
            return model_id
        resolved = await self._wire_id_resolver(model_id)
        if resolved is not None and resolved != model_id:
            log.debug(
                "embedding_client.wire_id_resolved",
                requested_model_id=model_id,
                wire_id=resolved,
            )
        return resolved or model_id

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _truncated(self, raw: str) -> str:
        """Return *raw* truncated to ``_MAX_BODY_IN_ERROR`` chars."""
        if len(raw) <= _MAX_BODY_IN_ERROR:
            return raw
        return raw[:_MAX_BODY_IN_ERROR] + "…"

    async def _post(
        self,
        body: dict,  # type: ignore[type-arg]
        *,
        model_id: str,
    ) -> dict:  # type: ignore[type-arg]
        """POST *body* to the embeddings endpoint and return the parsed JSON.

        Args:
            body:     The request body dict (already contains ``model`` +
                      ``input``).
            model_id: Model identifier — included in log events for
                      observability.

        Returns:
            Parsed JSON dict from the upstream response.

        Raises:
            EmbeddingUpstreamError: On non-200 HTTP status or network error.
        """
        try:
            response = await self._http.post(self._endpoint, json=body)
        except httpx.HTTPError as exc:
            log.warning(
                "embedding_client.network_error",
                model_id=model_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise EmbeddingUpstreamError(
                f"network error calling embeddings endpoint: {exc}",
                status_code=None,
            ) from exc

        if response.status_code != 200:
            body_preview = self._truncated(response.text)
            log.warning(
                "embedding_client.upstream_error",
                model_id=model_id,
                status_code=response.status_code,
                body_preview=body_preview,
            )
            raise EmbeddingUpstreamError(
                f"embeddings endpoint returned {response.status_code}: {body_preview}",
                status_code=response.status_code,
            )

        # Defensive: if upstream returns 200 but a non-JSON body (HTML
        # error page, plain text) the .json() call raises ValueError. Wrap
        # it so callers see a clean EmbeddingResponseError instead of
        # ValueError propagating from inside httpx.
        try:
            return response.json()  # type: ignore[no-any-return]
        except ValueError as exc:
            body_preview = response.text[:200] if response.text else ""
            log.warning(
                "embedding_client.response_decode_error",
                model_id=model_id,
                content_type=response.headers.get("content-type"),
                body_preview=body_preview,
            )
            raise EmbeddingResponseError(
                f"embeddings response is not valid JSON "
                f"(content-type={response.headers.get('content-type')!r}): "
                f"{body_preview}",
                model_id=model_id,
            ) from exc

    @staticmethod
    def _extract_embeddings(
        data: object,
        *,
        expected_count: int,
        model_id: str,
    ) -> list[list[float]]:
        """Validate and extract embeddings from the ``data`` array.

        Args:
            data:           The value under the ``"data"`` key in the
                            upstream response JSON.
            expected_count: How many embeddings are expected.  ``-1``
                            means any non-zero count is acceptable (used
                            by ``embed_one``).
            model_id:       Model identifier for error messages.

        Returns:
            List of embedding vectors sorted by their ``index`` field.

        Raises:
            EmbeddingResponseError: On structural problems.
        """
        if not isinstance(data, list):
            raise EmbeddingResponseError(
                f"embeddings response 'data' is not a list (got {type(data).__name__!r})",
                model_id=model_id,
            )
        if len(data) == 0:
            raise EmbeddingResponseError(
                "embeddings response 'data' array is empty",
                model_id=model_id,
            )

        # Sort by index defensively — spec says the array may not be ordered.
        try:
            items_sorted = sorted(data, key=lambda item: item["index"])
        except (KeyError, TypeError) as exc:
            raise EmbeddingResponseError(
                f"embeddings response item has missing or non-comparable 'index' field: {exc}",
                model_id=model_id,
            ) from exc

        if expected_count >= 0 and len(items_sorted) != expected_count:
            raise EmbeddingResponseError(
                f"expected {expected_count} embedding(s) but upstream returned "
                f"{len(items_sorted)} for model {model_id!r}",
                model_id=model_id,
            )

        vectors: list[list[float]] = []
        for item in items_sorted:
            embedding = item.get("embedding")
            if embedding is None:
                raise EmbeddingResponseError(
                    f"embeddings response item missing 'embedding' field: {item!r}",
                    model_id=model_id,
                )
            if not isinstance(embedding, list):
                raise EmbeddingResponseError(
                    f"'embedding' value is not a list (got {type(embedding).__name__!r})",
                    model_id=model_id,
                )
            vectors.append(embedding)

        return vectors

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def embed_one(self, *, text: str, model_id: str) -> list[float]:
        """Embed a single text string.

        Args:
            text:     The text to embed.
            model_id: Embedding model key or loaded-instance id. Resolved to
                the loaded instance wire id (via the injected resolver) before
                the upstream POST so a bare catalog key from an older stored
                row still hits the @quant-suffixed instance LM Studio expects.

        Returns:
            Embedding vector as a ``list[float]``.

        Raises:
            EmbeddingUpstreamError: On non-200 HTTP status or network error.
            EmbeddingResponseError: On malformed response body.
        """
        wire_id = await self._resolve_wire_id(model_id)
        body = {"model": wire_id, "input": text}
        payload = await self._post(body, model_id=wire_id)

        data = payload.get("data")
        if data is None:
            raise EmbeddingResponseError(
                "embeddings response missing 'data' field",
                model_id=wire_id,
            )

        vectors = self._extract_embeddings(data, expected_count=-1, model_id=wire_id)
        return vectors[0]

    async def embed_batch(
        self,
        *,
        texts: list[str],
        model_id: str,
    ) -> list[list[float]]:
        """Embed a batch of text strings.

        The upstream response ``data`` array may arrive with ``index`` fields
        out of input order; this method sorts by ``index`` before returning so
        callers always receive vectors in the same order as *texts*.

        Args:
            texts:    List of strings to embed.  An empty list returns ``[]``
                      immediately without making an upstream call.
            model_id: Embedding model key or loaded-instance id. Resolved to
                the loaded instance wire id (via the injected resolver) before
                the upstream POST.

        Returns:
            List of embedding vectors, one per input string, in input order.

        Raises:
            EmbeddingUpstreamError: On non-200 HTTP status or network error.
            EmbeddingResponseError: On malformed response body or count
                                    mismatch between inputs and returned
                                    embeddings.
        """
        if not texts:
            return []

        wire_id = await self._resolve_wire_id(model_id)
        body = {"model": wire_id, "input": texts}
        payload = await self._post(body, model_id=wire_id)

        data = payload.get("data")
        if data is None:
            raise EmbeddingResponseError(
                "embeddings response missing 'data' field",
                model_id=wire_id,
            )

        return self._extract_embeddings(
            data,
            expected_count=len(texts),
            model_id=wire_id,
        )
