# SPDX-License-Identifier: Apache-2.0
"""409 body shape on embedding-model-pin conflict.

Pins the wire shape so the UI
banner renders the re-embed flow without a second round trip:
``{embedding_model_id, active_embedding_model_id, re_embed_url}``.
"""
from __future__ import annotations

from lmchat.routes._form_utils import embedding_pin_conflict_response
from lmchat.services.documents_service import EmbeddingModelPinConflict


def test_embedding_pin_conflict_response_carries_all_three_fields() -> None:
    """Helper returns the exact three-field dict shape."""
    exc = EmbeddingModelPinConflict(
        project_id=42,
        pinned_model_id="embed-A",
        active_model_id="embed-B",
    )

    body = embedding_pin_conflict_response(exc)

    assert body == {
        "embedding_model_id": "embed-A",
        "active_embedding_model_id": "embed-B",
        "re_embed_url": "/project/42#documents",
    }


def test_embedding_pin_conflict_response_re_embed_url_format() -> None:
    """The re_embed_url field encodes the project id so the client
    banner can deep-link to the Documents tab with the right project
    selected."""
    exc = EmbeddingModelPinConflict(
        project_id=99,
        pinned_model_id="nomic-embed-text",
        active_model_id="text-embedding-3-small",
    )

    body = embedding_pin_conflict_response(exc)

    assert body["re_embed_url"] == "/project/99#documents"
