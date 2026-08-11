# SPDX-License-Identifier: Apache-2.0
"""Embedding error hierarchy for lm-chat."""
from __future__ import annotations


class EmbeddingError(Exception):
    """Base class for all embedding errors."""


class EmbeddingUpstreamError(EmbeddingError):
    """Raised when the upstream LM Studio embeddings endpoint returns a
    non-200 status or a network-level failure occurs.

    Attributes:
        status_code: The HTTP status code from the upstream response, or
            ``None`` for network-level errors (e.g. connection refused).
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code: int | None = status_code


class EmbeddingResponseError(EmbeddingError):
    """Raised when the upstream response is a 2xx but its JSON body is
    malformed — missing ``data`` array, missing ``embedding`` field, or
    a count mismatch between inputs and returned embeddings.

    Attributes:
        model_id: The LM Studio model that was being embedded against, or
            ``None`` if the caller did not supply one (older code paths).
            Carried so callers can distinguish failures across models when
            multiple embedding models are loaded.
    """

    def __init__(self, message: str, *, model_id: str | None = None) -> None:
        super().__init__(message)
        self.model_id: str | None = model_id
