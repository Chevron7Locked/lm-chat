# SPDX-License-Identifier: Apache-2.0
"""Contract tests for MessageService — append, edit, delete.

Tests for the message service.

All tests use a per-test tmp_path SQLite engine with the full schema
bootstrapped via ``alembic upgrade head`` (migration 0001 + 0002) so the
FTS5 triggers are active.  A mock MemoryService is injected to verify the
post-commit notification contract without a live embedding model.

Coverage targets:
- test_append_writes_with_state_final
- test_append_validates_role
- test_append_cross_user_chat_raises_MessageNotFoundError
- test_edit_user_role_message
- test_edit_assistant_role_raises_EditNotAllowedError
- test_delete_cascades_to_message_embeddings
- test_delete_calls_handle_message_deleted_post_commit
- test_delete_writes_audit_log
"""
from __future__ import annotations

import struct
import sys
from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock

import alembic.command
import alembic.config
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import audit_log, message_embeddings, messages
from lmchat.services.memory_service import MemoryService
from lmchat.services.message_service import (
    EditNotAllowedError,
    Message,
    MessageNotFoundError,
    MessageService,
)

# ---------------------------------------------------------------------------
# Ensure migrations/ is importable.
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _alembic_cfg(db_url: str) -> alembic.config.Config:
    """Return an Alembic Config pointing at the repo's alembic.ini."""
    ini = _REPO_ROOT / "alembic.ini"
    cfg = alembic.config.Config(str(ini))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    return cfg


def _run_upgrade(db_url: str) -> None:
    """Run ``alembic upgrade head`` synchronously."""
    alembic.command.upgrade(_alembic_cfg(db_url), "head")


def _pack(vec: list[float]) -> bytes:
    """Pack a float list as little-endian float32 bytes."""
    return struct.pack(f"<{len(vec)}f", *vec)


def _make_memory_service(engine: AsyncEngine) -> MemoryService:
    """Return a MemoryService with all external deps mocked."""
    from unittest.mock import AsyncMock

    from lmchat.embedding.client import EmbeddingClient
    from lmchat.services.models_service import Capabilities, ModelInfo, ModelsService

    mock_embedding_client = AsyncMock(spec=EmbeddingClient)
    mock_models_service = AsyncMock(spec=ModelsService)
    mock_model = ModelInfo(
        key="embed-model",
        type="embedding",
        capabilities=Capabilities(vision=False, trained_for_tool_use=False),
    )
    mock_models_service.list_loaded.return_value = [mock_model]
    return MemoryService(
        engine=engine,
        embedding_client=mock_embedding_client,
        models_service=mock_models_service,
    )


async def _seed_user_and_chat(
    engine: AsyncEngine,
    *,
    user_id: int = 1,
    username: str = "alice",
    chat_id: int = 1,
    title: str = "Test chat",
) -> None:
    """Insert a user and chat row for FK satisfaction."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO users (id, username, password_hash)"
                " VALUES (:id, :u, 'scrypt$dummy')"
            ),
            {"id": user_id, "u": username},
        )
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO chats (id, user_id, title)"
                " VALUES (:cid, :uid, :title)"
            ),
            {"cid": chat_id, "uid": user_id, "title": title},
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    """Yield a fresh per-test SQLite engine with migrations applied.

    Attaches the same PRAGMA hook that ``db/engine.py`` uses in production,
    ensuring ``foreign_keys = ON`` fires for every new DBAPI connection so
    that FK CASCADE constraints are enforced in tests.  Without this SQLite
    silently skips FK enforcement (OFF by default).
    """
    import asyncio

    from sqlalchemy import event as sa_event

    from lmchat.db.pragmas import apply_sqlite_pragmas

    db_path = tmp_path / "test_msg.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"

    await asyncio.to_thread(_run_upgrade, db_url)
    eng = create_async_engine(db_url, pool_pre_ping=True)

    @sa_event.listens_for(eng.sync_engine, "connect")
    def _on_connect(dbapi_conn: object, _record: object) -> None:
        apply_sqlite_pragmas(dbapi_conn)

    yield eng
    await eng.dispose()


@pytest.fixture()
def memory_svc(engine: AsyncEngine) -> MemoryService:
    """Return a mocked MemoryService bound to *engine*."""
    return _make_memory_service(engine)


@pytest.fixture()
def svc(engine: AsyncEngine, memory_svc: MemoryService) -> MessageService:
    """Return a MessageService bound to *engine* and *memory_svc*."""
    return MessageService(engine=engine, memory_service=memory_svc)


# ---------------------------------------------------------------------------
# Tests — append
# ---------------------------------------------------------------------------


async def test_append_writes_with_state_final(
    engine: AsyncEngine, svc: MessageService
) -> None:
    """append() inserts a message with state='final' (the streaming path
    writes 'draft' separately)."""
    await _seed_user_and_chat(engine)

    msg = await svc.append(
        chat_id=1,
        user_id=1,
        role="user",
        content="Hello, world!",
    )

    assert isinstance(msg, Message)
    assert msg.state == "final"
    assert msg.role == "user"
    assert msg.content == "Hello, world!"
    assert msg.chat_id == 1
    assert msg.id > 0
    assert msg.created_at is not None


async def test_list_for_chat_excludes_draft_rows(
    engine: AsyncEngine, svc: MessageService
) -> None:
    """list_for_chat() must NOT return the in-flight DRAFT assistant placeholder.

    Regression: the streaming service inserts an empty
    ``state='draft'`` assistant row before it streams. A mid-stream messages
    refetch — triggered by regenerate/edit/resend query invalidation — that
    surfaced this empty row made the FE treat it as authoritative and suppress
    the live streaming bubble (``persistedHasSameKey``), so the chat appeared
    frozen with no thinking indicator. The listing must skip DRAFT rows so an
    in-flight turn is invisible to a refetch.
    """
    await _seed_user_and_chat(engine)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO messages (chat_id, role, content, state) VALUES"
                " (:c, 'user', 'Hi', 'final'),"
                " (:c, 'assistant', 'Hello!', 'final'),"
                " (:c, 'assistant', '', 'draft')"
            ),
            {"c": 1},
        )

    msgs, _ = await svc.list_for_chat(1, user_id=1)
    states = [m.state for m in msgs]
    contents = [m.content for m in msgs]
    assert "draft" not in states, "DRAFT placeholder must be excluded from the listing"
    assert contents == ["Hi", "Hello!"], "only the two finalised turns are returned"


async def test_list_for_chat_keeps_final_and_aborted_rows(
    engine: AsyncEngine, svc: MessageService
) -> None:
    """Only DRAFT is filtered — final / aborted_by_client rows stay visible."""
    await _seed_user_and_chat(engine)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO messages (chat_id, role, content, state) VALUES"
                " (:c, 'user', 'Q', 'final'),"
                " (:c, 'assistant', 'partial answer', 'aborted_by_client')"
            ),
            {"c": 1},
        )
    msgs, _ = await svc.list_for_chat(1, user_id=1)
    assert {m.state for m in msgs} == {"final", "aborted_by_client"}
    assert any(m.content == "partial answer" for m in msgs)


async def test_append_stores_model_id(
    engine: AsyncEngine, svc: MessageService
) -> None:
    """append() persists model_id when supplied (assistant messages)."""
    await _seed_user_and_chat(engine)

    msg = await svc.append(
        chat_id=1,
        user_id=1,
        role="assistant",
        content="I can help with that.",
        model_id="qwen3-8b",
    )

    assert msg.model_id == "qwen3-8b"


async def test_append_stores_reasoning_content(
    engine: AsyncEngine, svc: MessageService
) -> None:
    """append() persists reasoning_content when supplied."""
    await _seed_user_and_chat(engine)

    msg = await svc.append(
        chat_id=1,
        user_id=1,
        role="assistant",
        content="Final answer.",
        reasoning_content="Let me think step by step ...",
    )

    assert msg.reasoning_content == "Let me think step by step ..."


async def test_append_validates_role(
    engine: AsyncEngine, svc: MessageService
) -> None:
    """append() raises ValueError for an invalid role string."""
    await _seed_user_and_chat(engine)

    with pytest.raises(ValueError, match="Invalid role"):
        await svc.append(
            chat_id=1,
            user_id=1,
            role="superuser",
            content="Bad role.",
        )


async def test_append_cross_user_chat_raises_MessageNotFoundError(
    engine: AsyncEngine, svc: MessageService
) -> None:
    """append() raises MessageNotFoundError when the chat belongs to a different user."""
    # user_id=1 owns chat_id=1; user_id=2 does not.
    await _seed_user_and_chat(engine, user_id=1, chat_id=1)
    await _seed_user_and_chat(engine, user_id=2, username="bob", chat_id=2)

    with pytest.raises(MessageNotFoundError):
        await svc.append(
            chat_id=1,   # belongs to user 1
            user_id=2,   # attacker is user 2
            role="user",
            content="Infiltration attempt.",
        )


async def test_append_writes_audit_log(
    engine: AsyncEngine, svc: MessageService
) -> None:
    """append() writes a message.appended audit_log row."""
    await _seed_user_and_chat(engine)

    await svc.append(
        chat_id=1,
        user_id=1,
        role="user",
        content="Audit me.",
    )

    async with engine.connect() as conn:
        result = await conn.execute(
            select(audit_log).where(audit_log.c.event == "message.appended")
        )
        row = result.fetchone()

    assert row is not None


# ---------------------------------------------------------------------------
# Tests — edit
# ---------------------------------------------------------------------------


async def test_edit_user_role_message(
    engine: AsyncEngine, svc: MessageService
) -> None:
    """edit() updates the content of a user-role message."""
    await _seed_user_and_chat(engine)

    msg = await svc.append(
        chat_id=1, user_id=1, role="user", content="Original text."
    )

    await svc.edit(msg.id, user_id=1, content="Edited text.")

    async with engine.connect() as conn:
        result = await conn.execute(
            select(messages.c.content).where(messages.c.id == msg.id)
        )
        row = result.fetchone()

    assert row is not None
    assert row.content == "Edited text."


async def test_edit_assistant_role_raises_EditNotAllowedError(
    engine: AsyncEngine, svc: MessageService
) -> None:
    """edit() raises EditNotAllowedError when the message role is 'assistant'."""
    await _seed_user_and_chat(engine)

    msg = await svc.append(
        chat_id=1,
        user_id=1,
        role="assistant",
        content="I cannot be changed.",
    )

    with pytest.raises(EditNotAllowedError):
        await svc.edit(msg.id, user_id=1, content="Attempted mutation.")


async def test_edit_nonexistent_message_raises_MessageNotFoundError(
    engine: AsyncEngine, svc: MessageService
) -> None:
    """edit() raises MessageNotFoundError for a missing message_id."""
    await _seed_user_and_chat(engine)

    with pytest.raises(MessageNotFoundError):
        await svc.edit(99999, user_id=1, content="Ghost edit.")


async def test_edit_cross_user_raises_MessageNotFoundError(
    engine: AsyncEngine, svc: MessageService
) -> None:
    """edit() raises MessageNotFoundError when user_id doesn't own the message."""
    await _seed_user_and_chat(engine, user_id=1, chat_id=1)
    await _seed_user_and_chat(engine, user_id=2, username="bob", chat_id=2)

    msg = await svc.append(
        chat_id=1, user_id=1, role="user", content="Alice's message."
    )

    with pytest.raises(MessageNotFoundError):
        await svc.edit(msg.id, user_id=2, content="Bob tries to edit Alice's message.")


async def test_edit_writes_audit_log(
    engine: AsyncEngine, svc: MessageService
) -> None:
    """edit() writes a message.edited audit_log row."""
    await _seed_user_and_chat(engine)

    msg = await svc.append(
        chat_id=1, user_id=1, role="user", content="Before."
    )
    await svc.edit(msg.id, user_id=1, content="After.")

    async with engine.connect() as conn:
        result = await conn.execute(
            select(audit_log).where(audit_log.c.event == "message.edited")
        )
        row = result.fetchone()

    assert row is not None


# ---------------------------------------------------------------------------
# Tests — delete
# ---------------------------------------------------------------------------


async def test_delete_removes_message_row(
    engine: AsyncEngine, svc: MessageService
) -> None:
    """delete() removes the messages row."""
    await _seed_user_and_chat(engine)

    msg = await svc.append(
        chat_id=1, user_id=1, role="user", content="Delete me."
    )

    await svc.delete(msg.id, user_id=1)

    async with engine.connect() as conn:
        result = await conn.execute(
            select(messages.c.id).where(messages.c.id == msg.id)
        )
        row = result.fetchone()

    assert row is None


async def test_delete_cascades_to_message_embeddings(
    engine: AsyncEngine, svc: MessageService
) -> None:
    """delete() removes the message_embeddings row via FK cascade."""
    await _seed_user_and_chat(engine)

    msg = await svc.append(
        chat_id=1, user_id=1, role="user", content="Embedded message."
    )

    # Manually insert an embedding row to verify cascade.
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO message_embeddings"
                " (message_id, embedding_model_id, embedding, text_hash)"
                " VALUES (:mid, 'model-v1', :emb, :th)"
            ),
            {
                "mid": msg.id,
                "emb": _pack([0.1, 0.2, 0.3]),
                "th": "a" * 64,
            },
        )

    await svc.delete(msg.id, user_id=1)

    # FK cascade must have dropped the embedding row.
    async with engine.connect() as conn:
        result = await conn.execute(
            select(message_embeddings.c.message_id).where(
                message_embeddings.c.message_id == msg.id
            )
        )
        row = result.fetchone()

    assert row is None


async def test_delete_calls_handle_message_deleted_post_commit(
    engine: AsyncEngine, svc: MessageService
) -> None:
    """delete() calls memory_service.handle_message_deleted after committing."""
    await _seed_user_and_chat(engine)

    msg = await svc.append(
        chat_id=1, user_id=1, role="user", content="Notify me on delete."
    )

    # Replace handle_message_deleted with a tracking mock.
    svc._memory_service.handle_message_deleted = AsyncMock()  # type: ignore[method-assign]

    await svc.delete(msg.id, user_id=1)

    svc._memory_service.handle_message_deleted.assert_called_once_with(msg.id)


async def test_delete_memory_notify_failure_does_not_reraise(
    engine: AsyncEngine, svc: MessageService
) -> None:
    """delete() swallows handle_message_deleted exceptions so the commit stands."""
    await _seed_user_and_chat(engine)

    msg = await svc.append(
        chat_id=1, user_id=1, role="user", content="Fault-tolerant delete."
    )

    async def _boom(message_id: int) -> None:
        raise RuntimeError("P3 transient failure")

    svc._memory_service.handle_message_deleted = _boom  # type: ignore[method-assign]

    # Should complete without raising, even though notify failed.
    await svc.delete(msg.id, user_id=1)

    # Confirm the message was actually deleted.
    async with engine.connect() as conn:
        result = await conn.execute(
            select(messages.c.id).where(messages.c.id == msg.id)
        )
        row = result.fetchone()

    assert row is None


async def test_delete_writes_audit_log(
    engine: AsyncEngine, svc: MessageService
) -> None:
    """delete() writes a message.deleted audit_log row."""
    await _seed_user_and_chat(engine)

    msg = await svc.append(
        chat_id=1, user_id=1, role="user", content="Audit on delete."
    )
    await svc.delete(msg.id, user_id=1)

    async with engine.connect() as conn:
        result = await conn.execute(
            select(audit_log).where(audit_log.c.event == "message.deleted")
        )
        row = result.fetchone()

    assert row is not None


async def test_delete_nonexistent_message_raises_MessageNotFoundError(
    engine: AsyncEngine, svc: MessageService
) -> None:
    """delete() raises MessageNotFoundError for a missing message_id."""
    await _seed_user_and_chat(engine)

    with pytest.raises(MessageNotFoundError):
        await svc.delete(99999, user_id=1)


async def test_delete_cross_user_raises_MessageNotFoundError(
    engine: AsyncEngine, svc: MessageService
) -> None:
    """delete() raises MessageNotFoundError when user_id doesn't own the message."""
    await _seed_user_and_chat(engine, user_id=1, chat_id=1)
    await _seed_user_and_chat(engine, user_id=2, username="bob", chat_id=2)

    msg = await svc.append(
        chat_id=1, user_id=1, role="user", content="Alice's private message."
    )

    with pytest.raises(MessageNotFoundError):
        await svc.delete(msg.id, user_id=2)


# ---------------------------------------------------------------------------
# list_for_chat pagination tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_list_for_chat_default_limit(
    engine: AsyncEngine, svc: MessageService
) -> None:
    """list_for_chat returns at most 200 messages by default."""
    await _seed_user_and_chat(engine, user_id=1, chat_id=1)
    # Insert 205 messages.
    for i in range(205):
        await svc.append(chat_id=1, user_id=1, role="user", content=f"msg {i}")
    msgs, has_more = await svc.list_for_chat(1, user_id=1)
    assert len(msgs) == 200
    assert has_more is True


@pytest.mark.asyncio()
async def test_list_for_chat_has_more_false_when_under_limit(
    engine: AsyncEngine, svc: MessageService
) -> None:
    """list_for_chat returns has_more=False when total <= limit."""
    await _seed_user_and_chat(engine, user_id=1, chat_id=1)
    for i in range(10):
        await svc.append(chat_id=1, user_id=1, role="user", content=f"msg {i}")
    msgs, has_more = await svc.list_for_chat(1, user_id=1)
    assert len(msgs) == 10
    assert has_more is False


@pytest.mark.asyncio()
async def test_list_for_chat_before_id_cursor(
    engine: AsyncEngine, svc: MessageService
) -> None:
    """list_for_chat with before_id returns older messages, oldest first."""
    await _seed_user_and_chat(engine, user_id=1, chat_id=1)
    appended = []
    for i in range(10):
        m = await svc.append(chat_id=1, user_id=1, role="user", content=f"msg {i}")
        appended.append(m)

    # Ask for messages older than the 6th (index 5), limit=3.
    pivot_id = appended[5].id
    msgs, has_more = await svc.list_for_chat(1, user_id=1, before_id=pivot_id, limit=3)
    # Should get messages at index 2, 3, 4 — all have id < pivot_id.
    assert len(msgs) == 3
    # Oldest first.
    assert msgs[0].id < msgs[1].id < msgs[2].id
    assert all(m.id < pivot_id for m in msgs)
    # has_more because there are msgs at 0 and 1 before these.
    assert has_more is True


@pytest.mark.asyncio()
async def test_list_for_chat_since_id_cursor(
    engine: AsyncEngine, svc: MessageService
) -> None:
    """list_for_chat with since_id returns newer messages, oldest first."""
    await _seed_user_and_chat(engine, user_id=1, chat_id=1)
    appended = []
    for i in range(6):
        m = await svc.append(chat_id=1, user_id=1, role="user", content=f"msg {i}")
        appended.append(m)

    # Poll for messages newer than the 3rd (index 2).
    pivot_id = appended[2].id
    msgs, has_more = await svc.list_for_chat(1, user_id=1, since_id=pivot_id)
    # Should return msgs at index 3, 4, 5.
    assert len(msgs) == 3
    assert all(m.id > pivot_id for m in msgs)
    assert msgs[0].id < msgs[1].id < msgs[2].id
    assert has_more is False


@pytest.mark.asyncio()
async def test_list_for_chat_oldest_first_ordering(
    engine: AsyncEngine, svc: MessageService
) -> None:
    """list_for_chat always returns messages in ascending id order."""
    await _seed_user_and_chat(engine, user_id=1, chat_id=1)
    for i in range(5):
        await svc.append(chat_id=1, user_id=1, role="user", content=f"msg {i}")
    msgs, _ = await svc.list_for_chat(1, user_id=1)
    ids = [m.id for m in msgs]
    assert ids == sorted(ids)


# ---------------------------------------------------------------------------
# Tests — tool_calls persistence round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_list_for_chat_round_trips_tool_calls(
    engine: AsyncEngine, svc: MessageService
) -> None:
    """A message row with tool_calls JSON survives reload via list_for_chat.

    A chat with tool calls, reloaded from disk, still carries the
    persisted ToolCall list (FE shape:
    {id, name, arguments, status, result?}) on the Message model.
    """
    await _seed_user_and_chat(engine, user_id=1, chat_id=1)
    persisted = [
        {
            "id": "tc_1",
            "name": "search_web",
            "arguments": '{"query": "lm studio"}',
            "status": "success",
            "result": "LM Studio is a desktop application.",
        },
        {
            "id": "tc_2",
            "name": "tool_beta",
            "arguments": "{}",
            "status": "failure",
        },
    ]
    async with engine.begin() as conn:
        await conn.execute(
            messages.insert().values(
                chat_id=1,
                role="assistant",
                content="I searched for you.",
                state="final",
                tool_calls=persisted,
            )
        )

    msgs, _ = await svc.list_for_chat(1, user_id=1)
    assert len(msgs) == 1
    assert msgs[0].tool_calls == persisted


@pytest.mark.asyncio()
async def test_list_for_chat_tool_calls_null_for_plain_rows(
    engine: AsyncEngine, svc: MessageService
) -> None:
    """Rows without tool calls expose tool_calls=None."""
    await _seed_user_and_chat(engine, user_id=1, chat_id=1)
    await svc.append(chat_id=1, user_id=1, role="user", content="plain")
    msgs, _ = await svc.list_for_chat(1, user_id=1)
    assert msgs[0].tool_calls is None


# ---------------------------------------------------------------------------
# Tests — delete_from_user_message_for_resend memory notify uses REAL ids
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_resend_delete_notifies_real_ids_not_fabricated_range(
    engine: AsyncEngine, svc: MessageService
) -> None:
    """delete_from_user_message_for_resend notifies the REAL deleted ids.

    The global autoincrement PK means post-U message ids in a single chat are
    almost never contiguous (other chats claim slots in between).  This test
    creates a second chat and interleaves inserts so that the target chat's
    post-U ids are non-contiguous, then asserts handle_message_deleted is
    called with the EXACT real ids — not the fabricated range
    (message_id+1 … message_id+n) that the old buggy loop produced.
    """
    # Two users, two chats — user 1 owns chat 1, user 2 owns chat 2.
    await _seed_user_and_chat(engine, user_id=1, username="alice", chat_id=1)
    await _seed_user_and_chat(engine, user_id=2, username="bob", chat_id=2)

    # Interleave inserts across both chats so the global autoincrement PK
    # assigns non-contiguous ids to chat-1's post-U messages.
    #
    # Sequence of inserts (global PK order):
    #   chat1: user msg   → id = A  (boundary U — now deleted along with the tail)
    #   chat2: user msg   → id = A+1  (belongs to other chat; id A+1 is "stolen")
    #   chat1: asst msg   → id = A+2  (to-be-deleted; real id != A+1)
    #   chat2: asst msg   → id = A+3  (belongs to other chat; id A+3 is "stolen")
    #   chat1: user msg2  → id = A+4  (to-be-deleted; real id != A+2)
    #
    # Fabricated-range loop would notify {A+1, A+2}; real-ids loop must
    # notify {A, A+2, A+4} — the boundary is now part of the deleted set.

    u_msg = await svc.append(chat_id=1, user_id=1, role="user", content="boundary U")

    # Chat-2 steals the next PK slot.
    await svc.append(chat_id=2, user_id=2, role="user", content="other chat msg 1")

    a_msg = await svc.append(chat_id=1, user_id=1, role="assistant", content="reply")
    assert a_msg.id != u_msg.id + 1, (
        "Test precondition: a_msg.id should NOT be contiguous with u_msg.id "
        "(a cross-chat insert must have claimed the next slot)"
    )

    # Chat-2 steals another slot.
    await svc.append(chat_id=2, user_id=2, role="assistant", content="other chat msg 2")

    u2_msg = await svc.append(chat_id=1, user_id=1, role="user", content="follow-up U")

    # Real deleted ids are {u_msg.id, a_msg.id, u2_msg.id} — the boundary is
    # deleted too now (inclusive delete).
    # Fabricated range would be {u_msg.id+1, u_msg.id+2}.
    real_ids = {u_msg.id, a_msg.id, u2_msg.id}
    fabricated_ids = {u_msg.id + 1, u_msg.id + 2}
    assert real_ids != fabricated_ids, (
        "Test precondition: real ids and fabricated range must differ "
        "(interleaving didn't produce non-contiguous ids)"
    )

    # Spy on handle_message_deleted.
    notified: list[int] = []

    async def _spy(message_id: int) -> None:
        notified.append(message_id)

    svc._memory_service.handle_message_deleted = _spy  # type: ignore[method-assign]

    deleted_count, user_content = await svc.delete_from_user_message_for_resend(
        chat_id=1,
        message_id=u_msg.id,
        user_id=1,
    )

    assert deleted_count == 3
    assert user_content == "boundary U"
    assert set(notified) == real_ids, (
        f"Memory notify used wrong ids: got {set(notified)!r}, "
        f"expected {real_ids!r}. "
        f"If {fabricated_ids!r} were notified instead, the fabricated-range bug is still present."
    )
