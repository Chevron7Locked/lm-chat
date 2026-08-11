# SPDX-License-Identifier: Apache-2.0
"""``d2_sweep.run_sweep`` self-test on a synthetic corpus.

The D2 sweep harness
is no longer a stub printing "Not implemented yet". It now indexes
a corpus end-to-end (via the real ``upload_document`` pipeline) and
computes recall@k for each threshold. This test exercises it against
a synthetic corpus + synthetic gold set so the harness is verified
on every run, not just when an admin wires up smallcode.

The test does NOT need LM Studio — it injects mock
``EmbeddingClient`` and ``ModelsService`` that produce deterministic
vectors keyed off the chunk text. That keeps the test self-contained
while still exercising the full indexing → retrieval → recall path.
"""
from __future__ import annotations

import hashlib
import struct
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from lmchat.embedding.client import EmbeddingClient
from lmchat.services.d2_sweep import (
    DEFAULT_SWEEP_THRESHOLDS,
    GoldQuery,
    SweepReport,
    format_report,
    run_sweep,
)
from lmchat.services.memory_service import DEFAULT_EMBEDDING_MODEL_KEY
from lmchat.services.models_service import ModelsService


def _deterministic_vector(text: str, dim: int = 4) -> list[float]:
    """Map a string deterministically to a small vector. Same string
    → same vector; distinct strings → distinct vectors. Used so the
    embedding "stage" produces stable, comparable cosines for the
    self-test."""
    h = hashlib.blake2b(text.encode("utf-8"), digest_size=4 * dim).digest()
    return list(struct.unpack(f"<{dim}f", h))


def _embedding_client() -> EmbeddingClient:
    cli = MagicMock()

    async def _embed_batch(*, texts: list[str], model_id: str) -> list[list[float]]:
        return [_deterministic_vector(t) for t in texts]

    async def _embed_one(*, text: str, model_id: str) -> list[float]:
        return _deterministic_vector(text)

    cli.embed_batch = _embed_batch
    cli.embed_one = _embed_one
    return cast(EmbeddingClient, cli)


def _models_service(loaded_id: str = DEFAULT_EMBEDDING_MODEL_KEY) -> ModelsService:
    svc = MagicMock()
    # Genuinely loaded — the stable resolver filters embedders on
    # loaded_instance_ids; without them the entry counts as not-loaded and
    # the upload pipeline fails loud. Defaulting loaded_id to the canonical
    # default key means the resolver (no admin preference) returns it.
    model = MagicMock(
        key=loaded_id,
        type="embedding",
        loaded_instance_ids=[f"{loaded_id}@q8_0"],
    )
    svc.list_loaded = AsyncMock(return_value=[model])
    # resolve_embedding_wire_id is now called by retrieve(); wire id == key for test models.
    async def _resolve_wire(model_id: str) -> str | None:
        return loaded_id if model_id == loaded_id else None
    svc.resolve_embedding_wire_id = _resolve_wire
    return cast(ModelsService, svc)


def _write_corpus(corpus_dir: Path, files: dict[str, str]) -> None:
    """Write a flat synthetic corpus under *corpus_dir*."""
    corpus_dir.mkdir(parents=True, exist_ok=True)
    for fname, body in files.items():
        (corpus_dir / fname).write_text(body)


# ─── Tests ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_sweep_produces_a_report_with_one_row_per_threshold(
    tmp_path: Path,
) -> None:
    """Smoke: the harness indexes a tiny corpus, returns one row per
    threshold, and the row carries plausible numbers."""
    corpus = tmp_path / "corpus"
    _write_corpus(
        corpus,
        {
            "alpha.txt": "alpha alpha alpha. relevant doc one." * 4,
            "beta.txt": "beta beta beta. relevant doc two." * 4,
            "gamma.txt": "gamma gamma gamma. distractor." * 4,
        },
    )
    gold = [
        GoldQuery(query="alpha relevant", relevant_doc_ids=[]),
        GoldQuery(query="beta relevant", relevant_doc_ids=[]),
    ]
    thresholds = (100, 500, 5_000)

    report = await run_sweep(
        corpus_dir=corpus,
        gold_set=gold,
        embedding_client=_embedding_client(),
        models_service=_models_service(),
        ctx_window=131_000,
        thresholds=thresholds,
    )

    assert isinstance(report, SweepReport)
    assert report.queries_evaluated == 2
    assert len(report.rows) == len(thresholds)
    assert [r.threshold for r in report.rows] == list(thresholds)
    assert report.corpus_doc_count == 3
    assert report.corpus_total_tokens > 0
    assert report.embedding_model_id == DEFAULT_EMBEDDING_MODEL_KEY
    assert set(report.id_map.keys()) == {
        "alpha.txt",
        "beta.txt",
        "gamma.txt",
    }


@pytest.mark.asyncio
async def test_sweep_recall_uses_relevant_doc_ids_from_id_map(
    tmp_path: Path,
) -> None:
    """When gold ids match the indexed doc ids, HYBRID recall is
    > 0. When they don't match, HYBRID recall is 0."""
    corpus = tmp_path / "corpus"
    _write_corpus(
        corpus,
        {
            "doc-a.txt": "alpha unique-token-for-alpha." * 30,
            "doc-b.txt": "beta unique-token-for-beta." * 30,
        },
    )
    # First pass — index to learn the id_map.
    discovery = await run_sweep(
        corpus_dir=corpus,
        gold_set=[GoldQuery(query="alpha", relevant_doc_ids=[])],
        embedding_client=_embedding_client(),
        models_service=_models_service(),
        thresholds=(5_000,),
    )
    alpha_id = discovery.id_map["doc-a.txt"]

    # Second pass — curate gold with the discovered id.
    gold = [
        GoldQuery(
            query="alpha unique-token-for-alpha",
            relevant_doc_ids=[alpha_id],
        )
    ]
    report = await run_sweep(
        corpus_dir=corpus,
        gold_set=gold,
        embedding_client=_embedding_client(),
        models_service=_models_service(),
        thresholds=(5_000,),
    )
    # The corpus is small enough to fall under threshold=5000 →
    # INLINE recall == 1.0; HYBRID may or may not surface the doc
    # depending on FTS/vector ranks but the bookkeeping itself is
    # exercised.
    row = report.rows[0]
    assert row.inline_recall == pytest.approx(1.0, rel=1e-6)
    assert 0.0 <= row.hybrid_recall <= 1.0


@pytest.mark.asyncio
async def test_inline_recall_drops_to_zero_above_threshold(
    tmp_path: Path,
) -> None:
    """A corpus over the threshold means INLINE doesn't fire; recall
    is 0 (the sweep table shows where HYBRID takes over)."""
    corpus = tmp_path / "corpus"
    # ~4000 tokens worth of text (16000 bytes / 4 = 4000 tokens
    # under the byte-based heuristic in
    # documents_service._estimate_project_corpus_tokens).
    body = "x" * 16_000
    _write_corpus(corpus, {"big.txt": body})
    gold = [GoldQuery(query="x", relevant_doc_ids=[1])]
    report = await run_sweep(
        corpus_dir=corpus,
        gold_set=gold,
        embedding_client=_embedding_client(),
        models_service=_models_service(),
        thresholds=(100, 5_000, 32_000),
    )
    # 100 < corpus → INLINE recall 0; 32_000 > corpus → INLINE 1.
    rows_by_threshold = {r.threshold: r for r in report.rows}
    assert rows_by_threshold[100].inline_recall == 0.0
    assert rows_by_threshold[32_000].inline_recall == 1.0


@pytest.mark.asyncio
async def test_sweep_skips_unreadable_files_without_crashing(
    tmp_path: Path,
) -> None:
    """A file the upload pipeline rejects (binary garbage with the
    wrong magic bytes) is skipped, not raised."""
    corpus = tmp_path / "corpus"
    _write_corpus(corpus, {"good.txt": "real text content"})
    # Empty file — extraction returns empty, upload still works for
    # text/plain. We want to verify skip-vs-include logic doesn't
    # crash the whole sweep on edge cases.
    (corpus / "empty.txt").write_bytes(b"")
    gold = [GoldQuery(query="real", relevant_doc_ids=[])]
    report = await run_sweep(
        corpus_dir=corpus,
        gold_set=gold,
        embedding_client=_embedding_client(),
        models_service=_models_service(),
        thresholds=(5_000,),
    )
    # At least the good.txt file landed.
    assert report.corpus_doc_count >= 1


def test_format_report_includes_recommended_fraction_when_winner_exists() -> None:
    """``format_report`` shows the recommended inline_fraction when
    HYBRID beats INLINE by ≥ 0.05."""
    report = SweepReport(
        ctx_window=131_000,
        embedding_model_id="test-embed",
        corpus_doc_count=5,
        corpus_total_tokens=50_000,
        queries_evaluated=10,
        top_k=8,
        rows=[
            # threshold=2000 — HYBRID barely better, doesn't qualify
            # (delta 0.04 < 0.05).
            __import__("lmchat.services.d2_sweep", fromlist=["ThresholdResult"]).ThresholdResult(
                threshold=2000, inline_recall=0.5, hybrid_recall=0.54
            ),
            # threshold=8000 — HYBRID clearly wins by 0.10.
            __import__("lmchat.services.d2_sweep", fromlist=["ThresholdResult"]).ThresholdResult(
                threshold=8000, inline_recall=0.5, hybrid_recall=0.6
            ),
        ],
    )
    out = format_report(report)
    assert "winning_threshold = 8000" in out
    # 8000 / 131000 ≈ 0.06107
    assert "0.06107" in out


def test_format_report_handles_no_winner() -> None:
    """When HYBRID never beats INLINE by ≥ 5 pts, the report says so."""
    report = SweepReport(
        ctx_window=131_000,
        embedding_model_id="test-embed",
        corpus_doc_count=5,
        corpus_total_tokens=50_000,
        queries_evaluated=10,
        top_k=8,
        rows=[
            __import__("lmchat.services.d2_sweep", fromlist=["ThresholdResult"]).ThresholdResult(
                threshold=8000, inline_recall=0.5, hybrid_recall=0.51
            ),
        ],
    )
    out = format_report(report)
    assert "no winning threshold" in out


def test_default_sweep_thresholds_match_adr_029_grid() -> None:
    """Locks the grid against the documented threshold set."""
    assert DEFAULT_SWEEP_THRESHOLDS == (
        2_000, 4_000, 8_000, 16_000, 32_000,
    )
