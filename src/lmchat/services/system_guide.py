# SPDX-License-Identifier: Apache-2.0
"""Retrieval over LM Chat's own user guide (``guide/*.md``), for two callers:
``search_guide`` (a PULL-style lookup — returns the best-matching sections,
or a table of contents when nothing matches) and system-prompt guide
injection (``services/streaming_service.py`` — the app PUSHES the top match
onto every turn's system prompt via ``is_app_directed_question`` +
``guide_context_block`` / ``guide_context_block_semantic``).

Design
------
This module does NOT own an embedding model. LM Chat already runs the
user's own LM Studio embedding model for document/memory RAG
(``embedding/client.py``, ``services/rag_service.py``) — bundling a SECOND
embedder here (a prior version shipped ``fastembed``'s
``BAAI/bge-small-en-v1.5``, an ~130MB ONNX model) duplicated that
dependency for no benefit. Instead:

- The KEYWORD/IDF scorer (``_scored_sections_keyword``) is this module's
  own, always-available, dependency-free engine. It powers ``search_guide``
  (the PULL tool) directly, and is ``guide_context_block``'s (the sync PUSH
  entry point) fallback when no embedding model is available or reachable.
  Scoring is a light term-frequency scheme — page title and heading matches
  are weighted well above body matches, repeated body hits saturate
  (BM25-style) rather than scaling linearly, and each term is additionally
  weighted by IDF (how rare it is across the corpus). Query terms are
  stopword-filtered first: natural-language queries like "how does memory
  work?" are how the model actually calls this tool, and generic words
  ("how", "does", "work") match generic headings ("Does it work offline?")
  everywhere, drowning out the one word that actually picks the right page
  ("memory") unless it's rarity-weighted and the filler is dropped.

- The SEMANTIC engine (``ensure_section_embeddings`` +
  ``guide_context_block_semantic``) is BYOE (bring your own embedder): the
  caller (``streaming_service._assemble_system_prompt``) resolves the
  user's active LM Studio embedding model and injects two async callables —
  ``embed_batch`` (to embed the guide corpus once, cached) and ``embed_one``
  (to embed each query) — bound to ``embedding.client.EmbeddingClient``.
  This module just does the cosine math. It is deliberately caller-agnostic
  about WHICH embedding model is in play — unlike the old bundled bge-small
  (an asymmetric retrieval model with a fixed, known query/passage split),
  an arbitrary user-configured embed model (nomic, bge-m3, whatever LM
  Studio has loaded) is symmetric as far as this module is concerned, so
  both sections and queries are embedded the same way (no query-prefix
  trick). The injection floor (``_INJECT_COSINE_MIN_SEMANTIC``) is
  correspondingly a LOOSE relevance floor, not a knife-edge — see its
  comment for the measured numbers and why precision leans on the intent
  gate instead of the floor alone.

- ``is_app_directed_question`` is a cheap, sync, model-agnostic PRIMARY
  gate applied by the caller BEFORE any retrieval runs at all (keyword or
  semantic): a declarative statement ("I'm refactoring my project's memory
  usage") never reaches either engine, so it can't be misread as an app
  question by either — see that function's docstring.

Each page is split on markdown headings (``#``/``##``/``###``) into
sections, so retrieval can point at the relevant paragraph of a long page
(troubleshooting, api-reference) instead of always returning the whole
page. Parsed sections are cached per-process, keyed on each file's mtime
(the same cache also holds the semantic embedding matrix, additionally
keyed on the embedding model), so an edit (or a new file, or a model
switch) is picked up without a restart.

``search_guide`` is the stable, swappable contract: callers (the builtin
tool executor) never see how sections are scored, so the backend can later
change the scoring engine again without changing the tool's behavior.
``guide_context_block`` / ``guide_context_block_semantic`` share the same
section corpus but apply their own, engine-specific, stricter threshold and
single-section return shape — appropriate for something injected
unconditionally rather than pulled on request.
"""
from __future__ import annotations

import asyncio
import math
import re
import threading
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from lmchat.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "ensure_section_embeddings",
    "ensure_section_embeddings_background",
    "get_cached_section_embeddings",
    "guide_context_block",
    "guide_context_block_semantic",
    "guide_topics",
    "is_app_directed_question",
    "search_guide",
]

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$")
_WORD_RE = re.compile(r"[a-z0-9]+")

# Generic English filler that carries no page-discriminating signal on its
# own. Dropped from the QUERY before KEYWORD scoring — a natural-language
# question ("how does memory work?") should score on "memory" alone, not on
# "how" / "does" / "work" matching whatever heading happens to contain them
# too. Not exhaustive (this is a light heuristic, not real NLP) — sized to
# what actually shows up in "how do I ..." / "how does ... work" style
# questions. Not used by the semantic engine (embedding similarity doesn't
# need stopword-stripping the way term-overlap scoring does).
_STOPWORDS = frozenset({
    "a", "am", "an", "and", "any", "are", "as", "at", "be", "been", "being",
    "but", "by", "can", "could", "did", "do", "does", "doing", "done",
    "for", "from", "get", "gets", "getting", "got", "had", "has", "have",
    "having", "how", "i", "if", "in", "into", "is", "it", "its", "just",
    "like", "may", "me", "might", "mine", "must", "my", "of", "on", "or",
    "our", "out", "shall", "should", "so", "some", "than", "that", "the",
    "their", "them", "then", "there", "these", "they", "this", "those",
    "to", "up", "us", "use", "used", "uses", "using", "was", "we", "were",
    "what", "when", "where", "which", "who", "whom", "whose", "why", "will",
    "with", "work", "working", "works", "would", "you", "your",
})

# Weights: a query term matching the page title or the section heading is a
# much stronger signal than the same term appearing in the body somewhere.
# Keyword engine only.
_TITLE_WEIGHT = 4.0
_HEADING_WEIGHT = 6.0
# BM25-style tf saturation for body-term hits: repeated occurrences of the
# same term keep contributing, but with rapidly diminishing returns, so a
# page that just repeats a word a lot can't out-rank a real heading match.
_BODY_SATURATION_K = 2.0

# A query with zero term overlap anywhere scores 0 and falls back to the
# table of contents; anything with at least one real hit clears this. IDF is
# floored at 1.0 (a term in every section) so this threshold — calibrated
# against the un-weighted (idf=1) score of a single body hit — still holds.
# Keyword engine only (``search_guide``'s PULL floor, via ``_ranked_sections``).
_MIN_SCORE = 0.5

# Embed the guide corpus in small chunks — LM Studio embed backends have a
# per-request batch ceiling (measured ~8-15 for nomic-embed-text; exceeding it
# returns 400 "LM Link connection closed" and can knock the model into a
# transient error state). 8 stays safely under it; the corpus (~265 sections)
# embeds in ~34 quick calls, once, then cached.
_EMBED_CHUNK_SIZE = 8

# Ceiling on the ONE-TIME background corpus embed
# (``_GuideCache._run_background_embed``) — deliberately GENEROUS, unlike
# the caller's per-turn ``_GUIDE_SEMANTIC_TIMEOUT_SEC`` (streaming_service,
# 8s): this runs once, off the per-turn hot path, and has to cover the
# real corpus's ~34 sequential chunked batch calls (~34s measured). A
# turn-scoped timeout on this exact work is what made the semantic engine
# permanently dead in production before this background split — every
# turn cancelled it before it could finish, so the matrix never cached.
# 1800 s (30 min) matches the app-wide local-first posture: the measured
# ~34s is nowhere near this ceiling, but each chunk is itself a call to a
# local embedding model, and this timeout must never be the thing that
# re-kills the semantic engine on slow hardware or a large corpus.
_BACKGROUND_EMBED_TIMEOUT_SEC = 1800.0

# Cap on the total size of a search_guide() result — generous (top sections
# are usually far smaller), but bounds a pathological very-large section.
_MAX_RESULT_CHARS = 6000


# ─── Semantic engine plumbing (BYOE — bring your own embedder) ─────────────
#
# This module owns none of the embedding call machinery — the caller injects
# it. ``_EmbedBatchFn`` mirrors ``embedding.client.EmbeddingClient.embed_batch``'s
# signature exactly (a bound method satisfies this Protocol with no
# adaptation); ``_EmbedOneFn`` is a single-text callable the caller closes
# over its resolved model key with — a small async closure, e.g.::
#
#     async def _embed_one(text):
#         return await embedding_client.embed_one(text=text, model_id=key)
#
# (NOT ``functools.partial`` — ``embed_one``'s ``text`` parameter is
# keyword-only, so a partial bound only on ``model_id`` still can't be
# called positionally the way this Protocol calls it) — see
# ``streaming_service._assemble_system_prompt``.
class _EmbedBatchFn(Protocol):
    async def __call__(self, *, texts: list[str], model_id: str) -> list[list[float]]: ...


_EmbedOneFn = Callable[[str], Awaitable[list[float]]]

# What ``ensure_section_embeddings`` returns and ``guide_context_block_semantic``
# consumes: the section list the matrix's rows are index-aligned to, PAIRED
# together so a caller can never accidentally zip a stale section list
# against a fresher matrix (or vice versa) across two separate cache reads.
SectionEmbeddings = tuple[list["_Section"], np.ndarray]


@dataclass(frozen=True)
class _Section:
    page_id: str
    page_title: str
    heading: str
    body: str


class _GuideCache:
    """Process-wide cache of parsed guide sections + their IDF table,
    invalidated by mtime — plus a SEPARATE semantic embedding-matrix cache,
    invalidated by mtime AND by embedding model key.

    Not a singleton instance held elsewhere — module-level state, guarded by
    locks so concurrent requests (async handlers running in a threadpool,
    or plain concurrent calls) can't race a rebuild. IDF is corpus-wide
    (depends on every section, not just one), so it's computed once per
    rebuild here rather than per-query.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._guide_dir: Path | None = None
        self._mtimes: dict[Path, float] = {}
        self._sections: list[_Section] = []
        self._idf: dict[str, float] = {}

        # Semantic embedding-matrix cache. Separate from the sections/idf
        # snapshot above because it's keyed ADDITIONALLY by the embedding
        # model: two different embed models produce vectors in different,
        # non-comparable spaces, so a model switch must invalidate it
        # independent of whether the guide files themselves changed.
        #
        # Keyed on ``(model_key, id(sections))`` rather than re-deriving a
        # guide-content fingerprint: ``snapshot()`` only ever produces a NEW
        # ``sections`` list object on an actual rebuild (mtime change) — an
        # unchanged snapshot returns the SAME cached list object — so
        # ``id(sections)`` is a free, exact proxy for "which guide-content
        # generation is this" with no extra bookkeeping, and it can't go
        # stale out from under a concurrent snapshot() the way re-reading
        # ``self._mtimes`` separately could.
        #
        # Guarded by its own ``asyncio.Lock`` (the rebuild awaits a real
        # network call via the caller-injected ``embed_batch``, so it can't
        # share the sync ``threading.Lock`` above without blocking unrelated
        # concurrent callers for the duration of that HTTP round trip).
        self._embed_lock = asyncio.Lock()
        self._embed_cache_key: tuple[str, int] | None = None
        self._embed_sections: list[_Section] = []
        self._embed_matrix: np.ndarray | None = None

        # Background corpus-embed tracking (fire-and-forget, kicked off by
        # ``ensure_section_embeddings_background``). AT MOST ONE task is
        # ever tracked here — a single reference + the model_key it's
        # embedding for, not a dict per model_key (a model switch mid-embed
        # just starts a second, untracked task rather than being blocked by
        # this bookkeeping; see that method's docstring). Both attributes
        # are only ever read-then-written from PLAIN SYNCHRONOUS code (no
        # ``await`` between the read and the write) inside
        # ``ensure_section_embeddings_background`` — unlike
        # ``_embed_cache_key``/``_embed_matrix`` above (mutated from inside
        # an ``await``-laden critical section, hence ``_embed_lock``),
        # asyncio's cooperative single-threaded scheduling already makes
        # that check-then-set sequence atomic with no separate lock needed.
        self._embed_bg_task: asyncio.Task[None] | None = None
        self._embed_bg_model_key: str | None = None

    def snapshot(self) -> tuple[list[_Section], dict[str, float]]:
        guide_dir = _resolve_guide_dir()
        with self._lock:
            if guide_dir is None:
                self._guide_dir = None
                self._mtimes = {}
                self._sections = []
                self._idf = {}
                return [], {}

            paths = sorted(guide_dir.glob("*.md"))
            current_mtimes = {p: p.stat().st_mtime for p in paths}
            if guide_dir == self._guide_dir and current_mtimes == self._mtimes:
                return self._sections, self._idf

            sections: list[_Section] = []
            for path in paths:
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    continue
                sections.extend(_parse_page(path.stem, text))

            self._guide_dir = guide_dir
            self._mtimes = current_mtimes
            self._sections = sections
            self._idf = _compute_idf(sections)
            return sections, self._idf

    def get_cached_section_embeddings(self, model_key: str) -> SectionEmbeddings | None:
        """Non-blocking read of the cached ``(sections, matrix)`` pair for
        *model_key* — ``None`` if no corpus embed has completed yet for
        this model_key against the CURRENT guide corpus (mtimes unchanged
        since the cache was built).

        Never embeds and never awaits anything beyond ``snapshot()``'s own
        plain synchronous mtime check (a ``stat()`` per guide file, itself
        cached and cheap) — safe to call on every turn's hot path. This is
        the ONLY way the per-turn path should ever touch the semantic
        cache; the one-time corpus embed itself belongs to
        ``ensure_section_embeddings_background``.
        """
        sections, _idf = self.snapshot()
        if not sections:
            return None
        cache_key = (model_key, id(sections))
        if self._embed_cache_key == cache_key and self._embed_matrix is not None:
            return self._embed_sections, self._embed_matrix
        return None

    def ensure_section_embeddings_background(
        self, *, embed_batch: _EmbedBatchFn, model_key: str
    ) -> None:
        """Idempotent, fire-and-forget kickoff of the ONE-TIME corpus embed
        for *model_key* as a detached ``asyncio.create_task``, run under a
        generous internal timeout (``_BACKGROUND_EMBED_TIMEOUT_SEC``) —
        this is deliberately a plain (non-``async``) function: the caller
        never awaits it (an un-awaited coroutine would simply never run),
        it only ever schedules a task and returns immediately.

        No-ops (does nothing, returns immediately) when:
          - a current cached matrix already exists for *model_key*
            (nothing to do — checked via ``get_cached_section_embeddings``),
            or
          - a background embed is already in flight for *model_key* (never
            start a second concurrent corpus embed against the same
            model — a DIFFERENT model_key while one is in flight is not
            blocked by this check and starts its own, untracked, task;
            a mid-flight model switch is a rare edge case not worth
            serializing behind).

        Never raises to the caller — any failure inside the background
        task (network, malformed response, or the internal timeout firing)
        is caught and logged ONCE inside ``_run_background_embed``, which
        then clears the tracked task handle via a done-callback so a LATER
        call (the next turn that finds the cache still cold) can retry.
        This function's own body has no ``await`` in it, so the
        check-then-launch sequence can't race a concurrent call to this
        same function — the single-threaded event loop can't interleave
        another coroutine's code until THIS function actually hits an
        await point, and it hits none.
        """
        if self.get_cached_section_embeddings(model_key) is not None:
            return
        if (
            self._embed_bg_task is not None
            and not self._embed_bg_task.done()
            and self._embed_bg_model_key == model_key
        ):
            return
        task = asyncio.create_task(
            self._run_background_embed(embed_batch=embed_batch, model_key=model_key)
        )
        # Keep a strong reference so the task can't be garbage-collected
        # mid-flight (a bare fire-and-forget ``create_task`` with no
        # retained reference is a classic asyncio footgun — the task can be
        # silently dropped before it completes). Cleared by the
        # done-callback below once the task finishes, whatever the outcome.
        self._embed_bg_task = task
        self._embed_bg_model_key = model_key
        task.add_done_callback(self._on_background_embed_done)

    def _on_background_embed_done(self, task: asyncio.Task[None]) -> None:
        # Clear the tracked handle so a future call can retry — regardless
        # of whether this run succeeded, failed (already logged inside
        # ``_run_background_embed``), or was cancelled (e.g. process
        # shutdown). Only clears if ``task`` is STILL the tracked task —
        # guards against clobbering a newer task's bookkeeping in the rare
        # case a later call already replaced it (a model switch mid-embed
        # started a second, untracked task before this one's callback ran).
        if self._embed_bg_task is task:
            self._embed_bg_task = None
            self._embed_bg_model_key = None

    async def _run_background_embed(
        self, *, embed_batch: _EmbedBatchFn, model_key: str
    ) -> None:
        """The actual background task body: run the (already-chunked,
        batch size ``_EMBED_CHUNK_SIZE``) corpus embed under a GENEROUS
        timeout. This runs once, off the per-turn hot path, so it can
        afford the real corpus's ~34 sequential batch calls (~34s) that
        made the OLD inline-per-turn design always blow past a turn-scoped
        timeout. ``_GUIDE_SEMANTIC_TIMEOUT_SEC`` (the caller's per-turn
        ceiling, in ``streaming_service``) only ever wraps the fast
        single-query embed now.

        Never raises into the event loop's default task-exception handler
        (which would otherwise log an unhandled "Task exception was never
        retrieved" warning): ``ensure_section_embeddings`` itself already
        never raises on an embedding failure (it logs and returns
        ``None``), so the ``except`` here exists specifically to catch the
        ``TimeoutError`` this method's own timeout produces.
        """
        try:
            async with asyncio.timeout(_BACKGROUND_EMBED_TIMEOUT_SEC):
                await self.ensure_section_embeddings(embed_batch=embed_batch, model_key=model_key)
        except Exception:  # noqa: BLE001 -- background embed must never crash the event loop
            log.warning(
                "system_guide.background_embed_timed_out_or_failed",
                model_key=model_key,
                exc_info=True,
            )

    async def ensure_section_embeddings(
        self, *, embed_batch: _EmbedBatchFn, model_key: str
    ) -> SectionEmbeddings | None:
        """Return the cached ``(sections, L2-normalized [n, dim] matrix)``
        pair for *model_key*, rebuilding via *embed_batch* when the guide
        corpus or the model key has changed since the last build (or on the
        first call).

        Never raises — any embedding failure (network, malformed response,
        a caller-side timeout wrapping this call) is logged and returns
        ``None`` so the caller falls back to keyword retrieval for this
        turn. Unlike the old bundled-embedder's load failure, this is NOT
        memoized as a permanent "unavailable" — the injected *embed_batch*
        talks to a live LM Studio model that can come back (or change)
        between calls with no process restart, so every call retries.

        This is the BLOCKING primitive — kept for the background task
        (``_run_background_embed``) and for direct measurement/tests. The
        per-turn streaming path must NEVER call this inline; it only ever
        reads via ``get_cached_section_embeddings`` (non-blocking) and
        kicks off a rebuild via ``ensure_section_embeddings_background``
        (fire-and-forget) — see both methods' docstrings for why: this
        method's own corpus embed takes ~34 sequential batch calls
        (~34s), which blows past any turn-scoped timeout every single
        time.
        """
        sections, _idf = self.snapshot()
        if not sections:
            return None
        cache_key = (model_key, id(sections))
        async with self._embed_lock:
            if self._embed_cache_key == cache_key and self._embed_matrix is not None:
                return self._embed_sections, self._embed_matrix
            try:
                texts = [f"{s.page_title}. {s.heading}. {s.body}" for s in sections]
                vectors: list[list[float]] = []
                for _start in range(0, len(texts), _EMBED_CHUNK_SIZE):
                    _chunk = texts[_start : _start + _EMBED_CHUNK_SIZE]
                    vectors.extend(await embed_batch(texts=_chunk, model_id=model_key))
                matrix = np.array(vectors, dtype=np.float64)
                if matrix.ndim != 2 or matrix.shape[0] != len(sections):
                    raise ValueError(
                        f"embed_batch returned shape {matrix.shape} for "
                        f"{len(sections)} sections"
                    )
            except Exception:  # noqa: BLE001 -- never fail guide retrieval over an embedding error
                log.warning(
                    "system_guide.embed_sections_failed",
                    model_key=model_key,
                    exc_info=True,
                )
                return None
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            norms[norms == 0.0] = 1.0
            matrix = matrix / norms
            self._embed_cache_key = cache_key
            self._embed_sections = sections
            self._embed_matrix = matrix
            return sections, matrix


_cache = _GuideCache()


def _resolve_guide_dir() -> Path | None:
    """Locate the ``guide/`` directory in dev or the Docker runtime image.

    Mirrors ``app._resolve_web_dist``'s package-relative resolution, plus a
    cwd-relative fallback for the odd case of a working directory that
    doesn't match the package layout. Returns ``None`` if neither resolves
    (e.g. a stripped install with no guide shipped) so callers degrade to
    "guide unavailable" rather than raising.
    """
    candidates = (
        # src/lmchat/services/system_guide.py -> .parent (services) ->
        # .parent (lmchat) -> .parent (src) -> .parent (repo root / image
        # root, matching deploy/Dockerfile's runtime COPY target).
        Path(__file__).resolve().parent.parent.parent.parent / "guide",
        Path.cwd() / "guide",
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def _page_title(page_id: str, text: str) -> str:
    for line in text.splitlines():
        match = _HEADING_RE.match(line)
        if match and len(match.group(1)) == 1:
            return match.group(2)
    return page_id.replace("-", " ").replace("_", " ").strip().title()


def _parse_page(page_id: str, text: str) -> list[_Section]:
    title = _page_title(page_id, text)
    sections: list[_Section] = []
    heading = title
    body_lines: list[str] = []

    def flush() -> None:
        body = "\n".join(body_lines).strip()
        if body:
            sections.append(
                _Section(page_id=page_id, page_title=title, heading=heading, body=body)
            )

    for line in text.splitlines():
        match = _HEADING_RE.match(line)
        if match:
            flush()
            heading = match.group(2)
            body_lines = []
        else:
            body_lines.append(line)
    flush()

    if not sections:
        stripped = text.strip()
        if stripped:
            sections.append(
                _Section(page_id=page_id, page_title=title, heading=title, body=stripped)
            )
    return sections


def _raw_terms(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _normalize(term: str) -> str:
    """Fold a trailing plural 's' so "tool"/"tools", "folder"/"folders"
    etc. are the same scoring term.

    Not real stemming — just enough that a query's singular/plural choice
    ("set up an MCP tool") doesn't silently miss a page whose heading/title
    happens to use the other form ("MCP and tools"). Guarded (len > 3, not
    a double-s) so short words and words genuinely ending in 's' ("access")
    aren't mangled; applied identically to every term extracted anywhere
    (title/heading/body/query and the IDF table below), so it can only ever
    widen a match, never change which term two equal strings normalize to.
    Keyword engine only.
    """
    if len(term) > 3 and term.endswith("s") and not term.endswith("ss"):
        return term[:-1]
    return term


def _terms(text: str) -> list[str]:
    return [_normalize(term) for term in _raw_terms(text)]


def _query_terms(query: str) -> set[str]:
    """Query terms, stopword-filtered then normalized.

    Stopwords are matched against the RAW (pre-normalization) token so the
    hand-written ``_STOPWORDS`` list stays readable English; body/title/
    heading tokenization is otherwise untouched — a page is free to say
    "how" or "work" as much as it likes, only the QUERY side is filtered, so
    a natural-language question scores on its content words alone.
    Keyword engine only.
    """
    return {_normalize(term) for term in _raw_terms(query or "") if term not in _STOPWORDS}


def _compute_idf(sections: list[_Section]) -> dict[str, float]:
    """Inverse document frequency per term, over the section corpus.

    ``df`` = number of sections containing the term at least once (title,
    heading, or body — anywhere it could match); ``N`` = total sections.
    Smoothed + floored at 1.0 (``+1`` in both halves of the log, ``+1``
    outside it) so a term present in literally every section still
    contributes a little rather than zeroing out, and an unseen term never
    divides by zero. Keyword engine only.
    """
    n = len(sections)
    doc_freq: Counter[str] = Counter()
    for section in sections:
        terms_in_section = (
            set(_terms(section.page_title))
            | set(_terms(section.heading))
            | set(_terms(section.body))
        )
        doc_freq.update(terms_in_section)
    return {term: math.log((n + 1) / (count + 1)) + 1.0 for term, count in doc_freq.items()}


def _score_section(query_terms: set[str], section: _Section, idf: dict[str, float]) -> float:
    if not query_terms:
        return 0.0
    title_terms = set(_terms(section.page_title))
    heading_terms = set(_terms(section.heading))
    body_counts = Counter(_terms(section.body))

    score = 0.0
    for term in query_terms:
        term_score = 0.0
        if term in title_terms:
            term_score += _TITLE_WEIGHT
        if term in heading_terms:
            term_score += _HEADING_WEIGHT
        tf = body_counts.get(term, 0)
        if tf:
            k = _BODY_SATURATION_K
            term_score += (tf * (k + 1)) / (tf + k)
        # A term unseen anywhere in the corpus never matches (term_score
        # stays 0 above), so the idf default here is never load-bearing.
        score += term_score * idf.get(term, 1.0)
    return score


def _section_text(section: _Section) -> str:
    return f"## {section.page_title} — {section.heading}\n\n{section.body}".strip()


def _topics(sections: list[_Section]) -> list[tuple[str, str]]:
    topics: dict[str, str] = {}
    for section in sections:
        topics.setdefault(section.page_id, section.page_title)
    return list(topics.items())


def _table_of_contents(sections: list[_Section]) -> str:
    topics = _topics(sections)
    if not topics:
        return "The LM Chat user guide is not available in this deployment."
    lines = ["No section of the guide matched that query closely. Guide topics:"]
    lines.extend(f"- {page_id}: {title}" for page_id, title in topics)
    return "\n".join(lines)


def guide_topics() -> list[tuple[str, str]]:
    """``(page_id, human_title)`` for every guide page, in file order."""
    sections, _idf = _cache.snapshot()
    return _topics(sections)


def _scored_sections_keyword(query: str) -> list[tuple[float, _Section]]:
    """``(score, section)`` pairs for every section via the keyword/IDF
    engine, best first, unfiltered.

    Shared by ``_ranked_sections`` (filters at ``_MIN_SCORE``, discards the
    score) and ``guide_context_block`` (filters at the stricter two-tier
    gate, needs the score to compare against it). Empty when the guide is
    unavailable or the query is all stopwords.
    """
    sections, idf = _cache.snapshot()
    if not sections:
        return []
    query_terms = _query_terms(query)
    if not query_terms:
        return []
    return sorted(
        ((_score_section(query_terms, section, idf), section) for section in sections),
        key=lambda pair: pair[0],
        reverse=True,
    )


def _ranked_sections(query: str) -> list[_Section]:
    """Sections matching *query*, best first, already threshold-filtered at
    ``_MIN_SCORE`` — the keyword engine's near-zero-overlap floor.

    Empty when the guide is unavailable, the query is all stopwords, or
    nothing clears ``_MIN_SCORE`` — callers fall back to the table of
    contents in every one of those cases, so they don't need to be told
    apart.

    Split out from ``search_guide`` (which only formats this into text) so
    tests can pin ranking — which PAGE comes out on top for a
    natural-language query — without parsing the rendered string.
    """
    return [section for score, section in _scored_sections_keyword(query) if score >= _MIN_SCORE]


def search_guide(query: str, *, max_sections: int = 4) -> str:
    """Return the guide sections best matching *query* as readable text.

    Scores every section via the keyword/IDF engine (``_ranked_sections``)
    — this is a PULL-style tool call, invoked synchronously by the builtin
    tool executor, so it does not use the semantic engine (which needs an
    async-injected embedder; see the module docstring). Returns up to
    *max_sections* top-scoring sections, each labeled with its page title
    and heading, capped to a total of a few KB. Falls back to the table of
    contents when nothing scores above a small relevance threshold — or the
    query was pure filler ("how do I do this?") — so the model can refine
    its query.
    """
    sections, _idf = _cache.snapshot()
    if not sections:
        return "The LM Chat user guide is not available in this deployment."

    matches = _ranked_sections(query)
    if not matches:
        return _table_of_contents(sections)

    parts: list[str] = []
    total_chars = 0
    for section in matches[:max_sections]:
        block = _section_text(section)
        if parts and total_chars + len(block) > _MAX_RESULT_CHARS:
            break
        parts.append(block)
        total_chars += len(block)
    return "\n\n---\n\n".join(parts)


# ─── Intent gate — the PRIMARY guard on injection, applied by the caller ──
#
# Injection (``guide_context_block`` / ``guide_context_block_semantic``) is a
# PUSH (unsolicited system-prompt insertion on every turn), unlike
# ``search_guide``'s PULL (a caller explicitly requests a lookup). Before
# either retrieval engine even runs, the caller
# (``streaming_service._assemble_system_prompt``) checks
# ``is_app_directed_question`` first: this is what keeps a purely
# declarative message ("I'm refactoring my project's memory usage") from
# ever reaching retrieval at all, and — for the semantic path specifically —
# is what keeps the app from embedding the user's message on every single
# turn regardless of whether it could plausibly be about LM Chat.
_APP_DIRECTED_OPENERS: tuple[str, ...] = (
    "how do i",
    "how does",
    "how can i",
    "what is",
    "where is",
    "where do i",
    "can i",
    "do i",
    "is there",
    "show me",
    "walk me through",
    "help me",
    "set up",
    "turn on",
    "enable",
    "configure",
)


def is_app_directed_question(text: str) -> bool:
    """Cheap, sync, model-agnostic heuristic: could *text* plausibly be a
    question or request about LM Chat itself?

    True when *text* ends with ``?``, or CONTAINS one of a small set of
    interrogative/request phrases (``"how do i"``, ``"what is"``,
    ``"can i"``, ``"set up"``, ...) — the forms LM Chat's own users actually
    type when asking about the app ("how do I set up a project in this
    app?", "where is the incognito toggle?"). False for declarative
    statements ("I'm refactoring my project's memory usage") and anything
    else that doesn't read as a question/request, regardless of whether it
    happens to share vocabulary with the guide corpus.

    This is a HEURISTIC, not NLP — intentionally small and NOT exhaustive
    (a phrased-as-statement request like "I need to know how to attach a
    document" will miss). It is the PRIMARY, first-applied gate: when it
    returns False, the caller skips retrieval entirely — no keyword scoring,
    no embedding call, no injection — so it doubles as the cost control that
    keeps the app from embedding the user's message on every turn.
    """
    stripped = (text or "").strip()
    if not stripped:
        return False
    if stripped.endswith("?"):
        return True
    lowered = stripped.lower()
    return any(opener in lowered for opener in _APP_DIRECTED_OPENERS)


# ─ Keyword-fallback injection gate ─
# A single scalar score can't gate cleanly on its own: "project" is repeated
# in nearly every heading of ``guide/06-projects.md``, so it alone hits BOTH
# the title and heading weights and saturates the body-tf term — "I'm
# refactoring my project's memory usage" (nothing to do with the Projects
# feature) scores ~39, ABOVE a real question like "how do I set up a
# project in this app" (~38). No threshold separates those. So the keyword
# engine's gate uses two signals: a HIGH score (distinctive guide phrasing
# that only shows up in a real app question, e.g. "custom instructions to a
# project"), OR a MODERATE score together with an explicit app-referential
# cue in the message, which is what actually distinguishes asking-about-the-
# app from an incidental word. (``is_app_directed_question`` above already
# filters out most non-questions before either engine runs, but a
# declarative sentence CAN still contain one of its opener phrases as a
# substring — e.g. none of the corpus's known false positives do today, but
# this gate is the second line of defense regardless.)
_INJECT_HIGH_SCORE = 50.0
_INJECT_MODERATE_SCORE = 20.0
_APP_REFERENTIAL_CUES: tuple[str, ...] = (
    "lm chat",
    "lmchat",
    "this app",
    "the app",
    "this tool",
    "in here",
    "this chat app",
)


def _has_app_referential_cue(query: str) -> bool:
    lowered = query.lower()
    return any(cue in lowered for cue in _APP_REFERENTIAL_CUES)


def _should_inject_keyword(query: str, score: float) -> bool:
    """Two-tier injection gate for the KEYWORD engine: a HIGH score alone
    (distinctive guide phrasing), or a MODERATE score plus an explicit
    app-referential cue that marks the message as a question about LM Chat
    rather than an incidental word overlap. Not used by the semantic
    engine — see ``_INJECT_COSINE_MIN_SEMANTIC``."""
    if score >= _INJECT_HIGH_SCORE:
        return True
    if score >= _INJECT_MODERATE_SCORE:
        return _has_app_referential_cue(query)
    return False


def guide_context_block(query: str) -> str | None:
    """Return the single best-matching guide section as a labeled block for
    SYSTEM-PROMPT INJECTION via the KEYWORD engine, or ``None`` when the
    message doesn't clear the two-tier injection gate.

    This is the SYNC fallback path: the caller
    (``streaming_service._assemble_system_prompt``) uses this when no
    embedding model is available/reachable (or ``is_app_directed_question``
    already said no, in which case it never calls this at all — see the
    module docstring). Unlike ``search_guide`` — a PULL-style lookup that
    returns up to several sections and falls back to a table of contents —
    this returns at most ONE section (keep the injection lean) and returns
    ``None`` on a non-match rather than falling back to anything, so callers
    add nothing to the prompt.

    Args:
        query: The latest user message text (or any free-text query).
            Empty/whitespace-only returns ``None``.
    """
    if not query or not query.strip():
        return None
    sections, _idf = _cache.snapshot()
    if not sections:
        return None
    scored = _scored_sections_keyword(query)
    if not scored:
        return None
    score, section = scored[0]
    if not _should_inject_keyword(query, score):
        return None
    return f"[LM Chat guide — {section.page_title} — {section.heading}]\n{section.body}"


# ─ Semantic injection gate ─
# A single cosine floor — LOOSE by design, not a knife-edge. Unlike the
# keyword engine, precision here leans primarily on ``is_app_directed_question``
# (already applied by the caller before this function is even reached): once
# a message has cleared that gate, this floor only needs to reject queries
# that are CLEARLY unrelated to the app (off-topic questions that happen to
# be phrased as questions), not to disambiguate app-vs-incidental word
# overlap the way the keyword gate's two-tier score+cue check does.
#
# The floor is MODEL-DEPENDENT: different embedding models produce cosines
# on different scales (query-instruction prefixes, symmetric vs asymmetric
# training, embedding dimensionality all shift the distribution), so a
# constant tuned against one model is not guaranteed to transfer exactly to
# another.
#
# Measured against nomic-embed-text-v1.5 (a common LM Studio embed model) on
# the real guide/*.md corpus: app questions score 0.69-0.81 (each hitting the
# right page), clearly-unrelated questions 0.51-0.53 — a wide, clean 0.16 gap.
# 0.60 sits mid-gap with margin on both sides. Kept LOOSE (biased toward
# recall) because the floor can't be re-measured per user-model at runtime and
# a missed app question (the feature silently fails its job) is worse than an
# occasional false-positive label the model is free to ignore. A very
# different embed model could shift the scale; the intent gate + keyword
# fallback keep the feature sane regardless.
_INJECT_COSINE_MIN_SEMANTIC = 0.60


async def ensure_section_embeddings(
    *, embed_batch: _EmbedBatchFn, model_key: str
) -> SectionEmbeddings | None:
    """Ensure the guide corpus is embedded under *model_key*, returning the
    cached ``(sections, matrix)`` pair (rebuilding via *embed_batch* first
    if the guide content or the model changed since the last build).

    Thin wrapper over ``_GuideCache.ensure_section_embeddings`` — see its
    docstring. Callers (``streaming_service``) should wrap this call in
    their own timeout; it never raises on its own (an embedding failure
    returns ``None``), but a hung upstream call has no deadline here.
    """
    return await _cache.ensure_section_embeddings(embed_batch=embed_batch, model_key=model_key)


def get_cached_section_embeddings(model_key: str) -> SectionEmbeddings | None:
    """Non-blocking wrapper over ``_GuideCache.get_cached_section_embeddings``
    — see its docstring. Safe to call on every turn's hot path: never
    embeds, never awaits anything.
    """
    return _cache.get_cached_section_embeddings(model_key)


def ensure_section_embeddings_background(
    *, embed_batch: _EmbedBatchFn, model_key: str
) -> None:
    """Idempotent, fire-and-forget wrapper over
    ``_GuideCache.ensure_section_embeddings_background`` — see its
    docstring. Call this WITHOUT awaiting it (it is not a coroutine — it
    schedules a background ``asyncio.create_task`` and returns immediately)
    when ``get_cached_section_embeddings`` returns ``None``, to kick off
    the one-time corpus embed; use the keyword-engine fallback for the
    CURRENT turn regardless of what this call does.
    """
    _cache.ensure_section_embeddings_background(embed_batch=embed_batch, model_key=model_key)


async def guide_context_block_semantic(
    query: str,
    *,
    embed_one: _EmbedOneFn,
    section_texts_and_meta: SectionEmbeddings | None,
) -> str | None:
    """Return the single best-matching guide section as a labeled block for
    SYSTEM-PROMPT INJECTION via the SEMANTIC engine, or ``None`` when the
    message doesn't clear ``_INJECT_COSINE_MIN_SEMANTIC`` (or embedding the
    query fails).

    Args:
        query: The latest user message text. Empty/whitespace-only returns
            ``None``.
        embed_one: Async single-text embedder closure (see ``_EmbedBatchFn``'s
            comment above for why this can't be a bare ``functools.partial``)
            — the caller has already resolved *which* model to use; this
            function only calls it.
        section_texts_and_meta: The ``(sections, matrix)`` pair from
            ``ensure_section_embeddings`` — passed straight through by the
            caller. ``None`` (corpus unavailable, or the ensure call
            failed/timed out) short-circuits to ``None`` here too.

    Never raises — a failure embedding the query (network, malformed
    response, caller-side timeout) is logged and returns ``None`` so the
    caller falls back to the keyword engine or injects nothing.
    """
    if not query or not query.strip():
        return None
    if section_texts_and_meta is None:
        return None
    sections, matrix = section_texts_and_meta
    if not sections or matrix.shape[0] != len(sections):
        return None
    try:
        vector = await embed_one(query)
        query_vec = np.array(vector, dtype=np.float64)
    except Exception:  # noqa: BLE001 -- never fail guide retrieval over a per-query embedding error
        log.warning("system_guide.embed_query_failed", exc_info=True)
        return None
    norm = float(np.linalg.norm(query_vec))
    if norm == 0.0:
        return None
    query_vec = query_vec / norm
    cosines = matrix @ query_vec
    idx = int(np.argmax(cosines))
    score = float(cosines[idx])
    if score < _INJECT_COSINE_MIN_SEMANTIC:
        return None
    section = sections[idx]
    return f"[LM Chat guide — {section.page_title} — {section.heading}]\n{section.body}"
