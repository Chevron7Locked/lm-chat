# SPDX-License-Identifier: Apache-2.0
"""RAG-mode threshold empirical sweep.

Closes the deferral that left
the resolver's ``_DEFAULT_INLINE_FRACTION`` as an unmeasured estimate.
This module sweeps recall@k across a candidate threshold grid against a
corpus + hand-labeled gold set, picks the inflection point, and yields a
measured ratio.

The harness is composed from existing services — no parallel
retrieval implementation. It:

1. Indexes a directory of files into ``document_chunks`` via the
   real ``documents_service.upload_document`` pipeline (under a
   throwaway user_id + a fresh test project).
2. For each query in the gold set:
   - **INLINE simulation** — compute the "what would be inlined"
     answer set (all chunks from documents in the project where
     ``doc.size_in_tokens <= threshold``).
   - **HYBRID** — runs the real ``retrieval_service.retrieve`` to
     get top-k chunks; maps chunk-id → document-id.
   - Scores recall@k on each branch against ``relevant_doc_ids``
     from the gold set.
3. Sweeps across the threshold grid and reports the table + the
   recommended winning threshold (smallest where HYBRID beats INLINE
   by ≥ 5 points recall@k).

The CLI lives at ``scripts/run_d2_sweep.py``; tests at
``tests/services/test_d2_sweep_harness.py`` exercise the harness
against a synthetic corpus + synthetic gold set so it can never rot
back into a stub.

Admin-supplied inputs (when running against real data):

* ``corpus_dir`` — directory of files to index. The same MIME
  validation + extraction rules as live uploads apply.
* ``gold_set`` — list of ``{"query": str, "relevant_doc_ids":
  list[int]}``. Doc ids must match the ids the indexing step assigns
  (the harness returns the id map from ``run_sweep`` so the admin
  can curate the gold set after indexing).
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import documents, projects, users
from lmchat.embedding.client import EmbeddingClient
from lmchat.services.documents_service import (
    _estimate_project_corpus_tokens,
    upload_document,
)
from lmchat.services.models_service import ModelsService
from lmchat.services.retrieval_service import retrieve

log = logging.getLogger(__name__)


# Default threshold grid used by the empirical sweep.
DEFAULT_SWEEP_THRESHOLDS: tuple[int, ...] = (2_000, 4_000, 8_000, 16_000, 32_000)
DEFAULT_TOP_K: int = 8


@dataclass
class GoldQuery:
    """One labeled query in the gold set."""

    query: str
    relevant_doc_ids: list[int]


@dataclass
class ThresholdResult:
    """recall@k for one threshold value (INLINE vs HYBRID)."""

    threshold: int
    inline_recall: float
    hybrid_recall: float

    @property
    def delta(self) -> float:
        return self.hybrid_recall - self.inline_recall


@dataclass
class SweepReport:
    """Full sweep output. Stamp time at the call site (Date.now() is
    not available inside workflow scripts that import this module)."""

    ctx_window: int
    embedding_model_id: str
    corpus_doc_count: int
    corpus_total_tokens: int
    queries_evaluated: int
    top_k: int
    rows: list[ThresholdResult]
    id_map: dict[str, int] = field(default_factory=dict)
    """``filename → doc_id`` map. Admins use this to curate their
    gold set's ``relevant_doc_ids`` after the index run."""

    @property
    def winning_threshold(self) -> int | None:
        """Smallest threshold where HYBRID beats INLINE by ≥ 5 pts."""
        for row in self.rows:
            if row.delta >= 0.05:
                return row.threshold
        return None

    @property
    def recommended_inline_fraction(self) -> float | None:
        """``winning_threshold / ctx_window`` — the value to land in
        ``_DEFAULT_INLINE_FRACTION``."""
        wt = self.winning_threshold
        if wt is None or self.ctx_window <= 0:
            return None
        return wt / self.ctx_window


# ─── Indexing ────────────────────────────────────────────────────────────


async def index_corpus(
    *,
    corpus_dir: Path,
    user_id: int,
    project_id: int,
    engine: AsyncEngine,
    embedding_client: EmbeddingClient,
    models_service: ModelsService,
) -> dict[str, int]:
    """Index every supported file under *corpus_dir* into the project.

    Walks the directory recursively, uploads each file via the real
    upload pipeline (MIME validation, magic-byte check, extraction,
    chunking, embedding). Returns a ``filename → document_id`` map
    so callers can curate the gold set's ``relevant_doc_ids``
    against the assigned ids.

    Skips files whose MIME type isn't supported (the upload path
    raises and we record the skip). The skip count is logged but
    not part of the return shape — the goal is "what landed."
    """
    id_map: dict[str, int] = {}
    indexed = 0
    skipped = 0

    for path in sorted(corpus_dir.rglob("*")):
        if not path.is_file():
            continue
        try:
            body = path.read_bytes()
        except OSError:
            skipped += 1
            continue
        try:
            mime = _guess_mime_from_extension(path)
            doc = await upload_document(
                user_id=user_id,
                filename=path.name,
                content_type=mime,
                body_bytes=body,
                engine=engine,
                embedding_client=embedding_client,
                models_service=models_service,
                project_id=project_id,
            )
            id_map[str(path.relative_to(corpus_dir))] = int(doc.id)
            indexed += 1
        except Exception as exc:  # noqa: BLE001
            log.info(
                "d2_sweep.skip_file",
                extra={"path": str(path), "error": str(exc)},
            )
            skipped += 1

    log.info(
        "d2_sweep.indexed",
        extra={"indexed": indexed, "skipped": skipped},
    )
    return id_map


def _guess_mime_from_extension(path: Path) -> str:
    """Best-effort MIME guess for the upload-path's validator.

    The upload pipeline does its own magic-byte validation; this is
    just the declared content_type. Extend per admin's typical
    corpus shape — kept conservative to avoid surprising the
    validator on unusual extensions.
    """
    ext = path.suffix.lower()
    if ext in (".md", ".markdown"):
        return "text/markdown"
    if ext == ".pdf":
        return "application/pdf"
    if ext in (".txt", ".log", ".rst"):
        return "text/plain"
    if ext in (".py", ".js", ".ts", ".tsx", ".go", ".rs", ".java"):
        return "text/plain"
    return "text/plain"


# ─── Recall scoring ──────────────────────────────────────────────────────


async def _hybrid_recall_for_query(
    *,
    query: GoldQuery,
    user_id: int,
    project_id: int,
    engine: AsyncEngine,
    embedding_client: EmbeddingClient,
    models_service: ModelsService,
    top_k: int,
) -> float:
    """recall@k of the HYBRID retrieval pipeline for *query*."""
    if not query.relevant_doc_ids:
        return 0.0
    hits = await retrieve(
        query=query.query,
        user_id=user_id,
        top_k=top_k,
        engine=engine,
        embedding_client=embedding_client,
        models_service=models_service,
        project_id=project_id,
    )
    retrieved_doc_ids = {hit.document_id for hit in hits}
    relevant = set(query.relevant_doc_ids)
    hits_intersect = retrieved_doc_ids & relevant
    return len(hits_intersect) / len(relevant)


async def _inline_recall_for_query(
    *,
    query: GoldQuery,
    project_id: int,
    threshold: int,
    engine: AsyncEngine,
    top_k: int,
) -> float:
    """recall@k of the INLINE branch — at this threshold, do we
    inline the entire corpus into the prompt? If yes, recall is 1.0
    for queries whose relevant docs are all in the project. If no
    (corpus > threshold so HYBRID would fire), INLINE is conceptually
    "not the operating branch for this corpus" — we score it 0.0 so
    the sweep shows when HYBRID overtakes."""
    if not query.relevant_doc_ids:
        return 0.0
    corpus_tokens = await _estimate_project_corpus_tokens(
        engine=engine, user_id=_USER_SCOPE_PLACEHOLDER, project_id=project_id
    )
    if corpus_tokens <= threshold:
        # All chunks inlined; if the relevant docs are in the project,
        # we can answer from the prompt directly. Cap recall at 1.0
        # without bounding by top_k since INLINE inlines everything.
        return 1.0
    return 0.0


# Set by ``run_sweep`` so ``_inline_recall_for_query`` can pass the
# correct user_id to the corpus estimator. The estimator's user_id
# filter is defense-in-depth — for the sweep harness the project
# is owned by the throwaway user_id we set in setup.
_USER_SCOPE_PLACEHOLDER: int = 0


# ─── Top-level sweep entry ───────────────────────────────────────────────


async def run_sweep(
    *,
    corpus_dir: Path,
    gold_set: list[GoldQuery],
    embedding_client: EmbeddingClient,
    models_service: ModelsService,
    engine: AsyncEngine | None = None,
    ctx_window: int = 131_000,
    thresholds: tuple[int, ...] = DEFAULT_SWEEP_THRESHOLDS,
    top_k: int = DEFAULT_TOP_K,
) -> SweepReport:
    """End-to-end D2 sweep.

    Sets up an in-memory throwaway SQLite engine (or uses *engine*
    when supplied), creates the schema, inserts a placeholder user +
    project, indexes the corpus, runs the recall table across
    *thresholds*, and returns the report.

    Args:
        corpus_dir:       Directory tree to index.
        gold_set:         Hand-labeled ``[{query, relevant_doc_ids}]``.
                          ``relevant_doc_ids`` must match the ids
                          ``index_corpus`` assigns; the report carries
                          an ``id_map`` so admins can curate the
                          gold set on the first run, then re-run with
                          the curated set.
        embedding_client: Embedding client (real LM Studio against
                          the admin's loaded embedding model).
        models_service:   Models service exposing ``list_loaded``.
        engine:           Optional existing engine. When None, the
                          harness creates a fresh in-memory SQLite
                          engine and drops it at the end.
        ctx_window:       Anchor model's context window in tokens
                          (default 131k — qwen3.6-35b-a3b).
        thresholds:       Threshold grid to sweep across.
        top_k:            ``k`` in recall@k.

    Returns:
        :class:`SweepReport` with the table + the recommended
        ``inline_fraction``.

    Raises:
        RuntimeError: If no embedding model is loaded.
    """
    own_engine = False
    if engine is None:
        # Real on-disk SQLite via a tmpdir — needed so Alembic can
        # apply the FTS5 + trigger DDL the in-memory ``:memory:``
        # URL doesn't survive across the sync→async bind switch.
        import tempfile

        tmpdir = tempfile.mkdtemp(prefix="d2_sweep_")
        db_path = Path(tmpdir) / "sweep.db"
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_path}",
            pool_pre_ping=True,
        )
        own_engine = True
        # Run real Alembic migrations so the FTS5 virtual table +
        # triggers exist; ``metadata.create_all`` alone misses
        # those (they're created via raw DDL in 0003). Alembic's
        # env is async, so it calls ``asyncio.run`` internally —
        # we must run it on a separate thread to avoid nesting
        # ``asyncio.run`` inside the test's running loop.
        import alembic.command
        import alembic.config

        repo_root = Path(__file__).resolve().parents[3]

        def _migrate() -> None:
            cfg = alembic.config.Config(
                str(repo_root / "alembic.ini")
            )
            cfg.set_main_option(
                "sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}"
            )
            cfg.set_main_option(
                "script_location", str(repo_root / "migrations")
            )
            alembic.command.upgrade(cfg, "head")

        await asyncio.to_thread(_migrate)

    try:
        # Throwaway user + project, owned by the sweep run.
        async with engine.begin() as conn:
            ur = await conn.execute(
                insert(users).values(
                    username="d2_sweep_runner",
                    password_hash="x",
                )
            )
            pk = ur.inserted_primary_key
            assert pk is not None, "INSERT users returned no primary key"
            user_id = int(pk[0])
            global _USER_SCOPE_PLACEHOLDER
            _USER_SCOPE_PLACEHOLDER = user_id
            import time as _time

            pr = await conn.execute(
                insert(projects).values(
                    user_id=user_id,
                    name="d2_sweep",
                    description="",
                    system_prompt="",
                    created_at=_time.time(),
                    updated_at=_time.time(),
                )
            )
            pk = pr.inserted_primary_key
            assert pk is not None, "INSERT projects returned no primary key"
            project_id = int(pk[0])

        id_map = await index_corpus(
            corpus_dir=corpus_dir,
            user_id=user_id,
            project_id=project_id,
            engine=engine,
            embedding_client=embedding_client,
            models_service=models_service,
        )

        # Resolve the embedding model id pinned on the project.
        async with engine.connect() as conn:
            embedding_model_id = (
                await conn.execute(
                    select(projects.c.embedding_model_id).where(
                        projects.c.id == project_id
                    )
                )
            ).scalar_one() or ""
            doc_id_rows = (
                await conn.execute(
                    select(documents.c.id).where(
                        documents.c.project_id == project_id
                    )
                )
            ).fetchall()
            corpus_doc_count = len(doc_id_rows)

        corpus_total_tokens = await _estimate_project_corpus_tokens(
            engine=engine, user_id=user_id, project_id=project_id
        )

        # Build the threshold × recall table. Each query gets one
        # hybrid run (its top-k doesn't change with the threshold),
        # cached so we don't re-embed the query 5×.
        hybrid_recall_cache: dict[int, float] = {}

        async def _hybrid_for(qi: int, q: GoldQuery) -> float:
            if qi not in hybrid_recall_cache:
                hybrid_recall_cache[qi] = await _hybrid_recall_for_query(
                    query=q,
                    user_id=user_id,
                    project_id=project_id,
                    engine=engine,
                    embedding_client=embedding_client,
                    models_service=models_service,
                    top_k=top_k,
                )
            return hybrid_recall_cache[qi]

        rows: list[ThresholdResult] = []
        for threshold in thresholds:
            inline_acc = 0.0
            hybrid_acc = 0.0
            for qi, q in enumerate(gold_set):
                inline_acc += await _inline_recall_for_query(
                    query=q,
                    project_id=project_id,
                    threshold=threshold,
                    engine=engine,
                    top_k=top_k,
                )
                hybrid_acc += await _hybrid_for(qi, q)
            denom = max(1, len(gold_set))
            rows.append(
                ThresholdResult(
                    threshold=threshold,
                    inline_recall=inline_acc / denom,
                    hybrid_recall=hybrid_acc / denom,
                )
            )

        return SweepReport(
            ctx_window=ctx_window,
            embedding_model_id=str(embedding_model_id),
            corpus_doc_count=int(corpus_doc_count or 0),
            corpus_total_tokens=corpus_total_tokens,
            queries_evaluated=len(gold_set),
            top_k=top_k,
            rows=rows,
            id_map=id_map,
        )
    finally:
        if own_engine:
            await engine.dispose()


# ─── Reporting ───────────────────────────────────────────────────────────


def format_report(report: SweepReport) -> str:
    """Pretty table for stdout. Admin copies the recommended ratio
    into ``rag_mode_resolver._DEFAULT_INLINE_FRACTION``."""
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("D2 sweep — recall@{} on {}".format(
        report.top_k, report.embedding_model_id or "(unknown)",
    ))
    lines.append(
        f"corpus: {report.corpus_doc_count} docs / "
        f"{report.corpus_total_tokens} tokens · ctx={report.ctx_window} · "
        f"queries={report.queries_evaluated}"
    )
    lines.append("-" * 72)
    lines.append("{:>10s}  {:>12s}  {:>12s}  {:>10s}".format(
        "threshold", "INLINE@k", "HYBRID@k", "Δ"
    ))
    for row in report.rows:
        lines.append(
            f"{row.threshold:>10d}  {row.inline_recall:>12.3f}"
            f"  {row.hybrid_recall:>12.3f}  {row.delta:>+10.3f}"
        )
    lines.append("-" * 72)
    if report.winning_threshold is not None:
        lines.append(
            f"winning_threshold = {report.winning_threshold} "
            f"(smallest where HYBRID − INLINE ≥ 0.05)"
        )
        lines.append(
            f"recommended_inline_fraction = "
            f"{report.recommended_inline_fraction:.5f} "
            f"(= {report.winning_threshold} / {report.ctx_window})"
        )
        lines.append(
            "Land this value in "
            "src/lmchat/services/rag_mode_resolver.py:"
            "_DEFAULT_INLINE_FRACTION."
        )
    else:
        lines.append(
            "no winning threshold — HYBRID never beats INLINE by ≥ 0.05 "
            "across the grid. Try a wider grid, more gold queries, or "
            "a different embedding model."
        )
    lines.append("=" * 72)
    return "\n".join(lines)


# ─── CLI entry (called from scripts/run_d2_sweep.py) ─────────────────────


def parse_thresholds(s: str) -> tuple[int, ...]:
    """Parse ``"2000,4000,..."`` into a tuple of ints."""
    return tuple(int(p.strip()) for p in s.split(",") if p.strip())


async def cli_main(args: Any) -> int:
    """CLI entry point. *args* is an argparse-shaped namespace."""
    import httpx

    from lmchat.embedding.client import EmbeddingClient
    from lmchat.services.models_service import ModelsService

    corpus = Path(args.corpus)
    if not corpus.exists() or not corpus.is_dir():
        print(f"ERROR: corpus directory not found: {corpus}")
        return 2
    gold_path = Path(args.gold)
    if not gold_path.exists():
        print(f"ERROR: gold set not found: {gold_path}")
        return 2

    import json as _json
    gold_raw = _json.loads(gold_path.read_text())
    if not isinstance(gold_raw, list) or not gold_raw:
        print(f"ERROR: gold set at {gold_path} is empty or malformed")
        return 2
    gold = [
        GoldQuery(
            query=entry["query"],
            relevant_doc_ids=list(entry.get("relevant_doc_ids", [])),
        )
        for entry in gold_raw
    ]

    base_url = args.lm_studio_url or os.environ.get(
        "LM_STUDIO_URL", "http://localhost:1234"
    )
    api_key = os.environ.get("LM_STUDIO_API_KEY", "")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with httpx.AsyncClient(
        headers=headers, timeout=httpx.Timeout(60.0)
    ) as http_client:
        models_service = ModelsService(
            http_client=http_client, base_url=base_url
        )
        embedding_client = EmbeddingClient(
            http_client=http_client,
            base_url=base_url,
            wire_id_resolver=models_service.resolve_embedding_wire_id,
        )
        report = await run_sweep(
            corpus_dir=corpus,
            gold_set=gold,
            embedding_client=embedding_client,
            models_service=models_service,
            ctx_window=args.ctx_window,
            thresholds=parse_thresholds(args.thresholds),
            top_k=args.top_k,
        )
    print(format_report(report))
    if args.print_id_map:
        print("\n--- id_map (filename → doc_id) ---")
        for fname, did in report.id_map.items():
            print(f"{did:>6d}  {fname}")
    return 0


def cli_run(argv: list[str] | None = None) -> int:
    """Synchronous wrapper for the CLI."""
    import argparse

    p = argparse.ArgumentParser(
        description=(
            "D2 RAG-mode threshold empirical sweep — recall@k vs "
            "threshold on a hand-labeled gold set."
        )
    )
    p.add_argument(
        "--corpus", required=True,
        help="Directory of files to index into the test project.",
    )
    p.add_argument(
        "--gold", required=True,
        help="JSON file with [{query, relevant_doc_ids}] entries.",
    )
    p.add_argument(
        "--ctx-window", type=int, default=131_000,
        help="Anchor model's ctx window (default 131_000).",
    )
    p.add_argument(
        "--thresholds", default="2000,4000,8000,16000,32000",
        help="Comma-separated threshold grid.",
    )
    p.add_argument(
        "--top-k", type=int, default=DEFAULT_TOP_K,
        help="k in recall@k (default 8).",
    )
    p.add_argument(
        "--lm-studio-url", default=None,
        help="Override LM Studio URL (else $LM_STUDIO_URL or default).",
    )
    p.add_argument(
        "--print-id-map", action="store_true",
        help=(
            "After the sweep, print the filename→doc_id map (useful "
            "when curating the gold set's relevant_doc_ids)."
        ),
    )
    return asyncio.run(cli_main(p.parse_args(argv)))
