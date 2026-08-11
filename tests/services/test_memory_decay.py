# SPDX-License-Identifier: Apache-2.0
"""Tests for memory insight scoring.

Covers:
- ``_score_insight`` math: per-category τ, use-count boost monotonicity,
  cold-start Laplace, floor clamp.
- ``recall_insights``: scoring order + pinned bypass + last_used/use_count
  side effects.
- ``_fade_pass``: rows whose pre-clamp compound < SCORE_FLOOR transition to
  ``state='faded'``.
"""
from __future__ import annotations

import math
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import memory_insights, metadata
from lmchat.embedding.client import EmbeddingClient
from lmchat.services.memory_service import (
    AUTO_INSIGHT_CATEGORY,
    CATEGORY_HALF_LIVES,
    CATEGORY_WEIGHTS,
    LAPLACE_ALPHA,
    LAPLACE_BETA,
    SCORE_FLOOR,
    MemoryService,
    _score_insight,
)
from lmchat.services.models_service import Capabilities, ModelInfo, ModelsService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    """Fresh per-test SQLite engine with the full schema."""
    db_path = tmp_path / "test_memory_decay.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}", pool_pre_ping=True)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


async def _insert_user(engine: AsyncEngine, user_id: int) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO users (id, username, password_hash)"
                " VALUES (:id, :u, :ph)"
            ),
            {"id": user_id, "u": f"user{user_id}", "ph": "scrypt$dummy"},
        )


async def _insert_insight(
    engine: AsyncEngine,
    insight_id: int,
    user_id: int,
    *,
    text_val: str = "fact",
    text_hash: str | None = None,
    category: str = "preference",
    use_count: int = 0,
    ups: float = 0.0,
    downs: float = 0.0,
    last_used: float | None = None,
    last_feedback_at: float | None = None,
    pinned: bool = False,
    state: str = "active",
) -> None:
    th = text_hash if text_hash is not None else (f"{insight_id:064x}")[:64]
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO memory_insights (id, user_id, text, text_hash, pinned,"
                " category, use_count, ups, downs, last_used, last_feedback_at, state)"
                " VALUES (:id, :uid, :t, :th, :p, :cat, :uc, :ups, :dn, :lu, :lf, :st)"
            ),
            {
                "id": insight_id,
                "uid": user_id,
                "t": text_val,
                "th": th,
                "p": pinned,
                "cat": category,
                "uc": use_count,
                "ups": ups,
                "dn": downs,
                "lu": last_used,
                "lf": last_feedback_at,
                "st": state,
            },
        )


def _make_service(engine: AsyncEngine) -> MemoryService:
    """MemoryService wired with mock LM Studio deps (we don't need them for insight tests)."""
    mock_embedding_client = AsyncMock(spec=EmbeddingClient)
    mock_models_service = AsyncMock(spec=ModelsService)
    mock_models_service.list_loaded.return_value = [
        ModelInfo(
            key="embed-model-v1",
            type="embedding",
            capabilities=Capabilities(vision=False, trained_for_tool_use=False),
        )
    ]
    return MemoryService(
        engine=engine,
        embedding_client=mock_embedding_client,
        models_service=mock_models_service,
    )


# ---------------------------------------------------------------------------
# _score_insight unit tests
# ---------------------------------------------------------------------------


def _row(**fields: object) -> SimpleNamespace:
    """Build a row-like object _score_insight accepts."""
    defaults = {
        "category": "preference",
        "use_count": 0,
        "ups": 0.0,
        "downs": 0.0,
        "last_used": None,
    }
    defaults.update(fields)
    return SimpleNamespace(**defaults)


def test_score_per_category_tau_differentiates_scores() -> None:
    """Same age+use_count, different category → different decay → different score."""
    now = 1_700_000_000.0
    # 30 days old; different τ per category → distinct decay.
    last_used = now - 30 * 86_400.0

    identity_score = _score_insight(_row(category="identity", last_used=last_used), now)
    context_score = _score_insight(_row(category="context", last_used=last_used), now)

    # τ_identity=180 vs τ_context=7 → identity decays much slower → higher score.
    assert identity_score > context_score


def test_score_use_count_boost_monotonic() -> None:
    """Increasing use_count strictly increases boost (and thus score)."""
    now = 1_700_000_000.0
    last_used = now  # zero age → decay = 1.0

    s0 = _score_insight(_row(category="preference", use_count=0, last_used=last_used), now)
    s5 = _score_insight(_row(category="preference", use_count=5, last_used=last_used), now)
    s50 = _score_insight(_row(category="preference", use_count=50, last_used=last_used), now)

    assert s0 < s5 < s50


def test_score_floor_clamp_for_heavily_downvoted_old_row() -> None:
    """A row with downs >> ups + ancient last_used still scores ≥ SCORE_FLOOR."""
    now = 1_700_000_000.0
    # Pick parameters that drive the unclamped product to ~0.
    last_used = now - 365 * 86_400.0  # 1 year old, context category τ=7d
    s = _score_insight(
        _row(category="context", ups=0.0, downs=50.0, last_used=last_used),
        now,
    )
    assert s >= SCORE_FLOOR
    assert s == pytest.approx(SCORE_FLOOR, abs=1e-9)


def test_score_cold_start_bayesian_equals_half() -> None:
    """ups=0, downs=0 → Laplace = (0+α)/(0+β) with α=1,β=2 → 0.5.

    v0.5.x convention: β is the full pseudocount denominator (NOT a
    beta-distribution parameter). Cold-start yields α/β = 0.5, matching
    the v0.5.x implementation. The textbook beta-binomial
    form (ups+α)/(ups+downs+α+β) would give 1/3 — not the intended
    'Laplace α=1, β=2' convention.
    """
    expected_bayesian = LAPLACE_ALPHA / LAPLACE_BETA
    assert expected_bayesian == pytest.approx(0.5, abs=1e-9)
    now = 1_700_000_000.0
    # Force decay = 1.0 + boost = 1.0 by zero-age, zero-use:
    s = _score_insight(_row(category="preference", last_used=now), now)
    # bayesian * cat_w["preference"] * 1.0 * 1.0
    expected = expected_bayesian * CATEGORY_WEIGHTS["preference"]
    assert s == pytest.approx(expected, abs=1e-9)


def test_score_decay_formula_matches_adr() -> None:
    """Exact scoring-formula reproduction for one carefully-chosen row."""
    now = 1_700_000_000.0
    last_used = now - 30 * 86_400.0  # 30 days ago
    row = _row(
        category="preference",
        use_count=3,
        ups=2.0,
        downs=1.0,
        last_used=last_used,
    )
    decay = 1.0 / (1.0 + 30.0 / CATEGORY_HALF_LIVES["preference"])
    boost = 1.0 + 0.3 * math.log(1 + 3)
    # v0.5.x Laplace convention: (ups+α)/(ups+downs+β); see test above for rationale.
    bayes = (2.0 + LAPLACE_ALPHA) / (2.0 + 1.0 + LAPLACE_BETA)
    expected = boost * decay * CATEGORY_WEIGHTS["preference"] * bayes
    expected = max(expected, SCORE_FLOOR)

    assert _score_insight(row, now) == pytest.approx(expected, abs=1e-9)


def test_score_unknown_category_falls_back_to_context_tau() -> None:
    """An unknown category uses CATEGORY_HALF_LIVES['context'] for τ."""
    now = 1_700_000_000.0
    last_used = now - 10 * 86_400.0  # 10 days
    score_unknown = _score_insight(
        _row(category="nonexistent", last_used=last_used),
        now,
    )
    score_context = _score_insight(
        _row(category="context", last_used=last_used),
        now,
    )
    # Same τ; cat_w defaults to 1.0 for both (context=1.0 too) → equal.
    assert score_unknown == pytest.approx(score_context, abs=1e-9)


def test_score_profile_category_matches_preference_durability() -> None:
    """AUTO_INSIGHT_CATEGORY ("profile") must carry its OWN half-life and
    weight, equal to "preference" — NOT silently fall back to the
    unknown-category ``.get(..., CATEGORY_HALF_LIVES["context"])`` default
    (7-day half-life, 1.0x weight), which would fade every auto-distilled
    fact within a week even though it is just as durable as an
    admin-entered preference.
    """
    assert AUTO_INSIGHT_CATEGORY == "profile"
    assert "profile" in CATEGORY_HALF_LIVES
    assert "profile" in CATEGORY_WEIGHTS
    assert CATEGORY_HALF_LIVES["profile"] == CATEGORY_HALF_LIVES["preference"]
    assert CATEGORY_WEIGHTS["profile"] == CATEGORY_WEIGHTS["preference"]

    now = 1_700_000_000.0
    last_used = now - 30 * 86_400.0  # 30 days ago
    profile_score = _score_insight(
        _row(category="profile", last_used=last_used), now
    )
    context_score = _score_insight(
        _row(category="context", last_used=last_used), now
    )
    # profile's longer half-life (90d vs 7d) + higher weight (2.0 vs 1.0)
    # must outscore the volatile "context" fallback for an identical row.
    assert profile_score > context_score


# ---------------------------------------------------------------------------
# recall_insights integration
# ---------------------------------------------------------------------------


async def test_recall_insights_orders_by_compound_score(engine: AsyncEngine) -> None:
    """recall_insights returns rows sorted by compound score descending."""
    await _insert_user(engine, 1)
    now = 1_700_000_000.0

    # High-tier identity insight (τ=180, weight=2.0).
    await _insert_insight(
        engine, 1, 1, category="identity", last_used=now, use_count=10
    )
    # Low-tier context insight (τ=7, weight=1.0).
    await _insert_insight(
        engine, 2, 1, category="context", last_used=now, use_count=0
    )

    svc = _make_service(engine)
    results = await svc.recall_insights(user_id=1, top_k=2, now=now)

    assert len(results) == 2
    assert results[0].id == 1, "identity insight should outrank context"
    assert results[0].score > results[1].score


async def test_recall_insights_pinned_first(engine: AsyncEngine) -> None:
    """Pinned rows bypass scoring and appear before scored rows."""
    await _insert_user(engine, 1)
    now = 1_700_000_000.0

    # Pinned context insight (would otherwise score low).
    await _insert_insight(
        engine, 1, 1, category="context", last_used=now - 30 * 86_400, pinned=True
    )
    # High-score identity insight.
    await _insert_insight(
        engine, 2, 1, category="identity", last_used=now, use_count=10, pinned=False
    )

    svc = _make_service(engine)
    results = await svc.recall_insights(user_id=1, top_k=2, now=now)

    assert len(results) == 2
    assert results[0].id == 1, "pinned should come first"
    assert results[0].pinned is True
    assert results[1].pinned is False


async def test_recall_insights_touches_last_used_and_use_count(engine: AsyncEngine) -> None:
    """After recall, returned rows have updated last_used + incremented use_count."""
    await _insert_user(engine, 1)
    now = 1_700_000_000.0
    old_ts = now - 86_400.0  # 1 day ago
    await _insert_insight(
        engine, 1, 1, category="preference", last_used=old_ts, use_count=5
    )

    svc = _make_service(engine)
    await svc.recall_insights(user_id=1, top_k=1, now=now)

    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(memory_insights).where(memory_insights.c.id == 1)
            )
        ).fetchone()
    assert row is not None
    assert row.last_used == pytest.approx(now)
    assert row.use_count == 6


async def test_recall_insights_touches_last_active_epoch(engine: AsyncEngine) -> None:
    """After recall, a returned row's last_active_epoch is bumped to `now`
    alongside last_used (migration 0043 — the two are kept in sync so the
    SQL-level recency ORDER BY in `_recency_order_expr` stays correct
    after a recall touch, not just at insert time).
    """
    await _insert_user(engine, 1)
    now = 1_700_000_000.0
    old_ts = now - 86_400.0  # 1 day ago
    await _insert_insight(
        engine, 1, 1, category="preference", last_used=old_ts, use_count=5
    )

    svc = _make_service(engine)
    await svc.recall_insights(user_id=1, top_k=1, now=now)

    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(memory_insights).where(memory_insights.c.id == 1)
            )
        ).fetchone()
    assert row is not None
    assert row.last_active_epoch == pytest.approx(now)
    assert row.last_active_epoch == pytest.approx(row.last_used)


async def test_recall_insights_excludes_faded_rows(engine: AsyncEngine) -> None:
    """Rows with state='faded' are excluded from active recall."""
    await _insert_user(engine, 1)
    now = 1_700_000_000.0

    await _insert_insight(engine, 1, 1, category="preference", last_used=now, state="faded")
    await _insert_insight(engine, 2, 1, category="preference", last_used=now, state="active")

    svc = _make_service(engine)
    results = await svc.recall_insights(user_id=1, top_k=10, now=now)

    assert len(results) == 1
    assert results[0].id == 2


async def test_recall_insights_candidate_pool_includes_never_recalled_row(
    engine: AsyncEngine,
) -> None:
    """A never-recalled row must still be able to enter the scoring
    candidate pool, even when the pool is small and an older recalled row
    is competing for the same slot.

    Regression for the bug where the active-row scan ordered by bare
    ``last_used DESC NULLS LAST``: every never-recalled row (last_used IS
    NULL) sorted after every recalled row regardless of actual recency, so
    once a user had ``candidate_pool``-many recalled rows, freshly-saved
    facts could never enter scoring at all.
    """
    await _insert_user(engine, 1)
    now = time.time()

    # An OLD, previously-recalled row (touched 60 days ago).
    await _insert_insight(
        engine, 1, 1, category="context", last_used=now - 60 * 86_400, use_count=0
    )
    # A never-recalled row, created just now (last_used still NULL) — the
    # freshest row in the table by any real measure.
    await _insert_insight(
        engine, 2, 1, category="identity", last_used=None, use_count=0
    )

    svc = _make_service(engine)
    # Pool size 1: only the single most-recently-active row can be scored.
    results = await svc.recall_insights(
        user_id=1, top_k=1, candidate_pool=1, now=now
    )

    assert len(results) == 1
    assert results[0].id == 2, "the never-recalled (freshest) row must win the pool slot"


async def test_recall_insights_top_k_zero_returns_empty(engine: AsyncEngine) -> None:
    """top_k <= 0 returns an empty list with no side effects."""
    await _insert_user(engine, 1)
    now = 1_700_000_000.0
    await _insert_insight(engine, 1, 1, category="preference", last_used=now)

    svc = _make_service(engine)
    results = await svc.recall_insights(user_id=1, top_k=0, now=now)
    assert results == []


# ---------------------------------------------------------------------------
# _fade_pass
# ---------------------------------------------------------------------------


async def test_fade_pass_transitions_low_score_rows(engine: AsyncEngine) -> None:
    """A row whose pre-clamp compound is < SCORE_FLOOR is moved to state='faded'."""
    await _insert_user(engine, 1)
    now = 1_700_000_000.0

    # context category τ=7d; 365 days old; downs=50 → tiny compound.
    await _insert_insight(
        engine,
        1,
        1,
        category="context",
        last_used=now - 365 * 86_400,
        downs=50.0,
        ups=0.0,
    )
    # Recent identity row — must remain active.
    await _insert_insight(
        engine, 2, 1, category="identity", last_used=now, use_count=3
    )

    svc = _make_service(engine)
    faded = await svc._fade_pass(now=now)
    assert faded >= 1

    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(memory_insights.c.id, memory_insights.c.state).order_by(
                    memory_insights.c.id
                )
            )
        ).fetchall()
    state_by_id = {int(r.id): r.state for r in rows}
    assert state_by_id[1] == "faded"
    assert state_by_id[2] == "active"


async def test_fade_pass_idempotent_no_low_score_rows(engine: AsyncEngine) -> None:
    """If no rows are below floor, _fade_pass returns 0 and changes nothing."""
    await _insert_user(engine, 1)
    now = 1_700_000_000.0
    await _insert_insight(engine, 1, 1, category="identity", last_used=now, use_count=2)

    svc = _make_service(engine)
    faded = await svc._fade_pass(now=now)
    assert faded == 0
