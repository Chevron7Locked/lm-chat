# SPDX-License-Identifier: Apache-2.0
"""Tests for system_guide — retrieval over guide/*.md.

Two independent pieces, tested separately:

- The KEYWORD/IDF engine (``_scored_sections_keyword`` / ``_ranked_sections``
  / ``search_guide`` / ``guide_context_block``) is dependency-free and
  always available — these tests run against the real ``guide/`` tree with
  no fixture needed to force a particular engine (there's only one sync
  engine now; the semantic engine lives entirely behind caller-injected
  async callables, see below).

- The SEMANTIC engine (``ensure_section_embeddings`` /
  ``guide_context_block_semantic``) takes a caller-injected async embedder
  — this module never calls a real embedding model. Tests use a small,
  deterministic FAKE embedder (``_FakeEmbedder`` below) against a synthetic
  ``tmp_guide_dir`` corpus, so cosine similarity is fully test-controlled
  and nothing here requires network access or a live LM Studio model.

Also covers ``is_app_directed_question`` — the cheap, sync PRIMARY gate the
caller (``streaming_service``) applies before EITHER engine runs.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest

from lmchat.services import system_guide
from lmchat.services.system_guide import (
    ensure_section_embeddings,
    guide_context_block,
    guide_context_block_semantic,
    guide_topics,
    is_app_directed_question,
    search_guide,
)

# ─── is_app_directed_question — the PRIMARY gate ───────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "how do I set up a project in this app?",
        "how does memory work?",
        "how can I attach a document for RAG?",
        "What is incognito mode?",
        "where is the dark mode toggle",
        "where do I enable RAG",
        "can I share a chat with someone",
        "do I need an API key",
        "is there a way to pin an insight",
        "show me how to configure MCP",
        "walk me through attaching a document",
        "help me set up a project",
        "set up an MCP tool",
        "turn on incognito mode",
        "enable dark mode",
        "configure the default model",
        "does this support markdown?",  # bare "?" alone is enough
    ],
)
def test_is_app_directed_question_true_for_questions_and_requests(text: str) -> None:
    assert is_app_directed_question(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "I'm refactoring my project's memory usage",
        "the weather is nice today",
        "python vs javascript performance",
        "this is a great app",
        "reticulating splines",
        "",
        "   ",
    ],
)
def test_is_app_directed_question_false_for_declaratives(text: str) -> None:
    assert is_app_directed_question(text) is False


def test_is_app_directed_question_handles_none_like_input() -> None:
    assert is_app_directed_question("") is False


# ─── Natural-language queries — pin the top-ranked PAGE (keyword engine) ──
#
# This is how the model actually calls the tool: a full question, not a
# bare content word. "how", "does", "work" etc. must not out-rank the one
# discriminating word in the query (see _STOPWORDS / IDF in
# system_guide.py) — a content-word query like "incognito chats" alone
# doesn't exercise that path, so these assert the top result's page_id
# specifically, against the real guide.


def test_natural_language_query_projects_ranks_projects_page_first() -> None:
    top = system_guide._ranked_sections("how do projects work?")[0]
    assert top.page_id == "06-projects"


def test_natural_language_query_memory_ranks_memory_page_first() -> None:
    top = system_guide._ranked_sections("how does memory work?")[0]
    assert top.page_id == "07-knowledge-and-memory"


def test_natural_language_query_mcp_setup_ranks_mcp_content_first() -> None:
    top = system_guide._ranked_sections("how do I set up an MCP tool?")[0]
    assert top.page_id == "05-mcp-and-tools" or (
        top.page_id == "11-how-tos" and "mcp" in top.body.lower()
    )


def test_all_stopword_query_falls_back_to_toc() -> None:
    assert system_guide._ranked_sections("how do i do this?") == []
    result = search_guide("how do i do this?")
    assert "Guide topics:" in result


def test_clear_query_returns_relevant_section() -> None:
    result = search_guide("incognito chats")
    assert "Organizing and sharing" in result  # page title label
    top_block = result.split("\n\n---\n\n")[0]
    assert "incognito" in top_block.lower()


def test_section_awareness_returns_heading_scoped_section_not_whole_page() -> None:
    result = search_guide("microphone button disabled")
    assert "voice (microphone) button is disabled" in result
    # A different section of the same page (e.g. the FAQ's privacy question)
    # should not dominate the top of the result.
    top_block = result.split("\n\n---\n\n")[0]
    assert "microphone" in top_block.lower()


def test_toc_fallback_when_nothing_matches() -> None:
    result = search_guide("zzqxx19notarealword blorptastic nonexistentia")
    assert "Guide topics:" in result
    assert "00-quickstart" in result
    assert "15-api-reference" in result


def test_empty_query_returns_toc() -> None:
    result = search_guide("")
    assert "Guide topics:" in result


def test_guide_topics_lists_real_pages_in_order() -> None:
    topics = guide_topics()
    page_ids = [page_id for page_id, _title in topics]
    assert page_ids[0] == "00-quickstart"
    assert page_ids[-1] == "15-api-reference"
    assert len(topics) == 16
    titles = dict(topics)
    assert titles["00-quickstart"] == "Quickstart"
    assert titles["05-mcp-and-tools"] == "MCP and tools"


def test_result_is_capped_to_a_reasonable_size() -> None:
    result = search_guide("model")
    assert len(result) <= system_guide._MAX_RESULT_CHARS + 4000


# ─── guide_context_block — the sync, keyword-only PUSH/injection path ─────
#
# guide_context_block runs unconditionally against every turn's latest user
# message (when the caller's own is_app_directed_question gate already
# passed — this module doesn't apply that gate itself, see its docstring),
# so it must stay quiet on incidental word overlap. A scalar score can't
# gate cleanly on its own: "project" is a heavily-repeated guide heading, so
# an incidental mention can outscore a real question. So the gate uses two
# signals — a HIGH score (distinctive guide phrasing), OR a MODERATE score
# plus an explicit app-referential cue ("this app", "lm chat", ...) that
# marks it as a real app question.

_CLEAR_APP_QUESTION = "how do I add custom instructions to a project"
_INCIDENTAL_MENTION = "I'm refactoring my project's memory usage"


def test_guide_context_block_matches_clear_app_question() -> None:
    block = guide_context_block(_CLEAR_APP_QUESTION)
    assert block is not None
    assert block.startswith("[LM Chat guide — ")
    assert "Projects" in block
    assert "custom instructions" in block.lower()


def test_guide_context_block_none_for_incidental_keyword_mention() -> None:
    """A generic sentence that merely uses words the guide also uses
    ("project", "memory") — not a real question about the app — must not
    inject under the two-tier score+cue gate, which was built to handle
    exactly this case (see ``_should_inject_keyword``)."""
    assert guide_context_block(_INCIDENTAL_MENTION) is None


def test_high_score_injects_without_a_cue() -> None:
    """Distinctive guide phrasing (a high score) injects on its own — no
    app-referential cue required."""
    assert (
        system_guide._scored_sections_keyword(_CLEAR_APP_QUESTION)[0][0]
        >= system_guide._INJECT_HIGH_SCORE
    )
    assert "this app" not in _CLEAR_APP_QUESTION.lower()
    assert guide_context_block(_CLEAR_APP_QUESTION) is not None


def test_moderate_score_needs_app_referential_cue_to_inject() -> None:
    """A moderate-scoring message (below the high bar) injects ONLY with an
    explicit app cue — that cue is what separates a real app question from
    an incidental word overlap the score alone can't distinguish. A lexical
    score cannot: the incidental mention scores ABOVE the real question."""
    with_cue = "how do I set up a project in this app?"
    without_cue = "how do I set up a project"
    assert (
        system_guide._scored_sections_keyword(with_cue)[0][0]
        < system_guide._INJECT_HIGH_SCORE
    )
    assert guide_context_block(with_cue) is not None
    assert guide_context_block(without_cue) is None


def test_incidental_mention_stays_quiet_despite_high_word_overlap() -> None:
    """An incidental 'project' mention scores in the moderate band (even
    higher than a real question) but has no app cue, so it must NOT
    inject."""
    assert (
        system_guide._scored_sections_keyword(_INCIDENTAL_MENTION)[0][0]
        < system_guide._INJECT_HIGH_SCORE
    )
    assert not system_guide._has_app_referential_cue(_INCIDENTAL_MENTION)
    assert guide_context_block(_INCIDENTAL_MENTION) is None


def test_guide_context_block_empty_query_returns_none() -> None:
    assert guide_context_block("") is None
    assert guide_context_block("   ") is None


def test_guide_context_block_is_labeled_with_page_and_heading_and_has_body() -> None:
    block = guide_context_block(_CLEAR_APP_QUESTION)
    assert block is not None
    header, _sep, body = block.partition("\n")
    assert header.startswith("[LM Chat guide — ") and header.endswith("]")
    assert body.strip(), "block must carry real section body text, not just a label"


def test_guide_context_block_returns_only_one_section() -> None:
    # A section separator ("\n\n---\n\n") would only appear if more than one
    # section's text got concatenated — guide_context_block never does that
    # (unlike search_guide, which can return up to max_sections).
    block = guide_context_block(_CLEAR_APP_QUESTION)
    assert block is not None
    assert "\n\n---\n\n" not in block


def test_app_cue_without_topical_match_does_not_inject() -> None:
    """An app-referential cue alone doesn't trigger injection — it only
    lifts a MODERATE topical match over the bar. No real guide-topic
    overlap, no injection, cue or not."""
    assert system_guide._has_app_referential_cue("I really like this app")
    assert guide_context_block("I really like this app, it's great") is None


# ─── mtime cache against a synthetic guide dir ────────────────────────────


@pytest.fixture()
def tmp_guide_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    guide_dir = tmp_path / "guide"
    guide_dir.mkdir()
    monkeypatch.setattr(system_guide, "_resolve_guide_dir", lambda: guide_dir)
    try:
        yield guide_dir
    finally:
        # Force the module-level cache to drop its reference to this
        # tmp_path's sections once the fixture (and monkeypatch) tear down
        # — otherwise a later test could observe a stale snapshot whose
        # mtimes dict still matches (tmp_path reused a freed inode/mtime by
        # coincidence).
        system_guide._cache._mtimes = {}


def test_new_file_is_picked_up_without_restart(tmp_guide_dir: Path) -> None:
    (tmp_guide_dir / "00-alpha.md").write_text(
        "# Alpha Page\n\n## Zebra section\n\nzebra content lives here.\n",
        encoding="utf-8",
    )
    first = search_guide("zebra")
    assert "Zebra section" in first
    assert "unicorn" not in search_guide("unicorn")

    (tmp_guide_dir / "01-beta.md").write_text(
        "# Beta Page\n\n## Unicorn section\n\nunicorn content lives here.\n",
        encoding="utf-8",
    )
    second = search_guide("unicorn")
    assert "Unicorn section" in second

    topics = guide_topics()
    assert ("00-alpha", "Alpha Page") in topics
    assert ("01-beta", "Beta Page") in topics


def test_edited_file_content_reflected_after_mtime_change(tmp_guide_dir: Path) -> None:
    page = tmp_guide_dir / "00-alpha.md"
    page.write_text("# Alpha Page\n\n## Intro\n\noriginal wording only.\n", encoding="utf-8")
    search_guide("original")

    import os
    import time

    time.sleep(0.01)
    page.write_text("# Alpha Page\n\n## Intro\n\nrevised wording only.\n", encoding="utf-8")
    os.utime(page, None)

    result = search_guide("revised")
    assert "revised wording" in result


def test_no_guide_dir_returns_unavailable_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system_guide, "_resolve_guide_dir", lambda: None)
    assert "not available" in search_guide("anything").lower()
    assert guide_topics() == []


def test_resolve_guide_dir_finds_real_repo_guide() -> None:
    guide_dir = system_guide._resolve_guide_dir()
    assert guide_dir is not None
    assert guide_dir.is_dir()
    assert (guide_dir / "00-quickstart.md").exists()


# ─── Semantic engine — caller-injected async embedder ─────────────────────
#
# This module owns no embedder; ``ensure_section_embeddings`` /
# ``guide_context_block_semantic`` take async callables the CALLER binds to
# a real embedding model (``embedding.client.EmbeddingClient`` in
# production, via ``streaming_service._assemble_system_prompt``). Tests use
# a small, fully deterministic fake instead — no network, no real model,
# cosines are exactly what the test sets up.


class _FakeEmbedder:
    """Deterministic async embedder: returns a fixed vector per exact text
    match from a lookup table supplied at construction time. Records calls
    so tests can assert caching behavior (embed_batch called once, not once
    per lookup)."""

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self._vectors = vectors
        self.batch_calls: list[tuple[str, ...]] = []
        self.query_texts: list[str] = []

    async def embed_batch(self, *, texts: list[str], model_id: str) -> list[list[float]]:
        self.batch_calls.append(tuple(texts))
        return [self._vectors[t] for t in texts]

    async def embed_one(self, text: str) -> list[float]:
        self.query_texts.append(text)
        return self._vectors[text]


@pytest.fixture()
def two_section_guide(tmp_guide_dir: Path) -> Path:
    """A synthetic 2-section guide corpus (mirrors the mtime-cache
    fixture's zebra/unicorn pages) for the semantic-engine tests below."""
    (tmp_guide_dir / "00-alpha.md").write_text(
        "# Alpha Page\n\n## Zebra section\n\nzebra content lives here.\n",
        encoding="utf-8",
    )
    (tmp_guide_dir / "01-beta.md").write_text(
        "# Beta Page\n\n## Unicorn section\n\nunicorn content lives here.\n",
        encoding="utf-8",
    )
    return tmp_guide_dir


def _section_texts() -> list[str]:
    sections, _idf = system_guide._cache.snapshot()
    return [f"{s.page_title}. {s.heading}. {s.body}" for s in sections]


async def test_ensure_section_embeddings_builds_normalized_matrix(
    two_section_guide: Path,
) -> None:
    texts = _section_texts()
    vectors = {t: ([1.0, 0.0, 0.0] if "Zebra" in t else [0.0, 2.0, 0.0]) for t in texts}
    fake = _FakeEmbedder(vectors)

    result = await ensure_section_embeddings(embed_batch=fake.embed_batch, model_key="fake-v1")
    assert result is not None
    sections, matrix = result
    assert len(sections) == 2
    assert matrix.shape == (2, 3)
    # L2-normalized: every row has unit norm, even though the fake supplied
    # an un-normalized [0, 2, 0] for the Unicorn section.
    norms = np.linalg.norm(matrix, axis=1)
    assert norms == pytest.approx([1.0, 1.0])


async def test_ensure_section_embeddings_caches_across_calls(two_section_guide: Path) -> None:
    texts = _section_texts()
    vectors = {t: [1.0, 0.0, 0.0] for t in texts}
    fake = _FakeEmbedder(vectors)

    await ensure_section_embeddings(embed_batch=fake.embed_batch, model_key="fake-v1")
    await ensure_section_embeddings(embed_batch=fake.embed_batch, model_key="fake-v1")
    assert len(fake.batch_calls) == 1, "unchanged guide + same model key must not re-embed"


async def test_ensure_section_embeddings_rebuilds_on_model_key_change(
    two_section_guide: Path,
) -> None:
    texts = _section_texts()
    vectors = {t: [1.0, 0.0, 0.0] for t in texts}
    fake = _FakeEmbedder(vectors)

    await ensure_section_embeddings(embed_batch=fake.embed_batch, model_key="model-a")
    await ensure_section_embeddings(embed_batch=fake.embed_batch, model_key="model-b")
    assert len(fake.batch_calls) == 2, "a different embedding model must invalidate the cache"


async def test_ensure_section_embeddings_rebuilds_on_guide_edit(two_section_guide: Path) -> None:
    texts = _section_texts()
    vectors = {t: [1.0, 0.0, 0.0] for t in texts}
    fake = _FakeEmbedder(vectors)
    await ensure_section_embeddings(embed_batch=fake.embed_batch, model_key="fake-v1")

    import asyncio
    import os

    await asyncio.sleep(0.01)
    page = two_section_guide / "00-alpha.md"
    page.write_text(
        "# Alpha Page\n\n## Zebra section\n\nzebra content, revised.\n", encoding="utf-8"
    )
    os.utime(page, None)

    new_texts = _section_texts()
    vectors2 = {t: [1.0, 0.0, 0.0] for t in new_texts}
    fake._vectors = vectors2
    await ensure_section_embeddings(embed_batch=fake.embed_batch, model_key="fake-v1")
    assert len(fake.batch_calls) == 2, "an edited guide file must invalidate the cache"


async def test_ensure_section_embeddings_returns_none_when_guide_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(system_guide, "_resolve_guide_dir", lambda: None)
    fake = _FakeEmbedder({})
    result = await ensure_section_embeddings(embed_batch=fake.embed_batch, model_key="fake-v1")
    assert result is None
    assert fake.batch_calls == []


async def test_ensure_section_embeddings_returns_none_on_embed_failure(
    two_section_guide: Path,
) -> None:
    async def _failing_embed_batch(*, texts: list[str], model_id: str) -> list[list[float]]:
        raise RuntimeError("upstream embeddings endpoint down")

    result = await ensure_section_embeddings(
        embed_batch=_failing_embed_batch, model_key="fake-v1"
    )
    assert result is None


async def test_guide_context_block_semantic_picks_top_cosine_section(
    two_section_guide: Path,
) -> None:
    texts = _section_texts()
    vectors = {t: ([1.0, 0.0, 0.0] if "Zebra" in t else [0.0, 1.0, 0.0]) for t in texts}
    vectors["find the zebra"] = [1.0, 0.0, 0.0]  # exact match with the Zebra section
    fake = _FakeEmbedder(vectors)

    result = await ensure_section_embeddings(embed_batch=fake.embed_batch, model_key="fake-v1")
    block = await guide_context_block_semantic(
        "find the zebra", embed_one=fake.embed_one, section_texts_and_meta=result
    )
    assert block is not None
    assert block.startswith("[LM Chat guide — ")
    assert "Zebra section" in block
    assert "zebra content lives here" in block.lower()


async def test_guide_context_block_semantic_gates_on_cosine_floor(
    two_section_guide: Path,
) -> None:
    """A query orthogonal to BOTH sections (cosine 0 to each) must not
    clear ``_INJECT_COSINE_MIN_SEMANTIC`` — the floor gates unrelated
    queries even when an embedder is available and working."""
    texts = _section_texts()
    vectors = {t: ([1.0, 0.0, 0.0] if "Zebra" in t else [0.0, 1.0, 0.0]) for t in texts}
    vectors["totally unrelated question"] = [0.0, 0.0, 1.0]
    fake = _FakeEmbedder(vectors)

    result = await ensure_section_embeddings(embed_batch=fake.embed_batch, model_key="fake-v1")
    block = await guide_context_block_semantic(
        "totally unrelated question", embed_one=fake.embed_one, section_texts_and_meta=result
    )
    assert block is None


async def test_guide_context_block_semantic_none_when_embeddings_unavailable() -> None:
    async def _embed_one(text: str) -> list[float]:
        return [1.0, 0.0]

    block = await guide_context_block_semantic(
        "anything", embed_one=_embed_one, section_texts_and_meta=None
    )
    assert block is None


async def test_guide_context_block_semantic_empty_query_returns_none(
    two_section_guide: Path,
) -> None:
    texts = _section_texts()
    vectors = {t: [1.0, 0.0, 0.0] for t in texts}
    fake = _FakeEmbedder(vectors)
    result = await ensure_section_embeddings(embed_batch=fake.embed_batch, model_key="fake-v1")

    assert await guide_context_block_semantic(
        "", embed_one=fake.embed_one, section_texts_and_meta=result
    ) is None
    assert await guide_context_block_semantic(
        "   ", embed_one=fake.embed_one, section_texts_and_meta=result
    ) is None


async def test_guide_context_block_semantic_none_on_embed_query_failure(
    two_section_guide: Path,
) -> None:
    texts = _section_texts()
    vectors = {t: [1.0, 0.0, 0.0] for t in texts}
    fake = _FakeEmbedder(vectors)
    result = await ensure_section_embeddings(embed_batch=fake.embed_batch, model_key="fake-v1")

    async def _failing_embed_one(text: str) -> list[float]:
        raise RuntimeError("upstream embeddings endpoint down")

    block = await guide_context_block_semantic(
        "find the zebra", embed_one=_failing_embed_one, section_texts_and_meta=result
    )
    assert block is None


# ─── Background corpus-embed split (get_cached_section_embeddings /
# ensure_section_embeddings_background) ────────────────────────────────────
#
# The one-time corpus embed must NEVER run inline on a per-turn streaming
# path (see streaming_service.py's ``_GUIDE_SEMANTIC_TIMEOUT_SEC`` comment):
# the real corpus (~265 sections) takes ~34 sequential chunked batch calls,
# which always blew past any turn-scoped timeout and got cancelled every
# single turn, so the matrix never finished caching -- permanently starving
# the semantic engine in production. These tests cover the fix: a
# NON-BLOCKING read (``get_cached_section_embeddings``) the per-turn path
# uses, and an idempotent, fire-and-forget kickoff
# (``ensure_section_embeddings_background``) of the actual (slow) embed as
# a detached ``asyncio.create_task``. Nothing here uses real network or
# real delay -- the fake embedder resolves instantly -- so awaiting the
# task directly (via ``system_guide._cache._embed_bg_task``) lets each test
# observe the background work complete deterministically, with no sleeps.


@pytest.fixture(autouse=True)
def _reset_guide_background_task_state() -> Iterator[None]:
    """``_GuideCache`` tracks at most one in-flight background corpus-embed
    task at module level (``_embed_bg_task`` / ``_embed_bg_model_key`` —
    see ``ensure_section_embeddings_background``). Cancel and clear it
    before and after every test in this file so a task started (and not
    awaited to completion) by one test can never bleed into a later one
    that inspects this same module-level state."""

    def _reset() -> None:
        task = system_guide._cache._embed_bg_task
        if task is not None and not task.done():
            task.cancel()
        system_guide._cache._embed_bg_task = None
        system_guide._cache._embed_bg_model_key = None

    _reset()
    yield
    _reset()


async def test_get_cached_section_embeddings_returns_none_before_any_embed(
    two_section_guide: Path,
) -> None:
    assert system_guide.get_cached_section_embeddings("fake-v1") is None


async def test_ensure_section_embeddings_background_never_calls_embed_batch_synchronously(
    two_section_guide: Path,
) -> None:
    """The kickoff call itself must return immediately without ever
    invoking the embedder -- it schedules a task and returns; it must have
    no ``await`` of its own that could run the embed inline."""
    texts = _section_texts()
    vectors = {t: [1.0, 0.0, 0.0] for t in texts}
    fake = _FakeEmbedder(vectors)

    system_guide.ensure_section_embeddings_background(
        embed_batch=fake.embed_batch, model_key="fake-v1"
    )
    assert fake.batch_calls == [], (
        "embed_batch must not run synchronously inside the kickoff call -- "
        "only once the event loop actually schedules the background task"
    )


async def test_get_cached_section_embeddings_returns_matrix_after_background_embed_completes(
    two_section_guide: Path,
) -> None:
    texts = _section_texts()
    vectors = {t: [1.0, 0.0, 0.0] for t in texts}
    fake = _FakeEmbedder(vectors)

    system_guide.ensure_section_embeddings_background(
        embed_batch=fake.embed_batch, model_key="fake-v1"
    )
    task = system_guide._cache._embed_bg_task
    assert task is not None, "a cold cache must kick off a background task"
    await task

    result = system_guide.get_cached_section_embeddings("fake-v1")
    assert result is not None
    sections, matrix = result
    assert len(sections) == 2
    assert matrix.shape == (2, 3)
    assert fake.batch_calls, "the background task must actually call embed_batch"


async def test_ensure_section_embeddings_background_is_idempotent_single_task(
    two_section_guide: Path,
) -> None:
    """Calling the kickoff twice back-to-back (no await between, so no
    event-loop turnover) for the SAME model_key must not start a second
    concurrent embed task."""
    texts = _section_texts()
    vectors = {t: [1.0, 0.0, 0.0] for t in texts}
    fake = _FakeEmbedder(vectors)

    system_guide.ensure_section_embeddings_background(
        embed_batch=fake.embed_batch, model_key="fake-v1"
    )
    first_task = system_guide._cache._embed_bg_task
    system_guide.ensure_section_embeddings_background(
        embed_batch=fake.embed_batch, model_key="fake-v1"
    )
    second_task = system_guide._cache._embed_bg_task
    assert first_task is second_task, "a second in-flight call must be a no-op"

    assert first_task is not None
    await first_task
    assert len(fake.batch_calls) == 1, "the corpus must only be embedded once"


async def test_ensure_section_embeddings_background_noop_once_cache_is_warm(
    two_section_guide: Path,
) -> None:
    """Once a background embed has completed and cached the matrix, a
    LATER kickoff call for the SAME model_key must be a pure no-op: no new
    task, no re-embed."""
    texts = _section_texts()
    vectors = {t: [1.0, 0.0, 0.0] for t in texts}
    fake = _FakeEmbedder(vectors)

    system_guide.ensure_section_embeddings_background(
        embed_batch=fake.embed_batch, model_key="fake-v1"
    )
    task = system_guide._cache._embed_bg_task
    assert task is not None
    await task
    assert len(fake.batch_calls) == 1
    assert system_guide._cache._embed_bg_task is None, (
        "the done-callback must clear the handle once the task completes"
    )

    system_guide.ensure_section_embeddings_background(
        embed_batch=fake.embed_batch, model_key="fake-v1"
    )
    assert system_guide._cache._embed_bg_task is None, (
        "a warm cache must not start a new background task at all"
    )
    assert len(fake.batch_calls) == 1, "a warm cache must not re-embed"


async def test_ensure_section_embeddings_background_never_raises_on_embed_failure(
    two_section_guide: Path,
) -> None:
    async def _failing_embed_batch(*, texts: list[str], model_id: str) -> list[list[float]]:
        raise RuntimeError("upstream embeddings endpoint down")

    system_guide.ensure_section_embeddings_background(
        embed_batch=_failing_embed_batch, model_key="fake-v1"
    )
    task = system_guide._cache._embed_bg_task
    assert task is not None
    await task  # must not raise -- the failure is caught and logged internally

    assert system_guide.get_cached_section_embeddings("fake-v1") is None
    assert system_guide._cache._embed_bg_task is None, (
        "the done-callback must clear the handle even on failure, so a "
        "later call can retry"
    )


async def test_ensure_section_embeddings_background_retries_after_a_prior_failure(
    two_section_guide: Path,
) -> None:
    """A failed background embed must not be memoized as permanently
    unavailable -- a later kickoff call (e.g. the next turn) with a
    WORKING embedder must actually retry and succeed."""

    async def _failing_embed_batch(*, texts: list[str], model_id: str) -> list[list[float]]:
        raise RuntimeError("upstream embeddings endpoint down")

    system_guide.ensure_section_embeddings_background(
        embed_batch=_failing_embed_batch, model_key="fake-v1"
    )
    failed_task = system_guide._cache._embed_bg_task
    assert failed_task is not None
    await failed_task
    assert system_guide.get_cached_section_embeddings("fake-v1") is None

    texts = _section_texts()
    vectors = {t: [1.0, 0.0, 0.0] for t in texts}
    fake = _FakeEmbedder(vectors)
    system_guide.ensure_section_embeddings_background(
        embed_batch=fake.embed_batch, model_key="fake-v1"
    )
    retry_task = system_guide._cache._embed_bg_task
    assert retry_task is not None
    await retry_task
    assert system_guide.get_cached_section_embeddings("fake-v1") is not None
