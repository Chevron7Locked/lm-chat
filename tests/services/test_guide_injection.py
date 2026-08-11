# SPDX-License-Identifier: Apache-2.0
"""Guide-context injection is mode-independent.

Proves ``_assemble_system_prompt`` appends the ``[LM Chat guide — ...]``
block onto the wire ``system_prompt`` in BOTH chain mode (default LM
Studio, no provider_registry) and replay mode (a resolved cloud/compat
provider). Injection is unconditional on endpoint mode; only the message
content gates it. Mirrors the harness in
``test_capability_legend_injection.py``.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import chats, metadata
from lmchat.lmstudio.types import CanonicalChatRequest, CanonicalEvent, CanonicalInputBlock
from lmchat.services import system_guide
from lmchat.services.streaming_service import ChatStreamRequest, StreamingService


@pytest.fixture(autouse=True)
def _reset_guide_semantic_cache() -> Iterator[None]:
    """``system_guide._cache``'s semantic embed-matrix cache is process-wide
    (keyed on ``(model_key, id(sections))``) — a real production feature
    (don't re-embed the guide on every turn), but it means two tests in
    this file that both resolve to the SAME model key against the SAME
    (unchanged) real guide corpus would otherwise silently reuse the FIRST
    test's fake embedder's matrix. Reset before and after every test in
    this file so each test's fake embedder is actually exercised.

    Also resets the background-embed task tracking (``_embed_bg_task`` /
    ``_embed_bg_model_key``) — the corpus embed now runs as a detached
    ``asyncio.create_task`` (see ``system_guide.ensure_section_embeddings_
    background``), so a task left over from one test (e.g. one that
    doesn't await it to completion) must not bleed into a later test's
    idempotency/cold-cache assertions."""

    def _reset() -> None:
        system_guide._cache._embed_cache_key = None
        system_guide._cache._embed_matrix = None
        system_guide._cache._embed_sections = []
        task = system_guide._cache._embed_bg_task
        if task is not None and not task.done():
            task.cancel()
        system_guide._cache._embed_bg_task = None
        system_guide._cache._embed_bg_model_key = None

    _reset()
    yield
    _reset()


# Same clear app question used in test_system_guide.py's guide_context_block
# tests — a distinctive high-score match that clears the injection gate (see
# test_inject_min_score_separates_match_from_incidental_mention).
_MATCHING_MESSAGE = "how do I add custom instructions to a project"
# No guide-page term overlap at all — scores 0, well below any threshold.
_NON_MATCHING_MESSAGE = "what's the weather like today"


def _happy_events() -> list[CanonicalEvent]:
    return [
        CanonicalEvent(type="chat.start"),
        CanonicalEvent(type="message.start"),
        CanonicalEvent(type="message.delta", content="ack"),
        CanonicalEvent(type="message.end"),
        CanonicalEvent(type="chat.end", response_id="r-guide-injection"),
    ]


def _mock_user(user_id: int = 1) -> MagicMock:
    u = MagicMock()
    u.id = user_id
    return u


def _mock_request() -> MagicMock:
    from tests.services.conftest import make_disconnect_receive

    r = MagicMock()
    r.receive = make_disconnect_receive(False)
    return r


async def _drain(stream: AsyncIterator[Any]) -> list[Any]:
    return [frame async for frame in stream]


def _parse_frames(frames: list[bytes]) -> list[dict]:  # type: ignore[type-arg]
    results = []
    for frame in frames:
        for line in frame.decode("utf-8").splitlines():
            if line.startswith("data:"):
                results.append(json.loads(line[5:].strip()))
    return results


@pytest.fixture
async def engine() -> AsyncEngine:
    e = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with e.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return e


async def _insert_chat(engine: AsyncEngine, *, settings: dict | None = None) -> int:
    async with engine.begin() as conn:
        result = await conn.execute(
            chats.insert().values(user_id=1, title="t", settings=settings or {})
        )
        return result.inserted_primary_key[0]  # type: ignore[index]


async def _run_chain(
    engine: AsyncEngine,
    message: str,
    *,
    embedding_client: Any = None,
    models_service: Any = None,
) -> str:
    """Drive the default LM Studio (chain) path; return the wire
    system_prompt sent to lm_client.stream.

    ``embedding_client`` / ``models_service`` default to ``None`` (matching
    every pre-existing caller) — that keeps guide injection on the sync
    keyword-only fallback. Passing both wires the SEMANTIC engine, for the
    tests further down that drive it through the real
    ``_assemble_system_prompt`` routing (resolve embed model -> ensure
    section embeddings -> embed query), not just the ``system_guide.py``
    unit level.
    """
    chat_id = await _insert_chat(engine)

    captured: dict[str, Any] = {}

    async def _fake_stream(*args: Any, **kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        captured["payload"] = kwargs.get("request") or (args[0] if args else None)
        for ev in _happy_events():
            yield ev

    lm_client = MagicMock()
    lm_client.stream = _fake_stream

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    svc = StreamingService(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        chat_locks={},
        embedding_client=embedding_client,
        models_service=models_service,
    )

    payload = ChatStreamRequest(
        chat_id=chat_id,
        payload=CanonicalChatRequest(
            model="test-model",
            input=[CanonicalInputBlock(type="text", content=message)],
        ),
    )

    await _drain(
        svc.stream_chat(
            chat_id=chat_id, user=_mock_user(), payload=payload, request=_mock_request()
        )
    )

    sent = captured.get("payload")
    assert sent is not None, "lm_client.stream was not called"
    return getattr(sent, "system_prompt", "") or ""


async def _run_replay(engine: AsyncEngine, message: str) -> str:
    """Drive the replay (cloud/compat) path; return the wire system_prompt
    sent to the provider's stream_chat."""
    chat_id = await _insert_chat(engine, settings={"provider": "openrouter"})

    captured: dict[str, Any] = {}

    async def _provider_stream_chat(
        request: Any, **kwargs: Any
    ) -> AsyncIterator[CanonicalEvent]:
        captured["request"] = request
        for ev in _happy_events():
            yield ev

    stub_provider = MagicMock()
    stub_provider.context_mode = "replay"
    stub_provider.name = "openrouter"
    stub_provider.stream_chat = _provider_stream_chat

    registry = MagicMock()
    registry.get = MagicMock(
        side_effect=lambda name: stub_provider if name == "openrouter" else None
    )

    lm_client = MagicMock()
    lm_client.stream = MagicMock(
        side_effect=AssertionError("lm_client.stream must NOT be called in replay mode")
    )

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    svc = StreamingService(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        chat_locks={},
        provider_registry=registry,
    )

    payload = ChatStreamRequest(
        chat_id=chat_id,
        payload=CanonicalChatRequest(
            model="test-model",
            input=[CanonicalInputBlock(type="text", content=message)],
        ),
    )

    await _drain(
        svc.stream_chat(
            chat_id=chat_id, user=_mock_user(), payload=payload, request=_mock_request()
        )
    )

    sent = captured.get("request")
    assert sent is not None, "provider.stream_chat was not called"
    return getattr(sent, "system_prompt", "") or ""


@pytest.mark.asyncio
async def test_chain_mode_injects_guide_context_on_matching_message(
    engine: AsyncEngine,
) -> None:
    sys_p = await _run_chain(engine, _MATCHING_MESSAGE)
    assert "[LM Chat guide —" in sys_p
    assert "custom instructions" in sys_p.lower()


@pytest.mark.asyncio
async def test_replay_mode_injects_guide_context_on_matching_message(
    engine: AsyncEngine,
) -> None:
    sys_p = await _run_replay(engine, _MATCHING_MESSAGE)
    assert "[LM Chat guide —" in sys_p
    assert "custom instructions" in sys_p.lower()


@pytest.mark.asyncio
async def test_chain_mode_does_not_inject_guide_context_on_non_matching_message(
    engine: AsyncEngine,
) -> None:
    sys_p = await _run_chain(engine, _NON_MATCHING_MESSAGE)
    assert "[LM Chat guide —" not in sys_p


@pytest.mark.asyncio
async def test_replay_mode_does_not_inject_guide_context_on_non_matching_message(
    engine: AsyncEngine,
) -> None:
    sys_p = await _run_replay(engine, _NON_MATCHING_MESSAGE)
    assert "[LM Chat guide —" not in sys_p


@pytest.mark.asyncio
async def test_guide_injection_runs_independent_of_openai_compat_gating(
    engine: AsyncEngine,
) -> None:
    """Native LM Studio chain mode (no provider_registry, no openai_compat)
    still gets the guide-context block -- proving injection is NOT gated
    behind endpoint mode at all; it is the sole, always-on guide-lookup
    path in both chain and replay mode."""
    sys_p = await _run_chain(engine, _MATCHING_MESSAGE)
    assert "[Capabilities]" in sys_p
    assert "[LM Chat guide —" in sys_p


# ─── Semantic engine wired through the REAL streaming routing ─────────────
#
# The tests above never wire an embedding_client, so they only exercise the
# sync KEYWORD fallback branch of ``_assemble_system_prompt``'s guide-
# injection routing. These drive the SAME routing with a fake
# embedding_client + models_service to prove the semantic branch itself --
# resolve the active embed model, ensure the corpus is embedded, embed the
# query, fall through to keyword on any failure -- actually works end to
# end through ``StreamingService``, not just at the ``system_guide.py`` unit
# level (see ``test_system_guide.py``'s ``_FakeEmbedder`` tests for that).


class _FakeGuideEmbeddingClient:
    """Deterministic fake matching ``EmbeddingClient``'s ``embed_batch`` /
    ``embed_one`` signatures. Any text containing *marker* (case-insensitive)
    embeds to *marker_vector*; everything else embeds to *default_vector*,
    UNLESS it's an exact match in *overrides* (used to give one specific
    query string its own vector, e.g. orthogonal to everything else, without
    having to enumerate the ~265 real guide sections' exact text)."""

    def __init__(
        self,
        *,
        marker: str,
        marker_vector: list[float],
        default_vector: list[float],
        overrides: dict[str, list[float]] | None = None,
    ) -> None:
        self._marker = marker.lower()
        self._marker_vector = marker_vector
        self._default_vector = default_vector
        self._overrides = overrides or {}

    def _vector(self, text: str) -> list[float]:
        if text in self._overrides:
            return self._overrides[text]
        if self._marker in text.lower():
            return self._marker_vector
        return self._default_vector

    async def embed_batch(self, *, texts: list[str], model_id: str) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    async def embed_one(self, *, text: str, model_id: str) -> list[float]:
        return self._vector(text)


def _full_turn_models_service_mock(*, embedding_loaded: bool) -> MagicMock:
    """A ModelsService stub that lets an ENTIRE turn complete (not just
    guide injection): ``resolve_to_loaded_or_fallback`` / ``get_capabilities``
    / ``get_max_context_length`` satisfy the downstream capability-gate
    section of ``stream_chat`` (which also runs once ``models_service`` is
    non-``None``, independent of guide injection), mirroring
    ``test_prompt_assembly.py``'s ``_models_service_mock``. ``list_loaded``
    additionally reports the canonical default embedding model
    (``memory_service.DEFAULT_EMBEDDING_MODEL_KEY``) as loaded when
    *embedding_loaded* is True (so ``resolve_active_embedding_model_key``
    resolves it with no stored preference row — the test DB's
    ``server_lm_studio_default`` is empty), or nothing at all when False (so
    that same call raises ``NoEmbeddingModelLoadedError``)."""
    from lmchat.services.memory_service import DEFAULT_EMBEDDING_MODEL_KEY
    from lmchat.services.models_service import ModelInfo, ResolvedModel

    svc = MagicMock()
    svc.resolve_to_loaded_or_fallback = AsyncMock(
        side_effect=lambda mid, **_kw: ResolvedModel(wire_id=mid, requested=mid)
    )
    svc.get_capabilities = AsyncMock(
        return_value=SimpleNamespace(trained_for_tool_use=True, reasoning=None)
    )
    svc.get_max_context_length = AsyncMock(return_value=8000)
    svc.list_loaded = AsyncMock(
        return_value=[
            ModelInfo(
                key=DEFAULT_EMBEDDING_MODEL_KEY,
                type="embedding",
                loaded_instance_ids=[DEFAULT_EMBEDDING_MODEL_KEY],
            )
        ]
        if embedding_loaded
        else []
    )
    return svc


def _spy_on_guide_semantic(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Monkeypatch ``streaming_service``'s bound name for
    ``guide_context_block_semantic`` with a pass-through spy; returns the
    list of queries it was actually invoked with (append-only, growing
    live as turns run).

    This is the only unambiguous way to prove the SEMANTIC engine (as
    opposed to the keyword fallback) served a given turn: RAG augmentation
    shares the SAME injected ``embedding_client`` and calls its
    ``embed_one`` with an identical ``embed_one(text=..., model_id=...)``
    signature for memory/document retrieval, so counting calls on the fake
    embedding client itself can't distinguish "RAG embedded the query"
    from "guide injection embedded the query" -- only spying on the guide
    module's own semantic entry point can.
    """
    import lmchat.services.streaming_service as streaming_service_module

    calls: list[str] = []
    original = streaming_service_module._guide_context_block_semantic

    async def _spy(query: str, **kwargs: Any) -> str | None:
        calls.append(query)
        return await original(query, **kwargs)

    monkeypatch.setattr(streaming_service_module, "_guide_context_block_semantic", _spy)
    return calls


@pytest.mark.asyncio
async def test_chain_mode_uses_keyword_first_turn_then_semantic_once_bg_embed_completes(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one-time corpus embed must never run inline on a per-turn path
    (it always blew past the per-turn timeout in production and the
    matrix never finished caching -- see streaming_service.py's
    ``_GUIDE_SEMANTIC_TIMEOUT_SEC`` comment). So: turn 1 hits a COLD
    semantic cache -- the routing kicks off the corpus embed as a
    fire-and-forget background task and serves THIS turn from the keyword
    engine, which never reaches the semantic engine at all. Once that
    background task completes (awaited directly here, deterministically --
    no real network/delay behind the fake embedder), a LATER turn against
    the now-WARM cache is served by the semantic engine, proven by
    ``_spy_on_guide_semantic``."""
    from lmchat.services.memory_service import DEFAULT_EMBEDDING_MODEL_KEY

    semantic_calls = _spy_on_guide_semantic(monkeypatch)
    fake_embedding_client = _FakeGuideEmbeddingClient(
        marker="custom instructions",
        marker_vector=[1.0, 0.0],
        default_vector=[0.0, 1.0],
    )
    models_service = _full_turn_models_service_mock(embedding_loaded=True)

    assert system_guide._cache.get_cached_section_embeddings(DEFAULT_EMBEDDING_MODEL_KEY) is None

    sys_p_1 = await _run_chain(
        engine,
        _MATCHING_MESSAGE,
        embedding_client=fake_embedding_client,
        models_service=models_service,
    )
    # The keyword engine already matches this exact message on its own
    # (see test_system_guide.py's _CLEAR_APP_QUESTION tests) -- so turn 1
    # injects, but via keyword: the cache was cold at the point the
    # per-turn routing checked it, so the semantic branch never ran.
    assert "[LM Chat guide —" in sys_p_1
    assert "custom instructions" in sys_p_1.lower()
    assert semantic_calls == [], (
        "turn 1 must never reach the semantic engine -- it only serves a "
        "turn once the corpus is already cached"
    )

    bg_task = system_guide._cache._embed_bg_task
    if bg_task is not None:
        await bg_task
    assert (
        system_guide._cache.get_cached_section_embeddings(DEFAULT_EMBEDDING_MODEL_KEY) is not None
    ), "the background task must have cached the corpus matrix by now"

    sys_p_2 = await _run_chain(
        engine,
        _MATCHING_MESSAGE,
        embedding_client=fake_embedding_client,
        models_service=models_service,
    )
    assert "[LM Chat guide —" in sys_p_2
    assert "custom instructions" in sys_p_2.lower()
    assert semantic_calls, "turn 2 must hit the semantic engine now that the corpus is cached"


@pytest.mark.asyncio
async def test_chain_mode_falls_back_to_keyword_when_no_embedding_model_loaded(
    engine: AsyncEngine,
) -> None:
    """embedding_client + models_service are both wired (non-None), but NO
    embedding model is loaded -- resolve_active_embedding_model_key raises
    NoEmbeddingModelLoadedError, which the routing catches and falls
    through to the keyword engine. The matching message still injects
    (via keyword's own two-tier gate, exactly as in the no-embedder tests
    above) -- proving the fallback path is live, not just theoretical."""
    fake_embedding_client = _FakeGuideEmbeddingClient(
        marker="zzz-marker-never-appears-zzz",
        marker_vector=[1.0, 0.0],
        default_vector=[0.0, 1.0],
    )

    sys_p = await _run_chain(
        engine,
        _MATCHING_MESSAGE,
        embedding_client=fake_embedding_client,
        models_service=_full_turn_models_service_mock(embedding_loaded=False),
    )
    assert "[LM Chat guide —" in sys_p
    assert "custom instructions" in sys_p.lower()


@pytest.mark.asyncio
async def test_chain_mode_semantic_floor_gates_below_threshold_query(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A working semantic engine that finds NO section above its cosine
    floor falls through to keyword -- which also finds nothing (no term
    overlap with the guide corpus) -- so no block is injected. Proves the
    floor actually gates through the real routing, not just at the
    ``system_guide.py`` unit level.

    Warms the semantic cache with a throwaway turn first (awaiting the
    background embed task) so this test genuinely exercises the semantic
    floor -- against a cold cache, the query would be served by keyword
    alone before the semantic branch ever ran, which wouldn't prove
    anything about the floor."""
    query = "what is the airspeed velocity of an unladen swallow?"
    semantic_calls = _spy_on_guide_semantic(monkeypatch)
    fake_embedding_client = _FakeGuideEmbeddingClient(
        marker="zzz-marker-never-appears-zzz",
        marker_vector=[1.0, 0.0, 0.0],
        default_vector=[0.0, 1.0, 0.0],
        overrides={query: [0.0, 0.0, 1.0]},  # orthogonal to every section
    )
    models_service = _full_turn_models_service_mock(embedding_loaded=True)

    await _run_chain(
        engine,
        _MATCHING_MESSAGE,
        embedding_client=fake_embedding_client,
        models_service=models_service,
    )
    bg_task = system_guide._cache._embed_bg_task
    if bg_task is not None:
        await bg_task

    sys_p = await _run_chain(
        engine,
        query,
        embedding_client=fake_embedding_client,
        models_service=models_service,
    )
    assert "[LM Chat guide —" not in sys_p
    assert query in semantic_calls, (
        "the semantic engine must actually have run this turn (cache was "
        "warmed) -- otherwise this test only proves the keyword engine's "
        "own floor, not the semantic one"
    )
