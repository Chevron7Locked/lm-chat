# SPDX-License-Identifier: Apache-2.0
"""Tests for hybrid-compaction context wiring in StreamingService.

Covers the chain-reset backstop and context exclusion:

1. Outbound context for a chat with archived messages excludes the archived
   rows and injects each compaction's summary, in span (anchor_msg_id) order.
2. Chain-reset backstop: a turn whose ``previous_response_id`` maps to the
   latest *kept* (non-archived) assistant turn, but whose timestamp PREDATES
   the chat's latest compaction, is dropped — the BE forces replay (summary +
   active only). This is the trap case: checking only
   "is the anchor archived?" would miss it.
3. Backstop leaves a normal chat (no compactions at all) byte-identical — the
   incoming previous_response_id passes through unchanged and no history is
   composed, exactly like the pre-compaction path.
4. A stale rid pointing directly at an ARCHIVED anchor message (the simpler
   backstop condition) also forces replay, and the forced-replay path never
   leaks archived content onto the wire.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import chats, compactions, messages, metadata
from lmchat.lmstudio.types import (
    CanonicalChatRequest,
    CanonicalEvent,
    CanonicalInputBlock,
)
from lmchat.services._stream_state import PersistState
from lmchat.services.streaming_service import ChatStreamRequest, StreamingService

# ---------------------------------------------------------------------------
# Helpers (mirrors tests/services/test_streaming_chain_tool_turns.py +
# test_streaming_replay.py — each streaming test module keeps its own small
# fixture helpers rather than sharing a mega-conftest).
# ---------------------------------------------------------------------------

_T0 = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)


def _t(offset_min: int) -> datetime:
    return _T0 + timedelta(minutes=offset_min)


async def _make_engine() -> AsyncEngine:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return engine


def _make_payload(
    model: str = "test-model",
    chat_text: str = "new question",
    *,
    previous_response_id: str | None = None,
) -> ChatStreamRequest:
    return ChatStreamRequest(
        chat_id=1,
        payload=CanonicalChatRequest(
            model=model,
            input=[CanonicalInputBlock(type="text", content=chat_text)],
            previous_response_id=previous_response_id,
        ),
    )


def _happy_events(response_id: str = "rid-new") -> list[CanonicalEvent]:
    return [
        CanonicalEvent(type="chat.start"),
        CanonicalEvent(type="message.start"),
        CanonicalEvent(type="message.delta", content="answer"),
        CanonicalEvent(type="message.end"),
        CanonicalEvent(type="chat.end", response_id=response_id),
    ]


def _mock_user(user_id: int = 1) -> MagicMock:
    u = MagicMock()
    u.id = user_id
    return u


def _mock_request(disconnected: bool = False) -> AsyncMock:
    from tests.services.conftest import make_disconnect_receive

    req = AsyncMock()
    req.receive = make_disconnect_receive(disconnected)
    return req


async def _drain(gen: AsyncIterator[bytes]) -> list[bytes]:
    frames: list[bytes] = []
    async for frame in gen:
        frames.append(frame)
    return frames


def _parse_frames(frames: list[bytes]) -> list[dict]:  # type: ignore[type-arg]
    results = []
    for frame in frames:
        for line in frame.decode("utf-8").splitlines():
            if line.startswith("data:"):
                results.append(json.loads(line[5:].strip()))
    return results


def _make_models_service(wire_id: str = "test-model") -> AsyncMock:
    """Minimal models_service mock for chain-mode tests."""
    svc = AsyncMock()
    svc.auth_failed = False
    res = MagicMock()
    res.wire_id = wire_id
    res.substituted = False
    svc.resolve_to_loaded_or_fallback = AsyncMock(return_value=res)
    svc.get_capabilities = AsyncMock(side_effect=KeyError(wire_id))
    svc.get_max_context_length = AsyncMock(return_value=0)
    return svc


def _make_stub_provider(
    context_mode: str = "replay",
    *,
    captured: dict[str, Any] | None = None,
) -> MagicMock:
    """Build a stub ChatProvider that records stream_chat kwargs into *captured*."""
    p = MagicMock()
    p.context_mode = context_mode
    p.name = "openrouter"

    async def _stream_chat(request: Any, **kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        if captured is not None:
            captured["request"] = request
            captured.update(kwargs)
        for ev in _happy_events():
            yield ev

    p.stream_chat = _stream_chat
    return p


def _make_registry(provider: object | None, name: str = "openrouter") -> MagicMock:
    reg = MagicMock()

    def _get(n: str) -> object | None:
        return provider if n == name else None

    reg.get = _get
    return reg


async def _insert_chat(engine: AsyncEngine, *, settings: dict | None = None) -> int:  # type: ignore[type-arg]
    async with engine.begin() as conn:
        result = await conn.execute(
            chats.insert().values(user_id=1, title="test", settings=settings or {})
        )
        return result.inserted_primary_key[0]  # type: ignore[index]


async def _insert_message(
    engine: AsyncEngine,
    chat_id: int,
    *,
    role: str,
    content: str,
    response_id: str | None = None,
    created_at: datetime | None = None,
    compaction_id: int | None = None,
) -> int:
    """Insert a FINAL message row and return its id."""
    values: dict[str, Any] = {
        "chat_id": chat_id,
        "role": role,
        "content": content,
        "state": PersistState.FINAL.value,
        "model_id": "test-model",
        "response_id": response_id,
        "compaction_id": compaction_id,
    }
    if created_at is not None:
        values["created_at"] = created_at
    async with engine.begin() as conn:
        result = await conn.execute(messages.insert().values(**values))
        return result.inserted_primary_key[0]  # type: ignore[index]


async def _archive(engine: AsyncEngine, message_id: int, compaction_id: int) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            messages.update()
            .where(messages.c.id == message_id)
            .values(compaction_id=compaction_id)
        )


async def _insert_compaction(
    engine: AsyncEngine,
    chat_id: int,
    *,
    summary: str,
    anchor_msg_id: int,
    created_at: datetime | None = None,
) -> int:
    values: dict[str, Any] = {
        "chat_id": chat_id,
        "summary": summary,
        "anchor_msg_id": anchor_msg_id,
        "original_token_count": 500,
        "summary_token_count": 50,
    }
    if created_at is not None:
        values["created_at"] = created_at
    async with engine.begin() as conn:
        result = await conn.execute(compactions.insert().values(**values))
        return result.inserted_primary_key[0]  # type: ignore[index]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = await _make_engine()
    yield eng
    await eng.dispose()


# ---------------------------------------------------------------------------
# 1. Context exclusion + summary injection in span order (replay history list)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_history_excludes_archived_and_injects_summaries_in_order(
    engine: AsyncEngine,
) -> None:
    """Archived rows are excluded; summaries are injected in anchor order.

    Two compaction spans, each archiving an older pair of turns, plus one
    surviving active pair. The loaded history must:
    - NOT contain any archived turn's content.
    - Contain both summaries, each as a role="system" synthetic message.
    - Order: summary #1, summary #2, then the active turns — matching
      anchor_msg_id (span) order, not insertion order.
    """
    chat_id = await _insert_chat(engine, settings={"provider": "openrouter"})

    # Span 1 (oldest).
    m1 = await _insert_message(
        engine, chat_id, role="user", content="ARCHIVED question one", created_at=_t(0)
    )
    m2 = await _insert_message(
        engine,
        chat_id,
        role="assistant",
        content="ARCHIVED answer one",
        response_id="rid-1",
        created_at=_t(1),
    )
    # Span 2 (middle).
    m3 = await _insert_message(
        engine, chat_id, role="user", content="ARCHIVED question two", created_at=_t(2)
    )
    m4 = await _insert_message(
        engine,
        chat_id,
        role="assistant",
        content="ARCHIVED answer two",
        response_id="rid-2",
        created_at=_t(3),
    )
    # Surviving active pair.
    await _insert_message(
        engine, chat_id, role="user", content="active question", created_at=_t(4)
    )
    await _insert_message(
        engine,
        chat_id,
        role="assistant",
        content="active answer",
        response_id="rid-3",
        created_at=_t(5),
    )

    cid1 = await _insert_compaction(
        engine,
        chat_id,
        summary="SUMMARY of span one",
        anchor_msg_id=m1,
        created_at=_t(10),
    )
    cid2 = await _insert_compaction(
        engine,
        chat_id,
        summary="SUMMARY of span two",
        anchor_msg_id=m3,
        created_at=_t(11),
    )
    await _archive(engine, m1, cid1)
    await _archive(engine, m2, cid1)
    await _archive(engine, m3, cid2)
    await _archive(engine, m4, cid2)

    captured: dict[str, Any] = {}
    lm_client = MagicMock()
    lm_client.stream = MagicMock(
        side_effect=AssertionError("lm_client.stream must NOT be called in replay mode")
    )
    replay_provider = _make_stub_provider("replay", captured=captured)
    registry = _make_registry(replay_provider, "openrouter")
    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    svc = StreamingService(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        chat_locks={},
        provider_registry=registry,
    )

    await _drain(
        svc.stream_chat(
            chat_id=chat_id,
            user=_mock_user(1),
            payload=_make_payload(chat_text="brand new question"),
            request=_mock_request(),
        )
    )

    history = captured.get("history")
    assert history is not None, "history must be loaded for replay mode"
    contents = [m.content for m in history]

    # No archived content anywhere in history.
    for archived_text in (
        "ARCHIVED question one",
        "ARCHIVED answer one",
        "ARCHIVED question two",
        "ARCHIVED answer two",
    ):
        assert not any(archived_text in (c or "") for c in contents), (
            f"Archived content leaked into history: {archived_text!r} in {contents}"
        )

    # Both summaries present, each as a synthetic system-role message.
    summary_entries = [m for m in history if m.role == "system"]
    summary_texts = [m.content or "" for m in summary_entries]
    assert any("SUMMARY of span one" in t for t in summary_texts), summary_texts
    assert any("SUMMARY of span two" in t for t in summary_texts), summary_texts

    # Span order: summary one before summary two, both before the active pair.
    idx_summary1 = next(
        i for i, m in enumerate(history) if "SUMMARY of span one" in (m.content or "")
    )
    idx_summary2 = next(
        i for i, m in enumerate(history) if "SUMMARY of span two" in (m.content or "")
    )
    idx_active_q = next(i for i, m in enumerate(history) if m.content == "active question")
    idx_active_a = next(i for i, m in enumerate(history) if m.content == "active answer")

    assert idx_summary1 < idx_summary2 < idx_active_q < idx_active_a, (
        f"Expected span order summary1 < summary2 < active turns, got indices "
        f"{idx_summary1}, {idx_summary2}, {idx_active_q}, {idx_active_a} "
        f"in {[(m.role, m.content) for m in history]}"
    )


# ---------------------------------------------------------------------------
# 2. Chain-reset backstop — the trap: anchor is the latest KEPT (non-
#    archived) assistant turn, but it predates the chat's latest compaction.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backstop_drops_stale_rid_when_anchor_predates_latest_compaction(
    engine: AsyncEngine,
) -> None:
    """The correctness lynchpin.

    The FE's cached previous_response_id points at the latest KEPT assistant
    turn (not archived — "is the anchor archived?" alone would say "safe to
    resume"). But that turn was created BEFORE the chat's latest compaction
    ran, so LM Studio's server-side chain for that response_id still carries
    the full pre-compaction history. The BE must drop the rid and force
    replay, sending summary + active turns only.
    """
    chat_id = await _insert_chat(engine, settings={})  # chain mode (no provider)

    m1 = await _insert_message(
        engine, chat_id, role="user", content="ARCHIVED old question", created_at=_t(0)
    )
    m2 = await _insert_message(
        engine,
        chat_id,
        role="assistant",
        content="ARCHIVED old answer",
        response_id="rid-old",
        created_at=_t(1),
    )
    await _insert_message(
        engine, chat_id, role="user", content="recent kept question", created_at=_t(2)
    )
    await _insert_message(
        engine,
        chat_id,
        role="assistant",
        content="recent kept answer",
        response_id="rid-recent",
        created_at=_t(3),
    )

    # Compact ran AFTER m4 was created (t=3) — the FE's cached rid at that
    # moment was "rid-recent" (m4, the latest kept assistant turn).
    cid = await _insert_compaction(
        engine,
        chat_id,
        summary="SUMMARY of the archived opening",
        anchor_msg_id=m1,
        created_at=_t(10),
    )
    await _archive(engine, m1, cid)
    await _archive(engine, m2, cid)
    # m4 (the anchor) is explicitly NOT archived — it is the latest kept turn.

    captured: dict[str, Any] = {}

    async def _fake_stream(**kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        captured.update(kwargs)
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
        models_service=_make_models_service(),
    )

    # The FE sends the stale (pre-compaction) rid it still has cached.
    payload = _make_payload(previous_response_id="rid-recent")

    frames = await _drain(
        svc.stream_chat(
            chat_id=chat_id,
            user=_mock_user(1),
            payload=payload,
            request=_mock_request(),
        )
    )

    parsed = _parse_frames(frames)
    assert any(d.get("type") == "chat.end" for d in parsed), (
        f"Stream must complete. Events: {[d.get('type') for d in parsed]}"
    )
    assert "request" in captured, "lm_client.stream was not called"
    wire_req: CanonicalChatRequest = captured["request"]

    # The backstop must have dropped the stale rid.
    assert wire_req.previous_response_id is None, (
        f"Backstop must force previous_response_id=None when the anchor "
        f"predates the latest compaction. Got {wire_req.previous_response_id!r}"
    )

    sys_p = wire_req.system_prompt or ""
    assert "## Prior turns" in sys_p, (
        f"Forced replay must compose prior turns into system_prompt. Got: {sys_p!r}"
    )
    # Summary present, archived raw content absent, active content present.
    assert "SUMMARY of the archived opening" in sys_p, sys_p
    assert "ARCHIVED old question" not in sys_p, sys_p
    assert "ARCHIVED old answer" not in sys_p, sys_p
    assert "recent kept question" in sys_p, sys_p
    assert "recent kept answer" in sys_p, sys_p


# ---------------------------------------------------------------------------
# 3. Backstop is a no-op for a normal chat with no compactions at all.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backstop_byte_identical_when_no_compactions(engine: AsyncEngine) -> None:
    """No compactions exist → the incoming previous_response_id passes through
    unchanged and no history is composed, exactly like the pre-compaction path
    (mirrors test_streaming_chain_tool_turns.test_chain_no_integrations_followup_unchanged).
    """
    chat_id = await _insert_chat(engine, settings={})

    await _insert_message(
        engine, chat_id, role="user", content="prior question", created_at=_t(0)
    )
    await _insert_message(
        engine,
        chat_id,
        role="assistant",
        content="prior answer",
        response_id="rid-prior",
        created_at=_t(1),
    )
    # No compactions row inserted at all for this chat.

    captured: dict[str, Any] = {}

    async def _fake_stream(**kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        captured.update(kwargs)
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
        models_service=_make_models_service(),
    )

    payload = _make_payload(previous_response_id="rid-prior")

    frames = await _drain(
        svc.stream_chat(
            chat_id=chat_id,
            user=_mock_user(1),
            payload=payload,
            request=_mock_request(),
        )
    )

    parsed = _parse_frames(frames)
    assert any(d.get("type") == "chat.end" for d in parsed), (
        f"Stream must complete. Events: {[d.get('type') for d in parsed]}"
    )
    assert "request" in captured
    wire_req: CanonicalChatRequest = captured["request"]

    assert wire_req.previous_response_id == "rid-prior", (
        f"No compactions exist — previous_response_id must pass through "
        f"unchanged. Got {wire_req.previous_response_id!r}"
    )
    sys_p = wire_req.system_prompt or ""
    assert "## Prior turns" not in sys_p, (
        f"No history should be composed when the chain is intact and there "
        f"are no compactions. Got: {sys_p!r}"
    )


# ---------------------------------------------------------------------------
# 4. Backstop also fires for the simpler condition (anchor itself archived),
#    and the forced-replay path never leaks archived rows onto the wire.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backstop_drops_rid_when_anchor_itself_archived(engine: AsyncEngine) -> None:
    """A stale rid pointing directly at an archived message is dropped too.

    Simpler than the predates-latest-compaction trap (test above), but part
    of the same backstop — and a good vector to reconfirm the forced-replay
    path sends summary + active only, never the raw archived rows.
    """
    chat_id = await _insert_chat(engine, settings={})

    m1 = await _insert_message(
        engine, chat_id, role="user", content="ARCHIVED question", created_at=_t(0)
    )
    m2 = await _insert_message(
        engine,
        chat_id,
        role="assistant",
        content="ARCHIVED answer",
        response_id="rid-archived",
        created_at=_t(1),
    )
    await _insert_message(
        engine, chat_id, role="user", content="active question", created_at=_t(2)
    )
    await _insert_message(
        engine,
        chat_id,
        role="assistant",
        content="active answer",
        response_id="rid-active",
        created_at=_t(3),
    )

    cid = await _insert_compaction(
        engine,
        chat_id,
        summary="SUMMARY block",
        anchor_msg_id=m1,
        created_at=_t(10),
    )
    await _archive(engine, m1, cid)
    await _archive(engine, m2, cid)

    captured: dict[str, Any] = {}

    async def _fake_stream(**kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        captured.update(kwargs)
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
        models_service=_make_models_service(),
    )

    # Client sends the (now-archived) anchor's rid directly.
    payload = _make_payload(previous_response_id="rid-archived")

    frames = await _drain(
        svc.stream_chat(
            chat_id=chat_id,
            user=_mock_user(1),
            payload=payload,
            request=_mock_request(),
        )
    )

    parsed = _parse_frames(frames)
    assert any(d.get("type") == "chat.end" for d in parsed)
    wire_req: CanonicalChatRequest = captured["request"]

    assert wire_req.previous_response_id is None, (
        f"An anchor pointing at an archived message must be dropped. "
        f"Got {wire_req.previous_response_id!r}"
    )
    sys_p = wire_req.system_prompt or ""
    assert "SUMMARY block" in sys_p
    assert "ARCHIVED question" not in sys_p
    assert "ARCHIVED answer" not in sys_p
    assert "active question" in sys_p
    assert "active answer" in sys_p


# ---------------------------------------------------------------------------
# 5. Orphaned-injected relocation must exclude archived injected messages.
#    (Second content path — assistant rows with response_id IS NULL that are
#    relocated into input[0] via relocate_per_turn_layers on chain-mode turns.)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orphaned_injected_relocation_excludes_archived(engine: AsyncEngine) -> None:
    """Second content path: an ARCHIVED injected assistant message
    (``response_id IS NULL`` + ``compaction_id`` set) must NOT be relocated
    back into the model's ``input`` on a chain-mode follow-up, while a
    NON-archived injected message still is.

    Isolation: a valid, non-stale ``previous_response_id`` is supplied and
    kept by the backstop (its anchor is newer than the compaction), so the
    history-composition path is skipped — the orphaned-injected
    relocation is the ONLY path that could surface the injected content.
    """
    chat_id = await _insert_chat(engine, settings={})  # chain mode

    # A plain (non-injected) prior user turn.
    await _insert_message(
        engine, chat_id, role="user", content="some question", created_at=_t(0)
    )
    # The chain anchor: a chained assistant turn (response_id set), NOT
    # archived, created AFTER the compaction so the backstop keeps its rid.
    await _insert_message(
        engine,
        chat_id,
        role="assistant",
        content="chained answer",
        response_id="rid-anchor",
        created_at=_t(5),
    )
    # An ARCHIVED injected assistant message (response_id NULL, compaction set).
    m_archived_injected = await _insert_message(
        engine,
        chat_id,
        role="assistant",
        content="ARCHIVED_INJECTED_MARKER",
        response_id=None,
        created_at=_t(6),
    )
    # A NON-archived injected assistant message (response_id NULL, active).
    await _insert_message(
        engine,
        chat_id,
        role="assistant",
        content="ACTIVE_INJECTED_MARKER",
        response_id=None,
        created_at=_t(7),
    )

    # Compaction older than the anchor turn — anchor rid must survive the
    # backstop so the history-composition path stays dormant.
    cid = await _insert_compaction(
        engine,
        chat_id,
        summary="prior span summary",
        anchor_msg_id=m_archived_injected,
        created_at=_t(1),
    )
    await _archive(engine, m_archived_injected, cid)

    captured: dict[str, Any] = {}

    async def _fake_stream(**kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        captured.update(kwargs)
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
        models_service=_make_models_service(),
    )

    payload = _make_payload(previous_response_id="rid-anchor")

    frames = await _drain(
        svc.stream_chat(
            chat_id=chat_id,
            user=_mock_user(1),
            payload=payload,
            request=_mock_request(),
        )
    )

    parsed = _parse_frames(frames)
    assert any(d.get("type") == "chat.end" for d in parsed), (
        f"Stream must complete. Events: {[d.get('type') for d in parsed]}"
    )
    wire_req: CanonicalChatRequest = captured["request"]

    # The backstop kept the rid (anchor is newer than the compaction), so the
    # history-composition path is dormant — this proves the assertions below
    # exercise the orphaned-relocation path, not history composition.
    assert wire_req.previous_response_id == "rid-anchor", (
        f"Anchor is newer than the compaction — rid must be kept so this test "
        f"isolates the orphaned-relocation path. Got {wire_req.previous_response_id!r}"
    )

    # Combined outbound surface = system_prompt + every input block's text.
    input_text = " ".join(
        blk.content or "" for blk in wire_req.input if blk.type == "text"
    )
    outbound = (wire_req.system_prompt or "") + "\n" + input_text

    # The NON-archived injected message is relocated into input as before.
    assert "ACTIVE_INJECTED_MARKER" in outbound, (
        f"Non-archived injected message must still be relocated into the "
        f"model's context. Outbound: {outbound!r}"
    )
    # The ARCHIVED injected message must NEVER reach the model.
    assert "ARCHIVED_INJECTED_MARKER" not in outbound, (
        f"Archived injected message leaked into the model's context via the "
        f"orphaned-relocation path — compaction defeated. Outbound: {outbound!r}"
    )
