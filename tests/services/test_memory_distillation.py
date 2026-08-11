# SPDX-License-Identifier: Apache-2.0
"""Tests for auto-memory distillation (automatically-saved long-term memory).

Covers the FINISHED feature that turns completed assistant turns into durable
AUTO insights on the /memory page:

- save_auto_insight / distill_and_store persist NEW durable facts as AUTO
  rows (pinned=False, state='active', category='profile') and bump the
  previously-dead MEMORY_DISTILLATIONS metric.
- Exact-hash + near-duplicate dedup: a repeat (verbatim or paraphrase) does
  NOT create a second row.
- An ephemeral / empty extraction (facts=[]) stores nothing.
- The streaming wrapper _safe_distill_memory skips incognito chats entirely.
- A stored AUTO memory is RECALLED on a later turn via recall_insights
  (insight_hits path), proving the embed-free recall contract.
- The auto cap fades the oldest least-recently-used rows past the limit.
- The OOB extractor's defensive JSON parse handles [] and prose-wrapped output.

The core extraction path (save_auto_insight) is asserted both green and
red-on-revert: deleting the insert in save_auto_insight makes
test_save_auto_insight_persists_durable_fact fail.
"""
from __future__ import annotations

import os
import time
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import memory_insights, metadata
from lmchat.embedding.client import EmbeddingClient
from lmchat.services.memory_service import (
    AUTO_INSIGHT_CATEGORY,
    MemoryService,
    _is_near_duplicate,
    _normalize,
    _text_hash,
)
from lmchat.services.models_service import Capabilities, ModelInfo, ModelsService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    """Fresh per-test SQLite engine with the full schema."""
    db_path = tmp_path / "test_memory_distillation.db"
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


async def _insert_chat(
    engine: AsyncEngine,
    *,
    chat_id: int,
    user_id: int,
    incognito: bool = False,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO chats (id, user_id, title, incognito)"
                " VALUES (:id, :uid, :t, :inc)"
            ),
            {
                "id": chat_id,
                "uid": user_id,
                "t": "Test chat",
                "inc": 1 if incognito else 0,
            },
        )


def _make_service(engine: AsyncEngine) -> MemoryService:
    """MemoryService with mock LM deps (distillation recall needs no embedder)."""
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


async def _count_insights(engine: AsyncEngine, user_id: int) -> int:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(memory_insights).where(
                    memory_insights.c.user_id == user_id
                )
            )
        ).fetchall()
    return len(rows)


# ---------------------------------------------------------------------------
# (a) A durable fact → an AUTO memory_insight row (pinned=False).
# ---------------------------------------------------------------------------


async def test_save_auto_insight_persists_durable_fact(engine: AsyncEngine) -> None:
    """A clear durable fact lands as an AUTO row: pinned=False, category=profile."""
    await _insert_user(engine, 1)
    svc = _make_service(engine)

    insight = await svc.save_auto_insight(user_id=1, text="Name is Kevin")

    assert insight is not None
    assert insight.text == "Name is Kevin"
    assert insight.pinned is False  # AUTO, not a manual pin.

    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(memory_insights).where(memory_insights.c.id == insight.id)
            )
        ).fetchone()
    assert row is not None
    assert bool(row.pinned) is False
    assert row.state == "active"
    assert row.category == AUTO_INSIGHT_CATEGORY
    # text_hash matches the blake2b of the normalized text (recall dedup key).
    assert row.text_hash == _text_hash(_normalize("Name is Kevin"))


async def test_save_auto_insight_sets_last_active_epoch(engine: AsyncEngine) -> None:
    """save_auto_insight stamps last_active_epoch (migration 0043) on insert.

    The recall/eviction candidate-pool ordering now runs as a plain SQL
    ``ORDER BY`` against ``last_active_epoch`` instead of a Python scan
    (see ``_recency_order_expr``); that only works if every row written
    through ``save_auto_insight`` carries a non-NULL value from the
    start — a never-recalled row's recency is its own insert time.
    """
    await _insert_user(engine, 1)
    svc = _make_service(engine)

    before = time.time()
    insight = await svc.save_auto_insight(user_id=1, text="Lives in Austin")
    after = time.time()
    assert insight is not None

    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(memory_insights).where(memory_insights.c.id == insight.id)
            )
        ).fetchone()
    assert row is not None
    assert row.last_active_epoch is not None
    # Stamped at insert time — bounded by the call's wall-clock window.
    assert before <= row.last_active_epoch <= after
    # Never recalled yet — last_used stays NULL, unlike last_active_epoch.
    assert row.last_used is None


async def test_distill_and_store_persists_multiple_facts(engine: AsyncEngine) -> None:
    """distill_and_store inserts each distinct durable fact as an AUTO row."""
    await _insert_user(engine, 1)
    svc = _make_service(engine)

    stored = await svc.distill_and_store(
        user_id=1,
        facts=[
            "Name is Kevin",
            "Into astrophysics and dark energy",
            "Prefers concise answers",
        ],
    )

    assert len(stored) == 3
    assert all(i.pinned is False for i in stored)
    assert await _count_insights(engine, 1) == 3


# ---------------------------------------------------------------------------
# (b) A repeat of the same fact → NO duplicate.
# ---------------------------------------------------------------------------


async def test_repeat_fact_exact_hash_no_duplicate(engine: AsyncEngine) -> None:
    """Saving the same fact verbatim twice yields exactly one row."""
    await _insert_user(engine, 1)
    svc = _make_service(engine)

    first = await svc.save_auto_insight(user_id=1, text="Into astrophysics")
    second = await svc.save_auto_insight(user_id=1, text="into  ASTROPHYSICS")  # normalizes equal

    assert first is not None
    assert second is None  # exact-hash dedup short-circuit.
    assert await _count_insights(engine, 1) == 1


async def test_paraphrase_fact_near_dup_no_duplicate(engine: AsyncEngine) -> None:
    """A paraphrase of an existing fact is dropped by the near-dup check."""
    await _insert_user(engine, 1)
    svc = _make_service(engine)

    stored = await svc.distill_and_store(
        user_id=1,
        facts=["Likes astrophysics", "Likes astrophysics a lot"],
    )
    # Second is a near-duplicate (high token overlap) → only one row stored.
    assert len(stored) == 1
    assert await _count_insights(engine, 1) == 1


async def test_distill_does_not_shadow_existing_pin(engine: AsyncEngine) -> None:
    """A distilled fact identical to an existing PIN is not re-stored as AUTO."""
    await _insert_user(engine, 1)
    svc = _make_service(engine)

    pinned = await svc.pin_insight(user_id=1, text="Name is Kevin")
    assert pinned.pinned is True

    stored = await svc.distill_and_store(user_id=1, facts=["Name is Kevin"])
    assert stored == []  # exact-hash collision with the pin → skip.
    assert await _count_insights(engine, 1) == 1  # only the pin.


# ---------------------------------------------------------------------------
# (c) Ephemeral / empty extraction → 0 insights.
# ---------------------------------------------------------------------------


async def test_empty_facts_stores_nothing(engine: AsyncEngine) -> None:
    """distill_and_store([]) — the common 'nothing worth saving' case — writes 0 rows."""
    await _insert_user(engine, 1)
    svc = _make_service(engine)

    stored = await svc.distill_and_store(user_id=1, facts=[])

    assert stored == []
    assert await _count_insights(engine, 1) == 0


async def test_blank_fact_stores_nothing(engine: AsyncEngine) -> None:
    """A whitespace-only candidate normalizes empty and is skipped."""
    await _insert_user(engine, 1)
    svc = _make_service(engine)

    insight = await svc.save_auto_insight(user_id=1, text="   ")

    assert insight is None
    assert await _count_insights(engine, 1) == 0


# ---------------------------------------------------------------------------
# (d) Incognito chat → 0 insights (streaming wrapper guard).
# ---------------------------------------------------------------------------


async def test_safe_distill_memory_skips_incognito(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_safe_distill_memory never extracts or stores for an incognito chat."""
    import lmchat.services.streaming_service as ss

    await _insert_user(engine, 1)
    await _insert_chat(engine, chat_id=7, user_id=1, incognito=True)
    svc = _make_service(engine)

    # Guard: the OOB extractor must NOT even be called for an incognito chat.
    extract_calls = 0

    async def _fake_extract(**_kwargs: object) -> list[str]:
        nonlocal extract_calls
        extract_calls += 1
        return ["Name is Kevin"]

    monkeypatch.setattr(ss, "_distill_memory_oob", _fake_extract)

    streamer = ss.StreamingService(
        engine=engine,
        lm_client=AsyncMock(),
        memory_service=svc,
        chat_locks={},
    )

    await streamer._safe_distill_memory(
        user_id=1,
        chat_id=7,
        model_id="some-model",
        user_text="My name is Kevin",
        assistant_answer="Nice to meet you, Kevin.",
        project_id=None,
    )

    assert extract_calls == 0  # incognito short-circuits before extraction.
    assert await _count_insights(engine, 1) == 0


async def test_safe_distill_memory_stores_for_normal_chat(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_safe_distill_memory persists facts for a NORMAL (non-incognito) chat."""
    import lmchat.services.streaming_service as ss

    await _insert_user(engine, 1)
    await _insert_chat(engine, chat_id=8, user_id=1, incognito=False)
    svc = _make_service(engine)

    async def _fake_extract(**_kwargs: object) -> list[str]:
        return ["Name is Kevin"]

    monkeypatch.setattr(ss, "_distill_memory_oob", _fake_extract)

    streamer = ss.StreamingService(
        engine=engine,
        lm_client=AsyncMock(),
        memory_service=svc,
        chat_locks={},
    )

    await streamer._safe_distill_memory(
        user_id=1,
        chat_id=8,
        model_id="some-model",
        user_text="My name is Kevin",
        assistant_answer="Nice to meet you, Kevin.",
        project_id=None,
    )

    rows = await svc.list_auto(user_id=1)
    assert len(rows) == 1
    assert rows[0].text == "Name is Kevin"
    assert rows[0].pinned is False


async def test_safe_distill_memory_uses_background_model_wire_id(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED-ON-REVERT: distillation must call the OOB extractor with the
    WIRE-ID of the admin-pinned background model, not the bare catalog key.

    LM Studio routes by loaded-instance wire-id only; the bare catalog key
    returns 400 Bad Request (the bug this test proves is fixed).

    Failure mode: if the fix is reverted, ``captured["model"]`` would be
    ``"bg-small-3b"`` (catalog key) instead of ``"bg-small-3b@q4"`` (wire-id).
    """
    import lmchat.services.streaming_service as ss
    from lmchat.services.lm_studio_overrides_service import (
        LmStudioOverridesService,
    )
    from lmchat.services.models_service import ResolvedModel

    await _insert_user(engine, 1)
    await _insert_chat(engine, chat_id=9, user_id=1, incognito=False)
    svc = _make_service(engine)

    # Pin a background model in the admin row.
    from lmchat.config import Settings  # noqa: PLC0415

    overrides = LmStudioOverridesService(
        engine=engine,
        settings=Settings(lm_chat_secret="test-secret-32-bytes-of-entropy!!"),  # type: ignore[call-arg]
    )
    await overrides.set_preferred_background_model("bg-small-3b")

    # models_service: list_loaded used by resolve_background_model_id;
    # resolve_to_loaded_or_fallback used by the wire-id resolution fix.
    mock_models = AsyncMock(spec=ModelsService)
    mock_models.list_loaded.return_value = [
        ModelInfo(
            key="bg-small-3b",
            type="llm",
            capabilities=Capabilities(vision=False, trained_for_tool_use=True),
            loaded_instance_ids=["bg-small-3b@q4"],
        )
    ]
    # Wire-id resolution: catalog key "bg-small-3b" → wire-id "bg-small-3b@q4".
    mock_models.resolve_to_loaded_or_fallback.return_value = ResolvedModel(
        wire_id="bg-small-3b@q4",
        requested="bg-small-3b",
    )

    captured: dict[str, object] = {}

    async def _fake_extract(**kwargs: object) -> list[str]:
        captured["model"] = kwargs.get("model")
        return ["Name is Kevin"]

    monkeypatch.setattr(ss, "_distill_memory_oob", _fake_extract)

    streamer = ss.StreamingService(
        engine=engine,
        lm_client=AsyncMock(),
        memory_service=svc,
        chat_locks={},
        models_service=mock_models,
    )

    await streamer._safe_distill_memory(
        user_id=1,
        chat_id=9,
        model_id="some-chat-model",
        user_text="My name is Kevin",
        assistant_answer="Nice to meet you, Kevin.",
        project_id=None,
    )

    # The OOB extractor must have been called with the WIRE-ID, not the catalog key.
    assert captured["model"] == "bg-small-3b@q4", (
        f"Expected wire-id 'bg-small-3b@q4', got {captured.get('model')!r}. "
        "The distill OOB path is sending the catalog key instead of the "
        "loaded-instance wire-id, which causes LM Studio to return 400."
    )


async def test_safe_distill_memory_skips_when_no_model_loaded(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-soft: when no model is loaded (wire_id=None), distillation is
    skipped entirely and no exception is raised.

    The 'distillation must never affect the turn' contract requires every
    failure path to return quietly. This covers the case where the background
    model was configured but has been unloaded since the turn started.
    """
    import lmchat.services.streaming_service as ss
    from lmchat.services.models_service import ResolvedModel

    await _insert_user(engine, 1)
    await _insert_chat(engine, chat_id=10, user_id=1, incognito=False)
    svc = _make_service(engine)

    mock_models = AsyncMock(spec=ModelsService)
    mock_models.list_loaded.return_value = []
    # Simulate no LLM loaded → wire_id is None.
    mock_models.resolve_to_loaded_or_fallback.return_value = ResolvedModel(
        wire_id=None,
        requested="some-chat-model",
        reason="no_models_loaded",
    )

    extract_calls = 0

    async def _fake_extract(**_kwargs: object) -> list[str]:
        nonlocal extract_calls
        extract_calls += 1
        return ["Should not be stored"]

    monkeypatch.setattr(ss, "_distill_memory_oob", _fake_extract)

    streamer = ss.StreamingService(
        engine=engine,
        lm_client=AsyncMock(),
        memory_service=svc,
        chat_locks={},
        models_service=mock_models,
    )

    # Must not raise.
    await streamer._safe_distill_memory(
        user_id=1,
        chat_id=10,
        model_id="some-chat-model",
        user_text="My name is Kevin",
        assistant_answer="Nice to meet you, Kevin.",
        project_id=None,
    )

    # The OOB extractor must NOT have been called (graceful skip, not error).
    assert extract_calls == 0, (
        "OOB extractor was called even though no model is loaded. "
        "Expected graceful skip (return []) without calling the LM Studio endpoint."
    )
    # No insights stored.
    assert await _count_insights(engine, 1) == 0


# ---------------------------------------------------------------------------
# (d2) Attribution: the OOB prompt must scope extraction to USER statements.
# ---------------------------------------------------------------------------


async def test_distill_oob_labels_assistant_and_scopes_to_user() -> None:
    """RED-ON-REVERT: the OOB extraction prompt must label the assistant turn
    unambiguously as ``Assistant:`` (never ``Reply:``) and must not carry the
    self-referential "following your instructions" phrase.

    The ambiguous ``Reply:`` label let the model mine the assistant's own reply
    as user facts (observed live: a topic suggestion became a stored
    "Conducts academic research"). The system prompt already forbids saving
    assistant content; the unambiguous label is what makes that rule
    enforceable.

    Scope: this asserts the prompt STRUCTURE the model receives (the speaker
    label + the user-scoped framing) — not the model's runtime extraction,
    which a unit test can't exercise without a live model. The stubbed ``[]``
    return only confirms the request was built and dispatched.
    """
    from unittest.mock import MagicMock

    import lmchat.services.streaming_service as ss
    from lmchat.services.lmstudio_adapter import LmstudioAdapter

    captured: dict[str, object] = {}

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:  # type: ignore[type-arg]
            return {"choices": [{"message": {"content": "[]"}}]}

    async def _fake_post(_url: str, **kwargs: object) -> _Resp:
        captured["body"] = kwargs.get("json")
        return _Resp()

    mock_http = MagicMock()
    mock_http.post = _fake_post

    adapter = MagicMock(spec=LmstudioAdapter)
    adapter._http_client = mock_http
    adapter._base_url = "http://lm-studio.local"

    lm_client = MagicMock()
    lm_client._adapter = adapter

    facts = await ss._distill_memory_oob(
        lm_client=lm_client,
        model="bg@q4",
        conversation_messages=[
            {"role": "user", "content": "suggest interesting topics"}
        ],
        assistant_answer=(
            "Given your interest, you might enjoy academic research on dark energy."
        ),
    )
    assert facts == []  # the stub returned an empty array

    body = captured["body"]
    assert isinstance(body, dict)
    messages = body["messages"]
    system_text = str(messages[0]["content"])
    user_text = str(messages[1]["content"])

    # The assistant answer is labelled "Assistant:", never the ambiguous "Reply:".
    assert "Assistant: Given your interest" in user_text
    assert "Reply:" not in user_text
    # The user's own turn keeps the "User:" label.
    assert "User: suggest interesting topics" in user_text
    # No self-referential phrase the model could store as a "fact".
    assert "following your instructions" not in user_text
    # System prompt scopes extraction to USER statements; assistant is context.
    assert "Assistant:" in system_text
    assert "CONTEXT ONLY" in system_text


async def test_safe_distill_memory_skips_empty_user_text(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED-ON-REVERT: an empty user turn must skip extraction entirely.

    A durable USER fact can only come from something the user said. With no
    user statement, running the extractor on the assistant-only reply is the
    exact path that fabricated user facts from the assistant's own content.
    """
    import lmchat.services.streaming_service as ss

    await _insert_user(engine, 1)
    await _insert_chat(engine, chat_id=11, user_id=1, incognito=False)
    svc = _make_service(engine)

    extract_calls = 0

    async def _fake_extract(**_kwargs: object) -> list[str]:
        nonlocal extract_calls
        extract_calls += 1
        return ["Should not be stored"]

    monkeypatch.setattr(ss, "_distill_memory_oob", _fake_extract)

    # A models_service so we can prove the skip happens BEFORE background-model
    # resolution — a reverter that moved the guard below resolution would still
    # skip extraction but waste a resolution round-trip; this pins the order.
    mock_models = AsyncMock(spec=ModelsService)
    streamer = ss.StreamingService(
        engine=engine,
        lm_client=AsyncMock(),
        memory_service=svc,
        chat_locks={},
        models_service=mock_models,
    )

    stored = await streamer._safe_distill_memory(
        user_id=1,
        chat_id=11,
        model_id="some-model",
        user_text="   ",  # whitespace-only: the user said nothing this turn
        assistant_answer="Here are some interesting topics you might enjoy.",
        project_id=None,
    )

    assert stored == 0
    assert extract_calls == 0  # extraction never reached
    assert await _count_insights(engine, 1) == 0
    # Skip precedes model resolution (no wasted round-trip).
    mock_models.resolve_to_loaded_or_fallback.assert_not_called()


async def test_distill_oob_returns_empty_without_a_user_turn() -> None:
    """RED-ON-REVERT: the OOB primitive refuses an assistant-only transcript.

    A durable USER fact can only come from a user turn. If a (future) caller
    passes ``conversation_messages`` with no user-role content, the extractor
    must return ``[]`` and never POST — otherwise it would mine the assistant's
    reply as a user fact regardless of the prompt scoping.
    """
    from unittest.mock import MagicMock

    import lmchat.services.streaming_service as ss
    from lmchat.services.lmstudio_adapter import LmstudioAdapter

    posted = False

    async def _fake_post(_url: str, **_kwargs: object) -> object:
        nonlocal posted
        posted = True
        raise AssertionError("must not POST without a user turn")

    mock_http = MagicMock()
    mock_http.post = _fake_post
    adapter = MagicMock(spec=LmstudioAdapter)
    adapter._http_client = mock_http
    adapter._base_url = "http://lm-studio.local"
    lm_client = MagicMock()
    lm_client._adapter = adapter

    facts = await ss._distill_memory_oob(
        lm_client=lm_client,
        model="bg@q4",
        # assistant-only prior turns; no user-role content anywhere.
        conversation_messages=[{"role": "assistant", "content": "earlier reply"}],
        assistant_answer="Here are some topics you might enjoy.",
    )

    assert facts == []
    assert posted is False


# ---------------------------------------------------------------------------
# (e) The stored AUTO memory is RECALLED on a later turn.
# ---------------------------------------------------------------------------


async def test_auto_memory_is_recalled(engine: AsyncEngine) -> None:
    """A distilled AUTO insight surfaces in recall_insights (insight_hits path)."""
    await _insert_user(engine, 1)
    svc = _make_service(engine)

    await svc.save_auto_insight(user_id=1, text="Into astrophysics and dark energy")

    # A LATER turn recalls insights for the prompt.
    recalled = await svc.recall_insights(user_id=1, top_k=8)

    texts = [r.text for r in recalled]
    assert "Into astrophysics and dark energy" in texts
    hit = next(r for r in recalled if r.text == "Into astrophysics and dark energy")
    assert hit.pinned is False  # surfaced as an AUTO memory, not a pin.


async def test_auto_memory_and_pin_both_recalled(engine: AsyncEngine) -> None:
    """Recall returns BOTH a pin and an AUTO memory (pinned-first ordering)."""
    await _insert_user(engine, 1)
    svc = _make_service(engine)

    await svc.pin_insight(user_id=1, text="Prefers metric units")
    await svc.save_auto_insight(user_id=1, text="Into astrophysics")

    recalled = await svc.recall_insights(user_id=1, top_k=8)
    texts = {r.text for r in recalled}
    assert "Prefers metric units" in texts
    assert "Into astrophysics" in texts


# ---------------------------------------------------------------------------
# Metric wiring — the previously-dead MEMORY_DISTILLATIONS counter.
# ---------------------------------------------------------------------------


async def test_metric_increments_per_stored_fact(engine: AsyncEngine) -> None:
    """MEMORY_DISTILLATIONS increments once per stored fact, never for dups."""
    from lmchat.metrics import MEMORY_DISTILLATIONS

    await _insert_user(engine, 1)
    svc = _make_service(engine)

    before = MEMORY_DISTILLATIONS._value.get()  # type: ignore[attr-defined]
    await svc.distill_and_store(
        user_id=1, facts=["Name is Kevin", "Into astrophysics"]
    )
    # A duplicate batch must not move the counter.
    await svc.distill_and_store(user_id=1, facts=["Name is Kevin"])
    after = MEMORY_DISTILLATIONS._value.get()  # type: ignore[attr-defined]

    assert after - before == 2  # two NEW facts, the repeat did not count.


# ---------------------------------------------------------------------------
# Cap / eviction — oldest LRU AUTO rows fade past the limit.
# ---------------------------------------------------------------------------


async def test_auto_cap_fades_oldest_lru(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Over the auto cap, the oldest never-used AUTO rows transition to faded."""
    from lmchat.config import get_settings

    # Force a tiny cap so we don't insert hundreds of rows.
    settings = get_settings()
    monkeypatch.setattr(settings, "lm_chat_auto_memory_cap", 2, raising=False)

    await _insert_user(engine, 1)
    svc = _make_service(engine)

    # Insert 3 distinct facts; cap=2 → exactly one fades (the oldest).
    a = await svc.save_auto_insight(user_id=1, text="Fact alpha one")
    b = await svc.save_auto_insight(user_id=1, text="Fact bravo two")
    c = await svc.save_auto_insight(user_id=1, text="Fact charlie three")
    assert a is not None and b is not None and c is not None

    # Only 2 active AUTO rows remain; total rows still 3 (faded, not deleted).
    active = await svc.list_auto(user_id=1)
    assert len(active) == 2
    assert await _count_insights(engine, 1) == 3

    # The OLDEST (alpha) is the one faded; the two newest stay active.
    active_texts = {r.text for r in active}
    assert "Fact alpha one" not in active_texts
    assert "Fact bravo two" in active_texts
    assert "Fact charlie three" in active_texts


async def test_auto_cap_does_not_evict_just_saved_fact(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuinely stale AUTO fact must fade before a brand-new one.

    Regression for the bug where eviction ordered candidates by
    ``last_used ASC NULLS FIRST``: a never-recalled row (``last_used IS
    NULL``) always sorted first regardless of its own creation time, so a
    fact saved moments ago could be faded ahead of a fact that was
    genuinely last touched 90 days ago.
    """
    from lmchat.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "lm_chat_auto_memory_cap", 1, raising=False)

    await _insert_user(engine, 1)
    now = datetime.now(UTC)
    stale_dt = now - timedelta(days=90)

    async with engine.begin() as conn:
        # Row 1: genuinely stale — last recalled (touched) 90 days ago.
        await conn.execute(
            text(
                "INSERT INTO memory_insights (id, user_id, text, text_hash,"
                " pinned, category, use_count, ups, downs, last_used,"
                " last_feedback_at, state, created_at)"
                " VALUES (1, 1, 'Stale recalled fact', 'h1', 0, 'profile',"
                " 0, 0, 0, :lu, NULL, 'active', :ca)"
            ),
            {
                "lu": stale_dt.timestamp(),
                "ca": stale_dt.strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
        # Row 2: never recalled (last_used NULL), created just now.
        await conn.execute(
            text(
                "INSERT INTO memory_insights (id, user_id, text, text_hash,"
                " pinned, category, use_count, ups, downs, last_used,"
                " last_feedback_at, state, created_at)"
                " VALUES (2, 1, 'Just saved fact', 'h2', 0, 'profile', 0,"
                " 0, 0, NULL, NULL, 'active', :ca)"
            ),
            {"ca": now.strftime("%Y-%m-%d %H:%M:%S")},
        )

    svc = _make_service(engine)
    await svc._evict_auto_over_cap(user_id=1)

    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(memory_insights.c.text, memory_insights.c.state)
            )
        ).fetchall()
    state_by_text = {r.text: r.state for r in rows}

    assert state_by_text["Stale recalled fact"] == "faded"
    assert state_by_text["Just saved fact"] == "active"


async def test_save_auto_insight_reports_none_when_cap_evicts_itself(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """save_auto_insight must not report a fact as stored if the auto-cap
    immediately faded that very row.

    Regression: eviction fades via ``state='faded'`` (never deletes), so
    the post-insert re-fetch in ``save_auto_insight`` found a non-None row
    even when THAT row was the one just faded by
    ``_evict_auto_over_cap`` — the caller saw a "successfully stored"
    :class:`MemoryInsight` for a fact that is not actually recallable.
    """
    from lmchat.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "lm_chat_auto_memory_cap", 1, raising=False)

    await _insert_user(engine, 1)

    # Pre-seed an active row with last_used far in the FUTURE, so it is
    # unambiguously "more recently active" than anything save_auto_insight
    # inserts next (whose created_at can only be "now").
    far_future_dt = datetime.now(UTC) + timedelta(days=1000)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO memory_insights (id, user_id, text, text_hash,"
                " pinned, category, use_count, ups, downs, last_used,"
                " last_feedback_at, state, created_at)"
                " VALUES (1, 1, 'Far-future-touched fact', 'h1', 0,"
                " 'profile', 0, 0, 0, :lu, NULL, 'active', :ca)"
            ),
            {
                "lu": far_future_dt.timestamp(),
                "ca": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
            },
        )

    svc = _make_service(engine)

    # cap=1, one row already active with a far-future last_used → this new
    # save is immediately over cap and IS the least-recently-active row.
    result = await svc.save_auto_insight(
        user_id=1, text="Fact that gets faded immediately"
    )

    assert result is None  # not reported as "stored" to the caller.

    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(memory_insights.c.text, memory_insights.c.state)
            )
        ).fetchall()
    state_by_text = {r.text: r.state for r in rows}
    assert state_by_text["Fact that gets faded immediately"] == "faded"


async def test_auto_cap_eviction_orders_created_at_as_utc_not_local(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Eviction ordering must interpret a naive ``created_at`` (SQLite's
    read-back format for a UTC ``func.now()`` write) as UTC, not as the
    process's local timezone.

    Regression for a partial reintroduction of the "evicts the just-saved
    fact" bug: comparing ``row.created_at.timestamp()`` directly treats a
    naive datetime as LOCAL time, which skews a never-touched row's
    effective recency by the host's UTC offset relative to ``last_used``
    (always a true UTC epoch). The two rows below are only ~1 hour apart —
    smaller than the forced UTC+8 offset — so the bug (if reintroduced)
    flips which row is genuinely more recent. The offset is forced via
    ``TZ`` rather than relying on the host's real timezone, since a CI box
    already running in UTC would not otherwise exercise this at all.
    """
    from lmchat.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "lm_chat_auto_memory_cap", 1, raising=False)

    original_tz = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Shanghai"  # UTC+8, no DST — larger than the 1h gap.
    time.tzset()
    try:
        await _insert_user(engine, 1)
        now_utc = datetime.now(UTC).replace(tzinfo=None)  # naive UTC wall-clock,
        # matching what SQLite hands back for a func.now() write.

        async with engine.begin() as conn:
            # Row 1: genuinely touched (recalled) 1 hour ago — true UTC epoch.
            await conn.execute(
                text(
                    "INSERT INTO memory_insights (id, user_id, text, text_hash,"
                    " pinned, category, use_count, ups, downs, last_used,"
                    " last_feedback_at, state, created_at)"
                    " VALUES (1, 1, 'Recalled one hour ago', 'h1', 0,"
                    " 'profile', 0, 0, 0, :lu, NULL, 'active', :ca)"
                ),
                {
                    "lu": (now_utc - timedelta(hours=1)).replace(tzinfo=UTC).timestamp(),
                    "ca": (now_utc - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
                },
            )
            # Row 2: never recalled, created just now (true UTC "now").
            await conn.execute(
                text(
                    "INSERT INTO memory_insights (id, user_id, text, text_hash,"
                    " pinned, category, use_count, ups, downs, last_used,"
                    " last_feedback_at, state, created_at)"
                    " VALUES (2, 1, 'Created just now', 'h2', 0, 'profile',"
                    " 0, 0, 0, NULL, NULL, 'active', :ca)"
                ),
                {"ca": now_utc.strftime("%Y-%m-%d %H:%M:%S")},
            )

        svc = _make_service(engine)
        await svc._evict_auto_over_cap(user_id=1)

        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(memory_insights.c.text, memory_insights.c.state)
                )
            ).fetchall()
        state_by_text = {r.text: r.state for r in rows}

        # Row 2 (created ~1h after row 1's last recall) is genuinely more
        # recent — row 1 (the true LRU) must be the one that fades.
        assert state_by_text["Recalled one hour ago"] == "faded"
        assert state_by_text["Created just now"] == "active"
    finally:
        if original_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_tz
        time.tzset()


# ---------------------------------------------------------------------------
# Helper-level: near-dup + OOB JSON parse contract.
# ---------------------------------------------------------------------------


def test_is_near_duplicate_catches_paraphrase() -> None:
    """High token-overlap paraphrases are flagged; distinct facts are not."""
    assert _is_near_duplicate("likes astrophysics", ["is into astrophysics"]) is True
    assert _is_near_duplicate("name is Kevin", ["lives in Berlin"]) is False
    assert _is_near_duplicate("anything", []) is False


def test_is_near_duplicate_requires_absolute_floor_for_short_facts() -> None:
    """Two DISTINCT 2-content-token facts sharing exactly one word must NOT
    be flagged as near-duplicates.

    The proportional overlap coefficient alone (1 shared / min(2,2) = 0.5)
    hits the 0.5 threshold, but for facts this short that single shared
    word is not enough evidence — "backend" and "frontend" are different
    specializations, not a paraphrase of each other.
    """
    assert _is_near_duplicate("python backend", ["python frontend"]) is False
    # Sanity: full containment on a single content word (the original
    # paraphrase-catching case) is unaffected by the added floor.
    assert _is_near_duplicate("likes astrophysics", ["is into astrophysics"]) is True


def test_distill_oob_parse_handles_empty_and_prose() -> None:
    """The OOB extractor reuses the defensive followups parser: [] and prose-wrapped."""
    from lmchat.services.streaming_service import _parse_followups_json

    assert _parse_followups_json("[]") == []
    assert _parse_followups_json(
        'Here are the facts: ["Name is Kevin", "Likes coffee"]'
    ) == ["Name is Kevin", "Likes coffee"]
    # Code-fenced output is unwrapped too.
    assert _parse_followups_json('```json\n["Into astrophysics"]\n```') == [
        "Into astrophysics"
    ]

