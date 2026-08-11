# SPDX-License-Identifier: Apache-2.0
"""Contract tests for ChatService.generate_title.

Covers AC1-AC9 per spec A-autotitle-verify.md.

Each test gets a fresh per-test SQLite engine with schema bootstrapped via
metadata.create_all and a minimal user row for FK satisfaction.

Fixture pattern mirrors tests/services/test_chat_service.py with in-process
sqlite engine. Mock http_client with a MagicMock whose .post returns an awaitable
shaped like httpx.Response (exposes .status_code, .text, .json(), .raise_for_status()).
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.pragmas import apply_sqlite_pragmas
from lmchat.db.schema import audit_log, chats, metadata
from lmchat.services.chat_service import (
    ChatService,
    TitleGenerationError,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    """Yield a fresh per-test SQLite engine with the full schema created."""
    db_path = tmp_path / "test_autotitle.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}", pool_pre_ping=True)

    @event.listens_for(eng.sync_engine, "connect")
    def _on_connect(dbapi_conn: object, _rec: object) -> None:
        apply_sqlite_pragmas(dbapi_conn)

    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


async def _insert_user(
    engine: AsyncEngine, user_id: int = 1, username: str = "alice"
) -> None:
    """Insert a minimal user row for FK constraint satisfaction."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO users (id, username, password_hash)"
                " VALUES (:id, :u, :ph)"
            ),
            {"id": user_id, "u": username, "ph": "scrypt$dummy"},
        )


def _make_service(engine: AsyncEngine) -> ChatService:
    """Build a ChatService with mocked memory and models services."""
    memory_mock = MagicMock()
    memory_mock.handle_message_deleted = AsyncMock(return_value=None)
    models_mock = MagicMock()
    models_mock.get_capabilities = AsyncMock(side_effect=KeyError("no model"))
    return ChatService(
        engine=engine,
        memory_service=memory_mock,
        models_service=models_mock,
        chat_locks={},
    )


async def _insert_chat(
    engine: AsyncEngine,
    user_id: int,
    title: str = "New Chat",
) -> int:
    """Insert a chat row and return its id."""
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "INSERT INTO chats (user_id, title) VALUES (:uid, :title)"
            ),
            {"uid": user_id, "title": title},
        )
        return result.lastrowid  # type: ignore[return-value]


async def _insert_assistant_message(
    engine: AsyncEngine, chat_id: int, content: str = "Hello!", model_id: str = "llama3"
) -> int:
    """Insert an assistant message row and return its id."""
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "INSERT INTO messages (chat_id, role, content, model_id, state)"
                " VALUES (:cid, :role, :content, :model_id, :state)"
            ),
            {"cid": chat_id, "role": "assistant", "content": content, "model_id": model_id, "state": "final"},
        )
        return result.lastrowid  # type: ignore[return-value]


async def _insert_user_message(
    engine: AsyncEngine, chat_id: int, content: str = "Hi"
) -> int:
    """Insert a user message row and return its id."""
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "INSERT INTO messages (chat_id, role, content, state)"
                " VALUES (:cid, :role, :content, :state)"
            ),
            {"cid": chat_id, "role": "user", "content": content, "state": "final"},
        )
        return result.lastrowid  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class MockHttpResponse:
    """Mock httpx.Response-like object."""

    def __init__(
        self, status_code: int, json_payload: dict | str, text_content: str | None = None
    ) -> None:
        self.status_code = status_code
        self.text = text_content or str(json_payload)
        self._json_payload = json_payload

    def json(self) -> dict | str:  # type: ignore[override]
        return self._json_payload

    def raise_for_status(self) -> None:
        if self.status_code != 200:
            raise httpx.HTTPError(f"HTTP {self.status_code}")


# ---------------------------------------------------------------------------
# AC1-AC9 tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_AC1_happy_path_generates_and_saves_title(
    engine: AsyncEngine,
) -> None:
    """AC1 happy path → row updated, audit row written with event="chat.renamed", detail.auto_generated=True."""
    await _insert_user(engine, user_id=1, username="alice")
    chat_id = await _insert_chat(engine, user_id=1, title="New Chat")
    await _insert_user_message(engine, chat_id=chat_id, content="Hello")
    await _insert_assistant_message(engine, chat_id=chat_id, content="Hi there!")

    mock_response = MockHttpResponse(
        status_code=200,
        json_payload={
            "choices": [{"message": {"content": "Generated Title"}}]
        },
    )
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=mock_response)

    svc = _make_service(engine)
    result = await svc.generate_title(
        chat_id=chat_id,
        user_id=1,
        http_client=mock_client,
        base_url="http://mock",
        fallback_model_id=None,
    )

    assert result == "Generated Title"

    # Verify title was persisted
    async with engine.begin() as conn:
        result = await conn.execute(
            select(chats).where(chats.c.id == chat_id)
        )
        row = result.fetchone()
        assert row is not None
        assert row.title == "Generated Title"

    # Verify audit log entry
    async with engine.begin() as conn:
        result = await conn.execute(
            select(audit_log).where(audit_log.c.event == "chat.renamed")
        )
        rows = result.fetchall()
        assert len(rows) == 1
        # chat_id is in the detail JSON
        assert rows[0].detail is not None
        assert rows[0].detail["chat_id"] == chat_id
        assert rows[0].detail["auto_generated"] is True


@pytest.mark.asyncio
async def test_AC2_transport_error_falls_back_to_user_message(
    engine: AsyncEngine,
) -> None:
    """AC2 (revised 2026-06-24): a transport error (httpx.HTTPError — incl. a
    timeout, the common case for this reasoning model) degrades to a title
    derived from the first user message rather than leaving the chat at
    "New Chat". The failure is logged, not surfaced."""
    await _insert_user(engine, user_id=1, username="alice")
    chat_id = await _insert_chat(engine, user_id=1, title="New Chat")
    await _insert_user_message(engine, chat_id=chat_id, content="Hello")
    await _insert_assistant_message(engine, chat_id=chat_id, content="Hi there!")

    # Simulate httpx.HTTPError raised by .post() call
    mock_client = MagicMock()
    mock_client.post = AsyncMock(side_effect=httpx.HTTPError("Connection refused"))

    svc = _make_service(engine)
    title = await svc.generate_title(
        chat_id=chat_id,
        user_id=1,
        http_client=mock_client,
        base_url="http://mock",
        fallback_model_id=None,
    )
    assert title == "Hello"

    # The fallback title IS persisted.
    async with engine.begin() as conn:
        result = await conn.execute(
            select(chats).where(chats.c.id == chat_id)
        )
        row = result.fetchone()
        assert row is not None
        assert row.title == "Hello"


@pytest.mark.asyncio
async def test_AC3_non_200_status_falls_back_to_user_message(
    engine: AsyncEngine,
) -> None:
    """AC3 (revised 2026-06-24): a non-200 upstream status degrades to a title
    derived from the first user message rather than leaving the chat at
    "New Chat" (the failure is logged)."""
    await _insert_user(engine, user_id=1, username="alice")
    chat_id = await _insert_chat(engine, user_id=1, title="New Chat")
    await _insert_user_message(engine, chat_id=chat_id, content="Hello")
    await _insert_assistant_message(engine, chat_id=chat_id, content="Hi there!")

    mock_response = MockHttpResponse(
        status_code=500,
        json_payload={"detail": "upstream error"},
    )
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=mock_response)

    svc = _make_service(engine)
    title = await svc.generate_title(
        chat_id=chat_id,
        user_id=1,
        http_client=mock_client,
        base_url="http://mock",
        fallback_model_id=None,
    )
    assert title == "Hello"


@pytest.mark.asyncio
async def test_AC4_empty_content_falls_back_to_first_user_message(
    engine: AsyncEngine,
) -> None:
    """AC4 (revised 2026-06-24): empty/whitespace/missing CONTENT (valid shape)
    falls back to a title derived from the first user message rather than
    leaving the chat at "New Chat". A reasoning model can spend its whole token
    budget thinking and return empty content — the chat should still get a
    sensible title."""
    await _insert_user(engine, user_id=1, username="alice")
    chat_id = await _insert_chat(engine, user_id=1, title="New Chat")
    await _insert_user_message(engine, chat_id=chat_id, content="Hello")
    await _insert_assistant_message(engine, chat_id=chat_id, content="Hi there!")

    # Empty choices → no LLM title → fall back to the user message.
    mock_response = MockHttpResponse(
        status_code=200,
        json_payload={"choices": []},
    )
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=mock_response)

    svc = _make_service(engine)
    title = await svc.generate_title(
        chat_id=chat_id,
        user_id=1,
        http_client=mock_client,
        base_url="http://mock",
        fallback_model_id=None,
    )
    assert title == "Hello"


@pytest.mark.asyncio
async def test_AC5_malformed_or_empty_choices_fall_back_to_user_message(
    engine: AsyncEngine,
) -> None:
    """AC5 (revised 2026-06-24): both a structurally malformed response (choices
    not a list) and a well-formed-but-empty one degrade to a title derived from
    the first user message rather than leaving the chat at "New Chat"."""
    await _insert_user(engine, user_id=1, username="alice")
    chat_id = await _insert_chat(engine, user_id=1, title="New Chat")
    await _insert_user_message(engine, chat_id=chat_id, content="Hello")
    await _insert_assistant_message(engine, chat_id=chat_id, content="Hi there!")

    svc = _make_service(engine)

    for payload in ({"choices": "not-a-list"}, {"choices": [{}]}):
        mock_response = MockHttpResponse(status_code=200, json_payload=payload)
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        title = await svc.generate_title(
            chat_id=chat_id,
            user_id=1,
            http_client=mock_client,
            base_url="http://mock",
            fallback_model_id=None,
        )
        assert title == "Hello", f"payload {payload!r} should fall back to user msg"


@pytest.mark.asyncio
async def test_AC6_non_json_body_falls_back_to_user_message(
    engine: AsyncEngine,
) -> None:
    """AC6 (revised 2026-06-24): a non-JSON body (json.JSONDecodeError) degrades
    to a title derived from the first user message rather than leaving the chat
    at "New Chat"."""
    await _insert_user(engine, user_id=1, username="alice")
    chat_id = await _insert_chat(engine, user_id=1, title="New Chat")
    await _insert_user_message(engine, chat_id=chat_id, content="Hello")
    await _insert_assistant_message(engine, chat_id=chat_id, content="Hi there!")

    mock_response = MockHttpResponse(
        status_code=200,
        json_payload="not json at all {{{",
    )
    # Override json() to raise ValueError
    def raise_value_error():
        raise ValueError("Expecting value")
    mock_response.json = raise_value_error

    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=mock_response)

    svc = _make_service(engine)
    title = await svc.generate_title(
        chat_id=chat_id,
        user_id=1,
        http_client=mock_client,
        base_url="http://mock",
        fallback_model_id=None,
    )
    assert title == "Hello"


@pytest.mark.asyncio
async def test_AC7_idempotency_returns_existing_title_when_not_default(
    engine: AsyncEngine,
) -> None:
    """AC7 idempotency: title NOT in _AUTO_TITLE_DEFAULT_VALUES → returns existing title, mock.call_count == 0."""
    await _insert_user(engine, user_id=1, username="alice")
    chat_id = await _insert_chat(engine, user_id=1, title="User Defined Title")
    await _insert_user_message(engine, chat_id=chat_id, content="Hello")
    await _insert_assistant_message(engine, chat_id=chat_id, content="Hi there!")

    mock_response = MockHttpResponse(
        status_code=200,
        json_payload={"choices": [{"message": {"content": "Generated Title"}}]},
    )
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=mock_response)

    svc = _make_service(engine)
    result = await svc.generate_title(
        chat_id=chat_id,
        user_id=1,
        http_client=mock_client,
        base_url="http://mock",
        fallback_model_id=None,
    )

    assert result == "User Defined Title"
    assert mock_client.post.call_count == 0  # No http call made


@pytest.mark.asyncio
async def test_AC8_zero_assistant_messages_raises_TitleGenerationError(
    engine: AsyncEngine,
) -> None:
    """AC8 zero assistant messages → TitleGenerationError, no http call."""
    await _insert_user(engine, user_id=1, username="alice")
    chat_id = await _insert_chat(engine, user_id=1, title="New Chat")
    # Insert only a user message (no assistant messages)
    await _insert_user_message(engine, chat_id=chat_id, content="Hello")

    mock_response = MockHttpResponse(
        status_code=200,
        json_payload={"choices": [{"message": {"content": "Generated Title"}}]},
    )
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=mock_response)

    svc = _make_service(engine)

    with pytest.raises(TitleGenerationError):
        await svc.generate_title(
            chat_id=chat_id,
            user_id=1,
            http_client=mock_client,
            base_url="http://mock",
            fallback_model_id=None,
        )
    assert mock_client.post.call_count == 0


@pytest.mark.asyncio
async def test_AC9_null_model_id_and_fallback_none_raises_TitleGenerationError(
    engine: AsyncEngine,
) -> None:
    """AC9 null model_id + fallback_model_id is None → TitleGenerationError, no http call."""
    await _insert_user(engine, user_id=1, username="alice")
    chat_id = await _insert_chat(engine, user_id=1, title="New Chat")
    await _insert_user_message(engine, chat_id=chat_id, content="Hello")
    # Insert assistant message with NULL model_id
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO messages (chat_id, role, content, model_id, state)"
                " VALUES (:cid, :role, :content, :model_id, :state)"
            ),
            {"cid": chat_id, "role": "assistant", "content": "Hi there!", "model_id": None, "state": "final"},
        )

    mock_response = MockHttpResponse(
        status_code=200,
        json_payload={"choices": [{"message": {"content": "Generated Title"}}]},
    )
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=mock_response)

    svc = _make_service(engine)

    with pytest.raises(TitleGenerationError):
        await svc.generate_title(
            chat_id=chat_id,
            user_id=1,
            http_client=mock_client,
            base_url="http://mock",
            fallback_model_id=None,
        )
    assert mock_client.post.call_count == 0


# ---------------------------------------------------------------------------
# AC10 — substance_fold salvage-prefix strip
# ---------------------------------------------------------------------------

# Chat title bug: when an assistant turn was salvage-folded
# from reasoning_content (substance_fold), the persisted ``messages.content``
# begins with "_(reasoning surfaced because the model produced no final
# answer)_\n\n" + reasoning text. ``generate_title`` was feeding that verbatim
# to the title model, so chats got titled "_(reasoning surfaced because the
# model produced no final answer)_ User wants a…" instead of a real headline.

_SALVAGE_PREFIX_LITERAL = (
    "_(reasoning surfaced because the model produced no final answer)_"
    "\n\n"
)


@pytest.mark.asyncio
async def test_AC10_pure_salvage_assistant_omitted_from_title_corpus(
    engine: AsyncEngine,
) -> None:
    """Assistant turn that is pure salvage (no real content before the prefix)
    is dropped from the title-call corpus; the title model never sees the
    marker text."""
    await _insert_user(engine, user_id=1, username="alice")
    chat_id = await _insert_chat(engine, user_id=1, title="New Chat")
    await _insert_user_message(engine, chat_id=chat_id, content="how do koalas sleep")
    await _insert_assistant_message(
        engine,
        chat_id=chat_id,
        content=_SALVAGE_PREFIX_LITERAL + "Koalas sleep up to 22 hours a day...",
    )
    # Second assistant turn with real content keeps the title call alive.
    await _insert_user_message(engine, chat_id=chat_id, content="why so much?")
    await _insert_assistant_message(
        engine, chat_id=chat_id, content="Their eucalyptus diet is low-energy."
    )

    mock_response = MockHttpResponse(
        status_code=200,
        json_payload={"choices": [{"message": {"content": "Koala Sleep"}}]},
    )
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=mock_response)

    svc = _make_service(engine)
    result = await svc.generate_title(
        chat_id=chat_id,
        user_id=1,
        http_client=mock_client,
        base_url="http://mock",
        fallback_model_id=None,
    )
    assert result == "Koala Sleep"

    posted = mock_client.post.await_args.kwargs["json"]
    rendered = "\n".join(m["content"] for m in posted["messages"])
    assert "reasoning surfaced" not in rendered, (
        "salvage prefix must not reach the title model"
    )
    assert "22 hours" not in rendered
    assert "eucalyptus" in rendered


@pytest.mark.asyncio
async def test_request_does_not_end_on_assistant_turn(
    engine: AsyncEngine,
) -> None:
    """Regression (2026-06-24): the title request must NOT be a transcript that
    ENDS on an assistant turn — the model then CONTINUES that turn (echoing the
    answer) and ignores the title instruction, producing the answer text as the
    title (e.g. "A vector embedding is a list of numbers that…"). The
    conversation is rendered as TEXT inside a final USER instruction instead."""
    await _insert_user(engine, user_id=1, username="alice")
    chat_id = await _insert_chat(engine, user_id=1, title="New Chat")
    await _insert_user_message(
        engine, chat_id=chat_id, content="What is a vector embedding?"
    )
    await _insert_assistant_message(
        engine,
        chat_id=chat_id,
        content="A vector embedding is a list of numbers representing meaning.",
    )

    mock_response = MockHttpResponse(
        status_code=200,
        json_payload={
            "choices": [{"message": {"content": "Vector Embeddings Explained"}}]
        },
    )
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    svc = _make_service(engine)
    result = await svc.generate_title(
        chat_id=chat_id,
        user_id=1,
        http_client=mock_client,
        base_url="http://mock",
        fallback_model_id=None,
    )
    assert result == "Vector Embeddings Explained"

    msgs = mock_client.post.await_args.kwargs["json"]["messages"]
    assert msgs[-1]["role"] == "user", (
        "request must END on a user instruction, not an assistant turn"
    )
    assert not any(m["role"] == "assistant" for m in msgs), (
        "no assistant-role turn — the answer is embedded as text in the "
        "final user instruction"
    )
    user_instr = msgs[-1]["content"].lower()
    assert "vector embedding" in user_instr  # conversation IS included (as text)
    assert "title" in user_instr  # instruction asks for a title


@pytest.mark.asyncio
async def test_AC10b_mid_content_salvage_keeps_only_pre_prefix_text(
    engine: AsyncEngine,
) -> None:
    """``base + _SALVAGE_PREFIX + reasoning`` → title corpus keeps ``base``
    only; everything from the prefix onward is dropped."""
    await _insert_user(engine, user_id=1, username="alice")
    chat_id = await _insert_chat(engine, user_id=1, title="New Chat")
    await _insert_user_message(engine, chat_id=chat_id, content="explain raft")
    await _insert_assistant_message(
        engine,
        chat_id=chat_id,
        content=(
            "Raft is a consensus algorithm for distributed logs."
            + "\n\n"
            + _SALVAGE_PREFIX_LITERAL
            + "It elects a leader and replicates entries..."
        ),
    )

    mock_response = MockHttpResponse(
        status_code=200,
        json_payload={"choices": [{"message": {"content": "Raft Consensus"}}]},
    )
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=mock_response)

    svc = _make_service(engine)
    result = await svc.generate_title(
        chat_id=chat_id,
        user_id=1,
        http_client=mock_client,
        base_url="http://mock",
        fallback_model_id=None,
    )
    assert result == "Raft Consensus"

    posted = mock_client.post.await_args.kwargs["json"]
    rendered = "\n".join(m["content"] for m in posted["messages"])
    assert "consensus algorithm" in rendered
    assert "reasoning surfaced" not in rendered
    assert "elects a leader" not in rendered


@pytest.mark.asyncio
async def test_AC10c_all_salvage_turns_raise_TitleGenerationError(
    engine: AsyncEngine,
) -> None:
    """All-salvage chats raise TitleGenerationError (same shape as AC8) so
    the FE swallows it and the chat keeps its default title."""
    await _insert_user(engine, user_id=1, username="alice")
    chat_id = await _insert_chat(engine, user_id=1, title="New Chat")
    await _insert_user_message(engine, chat_id=chat_id, content="hi")
    await _insert_assistant_message(
        engine,
        chat_id=chat_id,
        content=_SALVAGE_PREFIX_LITERAL + "thinking out loud only...",
    )

    mock_client = MagicMock()
    mock_client.post = AsyncMock()

    svc = _make_service(engine)
    with pytest.raises(TitleGenerationError):
        await svc.generate_title(
            chat_id=chat_id,
            user_id=1,
            http_client=mock_client,
            base_url="http://mock",
            fallback_model_id=None,
        )
    assert mock_client.post.call_count == 0
