# SPDX-License-Identifier: Apache-2.0
"""Contract tests for chat_service.ChatService — CRUD, fork, delete cascade.

Tests for the chat service.

Each test gets a fresh per-test SQLite engine with schema bootstrapped via
metadata.create_all and a minimal user row for FK satisfaction.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.pragmas import apply_sqlite_pragmas
from lmchat.db.schema import audit_log, chats, compactions, message_embeddings, messages, metadata
from lmchat.services.chat_service import Chat, ChatNotFoundError, ChatService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    """Yield a fresh per-test SQLite engine with the full schema created.

    The ``apply_sqlite_pragmas`` hook is attached to enable FK enforcement
    (``PRAGMA foreign_keys = ON``) so CASCADE deletes work correctly.
    """
    db_path = tmp_path / "test_chat_service.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}", pool_pre_ping=True)

    @event.listens_for(eng.sync_engine, "connect")
    def _on_connect(dbapi_conn: object, _rec: object) -> None:
        apply_sqlite_pragmas(dbapi_conn)

    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


async def _insert_user(engine: AsyncEngine, user_id: int = 1, username: str = "alice") -> None:
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
    models_mock.resolve_to_loaded_or_fallback = AsyncMock(
        return_value=MagicMock(wire_id=None)
    )
    models_mock.get_capabilities = AsyncMock(side_effect=KeyError("no model"))
    # No synthetic message/chat in this file carries a model_id, so
    # compact()'s summary-model resolution falls through to list_loaded()
    # (tier 3). Default to a single loaded chat model so existing tests
    # keep passing unchanged.
    models_mock.list_loaded = AsyncMock(
        return_value=[MagicMock(key="qwen-test-7b", type="llm")]
    )
    return ChatService(
        engine=engine,
        memory_service=memory_mock,
        models_service=models_mock,
        chat_locks={},
    )


async def _insert_message(
    engine: AsyncEngine,
    chat_id: int,
    role: str = "user",
    content: str = "hello",
) -> int:
    """Insert a message row and return its id."""
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "INSERT INTO messages (chat_id, role, content)"
                " VALUES (:cid, :role, :content)"
            ),
            {"cid": chat_id, "role": role, "content": content},
        )
        return result.lastrowid  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# CRUD tests
# ---------------------------------------------------------------------------


async def test_create_returns_chat_with_user_id_and_title(engine: AsyncEngine) -> None:
    """create() returns a Chat with the correct user_id and title."""
    await _insert_user(engine)
    svc = _make_service(engine)

    chat = await svc.create(user_id=1, title="My Chat")

    assert isinstance(chat, Chat)
    assert chat.user_id == 1
    assert chat.title == "My Chat"
    assert chat.id > 0


async def test_list_for_user_returns_only_users_chats(engine: AsyncEngine) -> None:
    """list_for_user() excludes chats belonging to other users."""
    await _insert_user(engine, user_id=1, username="alice")
    await _insert_user(engine, user_id=2, username="bob")
    svc = _make_service(engine)

    await svc.create(user_id=1, title="Alice Chat")
    await svc.create(user_id=2, title="Bob Chat")

    alice_chats = await svc.list_for_user(1)
    bob_chats = await svc.list_for_user(2)

    assert len(alice_chats) == 1
    assert alice_chats[0].title == "Alice Chat"
    assert len(bob_chats) == 1
    assert bob_chats[0].title == "Bob Chat"


async def test_list_for_user_filters_by_folder(engine: AsyncEngine) -> None:
    """list_for_user(folder=...) returns only chats in that folder."""
    await _insert_user(engine)
    svc = _make_service(engine)

    c1 = await svc.create(user_id=1, title="Work Chat")
    c2 = await svc.create(user_id=1, title="Personal Chat")

    await svc.move_to_folder(c1.id, user_id=1, folder="work")
    await svc.move_to_folder(c2.id, user_id=1, folder="personal")

    work = await svc.list_for_user(1, folder="work")
    personal = await svc.list_for_user(1, folder="personal")

    assert len(work) == 1
    assert work[0].id == c1.id
    assert len(personal) == 1
    assert personal[0].id == c2.id


async def test_get_returns_users_chat(engine: AsyncEngine) -> None:
    """get() returns the correct Chat for the owning user."""
    await _insert_user(engine)
    svc = _make_service(engine)

    created = await svc.create(user_id=1, title="My Chat")
    fetched = await svc.get(created.id, user_id=1)

    assert fetched.id == created.id
    assert fetched.title == "My Chat"


async def test_get_cross_user_raises_ChatNotFoundError(engine: AsyncEngine) -> None:
    """get() with wrong user_id raises ChatNotFoundError (not 403)."""
    await _insert_user(engine, user_id=1, username="alice")
    await _insert_user(engine, user_id=2, username="bob")
    svc = _make_service(engine)

    chat = await svc.create(user_id=1, title="Alice Chat")

    with pytest.raises(ChatNotFoundError):
        await svc.get(chat.id, user_id=2)


async def test_get_missing_raises_ChatNotFoundError(engine: AsyncEngine) -> None:
    """get() for a non-existent chat_id raises ChatNotFoundError."""
    await _insert_user(engine)
    svc = _make_service(engine)

    with pytest.raises(ChatNotFoundError):
        await svc.get(9999, user_id=1)


async def test_rename_updates_title_and_audit_log(engine: AsyncEngine) -> None:
    """rename() persists the new title and writes an audit_log row."""
    await _insert_user(engine)
    svc = _make_service(engine)

    chat = await svc.create(user_id=1, title="Old Title")
    await svc.rename(chat.id, user_id=1, title="New Title")

    updated = await svc.get(chat.id, user_id=1)
    assert updated.title == "New Title"

    async with engine.connect() as conn:
        result = await conn.execute(
            select(audit_log).where(audit_log.c.event == "chat.renamed")
        )
        rows = result.fetchall()
    assert len(rows) >= 1


async def test_move_to_folder_atomic_update(engine: AsyncEngine) -> None:
    """move_to_folder() updates the folder column in a single transaction."""
    await _insert_user(engine)
    svc = _make_service(engine)

    chat = await svc.create(user_id=1, title="Chat")
    await svc.move_to_folder(chat.id, user_id=1, folder="archive")

    updated = await svc.get(chat.id, user_id=1)
    assert updated.folder == "archive"

    # Move back to None.
    await svc.move_to_folder(chat.id, user_id=1, folder=None)
    cleared = await svc.get(chat.id, user_id=1)
    assert cleared.folder is None


async def test_pin_toggle(engine: AsyncEngine) -> None:
    """pin() toggles the pinned flag correctly."""
    await _insert_user(engine)
    svc = _make_service(engine)

    chat = await svc.create(user_id=1, title="Chat")
    assert not chat.pinned

    await svc.pin(chat.id, user_id=1, pinned=True)
    pinned = await svc.get(chat.id, user_id=1)
    assert pinned.pinned is True

    await svc.pin(chat.id, user_id=1, pinned=False)
    unpinned = await svc.get(chat.id, user_id=1)
    assert unpinned.pinned is False


# ---------------------------------------------------------------------------
# Tags + archive (migration 0046)
# ---------------------------------------------------------------------------


async def test_create_defaults_to_empty_tags_and_active(engine: AsyncEngine) -> None:
    """A freshly-created chat has an empty tag list and is not archived."""
    await _insert_user(engine)
    svc = _make_service(engine)

    chat = await svc.create(user_id=1, title="Chat")

    assert chat.tags == []
    assert chat.archived_at is None


async def test_set_tags_replaces_whole_list(engine: AsyncEngine) -> None:
    """set_tags() replaces the tag list, dedupes, and strips blanks."""
    await _insert_user(engine)
    svc = _make_service(engine)
    chat = await svc.create(user_id=1, title="Chat")

    await svc.set_tags(chat.id, user_id=1, tags=["work", "urgent", "work", " ", ""])
    tagged = await svc.get(chat.id, user_id=1)
    assert tagged.tags == ["work", "urgent"]

    # A second call fully replaces the prior list (not merges).
    await svc.set_tags(chat.id, user_id=1, tags=["personal"])
    replaced = await svc.get(chat.id, user_id=1)
    assert replaced.tags == ["personal"]

    # Empty list clears all tags.
    await svc.set_tags(chat.id, user_id=1, tags=[])
    cleared = await svc.get(chat.id, user_id=1)
    assert cleared.tags == []


async def test_set_tags_writes_audit_log(engine: AsyncEngine) -> None:
    """set_tags() writes a chat.tags_updated audit_log row."""
    await _insert_user(engine)
    svc = _make_service(engine)
    chat = await svc.create(user_id=1, title="Chat")

    await svc.set_tags(chat.id, user_id=1, tags=["work"])

    async with engine.connect() as conn:
        result = await conn.execute(
            select(audit_log).where(audit_log.c.event == "chat.tags_updated")
        )
        rows = result.fetchall()
    assert len(rows) >= 1


async def test_set_tags_missing_chat_raises_ChatNotFoundError(
    engine: AsyncEngine,
) -> None:
    """set_tags() on a non-existent chat raises ChatNotFoundError."""
    await _insert_user(engine)
    svc = _make_service(engine)

    with pytest.raises(ChatNotFoundError):
        await svc.set_tags(9999, user_id=1, tags=["work"])


async def test_set_tags_cross_user_raises_ChatNotFoundError(
    engine: AsyncEngine,
) -> None:
    """set_tags() on another user's chat raises ChatNotFoundError (not 403)."""
    await _insert_user(engine, user_id=1, username="alice")
    await _insert_user(engine, user_id=2, username="bob")
    svc = _make_service(engine)
    chat = await svc.create(user_id=1, title="Alice Chat")

    with pytest.raises(ChatNotFoundError):
        await svc.set_tags(chat.id, user_id=2, tags=["nope"])


async def test_set_archived_sets_and_clears_archived_at(engine: AsyncEngine) -> None:
    """set_archived() sets archived_at to a timestamp, then clears it back to None."""
    await _insert_user(engine)
    svc = _make_service(engine)
    chat = await svc.create(user_id=1, title="Chat")
    assert chat.archived_at is None

    await svc.set_archived(chat.id, user_id=1, archived=True)
    archived = await svc.get(chat.id, user_id=1)
    assert archived.archived_at is not None

    await svc.set_archived(chat.id, user_id=1, archived=False)
    restored = await svc.get(chat.id, user_id=1)
    assert restored.archived_at is None


async def test_set_archived_writes_audit_log(engine: AsyncEngine) -> None:
    """set_archived() writes chat.archived / chat.unarchived audit_log rows."""
    await _insert_user(engine)
    svc = _make_service(engine)
    chat = await svc.create(user_id=1, title="Chat")

    await svc.set_archived(chat.id, user_id=1, archived=True)
    await svc.set_archived(chat.id, user_id=1, archived=False)

    async with engine.connect() as conn:
        archived_rows = (
            await conn.execute(
                select(audit_log).where(audit_log.c.event == "chat.archived")
            )
        ).fetchall()
        unarchived_rows = (
            await conn.execute(
                select(audit_log).where(audit_log.c.event == "chat.unarchived")
            )
        ).fetchall()
    assert len(archived_rows) >= 1
    assert len(unarchived_rows) >= 1


async def test_set_archived_missing_chat_raises_ChatNotFoundError(
    engine: AsyncEngine,
) -> None:
    """set_archived() on a non-existent chat raises ChatNotFoundError."""
    await _insert_user(engine)
    svc = _make_service(engine)

    with pytest.raises(ChatNotFoundError):
        await svc.set_archived(9999, user_id=1, archived=True)


async def test_set_archived_cross_user_raises_ChatNotFoundError(
    engine: AsyncEngine,
) -> None:
    """set_archived() on another user's chat raises ChatNotFoundError (not 403)."""
    await _insert_user(engine, user_id=1, username="alice")
    await _insert_user(engine, user_id=2, username="bob")
    svc = _make_service(engine)
    chat = await svc.create(user_id=1, title="Alice Chat")

    with pytest.raises(ChatNotFoundError):
        await svc.set_archived(chat.id, user_id=2, archived=True)


async def test_list_for_user_excludes_archived_by_default(engine: AsyncEngine) -> None:
    """list_for_user() excludes archived chats unless include_archived=True."""
    await _insert_user(engine)
    svc = _make_service(engine)

    active = await svc.create(user_id=1, title="Active Chat")
    archived = await svc.create(user_id=1, title="Archived Chat")
    await svc.set_archived(archived.id, user_id=1, archived=True)

    default_list = await svc.list_for_user(1)
    assert [c.id for c in default_list] == [active.id]

    full_list = await svc.list_for_user(1, include_archived=True)
    assert {c.id for c in full_list} == {active.id, archived.id}


async def test_delete_cascades_to_messages_and_embeddings(engine: AsyncEngine) -> None:
    """delete() removes the chat, its messages, and message_embeddings rows."""
    await _insert_user(engine)
    svc = _make_service(engine)

    chat = await svc.create(user_id=1, title="Chat")
    msg_id = await _insert_message(engine, chat.id)

    # Insert a message_embeddings row.
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO message_embeddings"
                " (message_id, embedding_model_id, embedding, text_hash)"
                " VALUES (:mid, 'test-model', x'01020304', 'aabbcc')"
            ),
            {"mid": msg_id},
        )

    await svc.delete(chat.id, user_id=1)

    # Chat row gone.
    async with engine.connect() as conn:
        chat_row = (
            await conn.execute(select(chats).where(chats.c.id == chat.id))
        ).fetchone()
        msg_row = (
            await conn.execute(select(messages).where(messages.c.id == msg_id))
        ).fetchone()
        emb_row = (
            await conn.execute(
                select(message_embeddings).where(message_embeddings.c.message_id == msg_id)
            )
        ).fetchone()

    assert chat_row is None
    assert msg_row is None
    assert emb_row is None


async def test_delete_cascades_to_compactions(engine: AsyncEngine) -> None:
    """delete() also removes the chat's ``compactions`` rows.

    ``compactions.chat_id`` carries ``ON DELETE CASCADE`` from ``chats``,
    so deleting the whole chat must clear its archived spans too — an
    orphaned compactions row pointing at a deleted chat would be a leak.
    """
    await _insert_user(engine)
    svc = _make_service(engine)

    chat = await svc.create(user_id=1, title="Chat")
    await _insert_message(engine, chat.id, role="assistant", content="old one two three")
    await _insert_message(engine, chat.id, role="assistant", content="old two two three")
    await _insert_message(engine, chat.id, role="user", content="latest question here now")

    svc._run_llm_distill = AsyncMock(  # type: ignore[method-assign]
        return_value="stub summary of the archived turns"
    )
    result = await svc.compact(
        chat.id,
        user_id=1,
        target_tokens=6,
        http_client=AsyncMock(),
        base_url="http://lm-studio.test",
    )
    assert result.compaction_id is not None

    await svc.delete(chat.id, user_id=1)

    async with engine.connect() as conn:
        comp_row = (
            await conn.execute(
                select(compactions).where(compactions.c.id == result.compaction_id)
            )
        ).fetchone()
    assert comp_row is None, "compactions row must be gone after the chat is deleted"


async def test_clear_messages_removes_compactions_rows(engine: AsyncEngine) -> None:
    """clear_messages() also removes the chat's ``compactions`` rows.

    Unlike delete(), clear_messages() keeps the chat shell and only wipes
    ``messages`` — but an archived span with no messages left in the chat
    is meaningless, so the compactions rows must be explicitly cleared too
    (there's no chats-level cascade to rely on here since the chat itself
    survives).
    """
    await _insert_user(engine)
    svc = _make_service(engine)

    chat = await svc.create(user_id=1, title="Chat")
    await _insert_message(engine, chat.id, role="assistant", content="old one two three")
    await _insert_message(engine, chat.id, role="assistant", content="old two two three")
    await _insert_message(engine, chat.id, role="user", content="latest question here now")

    svc._run_llm_distill = AsyncMock(  # type: ignore[method-assign]
        return_value="stub summary of the archived turns"
    )
    result = await svc.compact(
        chat.id,
        user_id=1,
        target_tokens=6,
        http_client=AsyncMock(),
        base_url="http://lm-studio.test",
    )
    assert result.compaction_id is not None

    await svc.clear_messages(chat.id, user_id=1)

    # Chat shell survives.
    async with engine.connect() as conn:
        chat_row = (
            await conn.execute(select(chats).where(chats.c.id == chat.id))
        ).fetchone()
    assert chat_row is not None

    async with engine.connect() as conn:
        comp_row = (
            await conn.execute(
                select(compactions).where(compactions.c.id == result.compaction_id)
            )
        ).fetchone()
    assert comp_row is None, "compactions row must be gone after clear_messages()"


async def test_delete_writes_audit_log(engine: AsyncEngine) -> None:
    """delete() writes a chat.deleted audit_log row."""
    await _insert_user(engine)
    svc = _make_service(engine)

    chat = await svc.create(user_id=1, title="Chat")
    await svc.delete(chat.id, user_id=1)

    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(audit_log).where(audit_log.c.event == "chat.deleted")
            )
        ).fetchall()

    assert len(rows) >= 1


# ---------------------------------------------------------------------------
# Fork tests
# ---------------------------------------------------------------------------


async def test_fork_copies_messages_up_to_at_message_id(engine: AsyncEngine) -> None:
    """fork() copies messages with id <= at_message_id to the new chat."""
    await _insert_user(engine)
    svc = _make_service(engine)

    chat = await svc.create(user_id=1, title="Source")
    mid1 = await _insert_message(engine, chat.id, content="msg1")
    mid2 = await _insert_message(engine, chat.id, content="msg2")
    await _insert_message(engine, chat.id, content="msg3")

    forked = await svc.fork(chat.id, user_id=1, at_message_id=mid2)

    async with engine.connect() as conn:
        fork_msgs = (
            await conn.execute(
                select(messages)
                .where(messages.c.chat_id == forked.id)
                .order_by(messages.c.id)
            )
        ).fetchall()

    assert len(fork_msgs) == 2
    assert fork_msgs[0].content == "msg1"
    assert fork_msgs[1].content == "msg2"

    # mid1 is the first message in the fork.
    assert mid1 > 0


async def test_fork_preserves_original_created_at_on_message_copies(engine: AsyncEngine) -> None:
    """fork() message copies retain the original created_at timestamp."""
    await _insert_user(engine)
    svc = _make_service(engine)

    chat = await svc.create(user_id=1, title="Source")
    mid1 = await _insert_message(engine, chat.id, content="msg1")

    # Fetch original created_at.
    async with engine.connect() as conn:
        original_row = (
            await conn.execute(select(messages).where(messages.c.id == mid1))
        ).fetchone()
    assert original_row is not None
    original_created_at = original_row.created_at

    forked = await svc.fork(chat.id, user_id=1, at_message_id=mid1)

    async with engine.connect() as conn:
        forked_msgs = (
            await conn.execute(
                select(messages).where(messages.c.chat_id == forked.id)
            )
        ).fetchall()

    assert len(forked_msgs) == 1
    assert forked_msgs[0].created_at == original_created_at


async def test_fork_new_chat_title_includes_fork_marker(engine: AsyncEngine) -> None:
    """fork() creates a new chat whose title contains ' (fork)'."""
    await _insert_user(engine)
    svc = _make_service(engine)

    chat = await svc.create(user_id=1, title="My Chat")
    mid = await _insert_message(engine, chat.id, content="msg")

    forked = await svc.fork(chat.id, user_id=1, at_message_id=mid)

    assert "(fork)" in forked.title
    assert "My Chat" in forked.title


# ---------------------------------------------------------------------------
# update_settings tests
# ---------------------------------------------------------------------------


async def test_update_settings_shallow_merge_preserves_existing_keys(
    engine: AsyncEngine,
) -> None:
    """update_settings() shallow merge keeps keys not in the patch."""
    await _insert_user(engine)
    svc = _make_service(engine)

    chat = await svc.create(user_id=1, title="Chat")
    # Set an initial settings value.
    await svc.update_settings(chat.id, user_id=1, settings={"rag_enabled": True})
    # Now update only reasoning_effort — rag_enabled must be preserved.
    updated = await svc.update_settings(
        chat.id, user_id=1, settings={"reasoning_effort": "medium"}
    )

    assert updated.settings["rag_enabled"] is True
    assert updated.settings["reasoning_effort"] == "medium"


async def test_update_settings_returns_chat(engine: AsyncEngine) -> None:
    """update_settings() returns the updated Chat object."""
    await _insert_user(engine)
    svc = _make_service(engine)

    chat = await svc.create(user_id=1, title="Chat")
    result = await svc.update_settings(
        chat.id, user_id=1, settings={"rag_enabled": False}
    )

    assert isinstance(result, Chat)
    assert result.id == chat.id
    assert result.settings["rag_enabled"] is False


async def test_update_settings_invalid_reasoning_effort_raises(
    engine: AsyncEngine,
) -> None:
    """update_settings() raises ValueError for invalid reasoning_effort value."""
    await _insert_user(engine)
    svc = _make_service(engine)

    chat = await svc.create(user_id=1, title="Chat")

    with pytest.raises(ValueError, match="reasoning_effort"):
        await svc.update_settings(
            chat.id, user_id=1, settings={"reasoning_effort": "ultra"}
        )


async def test_update_settings_invalid_rag_enabled_raises(
    engine: AsyncEngine,
) -> None:
    """update_settings() raises ValueError if rag_enabled is not bool."""
    await _insert_user(engine)
    svc = _make_service(engine)

    chat = await svc.create(user_id=1, title="Chat")

    with pytest.raises(ValueError, match="rag_enabled"):
        await svc.update_settings(
            chat.id, user_id=1, settings={"rag_enabled": "yes"}
        )


async def test_update_settings_valid_ab_compare(engine: AsyncEngine) -> None:
    """update_settings() accepts a valid ab_compare dict."""
    await _insert_user(engine)
    svc = _make_service(engine)

    chat = await svc.create(user_id=1, title="Chat")
    updated = await svc.update_settings(
        chat.id,
        user_id=1,
        settings={
            "ab_compare": {
                "enabled": True,
                "model_a": "qwen3-35b",
                "model_b": "gemma-26b",
            }
        },
    )

    ab = updated.settings["ab_compare"]
    assert ab["enabled"] is True
    assert ab["model_a"] == "qwen3-35b"
    assert ab["model_b"] == "gemma-26b"


async def test_update_settings_invalid_ab_compare_missing_enabled(
    engine: AsyncEngine,
) -> None:
    """update_settings() raises ValueError if ab_compare.enabled is missing."""
    await _insert_user(engine)
    svc = _make_service(engine)

    chat = await svc.create(user_id=1, title="Chat")

    with pytest.raises(ValueError, match="ab_compare.enabled"):
        await svc.update_settings(
            chat.id,
            user_id=1,
            settings={"ab_compare": {"model_a": "qwen"}},
        )


@pytest.mark.asyncio
async def test_update_settings_unknown_key_emits_warning(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """update_settings() emits a WARNING for unknown keys and still persists them.

    Unknown keys are treated as forward-compatible (a newer client writing a
    key that this server version doesn't validate yet).  We WARN rather than
    reject to preserve forward-compatibility.

    Spies on ``chat_service.log.warning`` directly (order-independent,
    does not depend on structlog/stdlib routing).
    """
    from lmchat.services import chat_service as cs_module

    await _insert_user(engine)
    svc = _make_service(engine)

    chat = await svc.create(user_id=1, title="Chat")

    warning_events: list[str] = []
    original_warning = cs_module.log.warning

    def _spy_warning(event: str, **kwargs: object) -> None:
        warning_events.append(event)
        original_warning(event, **kwargs)

    monkeypatch.setattr(cs_module.log, "warning", _spy_warning)

    updated = await svc.update_settings(
        chat.id,
        user_id=1,
        settings={"unknown_future_key": "some_value"},
    )

    # The unknown-key warning event must have fired.
    assert any("unknown" in ev.lower() for ev in warning_events), (
        f"Expected a 'unknown_keys' warning event; got: {warning_events}"
    )

    # The key must still be persisted (forward-compat).
    assert updated.settings.get("unknown_future_key") == "some_value"


@pytest.mark.asyncio
async def test_update_settings_known_key_no_warning(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """update_settings() does NOT emit an unknown-key WARNING for known keys.

    Spies on ``chat_service.log.warning`` directly (order-independent).
    """
    from lmchat.services import chat_service as cs_module

    await _insert_user(engine)
    svc = _make_service(engine)

    chat = await svc.create(user_id=1, title="Chat")

    warning_events: list[str] = []
    original_warning = cs_module.log.warning

    def _spy_warning(event: str, **kwargs: object) -> None:
        warning_events.append(event)
        original_warning(event, **kwargs)

    monkeypatch.setattr(cs_module.log, "warning", _spy_warning)

    await svc.update_settings(
        chat.id,
        user_id=1,
        settings={"rag_enabled": True},
    )

    # No unknown-key warning for a known key.
    unknown_warnings = [ev for ev in warning_events if "unknown" in ev.lower()]
    assert unknown_warnings == [], (
        f"Unexpected unknown-key warnings for known key: {unknown_warnings}"
    )


async def test_update_settings_cross_user_raises_ChatNotFoundError(
    engine: AsyncEngine,
) -> None:
    """update_settings() with wrong user_id raises ChatNotFoundError."""
    await _insert_user(engine, user_id=1, username="alice")
    await _insert_user(engine, user_id=2, username="bob")
    svc = _make_service(engine)

    chat = await svc.create(user_id=1, title="Alice Chat")

    with pytest.raises(ChatNotFoundError):
        await svc.update_settings(
            chat.id, user_id=2, settings={"rag_enabled": True}
        )


async def test_update_settings_not_found_raises_ChatNotFoundError(
    engine: AsyncEngine,
) -> None:
    """update_settings() for a non-existent chat raises ChatNotFoundError."""
    await _insert_user(engine)
    svc = _make_service(engine)

    with pytest.raises(ChatNotFoundError):
        await svc.update_settings(
            9999, user_id=1, settings={"rag_enabled": True}
        )
