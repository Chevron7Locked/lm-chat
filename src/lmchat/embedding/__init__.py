# SPDX-License-Identifier: Apache-2.0
"""lm-chat embedding package.

Public surface:

- :class:`EmbeddingClient` — async client for LM Studio's
  ``/api/v1/embeddings`` endpoint.
- :class:`EmbeddingError` — base error.
- :class:`EmbeddingUpstreamError` — non-200 / network error.
- :class:`EmbeddingResponseError` — malformed response body.
"""
from __future__ import annotations

from lmchat.embedding.client import EmbeddingClient
from lmchat.embedding.errors import (
    EmbeddingError,
    EmbeddingResponseError,
    EmbeddingUpstreamError,
)

__all__ = [
    "EmbeddingClient",
    "EmbeddingError",
    "EmbeddingResponseError",
    "EmbeddingUpstreamError",
]
