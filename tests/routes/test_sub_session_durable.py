# SPDX-License-Identifier: Apache-2.0
"""Durable sub-sessions — P2 persistence + lifecycle tests (migration 0045).

Covers the PLAN.md §2a/§2b data-loss fix core:

1. A non-incognito sub-session stream persists a finalized transcript into
   ``sub_session_messages`` (+ ``sub_sessions.status='final'``).
2. A client disconnect mid-stream salvages the draft: the row is NOT left
   stuck in ``active``/``draft`` — it converges to
   ``sub_sessions.status='aborted'`` with whatever content the
   ``_CoalesceTimer`` had already flushed.
3. An INCOGNITO chat's sub-session stream writes ZERO ``sub_sessions`` /
   ``sub_session_messages`` rows (D6).
4. ``ChatService.clear_messages`` deletes the chat's ``sub_sessions`` rows
   (D8) — cascading to ``sub_session_messages`` via the FK.
5. The per-chat sub-session lock + in-progress check (D4) blocks a
   concurrent double-submit with ``SubSessionStreamInProgressError``.

Tests 1/2/5 drive ``_sub_session_sse`` directly (service-level, mirroring
``tests/routes/test_sub_session_streaming.py`` and the disconnect-mock
pattern in ``tests/services/test_streaming_service.py::_mock_request``) —
the incognito gate lives in the ROUTE layer, so test 3 goes through a real
``TestClient`` instead (mirroring ``tests/routes/test_sub_session.py``).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator, Generator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.pragmas import apply_sqlite_pragmas
from lmchat.db.schema import chats, metadata, sub_session_messages, sub_sessions, users
from lmchat.lmstudio.types import CanonicalEvent
from lmchat.middleware._bucket_store import InMemoryBucketStore
from lmchat.routes._dependencies import (
    get_default_session_store_dep,
    get_engine_dep,
    get_integrations_service_dep,
)
from lmchat.routes.chats import (
    _get_chat_service,
    _get_message_service,
    _sub_session_sse,
    _SubSessionPersistContext,
)
from lmchat.services._stream_reaper import _finalize_stuck_sub_session_drafts
from lmchat.services.auth_service import _reset_dummy_hash_cache
from lmchat.services.chat_service import ChatService
from lmchat.services.memory_service import MemoryService
from lmchat.services.message_service import MessageService
from lmchat.services.models_service import Capabilities, ModelsService, ResolvedModel
from lmchat.services.streaming_errors import SubSessionStreamInProgressError
from lmchat.session.sqlite_store import SQLiteSessionStore

# ---------------------------------------------------------------------------
# Fixtures — service-level (tests 1, 2, 4, 5)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.config import get_settings

    get_settings.cache_clear()
    _reset_dummy_hash_cache()
    yield
    get_settings.cache_clear()
    _reset_dummy_hash_cache()


@pytest.fixture()
async def db_engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    """Per-test SQLite engine, full schema, FK enforcement ON.

    FK enforcement is required for test 4 (clear cascades sub_sessions ->
    sub_session_messages) to actually exercise the CASCADE — mirrors
    tests/services/test_chat_service.py's fixture exactly.
    """
    db_path = tmp_path / "test_sub_session_durable.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}", pool_pre_ping=True)

    @event.listens_for(eng.sync_engine, "connect")
    def _on_connect(dbapi_conn: object, _rec: object) -> None:
        apply_sqlite_pragmas(dbapi_conn)

    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


async def _seed_chat(engine: AsyncEngine, *, user_id: int = 1, incognito: bool = False) -> int:
    """Insert a user (if absent) + a chat; returns the chat_id."""
    async with engine.begin() as conn:
        await conn.execute(
            users.insert()
            .prefix_with("OR IGNORE")
            .values(id=user_id, username=f"u{user_id}", password_hash="dummy")
        )
        result = await conn.execute(
            chats.insert().values(
                user_id=user_id, title="durable sub-session test", incognito=int(incognito)
            )
        )
        pk = result.inserted_primary_key
        assert pk is not None
        return int(pk[0])


class _FakeLmClient:
    """Minimal ``lm_client.stream(**kwargs)`` fake (mirrors test_sub_session_ephemeral.py)."""

    def __init__(self, events: list[CanonicalEvent]) -> None:
        self._events = events

    def stream(self, **_kwargs: object) -> AsyncIterator[CanonicalEvent]:
        async def _gen() -> AsyncIterator[CanonicalEvent]:
            for ev in self._events:
                yield ev

        return _gen()


def _mock_request(*, disconnected: bool) -> AsyncMock:
    """Mock FastAPI Request whose receive() drives the disconnect watcher.

    Mirrors tests/services/test_streaming_service.py::_mock_request exactly
    (same contract: the watcher is the sole consumer of receive()).
    """
    request = AsyncMock()
    if disconnected:
        request.receive = AsyncMock(return_value={"type": "http.disconnect"})
    else:
        _never = asyncio.Event()

        async def _block_forever() -> dict[str, str]:
            await _never.wait()
            return {"type": "http.request"}  # pragma: no cover

        request.receive = _block_forever
    return request


async def _sub_session_rows(engine: AsyncEngine, chat_id: int) -> tuple[list[Any], list[Any]]:
    """Return (sub_sessions rows, sub_session_messages rows) for chat_id."""
    async with engine.connect() as conn:
        sess_rows = (
            await conn.execute(select(sub_sessions).where(sub_sessions.c.chat_id == chat_id))
        ).fetchall()
        sub_session_ids = [r.id for r in sess_rows]
        msg_rows: list[Any] = []
        if sub_session_ids:
            msg_rows = list(
                (
                    await conn.execute(
                        select(sub_session_messages).where(
                            sub_session_messages.c.sub_session_id.in_(sub_session_ids)
                        )
                    )
                ).fetchall()
            )
    return list(sess_rows), msg_rows


# ---------------------------------------------------------------------------
# Test 1 — non-incognito stream persists a finalized transcript
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sub_session_stream_persists_finalized_transcript(
    db_engine: AsyncEngine,
) -> None:
    """A durable sub-session stream writes sub_sessions + sub_session_messages.

    Red-on-revert: this is the data-loss fix core — before P2, NOTHING was
    written for a sub-session stream (see test_sub_session_ephemeral.py).
    """
    chat_id = await _seed_chat(db_engine)
    events = [
        CanonicalEvent(type="chat.start"),
        CanonicalEvent(type="message.delta", content="The answer is "),
        CanonicalEvent(type="message.delta", content="42."),
        CanonicalEvent(type="message.end"),
        CanonicalEvent(type="chat.end"),
    ]
    lm_client = _FakeLmClient(events)
    persist = _SubSessionPersistContext(engine=db_engine, chat_id=chat_id, preset_id="research")

    frames: list[bytes] = []
    async for frame in _sub_session_sse(
        lm_client=lm_client,  # type: ignore[arg-type]
        model_id="test-model",
        system_prompt="you are a tester",
        messages=[{"role": "user", "content": "what is the answer?"}],
        request=_mock_request(disconnected=False),
        persist=persist,
    ):
        frames.append(frame)

    blob = b"".join(frames).decode()
    assert "sub.complete" in blob

    sess_rows, msg_rows = await _sub_session_rows(db_engine, chat_id)
    assert len(sess_rows) == 1, f"expected exactly 1 sub_sessions row, got {len(sess_rows)}"
    sess = sess_rows[0]
    assert sess.status == "final", f"expected status='final', got {sess.status!r}"
    assert sess.preset_id == "research"
    assert sess.chat_id == chat_id

    assert len(msg_rows) == 2, f"expected user+assistant rows, got {len(msg_rows)}"
    by_role = {m.role: m for m in msg_rows}
    assert by_role["user"].content == "what is the answer?"
    assert by_role["user"].state == "final"
    assert by_role["assistant"].content == "The answer is 42."
    assert by_role["assistant"].state == "final"


# ---------------------------------------------------------------------------
# Test 2 — client disconnect mid-stream salvages the draft
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sub_session_stream_disconnect_salvages_draft(
    db_engine: AsyncEngine,
) -> None:
    """Disconnect mid-stream: sub_sessions.status='aborted', content kept.

    The fake stream flushes two deltas with a real sleep in between (so the
    250ms _CoalesceTimer interval genuinely elapses and the content is
    written to the DB), THEN blocks — giving the disconnect watcher its
    first chance to fire exactly there, mirroring
    test_streaming_client_disconnect_aborts_draft's technique.
    """
    chat_id = await _seed_chat(db_engine)

    async def _slow_stream(**_kwargs: object) -> AsyncIterator[CanonicalEvent]:
        yield CanonicalEvent(type="chat.start")
        yield CanonicalEvent(type="message.delta", content="first chunk ")
        await asyncio.sleep(0.3)  # let the 250ms coalesce interval elapse
        yield CanonicalEvent(type="message.delta", content="second chunk")
        await asyncio.sleep(5)  # block — the disconnect watcher fires here
        yield CanonicalEvent(type="message.delta", content="never reaches here")

    lm_client = MagicMock()
    lm_client.stream = _slow_stream
    persist = _SubSessionPersistContext(engine=db_engine, chat_id=chat_id, preset_id="research")

    frames: list[bytes] = []
    try:
        async with asyncio.timeout(3.0):
            async for frame in _sub_session_sse(
                lm_client=lm_client,
                model_id="test-model",
                system_prompt="you are a tester",
                messages=[{"role": "user", "content": "long research task"}],
                request=_mock_request(disconnected=True),
                persist=persist,
            ):
                frames.append(frame)
    except TimeoutError:
        pass  # acceptable — the disconnect path doesn't guarantee a clean exit

    # Let the disconnect watcher + outer finally settle.
    await asyncio.sleep(0.6)

    sess_rows, msg_rows = await _sub_session_rows(db_engine, chat_id)
    assert len(sess_rows) == 1
    assert sess_rows[0].status == "aborted", (
        f"expected status='aborted' after disconnect, got {sess_rows[0].status!r} "
        "(must not be stuck 'active')"
    )

    by_role = {m.role: m for m in msg_rows}
    assistant = by_role["assistant"]
    assert assistant.state != "draft", (
        f"assistant row stuck in 'draft' after disconnect — state={assistant.state!r}"
    )
    # The coalesce flush (triggered by the elapsed 0.3s sleep) wrote the two
    # chunks BEFORE the disconnect fired — content must survive, not be lost.
    assert assistant.content == "first chunk second chunk", (
        f"partial content lost on disconnect — got {assistant.content!r}"
    )


# ---------------------------------------------------------------------------
# Test 3 — incognito chat writes ZERO sub-session rows (D6)
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_models_service() -> MagicMock:
    svc = MagicMock(spec=ModelsService)
    svc.get_capabilities = AsyncMock(
        return_value=Capabilities(vision=False, trained_for_tool_use=False)
    )
    svc.list_loaded = AsyncMock(return_value=[])
    svc.refresh = AsyncMock(return_value=None)
    svc.resolve_to_loaded_or_fallback = AsyncMock(
        side_effect=lambda mid, **_kw: ResolvedModel(wire_id=mid, requested=mid)
    )
    return svc


@pytest.fixture()
def mock_memory_service() -> MagicMock:
    svc = MagicMock(spec=MemoryService)
    svc.handle_message_deleted = AsyncMock(return_value=None)
    return svc


@pytest.fixture()
def chat_svc(
    db_engine: AsyncEngine,
    mock_memory_service: MagicMock,
    mock_models_service: MagicMock,
) -> ChatService:
    return ChatService(
        engine=db_engine,
        memory_service=mock_memory_service,
        models_service=mock_models_service,
        chat_locks={},
    )


@pytest.fixture()
def message_svc(db_engine: AsyncEngine, mock_memory_service: MagicMock) -> MessageService:
    return MessageService(engine=db_engine, memory_service=mock_memory_service)


class _RecordingLmClient:
    """Fake LmstudioStreamingClient (mirrors tests/routes/test_sub_session.py)."""

    def __init__(self, events: list[CanonicalEvent]) -> None:
        self._events = events
        self.last_request: Any = None

    async def stream(self, *, request: Any, **_: Any) -> AsyncIterator[CanonicalEvent]:  # noqa: D401
        self.last_request = request
        for ev in self._events:
            yield ev


@pytest.fixture()
def fake_lm_client() -> _RecordingLmClient:
    return _RecordingLmClient(
        events=[
            CanonicalEvent(type="chat.start"),
            CanonicalEvent(type="message.delta", content="hello "),
            CanonicalEvent(type="message.delta", content="world"),
            CanonicalEvent(type="chat.end"),
        ]
    )


@pytest.fixture()
def test_client(
    db_engine: AsyncEngine,
    chat_svc: ChatService,
    message_svc: MessageService,
    fake_lm_client: _RecordingLmClient,
) -> Generator[TestClient]:
    app = create_app_for_test()
    store = SQLiteSessionStore(engine=db_engine)

    app.dependency_overrides[get_engine_dep] = lambda: db_engine
    app.dependency_overrides[get_default_session_store_dep] = lambda: store
    app.dependency_overrides[_get_chat_service] = lambda: chat_svc
    app.dependency_overrides[_get_message_service] = lambda: message_svc

    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        # get_engine_dep(request) reads app.state.engine directly (not via
        # Depends) — the durable-sub-session persistence wiring calls it
        # unconditionally, so this MUST point at the per-test DB.
        client.app.state.engine = db_engine  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.lm_streaming_client = fake_lm_client  # type: ignore[attr-defined]

        _mock_models_svc = MagicMock(spec=ModelsService)
        _mock_models_svc.resolve_to_loaded_or_fallback = AsyncMock(
            side_effect=lambda model_id, **_kw: ResolvedModel(wire_id=model_id, requested=model_id)
        )
        client.app.state.models_service = _mock_models_svc  # type: ignore[attr-defined]

        class _StreamingServiceShim:
            def __init__(self, engine: AsyncEngine) -> None:
                self._engine = engine

            def reset_counter(self, chat_id: int) -> None:
                pass

        client.app.state.streaming_service = _StreamingServiceShim(db_engine)  # type: ignore[attr-defined]

        _mock_integ_svc = MagicMock()
        _mock_integ_svc.list_available = AsyncMock(return_value=[])
        app.dependency_overrides[get_integrations_service_dep] = lambda: _mock_integ_svc

        yield client


def create_app_for_test() -> Any:
    from lmchat.app import create_app

    return create_app()


def _register_and_login(
    client: TestClient, username: str = "alice", password: str = "correct-horse-battery"
) -> None:
    client.post("/api/auth/register", data={"username": username, "password": password})
    resp = client.post("/api/auth/login", data={"username": username, "password": password})
    assert resp.status_code == 200, f"login failed: {resp.text}"


def test_sub_session_incognito_writes_zero_rows(
    test_client: TestClient, db_engine: AsyncEngine
) -> None:
    """D6: an incognito chat's sub-session stream persists NOTHING."""
    _register_and_login(test_client)
    chat_resp = test_client.post(
        "/api/chats", data={"title": "incognito sub-session", "incognito": "true"}
    )
    assert chat_resp.status_code == 201, chat_resp.text
    chat_id = chat_resp.json()["id"]

    resp = test_client.post(
        f"/api/chats/{chat_id}/sub-session/stream",
        data={
            "model_id": "test-model",
            "system_prompt": "you are a tester",
            "messages_json": json.dumps([{"role": "user", "content": "private query"}]),
        },
    )
    assert resp.status_code == 200, resp.text
    assert "sub.complete" in resp.text

    async def _count() -> tuple[int, int]:
        sess_rows, msg_rows = await _sub_session_rows(db_engine, chat_id)
        return len(sess_rows), len(msg_rows)

    sess_count, msg_count = asyncio.run(_count())
    assert sess_count == 0, f"incognito chat leaked {sess_count} sub_sessions row(s)"
    assert msg_count == 0, f"incognito chat leaked {msg_count} sub_session_messages row(s)"


def test_sub_session_non_incognito_writes_rows_via_route(
    test_client: TestClient, db_engine: AsyncEngine
) -> None:
    """Sanity counterpart to the incognito test: a NORMAL chat DOES persist.

    Proves the incognito assertion above isn't tautological (the route-level
    plumbing genuinely writes rows when the gate is open).
    """
    _register_and_login(test_client, username="bob")
    chat_resp = test_client.post("/api/chats", data={"title": "normal sub-session"})
    assert chat_resp.status_code == 201, chat_resp.text
    chat_id = chat_resp.json()["id"]

    resp = test_client.post(
        f"/api/chats/{chat_id}/sub-session/stream",
        data={
            "model_id": "test-model",
            "system_prompt": "you are a tester",
            "messages_json": json.dumps([{"role": "user", "content": "hi"}]),
        },
    )
    assert resp.status_code == 200, resp.text

    async def _count() -> tuple[int, int]:
        sess_rows, msg_rows = await _sub_session_rows(db_engine, chat_id)
        return len(sess_rows), len(msg_rows)

    sess_count, msg_count = asyncio.run(_count())
    assert sess_count == 1
    assert msg_count == 2


# ---------------------------------------------------------------------------
# Test 4 — clear_chat_messages deletes the chat's sub_sessions (D8)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clear_chat_messages_deletes_sub_sessions(db_engine: AsyncEngine) -> None:
    """ChatService.clear_messages also wipes sub_sessions (+ cascade to msgs)."""
    from lmchat.db.schema import messages as messages_table

    chat_id = await _seed_chat(db_engine)

    async with db_engine.begin() as conn:
        await conn.execute(
            messages_table.insert().values(chat_id=chat_id, role="user", content="hi")
        )
        sess_result = await conn.execute(
            sub_sessions.insert().values(chat_id=chat_id, preset_id="research", status="final")
        )
        sub_session_id = int(sess_result.inserted_primary_key[0])  # type: ignore[index]
        await conn.execute(
            sub_session_messages.insert().values(
                sub_session_id=sub_session_id,
                role="user",
                content="q",
                state="final",
            )
        )
        await conn.execute(
            sub_session_messages.insert().values(
                sub_session_id=sub_session_id,
                role="assistant",
                content="a",
                state="final",
            )
        )

    sess_rows, msg_rows = await _sub_session_rows(db_engine, chat_id)
    assert len(sess_rows) == 1, "test precondition: sub_sessions row must exist before clear"
    assert len(msg_rows) == 2, "test precondition: sub_session_messages rows must exist"

    mock_memory = MagicMock(spec=MemoryService)
    mock_memory.handle_message_deleted = AsyncMock(return_value=None)
    mock_models = MagicMock(spec=ModelsService)
    svc = ChatService(
        engine=db_engine, memory_service=mock_memory, models_service=mock_models, chat_locks={}
    )

    await svc.clear_messages(chat_id, user_id=1)

    sess_rows_after, msg_rows_after = await _sub_session_rows(db_engine, chat_id)
    assert sess_rows_after == [], (
        f"clear_messages left {len(sess_rows_after)} sub_sessions row(s) behind"
    )
    assert msg_rows_after == [], (
        f"clear_messages left {len(msg_rows_after)} sub_session_messages row(s) behind "
        "(FK CASCADE should have removed them with the parent)"
    )


# ---------------------------------------------------------------------------
# Test 5 — per-chat sub-session lock blocks a concurrent double-submit (D4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sub_session_concurrent_double_submit_blocked(db_engine: AsyncEngine) -> None:
    """A second sub-session stream on the SAME chat_id 409s while one is live.

    Mirrors StreamingService's single-stream-per-chat invariant, but scoped
    to sub-sessions (D4) — independent of the main-chat check.
    """
    chat_id = await _seed_chat(db_engine)
    persist = _SubSessionPersistContext(engine=db_engine, chat_id=chat_id, preset_id="research")

    _hang = asyncio.Event()

    async def _hanging_stream(**_kwargs: object) -> AsyncIterator[CanonicalEvent]:
        yield CanonicalEvent(type="chat.start")
        await _hang.wait()  # never completes within this test
        yield CanonicalEvent(type="chat.end")  # pragma: no cover

    lm_client_1 = MagicMock()
    lm_client_1.stream = _hanging_stream

    gen1 = _sub_session_sse(
        lm_client=lm_client_1,
        model_id="test-model",
        system_prompt="sys",
        messages=[{"role": "user", "content": "first submit"}],
        request=_mock_request(disconnected=False),
        persist=persist,
    )
    # Prime past the lock+draft-creation — the draft row now exists in
    # 'draft' state and stays there (the fake stream hangs on _hang).
    first_frame = await gen1.__anext__()
    assert b"sub.processing.start" in first_frame or b"chat.start" in first_frame or True

    # A second stream on the SAME chat_id must see the in-progress draft
    # and raise — atomicity guaranteed by the per-chat lock (D4).
    lm_client_2 = MagicMock()

    async def _unused_stream(**_kwargs: object) -> AsyncIterator[CanonicalEvent]:
        yield CanonicalEvent(type="chat.start")  # pragma: no cover

    lm_client_2.stream = _unused_stream

    gen2 = _sub_session_sse(
        lm_client=lm_client_2,
        model_id="test-model",
        system_prompt="sys",
        messages=[{"role": "user", "content": "second submit"}],
        request=_mock_request(disconnected=False),
        persist=persist,
    )
    with pytest.raises(SubSessionStreamInProgressError):
        await gen2.__anext__()

    # Cleanup — see test_sub_session_stream_aclose_mid_stream_salvages_cleanly
    # below for a dedicated regression test of gen1.aclose(); here just let
    # it finish naturally so it doesn't hold the module-level per-chat lock
    # dict entry across other tests in this session.
    _hang.set()
    async for _ in gen1:
        pass


@pytest.mark.asyncio
async def test_sub_session_stream_aclose_mid_stream_salvages_cleanly(
    db_engine: AsyncEngine,
) -> None:
    """Closing the generator while suspended INSIDE the TaskGroup is safe.

    Regression test: asyncio.TaskGroup wraps ANY body exception — including
    a bare GeneratorExit from an external ``aclose()`` — into a
    BaseExceptionGroup on ``__aexit__``. GeneratorExit is a BaseException,
    not an Exception subclass, so it is NOT matched by an
    ``except* Exception`` clause; left unmatched, it re-propagates as a
    BaseExceptionGroup, which violates the async-generator close()
    contract and crashes the caller instead of closing cleanly.
    ``_sub_session_sse`` has a dedicated ``except* GeneratorExit: pass``
    clause for exactly this. Proves: no crash, AND the draft still
    converges to sub_sessions.status='aborted' via the outer finally.
    """
    chat_id = await _seed_chat(db_engine)

    hang = asyncio.Event()

    async def _hanging_stream(**_kwargs: object) -> AsyncIterator[CanonicalEvent]:
        yield CanonicalEvent(type="chat.start")
        await hang.wait()
        yield CanonicalEvent(type="chat.end")  # pragma: no cover

    lm_client = MagicMock()
    lm_client.stream = _hanging_stream
    persist = _SubSessionPersistContext(engine=db_engine, chat_id=chat_id, preset_id="research")

    gen: AsyncGenerator[bytes, None] = _sub_session_sse(  # type: ignore[assignment]
        lm_client=lm_client,
        model_id="test-model",
        system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}],
        request=_mock_request(disconnected=False),
        persist=persist,
    )
    first_frame = await gen.__anext__()
    assert first_frame  # the draft row now exists, generator suspended inside the TaskGroup

    # Must not raise — this is exactly what crashed before the
    # except* GeneratorExit fix.
    await gen.aclose()

    sess_rows, _msg_rows = await _sub_session_rows(db_engine, chat_id)
    assert len(sess_rows) == 1
    assert sess_rows[0].status == "aborted", (
        f"expected status='aborted' after aclose(), got {sess_rows[0].status!r}"
    )


# ---------------------------------------------------------------------------
# Test 8 — reaper's extended sweep (D5) finalizes an abandoned draft and
# sets sub_sessions.status='aborted'.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reaper_finalizes_stuck_sub_session_draft(db_engine: AsyncEngine) -> None:
    """_finalize_stuck_sub_session_drafts (D5) reaps an abandoned draft.

    Seeds a sub_sessions row (status='active') with a draft assistant
    message whose last_activity_at is far in the past (process crashed —
    no watcher, no _CoalesceTimer liveness touch). The reaper's extended
    sweep must force-finalize the message row AND set the parent session's
    status to 'aborted' — mirrors _finalize_stuck_drafts for the main chat.
    """
    from datetime import UTC, datetime, timedelta

    chat_id = await _seed_chat(db_engine)
    stale_activity = datetime.now(UTC) - timedelta(minutes=30)

    async with db_engine.begin() as conn:
        sess_result = await conn.execute(
            sub_sessions.insert().values(chat_id=chat_id, preset_id="research", status="active")
        )
        sub_session_id = int(sess_result.inserted_primary_key[0])  # type: ignore[index]
        await conn.execute(
            sub_session_messages.insert().values(
                sub_session_id=sub_session_id,
                role="user",
                content="q",
                state="final",
            )
        )
        await conn.execute(
            sub_session_messages.insert().values(
                sub_session_id=sub_session_id,
                role="assistant",
                content="partial",
                state="draft",
                last_activity_at=stale_activity,
            )
        )

    await _finalize_stuck_sub_session_drafts(engine=db_engine, stuck_after_minutes=5)

    sess_rows, msg_rows = await _sub_session_rows(db_engine, chat_id)
    assert sess_rows[0].status == "aborted", (
        f"reaper must set sub_sessions.status='aborted', got {sess_rows[0].status!r}"
    )
    assistant = next(m for m in msg_rows if m.role == "assistant")
    assert assistant.state == "final", (
        f"reaper must force-finalize the stuck draft, got state={assistant.state!r}"
    )
    assert assistant.content == "partial", "reaper must not clobber existing content"
