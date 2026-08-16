# SPDX-License-Identifier: Apache-2.0
"""Tests for replay-mode context wiring in StreamingService.

Verifies:
(a) A chat with settings.provider="openrouter" routes to the replay
    provider: FULL history is threaded through the resolved provider's
    stream_chat(), the "[Context] … via LM Studio" block is absent from
    the request system_prompt, and resolve_to_loaded_or_fallback is NOT
    called.
(b) A chat with no provider (default / "lmstudio") is byte-identical to
    the chain path: history=None to lm_client.stream(), the LM Studio
    context block IS present, resolution runs normally.
(c) RAG block reaches a replay request: the RAG context injected
    into system_prompt survives to the wire without relocation.
(d) Unknown provider emits an error frame and does NOT dispatch to LM Studio.
(e) DISPATCH TARGET: replay routes to the resolved provider's stream_chat,
    NOT lm_client; chain routes to lm_client and NOT the provider.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from lmchat.db.schema import chats, messages, metadata
from lmchat.lmstudio.types import (
    CanonicalChatRequest,
    CanonicalEvent,
    CanonicalInputBlock,
    CanonicalMessage,
    CanonicalToolCall,
)
from lmchat.services._stream_state import PersistState
from lmchat.services.streaming_service import (
    ChatStreamRequest,
    StreamingService,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_engine() -> AsyncEngine:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return engine


def _make_payload(model: str = "test-model", chat_text: str = "hello") -> ChatStreamRequest:
    return ChatStreamRequest(
        chat_id=1,
        payload=CanonicalChatRequest(
            model=model,
            input=[CanonicalInputBlock(type="text", content=chat_text)],
        ),
    )


def _happy_events(response_id: str = "rid-1") -> list[CanonicalEvent]:
    return [
        CanonicalEvent(type="chat.start"),
        CanonicalEvent(type="message.start"),
        CanonicalEvent(type="message.delta", content="hi"),
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


def _make_stub_provider(
    context_mode: str = "replay",
    *,
    captured: dict[str, Any] | None = None,
) -> MagicMock:
    """Build a stub ChatProvider.

    If *captured* is provided, the provider's stream_chat records keyword
    args there so callers can assert what was sent to the provider.
    """
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
    """Stub ProviderRegistry.get() returning *provider* for *name*."""
    reg = MagicMock()

    def _get(n: str) -> object | None:
        if n == name:
            return provider
        return None

    reg.get = _get
    return reg


async def _insert_chat(engine: AsyncEngine, *, settings: dict | None = None) -> int:  # type: ignore[type-arg]
    """Insert a test chat row; return its id."""
    async with engine.begin() as conn:
        result = await conn.execute(
            chats.insert().values(
                user_id=1,
                title="test",
                settings=settings or {},
            )
        )
        return result.inserted_primary_key[0]  # type: ignore[index]


async def _insert_final_messages(
    engine: AsyncEngine,
    chat_id: int,
    rows: list[dict],  # type: ignore[type-arg]
) -> None:
    """Insert FINAL messages into the test DB for history tests."""
    async with engine.begin() as conn:
        for row in rows:
            row_data: dict = {  # type: ignore[type-arg]
                "chat_id": chat_id,
                "role": row["role"],
                "content": row.get("content", ""),
                "state": PersistState.FINAL.value,
                "model_id": "test-model",
            }
            if "response_id" in row:
                row_data["response_id"] = row["response_id"]
            await conn.execute(messages.insert().values(**row_data))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = await _make_engine()
    yield eng
    await eng.dispose()


# ---------------------------------------------------------------------------
# (a) Replay path — provider=openrouter in chat settings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_path_threads_full_history(engine: AsyncEngine) -> None:
    """Replay provider receives full history + no LM Studio context block.

    Given:
    - A chat with settings.provider="openrouter"
    - Two existing FINAL messages (user + assistant)
    - A stub ProviderRegistry returning a replay provider for "openrouter"

    Asserts:
    - provider.stream_chat() is called with history= containing the 2 prior msgs
    - system_prompt seen on the wire does NOT contain "via LM Studio"
    - resolve_to_loaded_or_fallback is NOT called (no models_service needed)
    """
    chat_id = await _insert_chat(engine, settings={"provider": "openrouter"})

    # Insert 2 prior FINAL messages for history load.
    await _insert_final_messages(
        engine,
        chat_id,
        [
            {"role": "user", "content": "first question", "response_id": None},
            {"role": "assistant", "content": "first answer", "response_id": "rid-0"},
        ],
    )

    # Capture what stream_chat is called with on the provider.
    captured: dict[str, Any] = {}

    lm_client = MagicMock()
    # lm_client.stream must NOT be invoked in replay mode.
    lm_client.stream = MagicMock(side_effect=AssertionError(
        "lm_client.stream must NOT be called in replay mode"
    ))

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
        # No models_service — replay should not call resolve_to_loaded_or_fallback.
    )

    await _drain(
        svc.stream_chat(
            chat_id=chat_id,
            user=_mock_user(1),
            payload=_make_payload(),
            request=_mock_request(),
        )
    )

    # (1) history was threaded to the provider.
    # Includes the 2 seeded FINAL messages; may also include the current-
    # turn user row if _create_draft persists it as FINAL before the history
    # query runs. Assert on presence of the seeded rows rather than exact count.
    assert "history" in captured, "history kwarg not passed to provider.stream_chat"
    history = captured["history"]
    assert history is not None, "history must not be None in replay mode"
    assert len(history) >= 2, f"Expected at least 2 history messages, got {len(history)}"
    assert isinstance(history[0], CanonicalMessage)
    roles_contents = [(m.role, m.content) for m in history]
    assert ("user", "first question") in roles_contents, (
        f"Seeded user message not in history: {roles_contents}"
    )
    assert ("assistant", "first answer") in roles_contents, (
        f"Seeded assistant message not in history: {roles_contents}"
    )

    # (2) The request on the wire must not claim LM Studio.
    wire_req: CanonicalChatRequest = captured["request"]
    sys_p = wire_req.system_prompt or ""
    assert "LM Studio" not in sys_p, (
        "Replay provider must not receive the 'via LM Studio' context block. "
        f"Got system_prompt: {sys_p!r}"
    )


@pytest.mark.asyncio
async def test_replay_path_no_model_resolution(engine: AsyncEngine) -> None:
    """Replay path does not call resolve_to_loaded_or_fallback.

    A StreamingService without models_service must complete successfully
    in replay mode (chain mode would yield an error because it tries to
    resolve the model id against LM Studio's loaded instance list).
    """
    chat_id = await _insert_chat(engine, settings={"provider": "openrouter"})

    lm_client = MagicMock()
    lm_client.stream = MagicMock(side_effect=AssertionError(
        "lm_client.stream must NOT be called in replay mode"
    ))

    replay_provider = _make_stub_provider("replay")
    registry = _make_registry(replay_provider, "openrouter")

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    svc = StreamingService(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        chat_locks={},
        provider_registry=registry,
        # Intentionally no models_service — proves replay doesn't need it.
    )

    frames = await _drain(
        svc.stream_chat(
            chat_id=chat_id,
            user=_mock_user(1),
            payload=_make_payload(),
            request=_mock_request(),
        )
    )

    # The stream must complete without an upstream_unavailable error.
    parsed = _parse_frames(frames)
    event_types = [d.get("type") for d in parsed]
    assert "error" not in event_types, (
        f"Replay mode should not emit an error without models_service. "
        f"Got events: {event_types}"
    )
    assert "chat.end" in event_types, "Stream should complete with chat.end"


# ---------------------------------------------------------------------------
# (b) Chain path — default/lmstudio provider is byte-identical to today
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chain_path_unchanged_no_history_to_stream(engine: AsyncEngine) -> None:
    """Default (no provider) path: no history kwarg passed to lm_client.stream().

    The chain path must not thread history — LM Studio manages context
    via previous_response_id on the server side.
    """
    chat_id = await _insert_chat(engine, settings={})  # no provider key

    # Insert a prior message — it must NOT appear in history kwarg for chain.
    await _insert_final_messages(
        engine,
        chat_id,
        [{"role": "user", "content": "previous message", "response_id": None}],
    )

    captured: dict[str, Any] = {}

    async def _fake_stream(**kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        captured.update(kwargs)
        for ev in _happy_events():
            yield ev

    lm_client = MagicMock()
    lm_client.stream = _fake_stream

    # No provider_registry — purely chain/lmstudio path.
    # models_service must be a no-op mock so resolve_to_loaded_or_fallback works.
    models_mock = AsyncMock()
    models_mock.auth_failed = False
    _res = MagicMock()
    _res.wire_id = "test-model"
    _res.substituted = False
    models_mock.resolve_to_loaded_or_fallback = AsyncMock(return_value=_res)
    models_mock.get_capabilities = AsyncMock(side_effect=KeyError("test-model"))
    models_mock.get_max_context_length = AsyncMock(return_value=0)

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    svc = StreamingService(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        chat_locks={},
        models_service=models_mock,
        # No provider_registry → chain path.
    )

    frames = await _drain(
        svc.stream_chat(
            chat_id=chat_id,
            user=_mock_user(1),
            payload=_make_payload(),
            request=_mock_request(),
        )
    )

    # history kwarg must NOT be present in chain mode — the chain path uses
    # the identical call signature as before (no history kwarg at all).
    assert "history" not in captured, (
        f"Chain path must NOT pass history kwarg to lm_client.stream. "
        f"Got captured keys: {list(captured.keys())}"
    )

    # resolve_to_loaded_or_fallback must have been called (chain-specific).
    models_mock.resolve_to_loaded_or_fallback.assert_called()

    # The system prompt MUST contain the LM Studio context block.
    wire_req: CanonicalChatRequest = captured["request"]
    sys_p = wire_req.system_prompt or ""
    assert "LM Studio" in sys_p, (
        "Chain path must include the 'via LM Studio' context block. "
        f"Got system_prompt: {sys_p!r}"
    )

    # Stream must complete.
    parsed = _parse_frames(frames)
    event_types = [d.get("type") for d in parsed]
    assert "chat.end" in event_types


# ---------------------------------------------------------------------------
# (c) RAG block reaches the replay request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_rag_block_in_system_prompt(engine: AsyncEngine) -> None:
    """RAG block injected into system_prompt is visible in replay requests.

    In chain mode, relocate_per_turn_layers moves the RAG block from
    system_prompt into input[0] on follow-up turns (because encode_native
    drops system_prompt when previous_response_id is set).  In replay mode,
    the provider receives full system_prompt every turn — so the RAG block
    must stay in system_prompt and must NOT be relocated.
    """
    chat_id = await _insert_chat(engine, settings={"provider": "openrouter"})

    captured: dict[str, Any] = {}

    lm_client = MagicMock()
    lm_client.stream = MagicMock(side_effect=AssertionError(
        "lm_client.stream must NOT be called in replay mode"
    ))

    replay_provider = _make_stub_provider("replay", captured=captured)
    registry = _make_registry(replay_provider, "openrouter")

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    # Inject a mock embedding_client + models_service so RAG augment runs.
    # We mock the actual rag_service.augment_prompt via monkeypatching to
    # return a known context block without touching the real embedding stack.
    from unittest.mock import patch

    RAG_BLOCK = "[RAG context: this is a test retrieval result]"

    class _FakeAugmentResult:
        context_block = RAG_BLOCK
        memory_hits = 1
        doc_hits = 1
        ctx_window = 0

    # We also need a fake embedding_client so the `if self._embedding_client`
    # guard is True, and a models_service for the same guard.
    fake_ec = MagicMock()
    fake_ms = AsyncMock()

    svc = StreamingService(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        chat_locks={},
        provider_registry=registry,
        embedding_client=fake_ec,
        models_service=fake_ms,
    )

    with patch(
        "lmchat.services.rag_service.augment_prompt",
        new=AsyncMock(return_value=_FakeAugmentResult()),
    ):
        await _drain(
            svc.stream_chat(
                chat_id=chat_id,
                user=_mock_user(1),
                payload=_make_payload(),
                request=_mock_request(),
            )
        )

    # The wire request's system_prompt must contain the RAG block.
    wire_req: CanonicalChatRequest = captured["request"]
    sys_p = wire_req.system_prompt or ""
    assert RAG_BLOCK in sys_p, (
        "RAG block must be present in system_prompt for replay requests. "
        f"Got system_prompt: {sys_p!r}"
    )

    # And the RAG block must NOT be present in the text of the input blocks
    # (i.e., relocate_per_turn_layers did NOT fire for replay).
    input_text = " ".join(
        blk.content or ""
        for blk in wire_req.input
        if blk.type == "text"
    )
    assert RAG_BLOCK not in input_text, (
        "RAG block must not be relocated into input blocks for replay mode. "
        f"Got input text: {input_text!r}"
    )


# ---------------------------------------------------------------------------
# (d) Unknown provider emits error frame and does NOT dispatch to LM Studio
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_provider_emits_error_frame(engine: AsyncEngine) -> None:
    """Unknown provider name in settings → error frame, no LM Studio dispatch."""
    chat_id = await _insert_chat(engine, settings={"provider": "nonexistent-provider"})

    lm_client = MagicMock()
    lm_client.stream = MagicMock(side_effect=AssertionError(
        "lm_client.stream must NOT be called when provider is unknown"
    ))

    # Registry returns None for any name (simulates unregistered provider).
    registry = MagicMock()
    registry.get = MagicMock(return_value=None)

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    svc = StreamingService(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        chat_locks={},
        provider_registry=registry,
    )

    frames = await _drain(
        svc.stream_chat(
            chat_id=chat_id,
            user=_mock_user(1),
            payload=_make_payload(),
            request=_mock_request(),
        )
    )

    parsed = _parse_frames(frames)
    event_types = [d.get("type") for d in parsed]
    # Must emit an error frame with code=unknown_provider.
    assert "error" in event_types, (
        f"Unknown provider must emit an error frame. Got events: {event_types}"
    )
    error_events = [d for d in parsed if d.get("type") == "error"]
    assert any(
        (d.get("error") or {}).get("code") == "unknown_provider"
        for d in error_events
    ), f"Expected error.code='unknown_provider'. Got error events: {error_events}"
    # Must NOT complete normally.
    assert "chat.end" not in event_types, (
        "Unknown provider must not reach chat.end (stream should error-out)."
    )


# ---------------------------------------------------------------------------
# (f) a failed chat.settings read must fail loudly, not
#     silently fall back to lmstudio/chain-mode defaults.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settings_read_failure_emits_error_frame_not_silent_default(
    engine: AsyncEngine,
) -> None:
    """A failed chat.settings read must fail the turn loudly, mirroring
    ``test_unknown_provider_emits_error_frame`` above — an error frame
    with code='settings_unavailable', no LM Studio dispatch.

    Pre-fix, the except branch only logged and left ``_chat_settings={}``
    — the turn continued as if the chat had no settings at all, silently
    downgrading provider, context_mode, self-consistency/CoVe, and
    reasoning_effort with no signal to the caller.
    """
    chat_id = await _insert_chat(engine, settings={"provider": "openrouter"})

    lm_client = MagicMock()
    lm_client.stream = MagicMock(side_effect=AssertionError(
        "lm_client.stream must NOT be called when the settings read fails"
    ))

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    svc = StreamingService(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        chat_locks={},
    )

    # Fail ONLY the chat.settings read (a SELECT of exactly the
    # "settings" column) — every other query on the same in-memory DB
    # (chat lookup, draft creation, ...) runs normally.
    _orig_execute = AsyncConnection.execute

    async def _flaky_execute(
        self: AsyncConnection, statement: Any, *args: Any, **kwargs: Any
    ) -> Any:
        try:
            col_names = [c.name for c in statement.selected_columns]
        except Exception:  # noqa: BLE001
            col_names = []
        if col_names == ["settings"]:
            raise RuntimeError("simulated chats.settings read failure")
        return await _orig_execute(self, statement, *args, **kwargs)

    with patch.object(AsyncConnection, "execute", _flaky_execute):
        frames = await _drain(
            svc.stream_chat(
                chat_id=chat_id,
                user=_mock_user(1),
                payload=_make_payload(),
                request=_mock_request(),
            )
        )

    assert lm_client.stream.call_count == 0, (
        "lm_client.stream must not run once the settings read has failed"
    )

    parsed = _parse_frames(frames)
    event_types = [d.get("type") for d in parsed]
    assert "error" in event_types, (
        f"Settings-load failure must emit an error frame. Got events: {event_types}"
    )
    error_events = [d for d in parsed if d.get("type") == "error"]
    assert any(
        (d.get("error") or {}).get("code") == "settings_unavailable"
        for d in error_events
    ), f"Expected error.code='settings_unavailable'. Got error events: {error_events}"
    assert "chat.end" not in event_types, (
        "Settings-load failure must not reach chat.end (stream should error-out)."
    )


# ---------------------------------------------------------------------------
# (e) DISPATCH TARGET: replay → provider.stream_chat; chain → lm_client.stream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_dispatch_target_is_provider_not_lm_client(engine: AsyncEngine) -> None:
    """Replay mode dispatches through the resolved provider's stream_chat.

    Strict dispatch-target test: the resolved provider's stream_chat is spied
    upon and confirmed to fire. lm_client.stream raises if called, proving it
    is bypassed in replay mode. This is the test whose absence let the bug
    ship undetected.
    """
    chat_id = await _insert_chat(engine, settings={"provider": "openrouter"})

    provider_fired = False

    async def _provider_stream_chat(request: Any, **kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        nonlocal provider_fired
        provider_fired = True
        for ev in _happy_events():
            yield ev

    stub_provider = MagicMock()
    stub_provider.context_mode = "replay"
    stub_provider.name = "openrouter"
    stub_provider.stream_chat = _provider_stream_chat

    # lm_client.stream raises — if it fires, the test fails.
    lm_client = MagicMock()
    lm_client.stream = MagicMock(side_effect=AssertionError(
        "BUG: lm_client.stream was called in replay mode — provider not used"
    ))

    registry = _make_registry(stub_provider, "openrouter")

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    svc = StreamingService(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        chat_locks={},
        provider_registry=registry,
    )

    frames = await _drain(
        svc.stream_chat(
            chat_id=chat_id,
            user=_mock_user(1),
            payload=_make_payload(),
            request=_mock_request(),
        )
    )

    # Provider spy must have fired.
    assert provider_fired, (
        "provider.stream_chat was never called — replay dispatch did not route "
        "through the resolved cloud provider"
    )

    # Stream must complete normally.
    parsed = _parse_frames(frames)
    event_types = [d.get("type") for d in parsed]
    assert "chat.end" in event_types, f"Stream must complete. Got events: {event_types}"


@pytest.mark.asyncio
async def test_chain_dispatch_target_is_lm_client_not_provider(engine: AsyncEngine) -> None:
    """Chain mode dispatches through lm_client.stream, NOT through any provider.

    Complement to the replay dispatch-target test: confirms the symmetric
    case — chain/lmstudio path MUST NOT invoke a cloud provider.
    """
    chat_id = await _insert_chat(engine, settings={})  # no provider → chain

    lm_client_fired = False

    async def _fake_lm_stream(**kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        nonlocal lm_client_fired
        lm_client_fired = True
        for ev in _happy_events():
            yield ev

    lm_client = MagicMock()
    lm_client.stream = _fake_lm_stream

    # models_service mock required for chain path's model resolution.
    models_mock = AsyncMock()
    models_mock.auth_failed = False
    _res = MagicMock()
    _res.wire_id = "test-model"
    _res.substituted = False
    models_mock.resolve_to_loaded_or_fallback = AsyncMock(return_value=_res)
    models_mock.get_capabilities = AsyncMock(side_effect=KeyError("test-model"))
    models_mock.get_max_context_length = AsyncMock(return_value=0)

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    svc = StreamingService(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        chat_locks={},
        models_service=models_mock,
        # No provider_registry → chain path.
    )

    frames = await _drain(
        svc.stream_chat(
            chat_id=chat_id,
            user=_mock_user(1),
            payload=_make_payload(),
            request=_mock_request(),
        )
    )

    # lm_client must have been invoked.
    assert lm_client_fired, (
        "lm_client.stream was never called in chain mode"
    )

    # Stream must complete normally.
    parsed = _parse_frames(frames)
    event_types = [d.get("type") for d in parsed]
    assert "chat.end" in event_types, f"Stream must complete. Got events: {event_types}"


@pytest.mark.asyncio
async def test_replay_history_excludes_current_user_turn(engine: AsyncEngine) -> None:
    """Regression: the current user turn is carried in
    req.input (assemble_compat_messages appends it as the final user msg), and
    _create_draft persists that user row as FINAL before the history query
    runs. The replay history MUST drop that trailing user row so the model does
    not receive the question twice on the wire."""
    chat_id = await _insert_chat(engine, settings={"provider": "openrouter"})
    await _insert_final_messages(
        engine,
        chat_id,
        [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "first answer"},
        ],
    )

    captured: dict[str, Any] = {}

    lm_client = MagicMock()
    lm_client.stream = MagicMock(side_effect=AssertionError(
        "lm_client.stream must NOT be called in replay mode"
    ))

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
    # _make_payload default chat_text="hello" is the CURRENT turn (in req.input).
    await _drain(
        svc.stream_chat(
            chat_id=chat_id,
            user=_mock_user(1),
            payload=_make_payload(chat_text="hello"),
            request=_mock_request(),
        )
    )

    history = captured.get("history")
    assert history is not None
    contents = [m.content for m in history]
    # The current user turn must NOT be duplicated into history (it is in req.input).
    assert "hello" not in contents, (
        f"current user turn duplicated into replay history: {contents}"
    )
    assert "first question" in contents
    assert "first answer" in contents
    # History ends with the prior assistant turn, not the current user turn.
    assert history[-1].role == "assistant", (
        f"history should end with the prior assistant turn: "
        f"{[(m.role, m.content) for m in history]}"
    )


# ---------------------------------------------------------------------------
# replay history must not load system rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_history_excludes_system_rows(engine: AsyncEngine) -> None:
    """Hardening: stray persisted system rows must not enter replay history.

    assemble_compat_messages prepends req.system_prompt as the authoritative
    system block. If a persisted system row were also loaded into history, the
    model would see the system prompt twice. Harden: the allowed-roles filter
    excludes 'system' so no persisted system row can reach the wire.
    """
    chat_id = await _insert_chat(engine, settings={"provider": "openrouter"})

    # Seed a user, assistant, and a stray system row in the DB.
    await _insert_final_messages(
        engine,
        chat_id,
        [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
            {"role": "system", "content": "you are a helpful assistant"},
        ],
    )

    captured: dict[str, Any] = {}

    lm_client = MagicMock()
    lm_client.stream = MagicMock(side_effect=AssertionError(
        "lm_client.stream must NOT be called in replay mode"
    ))

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
            payload=_make_payload(chat_text="new question"),
            request=_mock_request(),
        )
    )

    history = captured.get("history")
    assert history is not None, "history must be loaded for replay mode"

    roles = [m.role for m in history]
    # No system row must appear in the loaded history.
    assert "system" not in roles, (
        f"Stray system row must be excluded from replay history. Got roles: {roles}"
    )
    # User and assistant rows must still be present.
    assert "user" in roles, f"user row missing from history: {roles}"
    assert "assistant" in roles, f"assistant row missing from history: {roles}"


# ---------------------------------------------------------------------------
# LM Studio endpoint-mode toggle (native vs openai_compat) — MCP-system-
# follows-mode wiring. Unlike the (a)-(e) blocks above, ``settings.provider``
# stays the default "lmstudio" in every test here; it's the
# server_lm_studio_default.lm_studio_endpoint_mode row that decides chain vs
# replay for the LM Studio entry.
# ---------------------------------------------------------------------------


async def _seed_endpoint_mode(engine: AsyncEngine, mode: str) -> None:
    """Seed server_lm_studio_default.lm_studio_endpoint_mode directly."""
    from lmchat.db.schema import server_lm_studio_default  # noqa: PLC0415

    async with engine.begin() as conn:
        await conn.execute(
            server_lm_studio_default.insert().values(
                id=1, lm_studio_endpoint_mode=mode
            )
        )


def _make_lmstudio_native_stub(compat_provider: object) -> MagicMock:
    """Stub the native LmstudioAdapter registered under the "lmstudio" name."""
    native = MagicMock()
    native.name = "lmstudio"
    native.context_mode = "chain"
    native.as_openai_compat_provider = MagicMock(return_value=compat_provider)
    return native


@pytest.mark.asyncio
async def test_lmstudio_native_default_ignores_registry_and_uses_lm_client(
    engine: AsyncEngine,
) -> None:
    """Native (default, no row) → lm_client.stream fires; the registry's
    "lmstudio" entry is consulted but as_openai_compat_provider is NEVER
    invoked. Proves the setting is byte-for-byte a no-op when left at
    the default, even though a provider_registry is now present for the
    "lmstudio" name (which was not true before this feature)."""
    chat_id = await _insert_chat(engine, settings={})

    lm_client_fired = False

    async def _fake_lm_stream(**kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        nonlocal lm_client_fired
        lm_client_fired = True
        for ev in _happy_events():
            yield ev

    lm_client = MagicMock()
    lm_client.stream = _fake_lm_stream

    native_stub = _make_lmstudio_native_stub(compat_provider=MagicMock())
    native_stub.as_openai_compat_provider = MagicMock(
        side_effect=AssertionError(
            "as_openai_compat_provider must NOT be called in native mode"
        )
    )
    registry = _make_registry(native_stub, "lmstudio")

    models_mock = AsyncMock()
    models_mock.auth_failed = False
    _res = MagicMock()
    _res.wire_id = "test-model"
    _res.substituted = False
    models_mock.resolve_to_loaded_or_fallback = AsyncMock(return_value=_res)
    models_mock.get_capabilities = AsyncMock(side_effect=KeyError("test-model"))
    models_mock.get_max_context_length = AsyncMock(return_value=0)

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    svc = StreamingService(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        chat_locks={},
        models_service=models_mock,
        provider_registry=registry,
    )

    frames = await _drain(
        svc.stream_chat(
            chat_id=chat_id,
            user=_mock_user(1),
            payload=_make_payload(),
            request=_mock_request(),
        )
    )

    assert lm_client_fired, "lm_client.stream was never called in native mode"
    native_stub.as_openai_compat_provider.assert_not_called()
    parsed = _parse_frames(frames)
    assert "chat.end" in [d.get("type") for d in parsed]


@pytest.mark.asyncio
async def test_lmstudio_openai_compat_mode_dispatches_to_compat_provider(
    engine: AsyncEngine,
) -> None:
    """openai_compat mode → the "lmstudio" registry entry's
    as_openai_compat_provider() result receives the turn (full history,
    replay), and lm_client.stream is NEVER called."""
    chat_id = await _insert_chat(engine, settings={})
    await _seed_endpoint_mode(engine, "openai_compat")

    await _insert_final_messages(
        engine,
        chat_id,
        [
            {"role": "user", "content": "first question", "response_id": None},
            {"role": "assistant", "content": "first answer", "response_id": "rid-0"},
        ],
    )

    captured: dict[str, Any] = {}
    compat_provider = _make_stub_provider("replay", captured=captured)
    compat_provider.name = "lmstudio"

    native_stub = _make_lmstudio_native_stub(compat_provider)
    registry = _make_registry(native_stub, "lmstudio")

    lm_client = MagicMock()
    lm_client.stream = MagicMock(
        side_effect=AssertionError(
            "lm_client.stream must NOT be called in openai_compat mode"
        )
    )

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    svc = StreamingService(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        chat_locks={},
        provider_registry=registry,
        # No models_service — replay should not call resolve_to_loaded_or_fallback.
    )

    # No web_search_service on app.state — this test is about dispatch
    # routing, not the web_search wiring (covered separately in
    # test_lmstudio_openai_compat_web_search_advertised_and_executed).
    # Without this, the AsyncMock auto-vivifies app.state.web_search_service
    # as a truthy child mock, which would spuriously wrap the turn.
    request = _mock_request()
    request.app.state.web_search_service = None

    frames = await _drain(
        svc.stream_chat(
            chat_id=chat_id,
            user=_mock_user(1),
            payload=_make_payload(),
            request=request,
        )
    )

    native_stub.as_openai_compat_provider.assert_called_once()
    assert "history" in captured, "history kwarg not passed to the compat provider"
    history = captured["history"]
    roles_contents = [(m.role, m.content) for m in history]
    assert ("user", "first question") in roles_contents
    assert ("assistant", "first answer") in roles_contents

    parsed = _parse_frames(frames)
    assert "chat.end" in [d.get("type") for d in parsed]


@pytest.mark.asyncio
async def test_lmstudio_openai_compat_reaches_agentic_mcp_gate(
    engine: AsyncEngine,
) -> None:
    """openai_compat + a selected mcp/ integration → the turn is wrapped in
    AgenticMcpProvider (LM Chat's own MCP Store), with the resolved compat
    provider as its `inner`. This is the "MCP system follows the toggle"
    wiring: same gate cloud providers already use, now reachable when
    LM Studio is the provider too."""
    chat_id = await _insert_chat(engine, settings={})
    await _seed_endpoint_mode(engine, "openai_compat")

    compat_provider = _make_stub_provider("replay")
    compat_provider.name = "lmstudio"
    native_stub = _make_lmstudio_native_stub(compat_provider)
    registry = _make_registry(native_stub, "lmstudio")

    lm_client = MagicMock()
    lm_client.stream = MagicMock(
        side_effect=AssertionError("lm_client.stream must NOT be called")
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
            input=[CanonicalInputBlock(type="text", content="search something")],
            integrations=["mcp/searxng"],
        ),
    )

    request = _mock_request()
    request.app.state.mcp_host = MagicMock()
    request.app.state.mcp_server_store = None
    # Irrelevant to this test (mcp/searxng-only); explicit so the assertions
    # below aren't accidentally riding on the AsyncMock's auto-vivified truthy
    # child mock. See test_lmstudio_openai_compat_web_search_advertised_and_executed.
    request.app.state.web_search_service = None

    agentic_stub = _make_stub_provider("replay")

    with patch(
        "lmchat.mcp.agentic.AgenticMcpProvider",
        MagicMock(return_value=agentic_stub),
    ) as mock_agentic_cls:
        frames = await _drain(
            svc.stream_chat(
                chat_id=chat_id,
                user=_mock_user(1),
                payload=payload,
                request=request,
            )
        )

    mock_agentic_cls.assert_called_once()
    assert mock_agentic_cls.call_args.kwargs["inner"] is compat_provider
    assert mock_agentic_cls.call_args.kwargs["server_ids"] == ["searxng"]

    parsed = _parse_frames(frames)
    assert "chat.end" in [d.get("type") for d in parsed]


@pytest.mark.asyncio
async def test_lmstudio_native_mode_does_not_reach_agentic_mcp_gate(
    engine: AsyncEngine,
) -> None:
    """Native (default) + the SAME mcp/ integration → AgenticMcpProvider is
    NEVER constructed. Native routes tool calls through LM Studio's own
    mcp.json host (server-side); the client-side MCP Store gate must stay
    unreachable."""
    chat_id = await _insert_chat(engine, settings={})
    # No _seed_endpoint_mode call — native is the default.

    native_stub = _make_lmstudio_native_stub(compat_provider=MagicMock())
    registry = _make_registry(native_stub, "lmstudio")

    lm_client_fired = False

    async def _fake_lm_stream(**kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        nonlocal lm_client_fired
        lm_client_fired = True
        for ev in _happy_events():
            yield ev

    lm_client = MagicMock()
    lm_client.stream = _fake_lm_stream

    models_mock = AsyncMock()
    models_mock.auth_failed = False
    _res = MagicMock()
    _res.wire_id = "test-model"
    _res.substituted = False
    models_mock.resolve_to_loaded_or_fallback = AsyncMock(return_value=_res)
    models_mock.get_capabilities = AsyncMock(side_effect=KeyError("test-model"))
    models_mock.get_max_context_length = AsyncMock(return_value=0)

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    svc = StreamingService(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        chat_locks={},
        models_service=models_mock,
        provider_registry=registry,
    )

    payload = ChatStreamRequest(
        chat_id=chat_id,
        payload=CanonicalChatRequest(
            model="test-model",
            input=[CanonicalInputBlock(type="text", content="search something")],
            integrations=["mcp/searxng"],
        ),
    )

    request = _mock_request()
    request.app.state.mcp_host = MagicMock()
    request.app.state.mcp_server_store = None

    with patch(
        "lmchat.mcp.agentic.AgenticMcpProvider",
        MagicMock(),
    ) as mock_agentic_cls:
        frames = await _drain(
            svc.stream_chat(
                chat_id=chat_id,
                user=_mock_user(1),
                payload=payload,
                request=request,
            )
        )

    mock_agentic_cls.assert_not_called()
    assert lm_client_fired, "lm_client.stream was never called in native mode"
    parsed = _parse_frames(frames)
    assert "chat.end" in [d.get("type") for d in parsed]


# ---------------------------------------------------------------------------
# Store-sourced MCP integration on a NATIVE LM Studio turn — the wiring fix.
#
# Today: a Store tool selected while LM Studio is in NATIVE endpoint mode is
# forwarded to LM Studio's own mcp.json host, which doesn't know about it —
# the turn silently goes text-only. Fix: classify requested integrations by
# source (curated mcp.json vs Store-installed) and, when at least one is a
# Store server, route the turn through the SAME replay + agentic-MCP
# dispatch openai_compat mode already uses — even though the endpoint-mode
# toggle is left at its native default. Curated-only native turns (the
# existing `..._does_not_reach_agentic_mcp_gate` test above) are untouched.
# ---------------------------------------------------------------------------


class _FakeStoreServer:
    """Minimal stand-in for McpServerSafeView (only the fields the
    classification helper reads: .slug / .enabled / .consented)."""

    def __init__(self, slug: str, *, enabled: bool = True, consented: bool = True) -> None:
        self.slug = slug
        self.enabled = enabled
        self.consented = consented


def _mock_request_with_store(servers: list[_FakeStoreServer] | None) -> AsyncMock:
    """Build a mock request with app.state.mcp_server_store wired to *servers*.

    ``servers=None`` simulates the store not being available on app.state at
    all (degrades to no store present); an empty list simulates an available
    but empty store.
    """
    req = _mock_request()
    if servers is None:
        req.app.state.mcp_server_store = None
    else:
        store = AsyncMock()
        store.list_all = AsyncMock(return_value=servers)
        req.app.state.mcp_server_store = store
    return req


@pytest.mark.asyncio
async def test_lmstudio_native_mode_with_store_integration_reaches_agentic_mcp_gate(
    engine: AsyncEngine,
) -> None:
    """Acceptance test — the core fix.

    Native (default, no endpoint-mode row) + a STORE-sourced mcp/ integration
    → the turn is routed through as_openai_compat_provider() and wrapped in
    AgenticMcpProvider, exactly like the openai_compat + mcp/searxng case in
    ``test_lmstudio_openai_compat_reaches_agentic_mcp_gate`` above. Proves the
    Store tool actually runs against the local model instead of silently
    doing nothing.
    """
    from lmchat.services.mcp_server_store import McpServerSafeView

    chat_id = await _insert_chat(engine, settings={})
    # No _seed_endpoint_mode call — native is the default.

    compat_provider = _make_stub_provider("replay")
    compat_provider.name = "lmstudio"
    native_stub = _make_lmstudio_native_stub(compat_provider)
    registry = _make_registry(native_stub, "lmstudio")

    lm_client = MagicMock()
    lm_client.stream = MagicMock(
        side_effect=AssertionError(
            "lm_client.stream must NOT be called — a Store integration must "
            "route this native turn through the agentic dispatch instead"
        )
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
            input=[CanonicalInputBlock(type="text", content="search something")],
            integrations=["mcp/searxng"],
        ),
    )

    request = _mock_request()
    request.app.state.mcp_host = MagicMock()
    mock_store = AsyncMock()
    mock_store.list_all = AsyncMock(
        return_value=[
            McpServerSafeView(
                id=1,
                slug="searxng",
                name="SearXNG",
                transport="stdio",
                command="searxng-mcp",
                args=None,
                url=None,
                secrets_set=[],
                enabled=True,
                source="store",
                trust="unverified",
                consented=True,
            ),
        ]
    )
    request.app.state.mcp_server_store = mock_store

    agentic_stub = _make_stub_provider("replay")

    with patch(
        "lmchat.mcp.agentic.AgenticMcpProvider",
        MagicMock(return_value=agentic_stub),
    ) as mock_agentic_cls:
        frames = await _drain(
            svc.stream_chat(
                chat_id=chat_id,
                user=_mock_user(1),
                payload=payload,
                request=request,
            )
        )

    native_stub.as_openai_compat_provider.assert_called_once()
    mock_agentic_cls.assert_called_once()
    assert mock_agentic_cls.call_args.kwargs["inner"] is compat_provider
    assert mock_agentic_cls.call_args.kwargs["server_ids"] == ["searxng"]

    parsed = _parse_frames(frames)
    assert "chat.end" in [d.get("type") for d in parsed]


@pytest.mark.asyncio
async def test_lmstudio_native_mode_store_present_unmatched_slug_stays_chain(
    engine: AsyncEngine,
) -> None:
    """Guard rail: classification is per-slug, not "a store exists at all".

    Native mode + a curated-only integration, with an UNRELATED Store server
    installed (different slug) → must stay on the existing native chain path
    (LM Studio's own mcp.json host); AgenticMcpProvider must never be
    constructed and as_openai_compat_provider must never be called.
    """
    from lmchat.services.mcp_server_store import McpServerSafeView

    chat_id = await _insert_chat(engine, settings={})

    native_stub = _make_lmstudio_native_stub(compat_provider=MagicMock())
    native_stub.as_openai_compat_provider = MagicMock(
        side_effect=AssertionError(
            "as_openai_compat_provider must NOT be called — the requested "
            "integration does not match any Store server"
        )
    )
    registry = _make_registry(native_stub, "lmstudio")

    lm_client_fired = False

    async def _fake_lm_stream(**kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        nonlocal lm_client_fired
        lm_client_fired = True
        for ev in _happy_events():
            yield ev

    lm_client = MagicMock()
    lm_client.stream = _fake_lm_stream

    models_mock = AsyncMock()
    models_mock.auth_failed = False
    _res = MagicMock()
    _res.wire_id = "test-model"
    _res.substituted = False
    models_mock.resolve_to_loaded_or_fallback = AsyncMock(return_value=_res)
    models_mock.get_capabilities = AsyncMock(side_effect=KeyError("test-model"))
    models_mock.get_max_context_length = AsyncMock(return_value=0)

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    svc = StreamingService(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        chat_locks={},
        models_service=models_mock,
        provider_registry=registry,
    )

    payload = ChatStreamRequest(
        chat_id=chat_id,
        payload=CanonicalChatRequest(
            model="test-model",
            input=[CanonicalInputBlock(type="text", content="search something")],
            integrations=["mcp/searxng"],  # curated-only slug
        ),
    )

    request = _mock_request()
    request.app.state.mcp_host = MagicMock()
    mock_store = AsyncMock()
    mock_store.list_all = AsyncMock(
        return_value=[
            McpServerSafeView(
                id=1,
                slug="other-tool",  # different slug — no match
                name="Other Tool",
                transport="stdio",
                command="other-tool",
                args=None,
                url=None,
                secrets_set=[],
                enabled=True,
                source="store",
                trust="unverified",
                consented=True,
            ),
        ]
    )
    request.app.state.mcp_server_store = mock_store

    with patch(
        "lmchat.mcp.agentic.AgenticMcpProvider",
        MagicMock(),
    ) as mock_agentic_cls:
        frames = await _drain(
            svc.stream_chat(
                chat_id=chat_id,
                user=_mock_user(1),
                payload=payload,
                request=request,
            )
        )

    mock_agentic_cls.assert_not_called()
    assert lm_client_fired, "lm_client.stream was never called in native mode"
    parsed = _parse_frames(frames)
    assert "chat.end" in [d.get("type") for d in parsed]


# ---------------------------------------------------------------------------
# App-executed web_search on the openai_compat dispatch.
#
# openai_compat is the ONLY branch that flips _ProviderResolution.builtin_web_
# search True; the dispatch fork threads it into maybe_wrap_agentic as
# builtin_registry/builtin_ctx ONLY when app.state.web_search_service is also
# present. Native endpoint mode, the store-integration replay reroute above,
# and cloud providers must all leave web_search disabled.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lmstudio_openai_compat_web_search_advertised_and_executed(
    engine: AsyncEngine,
) -> None:
    """openai_compat mode, NO mcp/ integrations selected, web_search_service
    present on app.state: the real BUILTIN_TOOL_REGISTRY's web_search tool is
    advertised to the model, a requested call executes against
    WebSearchService (not McpHost), and the turn's final answer streams to a
    clean chat.end. The acceptance test for this feature."""
    from lmchat.services.web_search_service import SearchResult

    chat_id = await _insert_chat(engine, settings={})
    await _seed_endpoint_mode(engine, "openai_compat")

    tc_id = "tc-web-search"
    round1 = [
        CanonicalEvent(
            type="tool_call.start",
            tool_call=CanonicalToolCall(id=tc_id, name="web_search", arguments={}),
        ),
        CanonicalEvent(
            type="tool_call.name",
            tool_call=CanonicalToolCall(id=tc_id, name="web_search", arguments={}),
        ),
        CanonicalEvent(
            type="tool_call.arguments",
            tool_call=CanonicalToolCall(
                id=tc_id, name="web_search", arguments={"query": "current weather"}
            ),
        ),
        CanonicalEvent(type="tool_call.success"),
        CanonicalEvent(type="chat.end"),
    ]
    round2 = [
        CanonicalEvent(type="message.delta", content="It is sunny today."),
        CanonicalEvent(type="chat.end", stop_reason="stop"),
    ]
    _rounds = iter([round1, round2])
    captured_tool_names: list[set[str]] = []

    async def _compat_stream_chat(
        req: CanonicalChatRequest, **kwargs: Any
    ) -> AsyncIterator[CanonicalEvent]:
        captured_tool_names.append({t.name for t in (req.tools or [])})
        try:
            evs = next(_rounds)
        except StopIteration:
            evs = [CanonicalEvent(type="chat.end", stop_reason="stop")]
        for ev in evs:
            yield ev

    compat_provider = MagicMock()
    compat_provider.name = "lmstudio"
    compat_provider.context_mode = "replay"
    compat_provider.stream_chat = _compat_stream_chat

    native_stub = _make_lmstudio_native_stub(compat_provider)
    registry = _make_registry(native_stub, "lmstudio")

    lm_client = MagicMock()
    lm_client.stream = MagicMock(
        side_effect=AssertionError("lm_client.stream must NOT be called")
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

    request = _mock_request()
    request.app.state.mcp_host = MagicMock()
    request.app.state.mcp_host.list_tools = MagicMock(return_value=[])
    request.app.state.mcp_server_store = None
    web_search_service = MagicMock()
    web_search_service.search = AsyncMock(
        return_value=[
            SearchResult(title="Weather", url="https://example.com", snippet="Sunny, 72F"),
        ]
    )
    request.app.state.web_search_service = web_search_service

    frames = await _drain(
        svc.stream_chat(
            chat_id=chat_id,
            user=_mock_user(1),
            payload=_make_payload(chat_text="what is the weather"),
            request=request,
        )
    )

    # Advertised: the first round's request carried the web_search tool.
    assert captured_tool_names and "web_search" in captured_tool_names[0]

    # Executed via WebSearchService — not McpHost.call_tool.
    web_search_service.search.assert_called_once_with("current weather", top_n=5)

    # Final answer streams to a clean chat.end.
    parsed = _parse_frames(frames)
    deltas = [d for d in parsed if d.get("type") == "message.delta"]
    assert any("sunny" in (d.get("content") or "").lower() for d in deltas)
    assert "chat.end" in [d.get("type") for d in parsed]


@pytest.mark.asyncio
async def test_lmstudio_native_mode_does_not_advertise_web_search(
    engine: AsyncEngine,
) -> None:
    """Native (default) endpoint mode with web_search_service present on
    app.state, NO integrations: AgenticMcpProvider is never constructed and
    WebSearchService is never touched — web_search stays openai_compat-only."""
    chat_id = await _insert_chat(engine, settings={})
    # No _seed_endpoint_mode call — native is the default.

    native_stub = _make_lmstudio_native_stub(compat_provider=MagicMock())
    registry = _make_registry(native_stub, "lmstudio")

    lm_client_fired = False

    async def _fake_lm_stream(**kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        nonlocal lm_client_fired
        lm_client_fired = True
        for ev in _happy_events():
            yield ev

    lm_client = MagicMock()
    lm_client.stream = _fake_lm_stream

    models_mock = AsyncMock()
    models_mock.auth_failed = False
    _res = MagicMock()
    _res.wire_id = "test-model"
    _res.substituted = False
    models_mock.resolve_to_loaded_or_fallback = AsyncMock(return_value=_res)
    models_mock.get_capabilities = AsyncMock(side_effect=KeyError("test-model"))
    models_mock.get_max_context_length = AsyncMock(return_value=0)

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    svc = StreamingService(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        chat_locks={},
        models_service=models_mock,
        provider_registry=registry,
    )

    request = _mock_request()
    request.app.state.mcp_host = MagicMock()
    request.app.state.mcp_server_store = None
    web_search_service = MagicMock()
    web_search_service.search = AsyncMock(return_value=[])
    request.app.state.web_search_service = web_search_service

    with patch(
        "lmchat.mcp.agentic.AgenticMcpProvider", MagicMock()
    ) as mock_agentic_cls:
        frames = await _drain(
            svc.stream_chat(
                chat_id=chat_id,
                user=_mock_user(1),
                payload=_make_payload(),
                request=request,
            )
        )

    mock_agentic_cls.assert_not_called()
    assert lm_client_fired, "lm_client.stream was never called in native mode"
    web_search_service.search.assert_not_called()
    parsed = _parse_frames(frames)
    assert "chat.end" in [d.get("type") for d in parsed]


@pytest.mark.asyncio
async def test_cloud_provider_does_not_advertise_web_search(engine: AsyncEngine) -> None:
    """A cloud provider (settings.provider="openrouter") with web_search_
    service present on app.state, NO integrations: AgenticMcpProvider is
    never constructed — builtin_web_search is set ONLY by the lmstudio/
    openai_compat branch, never for a resolved cloud provider."""
    chat_id = await _insert_chat(engine, settings={"provider": "openrouter"})

    captured: dict[str, Any] = {}
    cloud_provider = _make_stub_provider("replay", captured=captured)
    registry = _make_registry(cloud_provider, "openrouter")

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

    request = _mock_request()
    web_search_service = MagicMock()
    web_search_service.search = AsyncMock(return_value=[])
    request.app.state.web_search_service = web_search_service

    with patch(
        "lmchat.mcp.agentic.AgenticMcpProvider", MagicMock()
    ) as mock_agentic_cls:
        frames = await _drain(
            svc.stream_chat(
                chat_id=chat_id,
                user=_mock_user(1),
                payload=_make_payload(),
                request=request,
            )
        )

    mock_agentic_cls.assert_not_called()
    assert "request" in captured, "cloud provider stream_chat was never invoked"
    web_search_service.search.assert_not_called()
    parsed = _parse_frames(frames)
    assert "chat.end" in [d.get("type") for d in parsed]


@pytest.mark.asyncio
async def test_store_integration_replay_does_not_enable_web_search(
    engine: AsyncEngine,
) -> None:
    """Native mode + a Store-sourced mcp/ integration (the reroute tested in
    ``test_lmstudio_native_mode_with_store_integration_reaches_agentic_mcp_gate``
    above) still wraps in AgenticMcpProvider for the MCP tool — but must NOT
    also enable web_search: builtin_web_search is set ONLY by the resolver's
    openai_compat branch, and the store reroute never touches it."""
    from lmchat.services.mcp_server_store import McpServerSafeView

    chat_id = await _insert_chat(engine, settings={})
    # No _seed_endpoint_mode call — native is the default.

    compat_provider = _make_stub_provider("replay")
    compat_provider.name = "lmstudio"
    native_stub = _make_lmstudio_native_stub(compat_provider)
    registry = _make_registry(native_stub, "lmstudio")

    lm_client = MagicMock()
    lm_client.stream = MagicMock(
        side_effect=AssertionError("lm_client.stream must NOT be called")
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
            input=[CanonicalInputBlock(type="text", content="search something")],
            integrations=["mcp/searxng"],
        ),
    )

    request = _mock_request()
    request.app.state.mcp_host = MagicMock()
    mock_store = AsyncMock()
    mock_store.list_all = AsyncMock(
        return_value=[
            McpServerSafeView(
                id=1,
                slug="searxng",
                name="SearXNG",
                transport="stdio",
                command="searxng-mcp",
                args=None,
                url=None,
                secrets_set=[],
                enabled=True,
                source="store",
                trust="unverified",
                consented=True,
            ),
        ]
    )
    request.app.state.mcp_server_store = mock_store
    web_search_service = MagicMock()
    web_search_service.search = AsyncMock(return_value=[])
    request.app.state.web_search_service = web_search_service

    agentic_stub = _make_stub_provider("replay")

    with patch(
        "lmchat.mcp.agentic.AgenticMcpProvider",
        MagicMock(return_value=agentic_stub),
    ) as mock_agentic_cls:
        frames = await _drain(
            svc.stream_chat(
                chat_id=chat_id,
                user=_mock_user(1),
                payload=payload,
                request=request,
            )
        )

    mock_agentic_cls.assert_called_once()
    assert mock_agentic_cls.call_args.kwargs.get("builtin_registry") is None
    assert mock_agentic_cls.call_args.kwargs.get("builtin_ctx") is None
    web_search_service.search.assert_not_called()

    parsed = _parse_frames(frames)
    assert "chat.end" in [d.get("type") for d in parsed]


# ---------------------------------------------------------------------------
# Model resolution on the LM-Studio replay dispatch — the fix.
#
# LM Studio's OpenAI-compat endpoint routes by LOADED-INSTANCE LABEL (e.g.
# "default"), never the bare catalog key on the request (e.g. "test-model")
# — sending the catalog key 400s upstream as model_not_found. This applies
# to BOTH replay entries for the "lmstudio" provider: the openai_compat
# endpoint-mode dispatch above, and the native+store-integration reroute
# above that. Real cloud providers have no "loaded instances" and must keep
# sending the model id as-is (untouched by this fix).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lmstudio_openai_compat_resolves_model_to_wire_id(
    engine: AsyncEngine,
) -> None:
    """openai_compat mode + a models_service: the request reaching the
    compat provider carries the RESOLVED wire_id (loaded_instance_id), not
    the raw catalog key from the chat's payload."""
    from lmchat.services.models_service import ResolvedModel

    chat_id = await _insert_chat(engine, settings={})
    await _seed_endpoint_mode(engine, "openai_compat")

    captured: dict[str, Any] = {}
    compat_provider = _make_stub_provider("replay", captured=captured)
    compat_provider.name = "lmstudio"
    native_stub = _make_lmstudio_native_stub(compat_provider)
    registry = _make_registry(native_stub, "lmstudio")

    lm_client = MagicMock()
    lm_client.stream = MagicMock(
        side_effect=AssertionError("lm_client.stream must NOT be called in openai_compat mode")
    )

    models_mock = AsyncMock()
    models_mock.resolve_to_loaded_or_fallback = AsyncMock(
        return_value=ResolvedModel(wire_id="default", requested="test-model")
    )

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    svc = StreamingService(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        chat_locks={},
        provider_registry=registry,
        models_service=models_mock,
    )

    # No web_search_service on app.state — this test is about model
    # resolution, not the web_search wiring.
    request = _mock_request()
    request.app.state.web_search_service = None

    # Followups-OOB (see test_followups_oob.py) independently calls
    # resolve_to_loaded_or_fallback on every completed turn when a
    # models_service is present, regardless of provider/context_mode —
    # disable it so the assertion below counts only OUR resolution call.
    with patch("lmchat.config.get_settings") as mock_settings:
        _cfg = MagicMock()
        _cfg.lm_chat_followups_enabled = False
        # C3 mode adoption (streaming_service._infer_mode_oob) independently
        # calls resolve_to_loaded_or_fallback too, same as followups above —
        # disable it so the assertion below counts only OUR resolution call.
        _cfg.lm_chat_mode_adoption_enabled = False
        mock_settings.return_value = _cfg

        frames = await _drain(
            svc.stream_chat(
                chat_id=chat_id,
                user=_mock_user(1),
                payload=_make_payload(model="test-model"),
                request=request,
            )
        )

    models_mock.resolve_to_loaded_or_fallback.assert_called_once_with("test-model")
    assert "request" in captured, "compat provider stream_chat was never invoked"
    wire_req: CanonicalChatRequest = captured["request"]
    assert wire_req.model == "default", (
        f"Expected the resolved loaded-instance label on the wire, got {wire_req.model!r}"
    )

    parsed = _parse_frames(frames)
    assert "chat.end" in [d.get("type") for d in parsed]


@pytest.mark.asyncio
async def test_lmstudio_store_integration_reroute_resolves_model_to_wire_id(
    engine: AsyncEngine,
) -> None:
    """Native mode + a Store-sourced mcp/ integration (the reroute) with a
    models_service present: the request reaching the agentic-wrapped compat
    provider also carries the resolved wire_id, exactly like the openai_
    compat dispatch above."""
    from lmchat.services.mcp_server_store import McpServerSafeView
    from lmchat.services.models_service import ResolvedModel

    chat_id = await _insert_chat(engine, settings={})
    # No _seed_endpoint_mode call — native is the default.

    compat_provider = _make_stub_provider("replay")
    compat_provider.name = "lmstudio"
    native_stub = _make_lmstudio_native_stub(compat_provider)
    registry = _make_registry(native_stub, "lmstudio")

    lm_client = MagicMock()
    lm_client.stream = MagicMock(
        side_effect=AssertionError(
            "lm_client.stream must NOT be called — a Store integration must "
            "route this native turn through the agentic dispatch instead"
        )
    )

    models_mock = AsyncMock()
    models_mock.resolve_to_loaded_or_fallback = AsyncMock(
        return_value=ResolvedModel(wire_id="strong", requested="test-model")
    )

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    svc = StreamingService(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        chat_locks={},
        provider_registry=registry,
        models_service=models_mock,
    )

    payload = ChatStreamRequest(
        chat_id=chat_id,
        payload=CanonicalChatRequest(
            model="test-model",
            input=[CanonicalInputBlock(type="text", content="search something")],
            integrations=["mcp/searxng"],
        ),
    )

    request = _mock_request()
    request.app.state.mcp_host = MagicMock()
    mock_store = AsyncMock()
    mock_store.list_all = AsyncMock(
        return_value=[
            McpServerSafeView(
                id=1,
                slug="searxng",
                name="SearXNG",
                transport="stdio",
                command="searxng-mcp",
                args=None,
                url=None,
                secrets_set=[],
                enabled=True,
                source="store",
                trust="unverified",
                consented=True,
            ),
        ]
    )
    request.app.state.mcp_server_store = mock_store
    request.app.state.web_search_service = None

    captured: dict[str, Any] = {}
    agentic_stub = _make_stub_provider("replay", captured=captured)

    # Followups-OOB (see test_followups_oob.py) independently calls
    # resolve_to_loaded_or_fallback on every completed turn when a
    # models_service is present, regardless of provider/context_mode —
    # disable it so the assertion below counts only OUR resolution call.
    with (
        patch(
            "lmchat.mcp.agentic.AgenticMcpProvider",
            MagicMock(return_value=agentic_stub),
        ) as mock_agentic_cls,
        patch("lmchat.config.get_settings") as mock_settings,
    ):
        _cfg = MagicMock()
        _cfg.lm_chat_followups_enabled = False
        # C3 mode adoption (streaming_service._infer_mode_oob) independently
        # calls resolve_to_loaded_or_fallback too, same as followups above —
        # disable it so the assertion below counts only OUR resolution call.
        _cfg.lm_chat_mode_adoption_enabled = False
        mock_settings.return_value = _cfg

        frames = await _drain(
            svc.stream_chat(
                chat_id=chat_id,
                user=_mock_user(1),
                payload=payload,
                request=request,
            )
        )

    native_stub.as_openai_compat_provider.assert_called_once()
    mock_agentic_cls.assert_called_once()
    models_mock.resolve_to_loaded_or_fallback.assert_called_once_with("test-model")
    assert "request" in captured, "agentic stub stream_chat was never invoked"
    wire_req: CanonicalChatRequest = captured["request"]
    assert wire_req.model == "strong", (
        f"Expected the resolved loaded-instance label on the wire, got {wire_req.model!r}"
    )

    parsed = _parse_frames(frames)
    assert "chat.end" in [d.get("type") for d in parsed]


@pytest.mark.asyncio
async def test_replay_cloud_provider_ignores_models_service(engine: AsyncEngine) -> None:
    """A resolved cloud provider (settings.provider="openrouter") sends the
    model id AS-IS even when a models_service is wired up — cloud providers
    have no "loaded instances"; resolve_to_loaded_or_fallback must never be
    called on this path (that resolution is LM-Studio-only, see the openai_
    compat and store-reroute tests above)."""
    chat_id = await _insert_chat(engine, settings={"provider": "openrouter"})

    captured: dict[str, Any] = {}
    cloud_provider = _make_stub_provider("replay", captured=captured)
    registry = _make_registry(cloud_provider, "openrouter")

    lm_client = MagicMock()
    lm_client.stream = MagicMock(
        side_effect=AssertionError("lm_client.stream must NOT be called in replay mode")
    )

    models_mock = AsyncMock()
    models_mock.resolve_to_loaded_or_fallback = AsyncMock(return_value=MagicMock())

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    svc = StreamingService(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        chat_locks={},
        provider_registry=registry,
        models_service=models_mock,
    )

    # Followups-OOB (see test_followups_oob.py) independently calls
    # resolve_to_loaded_or_fallback on every completed turn when a
    # models_service is present, regardless of provider/context_mode —
    # disable it so assert_not_called below reflects only the replay
    # dispatch under test.
    with patch("lmchat.config.get_settings") as mock_settings:
        _cfg = MagicMock()
        _cfg.lm_chat_followups_enabled = False
        # C3 mode adoption (streaming_service._infer_mode_oob) independently
        # calls resolve_to_loaded_or_fallback too, same as followups above —
        # disable it so the assertion below counts only OUR resolution call.
        _cfg.lm_chat_mode_adoption_enabled = False
        mock_settings.return_value = _cfg

        frames = await _drain(
            svc.stream_chat(
                chat_id=chat_id,
                user=_mock_user(1),
                payload=_make_payload(model="test-model"),
                request=_mock_request(),
            )
        )

    models_mock.resolve_to_loaded_or_fallback.assert_not_called()
    assert "request" in captured, "cloud provider stream_chat was never invoked"
    wire_req: CanonicalChatRequest = captured["request"]
    assert wire_req.model == "test-model"

    parsed = _parse_frames(frames)
    assert "chat.end" in [d.get("type") for d in parsed]


@pytest.mark.asyncio
async def test_lmstudio_openai_compat_no_model_loaded_emits_clean_error(
    engine: AsyncEngine,
) -> None:
    """openai_compat mode, models_service resolves to wire_id=None (nothing
    loaded in LM Studio): a clean upstream_unavailable error frame — not a
    raw LM-Studio model_not_found 400 — and the compat provider is never
    dispatched to."""
    from lmchat.services.models_service import ResolvedModel

    chat_id = await _insert_chat(engine, settings={})
    await _seed_endpoint_mode(engine, "openai_compat")

    compat_provider = _make_stub_provider("replay")
    compat_provider.name = "lmstudio"
    native_stub = _make_lmstudio_native_stub(compat_provider)
    registry = _make_registry(native_stub, "lmstudio")

    lm_client = MagicMock()
    lm_client.stream = MagicMock(
        side_effect=AssertionError("lm_client.stream must NOT be called in openai_compat mode")
    )

    models_mock = AsyncMock()
    models_mock.resolve_to_loaded_or_fallback = AsyncMock(
        return_value=ResolvedModel(
            wire_id=None, requested="test-model", reason="no_models_loaded"
        )
    )

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    svc = StreamingService(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        chat_locks={},
        provider_registry=registry,
        models_service=models_mock,
    )

    request = _mock_request()
    request.app.state.web_search_service = None

    frames = await _drain(
        svc.stream_chat(
            chat_id=chat_id,
            user=_mock_user(1),
            payload=_make_payload(model="test-model"),
            request=request,
        )
    )

    parsed = _parse_frames(frames)
    error_frames = [d for d in parsed if d.get("type") == "error"]
    assert error_frames, f"Expected an upstream_unavailable error frame, got: {parsed}"
    assert error_frames[0]["error"]["code"] == "upstream_unavailable", error_frames


# ---------------------------------------------------------------------------
# _has_store_integration — pure classification helper, unit-level
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_has_store_integration_no_integrations() -> None:
    from lmchat.services.streaming_service import _has_store_integration

    req = _mock_request_with_store([_FakeStoreServer("searxng")])
    assert await _has_store_integration(request=req, integrations=None) is False
    assert await _has_store_integration(request=req, integrations=[]) is False


@pytest.mark.asyncio
async def test_has_store_integration_no_store_on_app_state() -> None:
    from lmchat.services.streaming_service import _has_store_integration

    req = _mock_request_with_store(None)
    assert await _has_store_integration(request=req, integrations=["mcp/searxng"]) is False


@pytest.mark.asyncio
async def test_has_store_integration_matched_enabled_consented() -> None:
    from lmchat.services.streaming_service import _has_store_integration

    req = _mock_request_with_store([_FakeStoreServer("searxng")])
    assert await _has_store_integration(request=req, integrations=["mcp/searxng"]) is True


@pytest.mark.asyncio
async def test_has_store_integration_disabled_server_excluded() -> None:
    from lmchat.services.streaming_service import _has_store_integration

    req = _mock_request_with_store([_FakeStoreServer("searxng", enabled=False)])
    assert await _has_store_integration(request=req, integrations=["mcp/searxng"]) is False


@pytest.mark.asyncio
async def test_has_store_integration_unconsented_server_excluded() -> None:
    from lmchat.services.streaming_service import _has_store_integration

    req = _mock_request_with_store([_FakeStoreServer("searxng", consented=False)])
    assert await _has_store_integration(request=req, integrations=["mcp/searxng"]) is False


@pytest.mark.asyncio
async def test_has_store_integration_curated_only_slug_not_in_store() -> None:
    from lmchat.services.streaming_service import _has_store_integration

    req = _mock_request_with_store([_FakeStoreServer("other-tool")])
    assert await _has_store_integration(request=req, integrations=["mcp/searxng"]) is False


@pytest.mark.asyncio
async def test_has_store_integration_store_read_failure_degrades_false() -> None:
    from lmchat.services.streaming_service import _has_store_integration

    req = _mock_request()
    store = AsyncMock()
    store.list_all = AsyncMock(side_effect=RuntimeError("db locked"))
    req.app.state.mcp_server_store = store
    assert await _has_store_integration(request=req, integrations=["mcp/searxng"]) is False


# ---------------------------------------------------------------------------
# (f) Write-path degraded signal — a swallowed index_message failure is
# countable via MemoryService.embedding_status(), not just log-only.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_safe_index_message_failure_is_countable_via_status(
    engine: AsyncEngine,
) -> None:
    """A _safe_index_message failure is swallowed (as before) but recorded.

    The swallow semantics are unchanged — no exception propagates out of
    _safe_index_message. What's new: the failure increments a counter that
    embedding_status() surfaces, so repeated stream.memory_index_failed
    log lines become visible on the status snapshot instead of log-only.
    """
    from lmchat.services.memory_service import MemoryService

    models_mock = AsyncMock()
    models_mock.list_loaded = AsyncMock(return_value=[])

    memory_service = MemoryService(
        engine=engine,
        embedding_client=MagicMock(),
        models_service=models_mock,
    )
    memory_service.index_message = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("embedding model offline")
    )

    svc = StreamingService(
        engine=engine,
        lm_client=MagicMock(),
        memory_service=memory_service,
        chat_locks={},
    )

    # Swallow still holds — no exception propagates.
    await svc._safe_index_message(msg_id=1, chat_id=2)

    status = await memory_service.embedding_status()
    assert status["write_failure_count"] == 1
    assert status["write_last_error"] is not None
    assert "embedding model offline" in status["write_last_error"]

    # A second failure increments the counter further.
    await svc._safe_index_message(msg_id=3, chat_id=2)
    status = await memory_service.embedding_status()
    assert status["write_failure_count"] == 2
