# SPDX-License-Identifier: Apache-2.0
"""§2.4 — Live(-ish) LM Studio integration for B1 pin + re-embed.

The earlier B1 tests at
``tests/services/test_documents_embedding_pin_write_once.py`` and the
re-embed tests at ``tests/services/test_documents_reembed.py`` use
``MagicMock`` for ``EmbeddingClient`` + ``ModelsService``. That's
fast but it bypasses the real HTTP composition + auth headers + JSON
shape parsing. This batch adds an integration test that wires the
**real** ``EmbeddingClient`` and ``ModelsService`` (no mocks of those
classes) against an ``httpx.MockTransport`` simulating LM Studio's
``/api/v1/models``, ``/api/v1/embeddings`` endpoints — so the
upload → pin → re-embed loop exercises every HTTP-shape contract that
mocked tests skip.

This is the closest we get to "real LM Studio" without a daemon
running locally; the admin demo at §3.3 is where the actual LM
Studio side gets verified end-to-end.
"""
from __future__ import annotations

import struct
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import create_async_engine

from lmchat.db.schema import document_chunks, documents, metadata, projects, users
from lmchat.embedding.client import EmbeddingClient
from lmchat.services.documents_service import re_embed_project_documents
from lmchat.services.models_service import ModelsService


def _vec(values: list[float]) -> list[float]:
    return list(values)


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


class _LMStudioSimulator:
    """In-process httpx.MockTransport that simulates LM Studio.

    Records every request so the test can assert on which endpoints
    were hit, which model id was sent, etc.

    Routes simulated:
    * ``GET /api/v1/models`` — returns the ``loaded`` list as the
      native shape ``{"models": [{"key": "<id>", "type": "embedding",
      "vision": false, "trained_for_tool_use": false}, ...]}``.
    * ``POST /api/v1/embeddings`` — for each ``input`` string,
      returns a 4-dim vector keyed off the input + model id.
    """

    def __init__(self, *, loaded: list[str]) -> None:
        self._loaded = loaded
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/api/v1/models":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "key": mid,
                            "type": "embedding",
                            "vision": False,
                            "trained_for_tool_use": False,
                            "loaded": True,
                            # Real LM Studio reports loadedness via the
                            # loaded_instances array (objects with an id), NOT
                            # the "loaded" bool. ModelsService._probe_upstream
                            # parses loaded_instance_ids from here, and the
                            # embedding resolver filters on it — so a genuinely
                            # loaded model must carry an instance entry.
                            "loaded_instances": [{"id": f"{mid}@q8_0"}],
                        }
                        for mid in self._loaded
                    ]
                },
            )
        if path == "/api/v1/embeddings" or path == "/v1/embeddings":
            body = request.content.decode("utf-8")
            import json as _json

            payload = _json.loads(body)
            model_id = payload["model"]
            inputs = payload.get("input")
            if isinstance(inputs, str):
                inputs = [inputs]
            assert isinstance(inputs, list)
            # Deterministic vector per (model_id, input) pair so the
            # test can verify the embed was actually called with the
            # right model id (cosine fingerprint).
            data: list[dict[str, Any]] = []
            for i, text in enumerate(inputs):
                # Vector encodes model_id as the first component so a
                # re-embed under a different model produces a
                # different vector.
                mid_seed = sum(ord(c) for c in model_id) % 1000 / 1000.0
                txt_seed = (sum(ord(c) for c in text) % 1000) / 1000.0
                data.append({
                    "object": "embedding",
                    "index": i,
                    "embedding": [
                        mid_seed,
                        txt_seed,
                        mid_seed + txt_seed,
                        mid_seed * txt_seed,
                    ],
                })
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": data,
                    "model": model_id,
                },
            )
        return httpx.Response(404, text=f"unhandled path {path}")

    def set_loaded(self, loaded: list[str]) -> None:
        """Mid-test mutation — simulate an admin-swap of the
        loaded embedding model."""
        self._loaded = loaded


@pytest.fixture()
def lm_studio() -> _LMStudioSimulator:
    return _LMStudioSimulator(loaded=["embed-old-model"])


@pytest.fixture()
def http_client(
    lm_studio: _LMStudioSimulator,
) -> Iterator[httpx.AsyncClient]:
    transport = httpx.MockTransport(lm_studio)
    client = httpx.AsyncClient(
        transport=transport, base_url="http://lm-studio.test:1234"
    )
    yield client


async def _make_engine(tmp_path: Path):
    eng = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/b1_integration.db",
        pool_pre_ping=True,
    )
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return eng


async def _seed_project_with_chunks(
    engine, *, pinned: str
) -> tuple[int, int, list[int]]:
    import time as _time
    async with engine.begin() as conn:
        u = await conn.execute(
            insert(users).values(username="alice", password_hash="x")
        )
        uid = int(u.inserted_primary_key[0])
        now = _time.time()
        p = await conn.execute(
            insert(projects).values(
                user_id=uid,
                name="P",
                description="",
                system_prompt="",
                embedding_model_id=pinned,
                created_at=now,
                updated_at=now,
            )
        )
        pid = int(p.inserted_primary_key[0])
        d = await conn.execute(
            insert(documents).values(
                user_id=uid,
                project_id=pid,
                title="doc.txt",
                sha256="sha-1",
                mime_type="text/plain",
                byte_size=20,
                chunk_count=2,
                embedding_model_id=pinned,
            )
        )
        did = int(d.inserted_primary_key[0])
        chunk_ids: list[int] = []
        for ord_ in range(2):
            cr = await conn.execute(
                insert(document_chunks).values(
                    document_id=did,
                    ordinal=ord_,
                    text=f"chunk-{ord_}",
                    text_hash=f"h-{ord_}",
                    embedding=_pack([0.5, 0.5, 0.5, 0.5]),
                )
            )
            chunk_ids.append(int(cr.inserted_primary_key[0]))
    return uid, pid, chunk_ids


# ─── Tests ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_re_embed_loop_uses_real_http_client_against_simulator(
    tmp_path: Path,
    lm_studio: _LMStudioSimulator,
    http_client: httpx.AsyncClient,
) -> None:
    """Wire the real EmbeddingClient + ModelsService against the
    LM Studio simulator. After re-embedding, the chunk blobs encode
    the NEW model id (vector first component), every chunk's
    embedding bytes changed, and the simulator recorded the right
    number of upstream calls."""
    eng = await _make_engine(tmp_path)
    uid, pid, chunk_ids = await _seed_project_with_chunks(
        eng, pinned="embed-old-model"
    )
    embedding_client = EmbeddingClient(
        http_client=http_client, base_url="http://lm-studio.test:1234"
    )
    models_service = ModelsService(
        http_client=http_client, base_url="http://lm-studio.test:1234"
    )
    # Admin-swap: simulator now reports the canonical default (nomic) loaded.
    # The no-preference resolver deterministically selects nomic (2026-06-25);
    # an arbitrary "embed-new-model" would now fail-loud instead of being picked.
    new_model = "text-embedding-nomic-embed-text-v1.5"
    lm_studio.set_loaded([new_model])

    result = await re_embed_project_documents(
        user_id=uid,
        project_id=pid,
        engine=eng,
        embedding_client=embedding_client,
        models_service=models_service,
    )
    assert result["documents_re_embedded"] == 1
    assert result["chunks_re_embedded"] == 2
    assert result["active_embedding_model_id"] == new_model

    # Verify the chunk blobs encode the new model id.
    new_mid_seed = sum(ord(c) for c in new_model) % 1000 / 1000.0
    async with eng.connect() as conn:
        for cid in chunk_ids:
            blob = (
                await conn.execute(
                    select(document_chunks.c.embedding).where(
                        document_chunks.c.id == cid
                    )
                )
            ).scalar_one()
            vec = _unpack(blob)
            # First component = model-id seed (per simulator).
            assert vec[0] == pytest.approx(new_mid_seed, abs=1e-5), (
                f"chunk {cid} vec[0]={vec[0]} doesn't match new model "
                f"seed {new_mid_seed}"
            )

    # Simulator should have seen at least one models lookup + at
    # least one embeddings POST per chunk (2 chunks).
    model_calls = sum(
        1 for r in lm_studio.requests if r.url.path == "/api/v1/models"
    )
    embed_calls = sum(
        1
        for r in lm_studio.requests
        if r.url.path in ("/api/v1/embeddings", "/v1/embeddings")
    )
    assert model_calls >= 1
    assert embed_calls >= 1
    await eng.dispose()
    await http_client.aclose()


@pytest.mark.asyncio
async def test_re_embed_raises_when_simulator_reports_no_embedding_model(
    tmp_path: Path,
    lm_studio: _LMStudioSimulator,
    http_client: httpx.AsyncClient,
) -> None:
    """If the simulator's ``/api/v1/models`` returns an empty
    embedding-model list, the re-embed path raises ``RuntimeError``
    (route layer maps to 503). Exercises the real
    ``models_service.list_loaded`` HTTP round-trip + the resolver's
    no-model branch."""
    eng = await _make_engine(tmp_path)
    uid, pid, _ = await _seed_project_with_chunks(
        eng, pinned="embed-old-model"
    )
    embedding_client = EmbeddingClient(
        http_client=http_client, base_url="http://lm-studio.test:1234"
    )
    models_service = ModelsService(
        http_client=http_client, base_url="http://lm-studio.test:1234"
    )
    lm_studio.set_loaded([])  # nothing loaded

    with pytest.raises(RuntimeError, match="No embedding model"):
        await re_embed_project_documents(
            user_id=uid,
            project_id=pid,
            engine=eng,
            embedding_client=embedding_client,
            models_service=models_service,
        )
    await eng.dispose()
    await http_client.aclose()
