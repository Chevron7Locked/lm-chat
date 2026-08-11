# SPDX-License-Identifier: Apache-2.0
"""Tests for lmchat.embedding.client — EmbeddingClient.

Tests for the embedding client and the cluster-A specification in
the task description.

All tests mock httpx.AsyncClient.send so no live LM Studio instance
is required.  Fixtures follow the OpenAI-compat embeddings response
shape verified against LM Studio (2026-05-18):

    {"data": [{"embedding": [...], "index": 0}], "model": "...", ...}
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from lmchat.embedding.client import EmbeddingClient
from lmchat.embedding.errors import (
    EmbeddingResponseError,
    EmbeddingUpstreamError,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BASE_URL = "http://localhost:1234"
MODEL_ID = "text-embedding-nomic-embed-text-v1.5"

# A tiny but realistic embedding vector (3 floats for fixture brevity).
_VEC_A: list[float] = [0.1, 0.2, 0.3]
_VEC_B: list[float] = [0.4, 0.5, 0.6]
_VEC_C: list[float] = [0.7, 0.8, 0.9]


def _make_response(
    status_code: int,
    body: Any,
) -> httpx.Response:
    """Build a minimal httpx.Response with a JSON body."""
    content = json.dumps(body).encode()
    return httpx.Response(
        status_code=status_code,
        content=content,
        headers={"Content-Type": "application/json"},
        request=httpx.Request("POST", f"{BASE_URL}/v1/embeddings"),
    )


def _make_client(send_response: httpx.Response | Exception) -> EmbeddingClient:
    """Build an EmbeddingClient whose underlying AsyncClient returns *send_response*.

    If *send_response* is an Exception subclass or instance, the mock raises it
    instead of returning a response.
    """
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    if isinstance(send_response, Exception):
        mock_http.post = AsyncMock(side_effect=send_response)
    else:
        mock_http.post = AsyncMock(return_value=send_response)
    return EmbeddingClient(http_client=mock_http, base_url=BASE_URL)


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


class TestEmbedOneHappyPath:
    """embed_one with a well-formed upstream response."""

    @pytest.mark.asyncio
    async def test_embed_one_happy_path(self) -> None:
        """Single text → returns the embedding vector as list[float]."""
        response = _make_response(
            200,
            {"data": [{"embedding": _VEC_A, "index": 0}]},
        )
        client = _make_client(response)

        result = await client.embed_one(text="hello world", model_id=MODEL_ID)

        assert result == _VEC_A
        assert isinstance(result, list)
        assert all(isinstance(v, float) for v in result)


class TestEmbedBatchHappyPath:
    """embed_batch with a well-formed upstream response."""

    @pytest.mark.asyncio
    async def test_embed_batch_happy_path(self) -> None:
        """List of texts → returns list[list[float]] in input order."""
        response = _make_response(
            200,
            {
                "data": [
                    {"embedding": _VEC_A, "index": 0},
                    {"embedding": _VEC_B, "index": 1},
                    {"embedding": _VEC_C, "index": 2},
                ]
            },
        )
        client = _make_client(response)

        result = await client.embed_batch(
            texts=["one", "two", "three"],
            model_id=MODEL_ID,
        )

        assert result == [_VEC_A, _VEC_B, _VEC_C]
        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_embed_batch_orders_by_index(self) -> None:
        """Shuffled index field in response → result is in input order."""
        # Upstream returns items with index 2, 0, 1 — deliberately shuffled.
        response = _make_response(
            200,
            {
                "data": [
                    {"embedding": _VEC_C, "index": 2},
                    {"embedding": _VEC_A, "index": 0},
                    {"embedding": _VEC_B, "index": 1},
                ]
            },
        )
        client = _make_client(response)

        result = await client.embed_batch(
            texts=["one", "two", "three"],
            model_id=MODEL_ID,
        )

        # Must come back sorted: index 0 first, then 1, then 2.
        assert result[0] == _VEC_A
        assert result[1] == _VEC_B
        assert result[2] == _VEC_C


# ---------------------------------------------------------------------------
# Upstream error tests
# ---------------------------------------------------------------------------


class TestUpstreamErrors:
    """Non-200 and network-level errors raise EmbeddingUpstreamError."""

    @pytest.mark.asyncio
    async def test_embed_one_non_200_raises_upstream_error(self) -> None:
        """500 response → EmbeddingUpstreamError with status_code=500."""
        response = _make_response(500, {"error": "internal"})
        client = _make_client(response)

        with pytest.raises(EmbeddingUpstreamError) as exc_info:
            await client.embed_one(text="hello", model_id=MODEL_ID)

        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_embed_one_400_raises_upstream_error(self) -> None:
        """400 response → EmbeddingUpstreamError with status_code=400."""
        response = _make_response(
            400,
            {"error": {"message": "model not loaded", "type": "invalid_request"}},
        )
        client = _make_client(response)

        with pytest.raises(EmbeddingUpstreamError) as exc_info:
            await client.embed_one(text="hello", model_id=MODEL_ID)

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_embed_batch_network_error_raises_upstream_error(self) -> None:
        """httpx.ConnectError → EmbeddingUpstreamError with status_code=None."""
        connect_error = httpx.ConnectError("Connection refused")
        client = _make_client(connect_error)

        with pytest.raises(EmbeddingUpstreamError) as exc_info:
            await client.embed_batch(texts=["hello"], model_id=MODEL_ID)

        assert exc_info.value.status_code is None


# ---------------------------------------------------------------------------
# Malformed response tests
# ---------------------------------------------------------------------------


class TestMalformedResponses:
    """Well-formed HTTP 200 but structurally invalid JSON body."""

    @pytest.mark.asyncio
    async def test_embed_one_missing_data_field_raises_response_error(self) -> None:
        """Response with no 'data' key → EmbeddingResponseError."""
        response = _make_response(200, {"object": "list"})
        client = _make_client(response)

        with pytest.raises(EmbeddingResponseError):
            await client.embed_one(text="hello", model_id=MODEL_ID)

    @pytest.mark.asyncio
    async def test_embed_one_missing_embedding_in_data_raises_response_error(
        self,
    ) -> None:
        """data[0] has no 'embedding' field → EmbeddingResponseError."""
        response = _make_response(200, {"data": [{"index": 0}]})
        client = _make_client(response)

        with pytest.raises(EmbeddingResponseError):
            await client.embed_one(text="hello", model_id=MODEL_ID)


# ---------------------------------------------------------------------------
# Edge-case tests
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Boundary conditions and guard-rail behaviour."""

    @pytest.mark.asyncio
    async def test_embed_batch_empty_input_returns_empty(self) -> None:
        """embed_batch with empty list returns [] without making an upstream call."""
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.post = AsyncMock()
        client = EmbeddingClient(http_client=mock_http, base_url=BASE_URL)

        result = await client.embed_batch(texts=[], model_id=MODEL_ID)

        assert result == []
        mock_http.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_embed_batch_count_mismatch_raises(self) -> None:
        """N inputs but M != N embeddings in response → EmbeddingResponseError."""
        # 2 inputs but upstream only returns 1 embedding.
        response = _make_response(
            200,
            {"data": [{"embedding": _VEC_A, "index": 0}]},
        )
        client = _make_client(response)

        with pytest.raises(EmbeddingResponseError, match="expected 2"):
            await client.embed_batch(
                texts=["one", "two"],
                model_id=MODEL_ID,
            )

    @pytest.mark.asyncio
    async def test_base_url_trailing_slash_stripped(self) -> None:
        """Trailing slash on base_url is stripped — no double-slash in the URL."""
        response = _make_response(
            200,
            {"data": [{"embedding": _VEC_A, "index": 0}]},
        )
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.post = AsyncMock(return_value=response)
        client = EmbeddingClient(
            http_client=mock_http,
            base_url="http://localhost:1234/",  # trailing slash
        )

        await client.embed_one(text="hello", model_id=MODEL_ID)

        called_url = mock_http.post.call_args[0][0]
        assert "//" not in called_url.replace("http://", "").replace("https://", "")

    @pytest.mark.asyncio
    async def test_embed_one_data_not_a_list_raises_response_error(self) -> None:
        """'data' value is not a list → EmbeddingResponseError."""
        response = _make_response(200, {"data": "not-a-list"})
        client = _make_client(response)

        with pytest.raises(EmbeddingResponseError):
            await client.embed_one(text="hello", model_id=MODEL_ID)

    @pytest.mark.asyncio
    async def test_embed_one_empty_data_array_raises_response_error(self) -> None:
        """'data' is an empty list → EmbeddingResponseError."""
        response = _make_response(200, {"data": []})
        client = _make_client(response)

        with pytest.raises(EmbeddingResponseError):
            await client.embed_one(text="hello", model_id=MODEL_ID)

    @pytest.mark.asyncio
    async def test_embed_one_item_missing_index_raises_response_error(self) -> None:
        """data item has no 'index' key → EmbeddingResponseError."""
        response = _make_response(
            200,
            {"data": [{"embedding": _VEC_A}]},  # no index field
        )
        client = _make_client(response)

        with pytest.raises(EmbeddingResponseError):
            await client.embed_one(text="hello", model_id=MODEL_ID)

    @pytest.mark.asyncio
    async def test_embed_one_embedding_not_a_list_raises_response_error(self) -> None:
        """'embedding' value is a string, not a list → EmbeddingResponseError."""
        response = _make_response(
            200,
            {"data": [{"embedding": "not-a-list", "index": 0}]},
        )
        client = _make_client(response)

        with pytest.raises(EmbeddingResponseError):
            await client.embed_one(text="hello", model_id=MODEL_ID)

    @pytest.mark.asyncio
    async def test_embed_batch_missing_data_field_raises_response_error(self) -> None:
        """embed_batch response with no 'data' key → EmbeddingResponseError."""
        response = _make_response(200, {"object": "list"})
        client = _make_client(response)

        with pytest.raises(EmbeddingResponseError):
            await client.embed_batch(texts=["hello"], model_id=MODEL_ID)

    @pytest.mark.asyncio
    async def test_error_body_truncated_to_200_chars(self) -> None:
        """Long error body in a non-200 response is truncated in the error message."""
        long_body = "x" * 500
        response = _make_response(503, long_body)
        client = _make_client(response)

        with pytest.raises(EmbeddingUpstreamError) as exc_info:
            await client.embed_one(text="hello", model_id=MODEL_ID)

        # The error message should contain the truncated body (max 200 chars + ellipsis),
        # not the full 500-char string.
        error_msg = str(exc_info.value)
        assert len(error_msg) < 500 + 100  # generous upper bound
        assert "…" in error_msg or len(error_msg) < 400


class TestEndpointPath:
    """Verify the client uses /v1/embeddings (OpenAI-compat path), not /api/v1/embeddings.

    Regression for Bug B: the client previously sent requests to
    /api/v1/embeddings which LM Studio does not expose, returning 404.
    """

    @pytest.mark.asyncio
    async def test_client_posts_to_v1_embeddings_path(self) -> None:
        """embed_one posts to BASE_URL/v1/embeddings, not /api/v1/embeddings."""
        response = _make_response(
            200,
            {"data": [{"embedding": _VEC_A, "index": 0}], "object": "list", "model": MODEL_ID},
        )
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.post = AsyncMock(return_value=response)
        client = EmbeddingClient(http_client=mock_http, base_url=BASE_URL)

        result = await client.embed_one(text="hello", model_id=MODEL_ID)
        assert result == _VEC_A

        # Verify the URL posted to is the OpenAI-compat path.
        mock_http.post.assert_called_once()
        called_url: str = mock_http.post.call_args[0][0]
        assert called_url.endswith("/v1/embeddings"), (
            f"Expected URL ending in /v1/embeddings, got {called_url!r}"
        )
        assert "/api/v1/embeddings" not in called_url, (
            f"URL must not use /api/v1/embeddings path: {called_url!r}"
        )

    @pytest.mark.asyncio
    async def test_client_parses_openai_compat_data_embedding_shape(self) -> None:
        """embed_batch correctly parses the OpenAI data[].embedding shape from /v1/embeddings.

        The OpenAI-compat response is:
            {"object": "list", "data": [{"object": "embedding", "embedding": [...], "index": 0}]}
        The client must read data[i]["embedding"] (not data[i]["vector"] or any other key).
        """
        response = _make_response(
            200,
            {
                "object": "list",
                "data": [
                    {"object": "embedding", "embedding": _VEC_B, "index": 1},
                    {"object": "embedding", "embedding": _VEC_A, "index": 0},
                ],
                "model": MODEL_ID,
            },
        )
        client = _make_client(response)

        results = await client.embed_batch(texts=["first", "second"], model_id=MODEL_ID)

        # Results must be ordered by index (0 first, then 1).
        assert results == [_VEC_A, _VEC_B], (
            f"embed_batch must sort by 'index' and read 'embedding' key: {results}"
        )


# ---------------------------------------------------------------------------
# Additional embed_one / embed_batch edge-case coverage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_one_read_timeout_raises_upstream_error() -> None:
    """httpx.ReadTimeout (separate HTTPError subclass from ConnectError) is
    also caught and surfaced as EmbeddingUpstreamError (HTTPError subclass
    coverage beyond ConnectError).
    """
    http_client = MagicMock(spec=httpx.AsyncClient)
    http_client.post = AsyncMock(side_effect=httpx.ReadTimeout("upstream read timed out"))

    client = EmbeddingClient(http_client=http_client, base_url="http://upstream")
    with pytest.raises(EmbeddingUpstreamError) as exc_info:
        await client.embed_one(text="hello", model_id="test-embed-model")

    # No status code on a pure network error.
    assert exc_info.value.status_code is None
    assert "read timed out" in str(exc_info.value).lower() or "ReadTimeout" in str(exc_info.value)


@pytest.mark.asyncio
async def test_embed_batch_non_numeric_index_raises_response_error() -> None:
    """Data items with a non-comparable 'index' (e.g. a string) raise
    EmbeddingResponseError rather than propagating TypeError from sorted().
    """
    # Mixing int and str under sorted() in Python 3 raises TypeError —
    # we want that to surface as EmbeddingResponseError(model_id=...).
    payload = {
        "data": [
            {"index": 0, "embedding": [1.0, 2.0, 3.0]},
            {"index": "not-a-number", "embedding": [4.0, 5.0, 6.0]},
        ]
    }
    response = httpx.Response(
        status_code=200,
        json=payload,
        headers={"content-type": "application/json"},
    )
    http_client = MagicMock(spec=httpx.AsyncClient)
    http_client.post = AsyncMock(return_value=response)

    client = EmbeddingClient(http_client=http_client, base_url="http://upstream")
    with pytest.raises(EmbeddingResponseError) as exc_info:
        await client.embed_batch(texts=["a", "b"], model_id="test-embed-model")

    assert exc_info.value.model_id == "test-embed-model"
    assert "index" in str(exc_info.value)


@pytest.mark.asyncio
async def test_embed_one_non_json_response_body_raises_response_error() -> None:
    """200 OK with a non-JSON body (HTML error page, plain text) raises
    EmbeddingResponseError with model_id populated, NOT a misleading
    EmbeddingUpstreamError.
    """
    response = httpx.Response(
        status_code=200,
        headers={"content-type": "text/html"},
        content=b"<html><body>upstream returned HTML somehow</body></html>",
    )
    http_client = MagicMock(spec=httpx.AsyncClient)
    http_client.post = AsyncMock(return_value=response)

    client = EmbeddingClient(http_client=http_client, base_url="http://upstream")
    with pytest.raises(EmbeddingResponseError) as exc_info:
        await client.embed_one(text="hello", model_id="test-embed-model")

    assert exc_info.value.model_id == "test-embed-model"
    assert "JSON" in str(exc_info.value) or "json" in str(exc_info.value).lower()


def test_embedding_response_error_carries_model_id() -> None:
    """EmbeddingResponseError exposes model_id when constructed with one
    (model_id propagation for cross-model diagnostics)."""
    err = EmbeddingResponseError("oops", model_id="my-model")
    assert err.model_id == "my-model"
    # Backwards-compat: model_id is optional.
    err2 = EmbeddingResponseError("oops")
    assert err2.model_id is None


# ---------------------------------------------------------------------------
# Wire-id resolution (quant-suffix) — the centralized fix for "Invalid model
# identifier" 400s. The injected resolver maps a configured/stored catalog key
# to the LOADED INSTANCE wire id (e.g. ``…-v1.5@q8_0``) BEFORE the upstream POST.
# ---------------------------------------------------------------------------


class TestWireIdResolution:
    """embed_one / embed_batch resolve model_id to the wire id before POST."""

    @pytest.mark.asyncio
    async def test_embed_one_sends_resolved_wire_id_in_body(self) -> None:
        """A resolver mapping bare key → @quant wire id must change the POST body.

        Regression for the live bug: the bare catalog key
        ``text-embedding-nomic-embed-text-v1.5`` 400s because LM Studio loaded
        it as ``…-v1.5@q8_0``. With the resolver wired, the request body's
        ``model`` field must carry the @suffixed wire id, not the bare key.
        """
        wire_id = "text-embedding-nomic-embed-text-v1.5@q8_0"

        async def _resolver(model_id: str) -> str | None:
            assert model_id == MODEL_ID  # the bare catalog key came in
            return wire_id

        response = _make_response(200, {"data": [{"embedding": _VEC_A, "index": 0}]})
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.post = AsyncMock(return_value=response)
        client = EmbeddingClient(
            http_client=mock_http,
            base_url=BASE_URL,
            wire_id_resolver=_resolver,
        )

        result = await client.embed_one(text="hello", model_id=MODEL_ID)
        assert result == _VEC_A

        mock_http.post.assert_called_once()
        sent_body = mock_http.post.call_args.kwargs["json"]
        assert sent_body["model"] == wire_id, (
            f"POST body must carry the @quant wire id, got {sent_body['model']!r}"
        )

    @pytest.mark.asyncio
    async def test_embed_batch_sends_resolved_wire_id_in_body(self) -> None:
        """embed_batch also resolves the wire id before POST."""
        wire_id = "text-embedding-nomic-embed-text-v1.5@q8_0"

        async def _resolver(model_id: str) -> str | None:
            return wire_id

        response = _make_response(
            200,
            {"data": [{"embedding": _VEC_A, "index": 0}, {"embedding": _VEC_B, "index": 1}]},
        )
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.post = AsyncMock(return_value=response)
        client = EmbeddingClient(
            http_client=mock_http,
            base_url=BASE_URL,
            wire_id_resolver=_resolver,
        )

        result = await client.embed_batch(texts=["a", "b"], model_id=MODEL_ID)
        assert result == [_VEC_A, _VEC_B]

        sent_body = mock_http.post.call_args.kwargs["json"]
        assert sent_body["model"] == wire_id

    @pytest.mark.asyncio
    async def test_resolver_none_keeps_original_model_id(self) -> None:
        """Resolver returning None → original model_id used (legacy degrade path).

        When no loaded instance matches, the client must POST the unmodified
        model_id so the upstream surfaces the same 400/EmbeddingError as before
        and callers degrade exactly as today (no silent swap, no crash).
        """
        async def _resolver(model_id: str) -> str | None:
            return None

        response = _make_response(200, {"data": [{"embedding": _VEC_A, "index": 0}]})
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.post = AsyncMock(return_value=response)
        client = EmbeddingClient(
            http_client=mock_http,
            base_url=BASE_URL,
            wire_id_resolver=_resolver,
        )

        await client.embed_one(text="hello", model_id=MODEL_ID)
        sent_body = mock_http.post.call_args.kwargs["json"]
        assert sent_body["model"] == MODEL_ID

    @pytest.mark.asyncio
    async def test_no_resolver_passes_model_id_unchanged(self) -> None:
        """No resolver injected → model_id is sent verbatim (standalone usage)."""
        response = _make_response(200, {"data": [{"embedding": _VEC_A, "index": 0}]})
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.post = AsyncMock(return_value=response)
        client = EmbeddingClient(http_client=mock_http, base_url=BASE_URL)

        await client.embed_one(text="hello", model_id=MODEL_ID)
        sent_body = mock_http.post.call_args.kwargs["json"]
        assert sent_body["model"] == MODEL_ID
