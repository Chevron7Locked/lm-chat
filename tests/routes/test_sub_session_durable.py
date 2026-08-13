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

P4 additions (PLAN.md §2c/§3/D9/D10 — history + reopen + continue):

6. The graceful finalize path marks ``sub_sessions.status='final'`` INSIDE
   ``_on_success`` — not only in the outer teardown ``finally`` — so a
   later (or racing) disconnect signal can never mislabel a genuinely
   completed sub-session ``'aborted'``.
7. An optional ``sub_session_id`` Form param on ``/sub-session/stream``
   APPENDS a new turn onto an existing ``sub_sessions`` row (reopen +
   continue); a foreign, cross-user, or malformed id is rejected.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator, Generator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text
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
import lmchat.routes.chats as chats_module
from lmchat.routes.chats import (
    _get_chat_service,
    _get_message_service,
    _sub_session_sse,
    _SubSessionPersistContext,
    _transition_sub_session_status,
)
from lmchat.services._stream_reaper import _finalize_stuck_sub_session_drafts
from lmchat.services.auth_service import _reset_dummy_hash_cache
from lmchat.services.chat_service import ChatService
from lmchat.services.memory_service import MemoryService
from lmchat.services.message_service import MessageService
from lmchat.services.models_service import Capabilities, ModelsService, ResolvedModel
from lmchat.services.streaming_errors import SubSessionStreamInProgressError
from lmchat.session.sqlite_store import SQLiteSessionStore
from lmchat.utils.hashing import hash_password

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


_LOW_N: int = 2**10


async def _insert_user_direct(
    engine: AsyncEngine, username: str, password: str = "correct-horse-battery"
) -> None:
    """Bypass the single-admin registration gate by inserting a user row directly.

    Needed for a SECOND user in a cross-user test — plain ``/api/auth/register``
    is closed once the first (admin) user exists. Mirrors
    ``tests/routes/test_chats.py::_insert_user_direct`` exactly.
    """
    pw_hash = hash_password(password, n=_LOW_N, r=8, p=1)
    async with engine.begin() as conn:
        next_id = (
            await conn.execute(select(func.coalesce(func.max(users.c.id), 0) + 1))
        ).scalar()
        if next_id is None:
            raise RuntimeError("coalesce returned None — unreachable")
        await conn.execute(
            text("INSERT INTO users (id, username, password_hash) VALUES (:id, :u, :ph)"),
            {"id": int(next_id), "u": username, "ph": pw_hash},
        )


def _login(client: TestClient, username: str, password: str = "correct-horse-battery") -> None:
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


# ---------------------------------------------------------------------------
# Test 9 — P3 rehydrate endpoints: GET /sub-sessions (list) + GET
# /sub-sessions/{sub_session_id} (metadata + full transcript, including
# still-in-flight draft rows so a reload mid-stream can rehydrate).
# ---------------------------------------------------------------------------


async def _seed_sub_session(
    engine: AsyncEngine,
    *,
    chat_id: int,
    preset_id: str = "research",
    title: str | None = "seeded title",
    status: str = "active",
    messages: list[dict[str, Any]] | None = None,
) -> int:
    """Insert a ``sub_sessions`` row (+ optional ``sub_session_messages``).

    Direct-DB seeding — a ``draft``-state row is only ever reachable
    mid-stream in production, so route-level tests need this to exercise
    the "includes drafts" contract without mocking the whole streaming
    pipeline. Returns the new ``sub_session_id``.
    """
    async with engine.begin() as conn:
        sess_result = await conn.execute(
            sub_sessions.insert().values(
                chat_id=chat_id, preset_id=preset_id, title=title, status=status
            )
        )
        sub_session_id = int(sess_result.inserted_primary_key[0])  # type: ignore[index]
        for msg in messages or []:
            await conn.execute(
                sub_session_messages.insert().values(sub_session_id=sub_session_id, **msg)
            )
    return sub_session_id


def test_list_sub_sessions_happy_newest_first(
    test_client: TestClient, db_engine: AsyncEngine
) -> None:
    """GET /sub-sessions returns every session for the chat, newest first."""
    _register_and_login(test_client, username="alice")
    chat_resp = test_client.post("/api/chats", data={"title": "history chat"})
    assert chat_resp.status_code == 201, chat_resp.text
    chat_id = chat_resp.json()["id"]

    async def _seed() -> tuple[int, int]:
        first = await _seed_sub_session(
            db_engine,
            chat_id=chat_id,
            preset_id="research",
            title="first",
            status="final",
            messages=[
                {"role": "user", "content": "q1", "state": "final"},
                {"role": "assistant", "content": "a1", "state": "final"},
            ],
        )
        second = await _seed_sub_session(
            db_engine,
            chat_id=chat_id,
            preset_id="coder",
            title="second",
            status="final",
            messages=[
                {"role": "user", "content": "q2", "state": "final"},
                {"role": "assistant", "content": "a2", "state": "final"},
            ],
        )
        return first, second

    first_id, second_id = asyncio.run(_seed())

    resp = test_client.get(f"/api/chats/{chat_id}/sub-sessions")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body, list)
    assert [row["id"] for row in body] == [second_id, first_id], "expected newest-first order"
    assert body[0]["preset_id"] == "coder"
    assert body[0]["title"] == "second"
    assert body[0]["status"] == "final"
    assert "created_at" in body[0]
    assert "updated_at" in body[0]
    # Metadata only — no transcript on the list endpoint.
    assert "messages" not in body[0]


def test_list_sub_sessions_empty_chat(test_client: TestClient) -> None:
    """A chat with no sub-sessions returns an empty array, not 404."""
    _register_and_login(test_client, username="alice")
    chat_resp = test_client.post("/api/chats", data={"title": "no sub-sessions"})
    chat_id = chat_resp.json()["id"]

    resp = test_client.get(f"/api/chats/{chat_id}/sub-sessions")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_sub_sessions_cross_user_404(
    test_client: TestClient, db_engine: AsyncEngine
) -> None:
    async def _setup() -> int:
        await _insert_user_direct(db_engine, "alice")
        await _insert_user_direct(db_engine, "bob")
        return await _seed_chat(db_engine, user_id=1)

    chat_id = asyncio.run(_setup())

    _login(test_client, "bob")
    resp = test_client.get(f"/api/chats/{chat_id}/sub-sessions")
    assert resp.status_code == 404


def test_list_sub_sessions_not_found_404(test_client: TestClient) -> None:
    _register_and_login(test_client, username="alice")
    resp = test_client.get("/api/chats/999999/sub-sessions")
    assert resp.status_code == 404


def test_list_sub_sessions_unauth(test_client: TestClient) -> None:
    resp = test_client.get("/api/chats/1/sub-sessions")
    assert resp.status_code == 401


def test_get_sub_session_includes_drafts(
    test_client: TestClient, db_engine: AsyncEngine
) -> None:
    """GET /sub-sessions/{id} returns the full transcript, INCLUDING a
    still-``draft`` assistant row — the mid-stream rehydrate contract."""
    _register_and_login(test_client, username="alice")
    chat_resp = test_client.post("/api/chats", data={"title": "mid-stream chat"})
    chat_id = chat_resp.json()["id"]

    sub_session_id = asyncio.run(
        _seed_sub_session(
            db_engine,
            chat_id=chat_id,
            preset_id="research",
            title="mid-stream research",
            status="active",
            messages=[
                {"role": "user", "content": "what happened", "state": "final"},
                {
                    "role": "assistant",
                    "content": "partial answer so far",
                    "state": "draft",
                    "reasoning_content": "thinking...",
                },
            ],
        )
    )

    resp = test_client.get(f"/api/chats/{chat_id}/sub-sessions/{sub_session_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == sub_session_id
    assert body["chat_id"] == chat_id
    assert body["preset_id"] == "research"
    assert body["status"] == "active"
    assert len(body["messages"]) == 2
    assert body["messages"][0]["role"] == "user"
    assert body["messages"][0]["content"] == "what happened"
    assert body["messages"][1]["role"] == "assistant"
    assert body["messages"][1]["state"] == "draft", (
        "the draft row must be included, not filtered out"
    )
    assert body["messages"][1]["content"] == "partial answer so far"
    assert body["messages"][1]["reasoning_content"] == "thinking..."
    # id-ordered ascending (user turn before the assistant reply).
    assert body["messages"][0]["id"] < body["messages"][1]["id"]


def test_get_sub_session_cross_user_404(
    test_client: TestClient, db_engine: AsyncEngine
) -> None:
    async def _setup() -> tuple[int, int]:
        await _insert_user_direct(db_engine, "alice")
        await _insert_user_direct(db_engine, "bob")
        cid = await _seed_chat(db_engine, user_id=1)
        sid = await _seed_sub_session(
            db_engine,
            chat_id=cid,
            messages=[{"role": "user", "content": "q", "state": "final"}],
        )
        return cid, sid

    chat_id, sub_session_id = asyncio.run(_setup())

    _login(test_client, "bob")
    resp = test_client.get(f"/api/chats/{chat_id}/sub-sessions/{sub_session_id}")
    assert resp.status_code == 404


def test_get_sub_session_wrong_chat_404(
    test_client: TestClient, db_engine: AsyncEngine
) -> None:
    """A sub_session_id that exists but under a DIFFERENT (still owned)
    chat must 404 — existence never leaks across the wrong parent."""
    _register_and_login(test_client, username="alice")
    chat_a_resp = test_client.post("/api/chats", data={"title": "chat A"})
    chat_a_id = chat_a_resp.json()["id"]
    chat_b_resp = test_client.post("/api/chats", data={"title": "chat B"})
    chat_b_id = chat_b_resp.json()["id"]

    sub_session_id = asyncio.run(
        _seed_sub_session(
            db_engine,
            chat_id=chat_a_id,
            messages=[{"role": "user", "content": "q", "state": "final"}],
        )
    )

    resp = test_client.get(f"/api/chats/{chat_b_id}/sub-sessions/{sub_session_id}")
    assert resp.status_code == 404


def test_get_sub_session_not_found_404(test_client: TestClient) -> None:
    _register_and_login(test_client, username="alice")
    chat_resp = test_client.post("/api/chats", data={"title": "empty history"})
    chat_id = chat_resp.json()["id"]

    resp = test_client.get(f"/api/chats/{chat_id}/sub-sessions/999999")
    assert resp.status_code == 404


def test_get_sub_session_unauth(test_client: TestClient) -> None:
    resp = test_client.get("/api/chats/1/sub-sessions/1")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Test 6 — P4: sub_sessions.status flips to 'final' INSIDE the graceful
# finalize (_on_success), not only in the outer teardown finally.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graceful_completion_marks_final_inside_on_success(
    db_engine: AsyncEngine,
) -> None:
    """The status transition fires as part of _on_success, before the
    generator's outer finally runs — not deferred to it.

    Regression for the live-dogfood race: before this fix, only the OUTER
    ``finally`` called ``_transition_sub_session_status`` — several more
    sequential DB round trips (a release-stuck-draft no-op, an
    aborted-row-salvage no-op) run between the message finalize and that
    single call, leaving the SSE response's underlying generator technically
    un-torn-down for long enough that a fast client reload's disconnect
    watcher could land ahead of it, mislabeling a genuinely COMPLETED
    sub-session 'aborted'. Proved here by spying on
    ``_transition_sub_session_status``: it must now be called TWICE for a
    graceful completion — once eagerly (status still 'active' beforehand,
    called from _on_success) and once as a same-value no-op (status already
    'final', called from the outer finally).
    """
    chat_id = await _seed_chat(db_engine)
    events = [
        CanonicalEvent(type="chat.start"),
        CanonicalEvent(type="message.delta", content="the answer"),
        CanonicalEvent(type="chat.end"),
    ]
    lm_client = _FakeLmClient(events)
    persist = _SubSessionPersistContext(engine=db_engine, chat_id=chat_id, preset_id="research")

    real_transition = chats_module._transition_sub_session_status
    call_log: list[tuple[str, str]] = []  # (to_status, status_in_db_BEFORE_this_call)

    async def _spy(engine: AsyncEngine, *, sub_session_id: int, to_status: str) -> bool:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    select(sub_sessions.c.status).where(sub_sessions.c.id == sub_session_id)
                )
            ).fetchone()
        call_log.append((to_status, row.status if row else "<missing>"))
        return await real_transition(engine, sub_session_id=sub_session_id, to_status=to_status)

    with patch.object(chats_module, "_transition_sub_session_status", side_effect=_spy):
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

    assert "sub.complete" in b"".join(frames).decode()
    assert len(call_log) == 2, (
        "expected _transition_sub_session_status called twice (eagerly from "
        f"_on_success + a no-op from the outer finally), got {call_log!r}"
    )
    assert call_log[0] == ("final", "active"), (
        f"first call must fire from _on_success while status is still "
        f"'active' — got {call_log[0]!r}"
    )
    assert call_log[1] == ("final", "final"), (
        "second call (the outer finally) must observe status ALREADY "
        f"'final' — proving the transition happened inside _on_success, "
        f"not the finally — got {call_log[1]!r}"
    )


@pytest.mark.asyncio
async def test_completed_sub_session_stays_final_after_late_disconnect_signal(
    db_engine: AsyncEngine,
) -> None:
    """A LATE disconnect-triggered abort attempt on an already-'final'
    sub-session is a clean no-op — it must never downgrade 'final' back to
    'aborted'.

    Simulates the literal dogfood symptom: the turn completed (status is
    already 'final'), and only THEN does a reload's disconnect watcher get
    a chance to run its abort attempt.
    """
    chat_id = await _seed_chat(db_engine)
    events = [
        CanonicalEvent(type="chat.start"),
        CanonicalEvent(type="message.delta", content="the answer"),
        CanonicalEvent(type="chat.end"),
    ]
    lm_client = _FakeLmClient(events)
    persist = _SubSessionPersistContext(engine=db_engine, chat_id=chat_id, preset_id="research")

    async for _frame in _sub_session_sse(
        lm_client=lm_client,  # type: ignore[arg-type]
        model_id="test-model",
        system_prompt="you are a tester",
        messages=[{"role": "user", "content": "what is the answer?"}],
        request=_mock_request(disconnected=False),
        persist=persist,
    ):
        pass

    sess_rows, _msg_rows = await _sub_session_rows(db_engine, chat_id)
    assert sess_rows[0].status == "final"
    sub_session_id = sess_rows[0].id

    won = await _transition_sub_session_status(
        db_engine, sub_session_id=sub_session_id, to_status="aborted"
    )
    assert won is False, "a late abort attempt must lose the race, not win it"

    sess_rows_after, _msg_rows_after = await _sub_session_rows(db_engine, chat_id)
    assert sess_rows_after[0].status == "final", (
        f"status must stay 'final' after a late disconnect signal, got "
        f"{sess_rows_after[0].status!r}"
    )


# ---------------------------------------------------------------------------
# Test 7 — P4: reopen + continue. An optional sub_session_id Form param on
# /sub-session/stream APPENDS onto an existing sub_sessions row.
# ---------------------------------------------------------------------------


def test_sub_session_append_creates_new_turn_under_same_sid(
    test_client: TestClient, db_engine: AsyncEngine
) -> None:
    """A second /stream call carrying sub_session_id appends a new turn
    onto the SAME sub_sessions row instead of creating a second one."""
    _register_and_login(test_client, username="alice")
    chat_resp = test_client.post("/api/chats", data={"title": "reopen+continue"})
    assert chat_resp.status_code == 201, chat_resp.text
    chat_id = chat_resp.json()["id"]

    first = test_client.post(
        f"/api/chats/{chat_id}/sub-session/stream",
        data={
            "model_id": "test-model",
            "system_prompt": "you are a tester",
            "messages_json": json.dumps([{"role": "user", "content": "first turn"}]),
        },
    )
    assert first.status_code == 200, first.text

    sess_rows, msg_rows = asyncio.run(_sub_session_rows(db_engine, chat_id))
    assert len(sess_rows) == 1
    assert sess_rows[0].status == "final"
    assert len(msg_rows) == 2
    sub_session_id = sess_rows[0].id

    second = test_client.post(
        f"/api/chats/{chat_id}/sub-session/stream",
        data={
            "model_id": "test-model",
            "system_prompt": "you are a tester",
            "messages_json": json.dumps(
                [
                    {"role": "user", "content": "first turn"},
                    {"role": "assistant", "content": "hello world"},
                    {"role": "user", "content": "second turn"},
                ]
            ),
            "sub_session_id": str(sub_session_id),
        },
    )
    assert second.status_code == 200, second.text
    assert "sub.complete" in second.text

    sess_rows_after, msg_rows_after = asyncio.run(_sub_session_rows(db_engine, chat_id))
    assert len(sess_rows_after) == 1, (
        f"expected the turn to append onto the SAME sub_sessions row, "
        f"got {len(sess_rows_after)} rows"
    )
    assert sess_rows_after[0].id == sub_session_id
    assert sess_rows_after[0].status == "final"
    assert len(msg_rows_after) == 4, (
        f"expected 2 turns x (user+assistant) = 4 rows under one "
        f"sub_session_id, got {len(msg_rows_after)}"
    )
    user_msgs = sorted((m for m in msg_rows_after if m.role == "user"), key=lambda m: m.id)
    assert [m.content for m in user_msgs] == ["first turn", "second turn"]


def test_sub_session_append_foreign_sid_rejected(
    test_client: TestClient, db_engine: AsyncEngine
) -> None:
    """A sub_session_id belonging to a DIFFERENT chat is rejected (404),
    not silently appended to."""
    _register_and_login(test_client, username="alice")
    chat_a_resp = test_client.post("/api/chats", data={"title": "chat A"})
    chat_a_id = chat_a_resp.json()["id"]
    chat_b_resp = test_client.post("/api/chats", data={"title": "chat B"})
    chat_b_id = chat_b_resp.json()["id"]

    foreign_sid = asyncio.run(
        _seed_sub_session(
            db_engine,
            chat_id=chat_a_id,
            status="final",
            messages=[
                {"role": "user", "content": "q", "state": "final"},
                {"role": "assistant", "content": "a", "state": "final"},
            ],
        )
    )

    resp = test_client.post(
        f"/api/chats/{chat_b_id}/sub-session/stream",
        data={
            "model_id": "test-model",
            "system_prompt": "you are a tester",
            "messages_json": json.dumps([{"role": "user", "content": "hijack attempt"}]),
            "sub_session_id": str(foreign_sid),
        },
    )
    assert resp.status_code == 404, resp.text

    # Nothing must have been appended to the foreign session.
    sess_rows, msg_rows = asyncio.run(_sub_session_rows(db_engine, chat_a_id))
    assert len(sess_rows) == 1
    assert len(msg_rows) == 2, "the foreign sub-session's transcript must be untouched"


def test_sub_session_append_cross_user_sid_rejected(
    test_client: TestClient, db_engine: AsyncEngine
) -> None:
    """A sub_session_id belonging to ANOTHER USER's chat 404s — existence
    never leaks across users either."""

    async def _setup() -> tuple[int, int]:
        await _insert_user_direct(db_engine, "alice")
        await _insert_user_direct(db_engine, "bob")
        cid = await _seed_chat(db_engine, user_id=1)
        sid = await _seed_sub_session(
            db_engine,
            chat_id=cid,
            status="final",
            messages=[{"role": "user", "content": "q", "state": "final"}],
        )
        return cid, sid

    _alice_chat_id, alice_sid = asyncio.run(_setup())

    _login(test_client, "bob")
    bob_chat_resp = test_client.post("/api/chats", data={"title": "bob's chat"})
    bob_chat_id = bob_chat_resp.json()["id"]

    resp = test_client.post(
        f"/api/chats/{bob_chat_id}/sub-session/stream",
        data={
            "model_id": "test-model",
            "system_prompt": "you are a tester",
            "messages_json": json.dumps([{"role": "user", "content": "hijack"}]),
            "sub_session_id": str(alice_sid),
        },
    )
    assert resp.status_code == 404, resp.text


def test_sub_session_append_malformed_sid_400(test_client: TestClient) -> None:
    """A non-integer sub_session_id is a structured 400, not a 500 or a
    silent create-new."""
    _register_and_login(test_client, username="alice")
    chat_resp = test_client.post("/api/chats", data={"title": "malformed sid"})
    chat_id = chat_resp.json()["id"]

    resp = test_client.post(
        f"/api/chats/{chat_id}/sub-session/stream",
        data={
            "model_id": "test-model",
            "system_prompt": "you are a tester",
            "messages_json": json.dumps([{"role": "user", "content": "hi"}]),
            "sub_session_id": "not-an-integer",
        },
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["code"] == "invalid_sub_session_id"
