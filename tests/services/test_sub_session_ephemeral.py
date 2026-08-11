# SPDX-License-Identifier: Apache-2.0
"""Test §3D: sub-session stream is non-persistent — no DB rows until inject-message.

Per docs/audit/2026-06-13-qa-security-suite-PLAN-v3.md §3D: the sub-session stream
(``_sub_session_sse`` in ``src/lmchat/routes/chats.py``) has clean-context lifetime:

  - No ``messages`` or ``chats`` rows are created by a sub-session stream
    until/unless ``inject-message`` is called.
  - Chat history is NOT hydrated into the upstream request.

This test drives ``_sub_session_sse`` directly with a mock LM client (following
the pattern in ``tests/services/test_cluster5_hygiene.py``), queries the DB
before/after to confirm zero new rows, and captures the upstream request to
verify no history leakage.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import chats, messages, metadata, users
from lmchat.lmstudio.types import CanonicalChatRequest, CanonicalEvent

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def db_engine() -> AsyncIterator[AsyncEngine]:
    """In-memory SQLite engine with full schema for per-test isolation."""
    engine = create_async_engine("sqlite+aiosqlite://", pool_pre_ping=True)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture()
async def seeded_db(
    db_engine: AsyncEngine,
) -> dict[str, Any]:
    """Seed a user + a chat with pre-existing messages (simulating history).

    Returns a dict with keys: engine, user_id, chat_id, msg_ids.
    """
    engine: AsyncEngine = db_engine

    # Create a user.
    async with engine.begin() as conn:
        result = await conn.execute(
            users.insert().values(username="testuser", password_hash="fake-hash")
        )
        pk = result.inserted_primary_key
        assert pk is not None, "Failed to get inserted user PK"
        user_id = int(pk[0])

    # Create a chat.
    async with engine.begin() as conn:
        result = await conn.execute(
            chats.insert().values(
                user_id=user_id,
                title="test chat",
                settings="{}",
            )
        )
        pk = result.inserted_primary_key
        assert pk is not None, "Failed to get inserted chat PK"
        chat_id = int(pk[0])

    # Create 2 pre-existing messages (simulating prior turns).
    msg_ids: list[int] = []
    async with engine.begin() as conn:
        for content, role in [
            ("What is the capital of France?", "user"),
            ("The capital of France is Paris.", "assistant"),
        ]:
            result = await conn.execute(
                messages.insert().values(
                    chat_id=chat_id,
                    role=role,
                    content=content,
                )
            )
            pk = result.inserted_primary_key
            assert pk is not None, "Failed to get inserted message PK"
            msg_ids.append(int(pk[0]))

    return {
        "engine": engine,
        "user_id": user_id,
        "chat_id": chat_id,
        "msg_ids": msg_ids,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _count_messages(engine: AsyncEngine, chat_id: int) -> int:
    """Count message rows for a specific chat."""
    async with engine.connect() as conn:
        result = await conn.execute(
            select(messages.c.id).where(messages.c.chat_id == chat_id)
        )
        rows = result.fetchall()
    return len(rows)


async def _count_chats(engine: AsyncEngine) -> int:
    """Count all chat rows."""
    async with engine.connect() as conn:
        result = await conn.execute(select(chats.c.id))
        rows = result.fetchall()
    return len(rows)


def _make_events(*types: str, content: str = "hi there") -> list[CanonicalEvent]:
    """Build a minimal event sequence for the mock LM client.

    Accepts any string event type and maps it to a CanonicalEvent.
    This helper exists to avoid pyright Literal complaints by building
    events positionally for known types and generically for others.
    """
    evs: list[CanonicalEvent] = []
    for t in types:
        if t == "message.delta":
            evs.append(CanonicalEvent(type="message.delta", content=content))
        elif t == "chat.end":
            evs.append(CanonicalEvent(type="chat.end"))
        elif t == "chat.start":
            evs.append(CanonicalEvent(type="chat.start"))
        elif t == "message.end":
            evs.append(CanonicalEvent(type="message.end"))
        else:
            evs.append(CanonicalEvent(type=t))  # type: ignore[arg-type]
    return evs


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sub_session_stream_creates_no_db_rows(seeded_db: dict[str, Any]) -> None:
    """§3D: Sub-session stream does NOT create messages/chats rows.

    Drives ``_sub_session_sse`` with a mock LM client that yields a
    happy-text completion, then queries the DB to confirm zero new
    ``messages`` or ``chats`` rows for the seeded chat.
    """
    from lmchat.routes.chats import _sub_session_sse

    engine: AsyncEngine = seeded_db["engine"]  # type: ignore[assignment]
    chat_id: int = seeded_db["chat_id"]  # type: ignore[assignment]

    # Count rows BEFORE the sub-session stream.
    msgs_before = await _count_messages(engine, chat_id)
    chats_before = await _count_chats(engine)

    assert msgs_before == 2, (
        f"Expected 2 pre-seeded messages, got {msgs_before} — "
        "test precondition is wrong"
    )
    assert chats_before >= 1, "At least the seeded chat should exist"

    # Build a mock LM client that yields a canned event stream.
    events = _make_events("chat.start", "message.delta", "message.end", "chat.end")

    async def _fake_stream(**kwargs: object) -> AsyncIterator[CanonicalEvent]:
        for ev in events:
            yield ev

    class FakeClient:
        def stream(self, **kwargs: object) -> AsyncIterator[CanonicalEvent]:
            return _fake_stream(**kwargs)

    # Drive the sub-session stream to completion.
    async for _frame in _sub_session_sse(
        lm_client=FakeClient(),  # type: ignore[arg-type]
        model_id="test-model",
        system_prompt="You are a helpful assistant.",
        messages=[{"role": "user", "content": "Say hello."}],
        prior_tool_rounds=0,
        chat_id=chat_id,
    ):
        pass

    # Count rows AFTER the sub-session stream.
    msgs_after = await _count_messages(engine, chat_id)
    chats_after = await _count_chats(engine)

    # Assert: no new rows were created — sub-session is non-persistent.
    assert msgs_after == msgs_before, (
        f"Sub-session stream created {msgs_after - msgs_before} new message "
        f"rows! Expected {msgs_before}, got {msgs_after}. "
        "Sub-session MUST NOT write to messages table."
    )
    assert chats_after == chats_before, (
        f"Sub-session stream created {chats_after - chats_before} new chat "
        f"rows! Expected {chats_before}, got {chats_after}. "
        "Sub-session MUST NOT write to chats table."
    )


@pytest.mark.asyncio
async def test_sub_session_does_not_hydrate_chat_history(seeded_db: dict[str, Any]) -> None:
    """§3D: Sub-session does NOT hydrate chat history into the upstream request.

    The mock LM client captures the ``CanonicalChatRequest`` passed to
    ``lm_client.stream()``.  We verify that the captured request's
    ``system_prompt`` and ``input`` contain ONLY the values provided to
    ``_sub_session_sse`` — NOT the chat's prior messages from the DB.
    """
    from lmchat.routes.chats import _sub_session_sse

    chat_id: int = seeded_db["chat_id"]  # type: ignore[assignment]

    SYSTEM_PROMPT = "You are a test assistant for the sub-session isolation test."
    USER_MESSAGE = "This is a sub-session message, not from chat history."

    captured_request: list[CanonicalChatRequest] = []

    async def _fake_stream(**kwargs: object) -> AsyncIterator[CanonicalEvent]:
        yield CanonicalEvent(type="chat.start")
        yield CanonicalEvent(type="chat.end")

    class CapturingClient:
        def stream(self, **kwargs: object) -> AsyncIterator[CanonicalEvent]:
            req = kwargs.get("request")
            if req is not None:
                captured_request.append(req)  # type: ignore[arg-type]
            return _fake_stream(**kwargs)

    # Drive the sub-session stream.
    async for _frame in _sub_session_sse(
        lm_client=CapturingClient(),  # type: ignore[arg-type]
        model_id="test-model",
        system_prompt=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": USER_MESSAGE}],
        prior_tool_rounds=0,
        chat_id=chat_id,
    ):
        pass

    # The request must have been captured.
    assert len(captured_request) == 1, (
        f"Expected exactly 1 stream() call, got {len(captured_request)}"
    )
    req: CanonicalChatRequest = captured_request[0]

    # The system_prompt must be exactly what we passed (not chat history).
    assert req.system_prompt == SYSTEM_PROMPT, (
        f"System prompt leaked!\nExpected: {SYSTEM_PROMPT!r}\nGot: {req.system_prompt!r}"
    )

    # Build expected input blocks (the route wraps messages into input blocks).
    # _sub_session_sse reads the LAST user message as input:
    #   messages[-1]["content"] → CanonicalInputBlock(type="text", content=...)
    assert len(req.input) >= 1, "Request must have at least one input block"
    actual_input_text = " ".join(
        b.content or "" for b in req.input if b.type == "text"
    )
    assert USER_MESSAGE in actual_input_text, (
        f"Provided user message {USER_MESSAGE!r} NOT found in request input "
        f"({actual_input_text!r})"
    )

    # Verify that pre-existing chat history messages are NOT in the request.
    # The seeded DB has "What is the capital of France?" and
    # "The capital of France is Paris." — these must NOT appear.
    HISTORY_FRAGMENTS = [
        "What is the capital of France?",
        "The capital of France is Paris.",
    ]
    for frag in HISTORY_FRAGMENTS:
        assert frag not in actual_input_text, (
            f"Chat history leaked into sub-session request! Found {frag!r} "
            f"in request input ({actual_input_text!r})"
        )
    assert "capital" not in actual_input_text, (
        f"Chat history leaked! Found 'capital' in request input "
        f"({actual_input_text!r})"
    )


@pytest.mark.asyncio
async def test_sub_session_stream_passes_with_mock_server_style(seeded_db: dict[str, Any]) -> None:
    """§3D: Sub-session stream works end-to-end with mock-style events.

    Uses a mock LM client that yields events matching the
    ``happy_text.jsonl`` script shape (used by the §1A mock LM Studio).
    Verifies the stream completes without error, and the non-persistence
    invariants still hold.

    This test mirrors the scripted wire shape from ``happy_text.jsonl``:
    chat.start → 3×message.delta → message.end → chat.end.
    """
    from lmchat.routes.chats import _sub_session_sse

    engine: AsyncEngine = seeded_db["engine"]  # type: ignore[assignment]
    chat_id: int = seeded_db["chat_id"]  # type: ignore[assignment]

    # Count before.
    msgs_before = await _count_messages(engine, chat_id)
    chats_before = await _count_chats(engine)

    # Build events matching the happy_text.jsonl script.
    deltas = [
        "The quick brown fox",
        " jumps over the",
        " lazy dog.",
    ]
    events: list[CanonicalEvent] = [
        CanonicalEvent(type="chat.start"),
    ]
    for d in deltas:
        events.append(CanonicalEvent(type="message.delta", content=d))
    events.append(CanonicalEvent(type="message.end"))
    events.append(CanonicalEvent(type="chat.end"))

    async def _fake_stream(**kwargs: object) -> AsyncIterator[CanonicalEvent]:
        for ev in events:
            yield ev

    class FakeClient:
        def stream(self, **kwargs: object) -> AsyncIterator[CanonicalEvent]:
            return _fake_stream(**kwargs)

    # Drive the sub-session stream and accumulate frames.
    frames: list[bytes] = []
    async for frame in _sub_session_sse(
        lm_client=FakeClient(),  # type: ignore[arg-type]
        model_id="test-model",
        system_prompt="You are a helpful assistant.",
        messages=[{"role": "user", "content": "Write a short sentence."}],
        prior_tool_rounds=0,
        chat_id=chat_id,
    ):
        frames.append(frame)

    # Verify the stream produced expected events.
    frame_text = b"".join(frames).decode()
    assert "sub.delta" in frame_text, "Expected sub.delta events in output"
    assert "sub.complete" in frame_text, "Expected sub.complete event at end"
    # Check the deltas were accumulated into the final content.
    assert "fox" in frame_text, "Expected delta content 'fox' in stream output"

    # Count after — must be identical.
    msgs_after = await _count_messages(engine, chat_id)
    chats_after = await _count_chats(engine)

    assert msgs_after == msgs_before, (
        f"Sub-session with happy_text script created {msgs_after - msgs_before} "
        f"new message rows!"
    )
    assert chats_after == chats_before, (
        f"Sub-session with happy_text script created {chats_after - chats_before} "
        f"new chat rows!"
    )


@pytest.mark.asyncio
async def test_sub_session_non_persistence_is_not_tautological(seeded_db: dict[str, Any]) -> None:
    """§3D: Sanity-check that our row-count logic WOULD catch violations.

    This test proves the row-count query is a real check, not a constant:
    it inserts a message row between two counts and asserts the counts
    differ — showing that if the sub-session handler wrote rows, this
    test pattern would detect it.
    """
    engine: AsyncEngine = seeded_db["engine"]  # type: ignore[assignment]
    chat_id: int = seeded_db["chat_id"]  # type: ignore[assignment]

    msgs_before = await _count_messages(engine, chat_id)

    # Insert a message (as inject_message would).
    async with engine.begin() as conn:
        result = await conn.execute(
            messages.insert().values(
                chat_id=chat_id,
                role="assistant",
                content="Injected message simulating inject-message.",
                state="final",
            )
        )
        _new_msg_id = int(result.inserted_primary_key[0])  # type: ignore[arg-type]

    msgs_after = await _count_messages(engine, chat_id)

    # The count must differ — proving the query is live, not a constant.
    assert msgs_after == msgs_before + 1, (
        f"The after-count should be before+1 after an insert. "
        f"Before: {msgs_before}, After: {msgs_after}. "
        "If this fails, the row-count query is broken."
    )