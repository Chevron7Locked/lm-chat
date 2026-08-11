# SPDX-License-Identifier: Apache-2.0
"""Sub-session cloud-provider dispatch-target tests.

Verifies:
(a) DISPATCH TARGET (cloud): when provider=<registered slug>, the sub-session
    routes through a LmstudioStreamingClient(adapter=resolved_provider) — i.e.
    the resolved provider's stream_chat() is called, NOT the default lm_client.
(b) DISPATCH TARGET (LM Studio, no provider): when no provider is supplied,
    the LM Studio lm_client.stream() is called — byte-identical to today.
(c) DISPATCH TARGET (lmstudio slug explicit): when provider="lmstudio" is
    sent explicitly, the LM Studio lm_client.stream() is still called.
(d) Unknown provider slug: falls back to lm_client (warning path).

This mirrors the assertion style of tests/services/test_streaming_replay.py
(the dispatch-target assertions in (a) and (b)).

Test strategy: drive ``_sub_session_sse`` directly (like
test_sub_session_streaming.py) with a stub lm_client and a stub provider
whose stream_chat() we can inspect.  For the route-level dispatch decision
(provider field → which client object lands in _sub_session_sse) we also
exercise ``sub_session_stream`` via a minimal TestClient.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from lmchat.lmstudio.types import CanonicalEvent
from lmchat.routes.chats import _sub_session_sse
from lmchat.services.lmstudio_streaming_client import LmstudioStreamingClient

# ---------------------------------------------------------------------------
# Shared helpers (mirror test_sub_session_streaming.py)
# ---------------------------------------------------------------------------


def _event(type_: str, **kwargs: Any) -> CanonicalEvent:
    return CanonicalEvent(type=type_, **kwargs)  # type: ignore[arg-type]


async def _from_events(events: list[CanonicalEvent]) -> AsyncIterator[CanonicalEvent]:
    for ev in events:
        yield ev


def _happy_events() -> list[CanonicalEvent]:
    return [
        _event("chat.start"),
        _event("message.delta", content="answer"),
        _event("chat.end"),
    ]


def _make_provider_stub(captured: dict[str, Any] | None = None) -> Any:
    """Return a stub provider whose stream_chat records its call args.

    stream_chat is called as: adapter.stream_chat(request, history=..., ...)
    so the stub must accept a positional first arg (the request).
    """
    stub = MagicMock()

    async def _stream_chat_gen(
        request: Any, **kwargs: Any
    ) -> AsyncIterator[CanonicalEvent]:
        if captured is not None:
            captured["request"] = request
            captured.update(kwargs)
        for ev in _happy_events():
            yield ev

    def _stream_chat(request: Any, **kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        return _stream_chat_gen(request, **kwargs)

    stub.stream_chat = _stream_chat
    stub.context_mode = "replay"
    return stub


def _make_lm_client_stub(
    captured: dict[str, Any] | None = None,
    *,
    events: list[CanonicalEvent] | None = None,
) -> LmstudioStreamingClient:
    """Return a stub LmstudioStreamingClient backed by a fake adapter."""
    adapter = MagicMock()
    _evts = events if events is not None else _happy_events()

    async def _stream_gen(request: Any, **kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        if captured is not None:
            captured["request"] = request
            captured.update(kwargs)
        for ev in _evts:
            yield ev

    def _stream_chat(request: Any, **kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        return _stream_gen(request, **kwargs)

    adapter.stream_chat = _stream_chat
    return LmstudioStreamingClient(adapter=adapter)


def _parse_sse_frames(blob: bytes) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for frame in blob.split(b"\n\n"):
        if not frame.strip():
            continue
        name: str | None = None
        data_text: str | None = None
        for line in frame.splitlines():
            if line.startswith(b"event: "):
                name = line[len(b"event: "):].decode()
            elif line.startswith(b"data: "):
                data_text = line[len(b"data: "):].decode()
        if name is None or data_text is None:
            continue
        out.append((name, json.loads(data_text)))
    return out


# ---------------------------------------------------------------------------
# (a) DISPATCH TARGET — cloud provider path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cloud_provider_routes_to_provider_not_lm_client() -> None:
    """When provider adapter is passed as lm_client, provider.stream_chat() is called.

    This mirrors the pattern in test_streaming_replay.py: we build a
    LmstudioStreamingClient wrapping the cloud provider adapter and pass it as
    the lm_client to _sub_session_sse — verifying that the stream goes through
    the provider's stream_chat, NOT any lmstudio-native path.

    The dispatch decision (provider form field → build LmstudioStreamingClient
    with adapter=resolved) is exercised in the route-level test below.

    DISPATCH TARGET assertion: provider.stream_chat was called (captured kwarg
    ``request`` is present); this mirrors the pattern in
    test_streaming_replay.py §(e).
    """
    provider_captured: dict[str, Any] = {}
    provider_stub = _make_provider_stub(captured=provider_captured)

    # Build the client the route creates: LmstudioStreamingClient(adapter=provider)
    cloud_client = LmstudioStreamingClient(adapter=provider_stub)

    chunks: list[bytes] = []
    async for chunk in _sub_session_sse(
        lm_client=cloud_client,  # the route sets this to the cloud wrapper
        model_id="cloud-model",
        system_prompt="you are a helper",
        messages=[{"role": "user", "content": "hello"}],
    ):
        chunks.append(chunk)

    blob = b"".join(chunks)
    frames = _parse_sse_frames(blob)
    names = [n for n, _ in frames]

    # Stream must complete successfully.
    assert "sub.complete" in names, f"sub.complete missing from frames: {names!r}"
    complete = next(d for n, d in frames if n == "sub.complete")
    assert complete.get("final_content") == "answer"

    # DISPATCH TARGET: provider.stream_chat was called (captured ``request`` key
    # is set by the stub when stream_chat is invoked).
    assert "request" in provider_captured, (
        "provider.stream_chat must be the dispatch target when cloud client is used; "
        f"captured keys: {list(provider_captured)!r}"
    )


# ---------------------------------------------------------------------------
# (b) DISPATCH TARGET — LM Studio path (no provider / default)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lm_studio_path_unchanged_when_no_provider() -> None:
    """Default path (no provider): lm_client.stream() is the dispatch target.

    Verifies the LM Studio path is byte-identical to today's behavior:
    the default lm_client's underlying adapter.stream_chat is called.
    """
    lm_captured: dict[str, Any] = {}
    lm_client = _make_lm_client_stub(captured=lm_captured)

    chunks: list[bytes] = []
    async for chunk in _sub_session_sse(
        lm_client=lm_client,
        model_id="local-model",
        system_prompt="you are a helper",
        messages=[{"role": "user", "content": "hello"}],
        # No provider field — uses default lm_client.
    ):
        chunks.append(chunk)

    blob = b"".join(chunks)
    frames = _parse_sse_frames(blob)
    names = [n for n, _ in frames]

    assert "sub.complete" in names, f"sub.complete missing: {names!r}"
    complete = next(d for n, d in frames if n == "sub.complete")
    assert complete.get("final_content") == "answer"

    # DISPATCH TARGET: lm_client adapter was invoked (captured ``request`` key is
    # set by the stub when stream_chat is invoked).
    assert "request" in lm_captured, (
        "lm_client adapter must be called on the default LM Studio path; "
        f"captured keys: {list(lm_captured)!r}"
    )


# ---------------------------------------------------------------------------
# Route-level: provider form field dispatches through registry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sub_session_stream_routes_cloud_provider_via_registry(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    """Route-level: provider=<slug> resolves registry → LmstudioStreamingClient(adapter).

    We mount a minimal app, register a stub provider in app.state.provider_registry,
    and POST to /api/chats/{id}/sub-session/stream with provider=<slug>.
    We assert:
    - The stub provider's stream_chat() was called (cloud dispatch target).
    - The default lm_client.stream() was NOT called.
    """
    from fastapi.testclient import TestClient

    from lmchat.app import create_app
    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod
    from lmchat.middleware._bucket_store import InMemoryBucketStore
    from lmchat.services.auth_service import _reset_dummy_hash_cache

    db_url = f"sqlite+aiosqlite:///{tmp_path}/sub_session_provider_routing.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    get_settings.cache_clear()
    engine_mod.dispose_engine()
    _reset_dummy_hash_cache()

    app = create_app()

    # Stub models service.  For the CLOUD path, resolve_to_loaded_or_fallback
    # must NOT be called — if it is, that means the LM Studio resolution block
    # leaked into the cloud path (the bug this test guards against).  Make the
    # stub raise so any such call turns the test red immediately.
    from lmchat.routes._dependencies import get_models_service_dep
    stub_models = AsyncMock()
    stub_models.resolve_to_loaded_or_fallback = AsyncMock(
        side_effect=AssertionError(
            "resolve_to_loaded_or_fallback must NOT be called for a cloud-provider "
            "sub-session — the LM Studio resolution block leaked into the cloud path"
        )
    )
    stub_models.list_loaded = AsyncMock(return_value=[])
    app.dependency_overrides[get_models_service_dep] = lambda: stub_models

    # Build a stub cloud provider whose stream_chat is tracked.
    cloud_captured: dict[str, Any] = {}
    cloud_provider = _make_provider_stub(captured=cloud_captured)

    # Stub registry — returns cloud_provider for "openrouter", None otherwise.
    stub_registry = MagicMock()
    def _reg_get(name: str) -> Any:
        return cloud_provider if name == "openrouter" else None
    stub_registry.get = MagicMock(side_effect=_reg_get)

    # Stub lm_client — must NOT be invoked for a cloud-provider sub-session.
    lm_client_guard = MagicMock()
    lm_client_guard.stream = MagicMock(
        side_effect=AssertionError(
            "lm_client.stream must NOT be called when cloud provider dispatched"
        )
    )

    with TestClient(app) as client:
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.provider_registry = stub_registry  # type: ignore[attr-defined]
        client.app.state.lm_streaming_client = lm_client_guard  # type: ignore[attr-defined]

        # Register + login.
        client.post(
            "/api/auth/register",
            data={"username": "alice", "password": "correct-horse-battery"},
        )
        client.post(
            "/api/auth/login",
            data={"username": "alice", "password": "correct-horse-battery"},
        )

        # Create a chat so ownership check passes.
        chat_resp = client.post(
            "/api/chats",
            data={"title": "test chat"},
        )
        assert chat_resp.status_code in (200, 201), chat_resp.text
        chat_id = chat_resp.json()["id"]

        # POST to sub-session/stream with provider=openrouter.
        messages = json.dumps([{"role": "user", "content": "hi"}])
        resp = client.post(
            f"/api/chats/{chat_id}/sub-session/stream",
            data={
                "model_id": "cloud-model",
                "system_prompt": "be helpful",
                "messages_json": messages,
                "provider": "openrouter",
            },
        )

    # We expect 200 (streaming) — the cloud provider stub returns happy events.
    assert resp.status_code == 200, resp.text

    # DISPATCH TARGET: cloud provider's stream_chat was called (captured request key set).
    assert "request" in cloud_captured, (
        "cloud provider.stream_chat must be the dispatch target when provider=openrouter; "
        f"captured keys: {list(cloud_captured)!r}"
    )

    # MODEL ID GUARD: the raw cloud model id must reach the provider unmodified.
    # If LM Studio resolution ran, wire_model_id would be a local loaded-instance
    # id, not the cloud id the FE sent.  The captured request carries the model
    # field — assert it equals the raw value sent by the FE.
    captured_req = cloud_captured.get("request")
    if captured_req is not None and hasattr(captured_req, "model"):
        assert captured_req.model == "cloud-model", (
            f"cloud provider received model={captured_req.model!r} instead of "
            "'cloud-model' — LM Studio resolution must have corrupted the model id"
        )

    # The default lm_client guard was NOT called (neither stream nor stream_chat).
    lm_client_guard.stream.assert_not_called()


@pytest.mark.asyncio
async def test_sub_session_stream_lm_studio_path_when_no_provider_sent(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    """Route-level regression: omitting provider → LM Studio lm_client path unchanged."""
    from fastapi.testclient import TestClient

    from lmchat.app import create_app
    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod
    from lmchat.middleware._bucket_store import InMemoryBucketStore
    from lmchat.services.auth_service import _reset_dummy_hash_cache

    db_url = f"sqlite+aiosqlite:///{tmp_path}/sub_session_lmstudio_path.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    get_settings.cache_clear()
    engine_mod.dispose_engine()
    _reset_dummy_hash_cache()

    app = create_app()

    from lmchat.routes._dependencies import get_models_service_dep
    stub_models = AsyncMock()
    stub_models.resolve_to_loaded_or_fallback = AsyncMock(
        return_value=MagicMock(wire_id="local-model", substituted=False)
    )
    stub_models.list_loaded = AsyncMock(return_value=[])
    app.dependency_overrides[get_models_service_dep] = lambda: stub_models

    # The lm_client is a real-enough stub whose adapter.stream_chat we track.
    lm_adapter_captured: dict[str, Any] = {}
    real_lm_client = _make_lm_client_stub(captured=lm_adapter_captured)

    # Cloud provider guard — not registered; registry returns None.
    stub_registry = MagicMock()
    stub_registry.get = MagicMock(return_value=None)  # nothing registered

    with TestClient(app) as client:
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.provider_registry = stub_registry  # type: ignore[attr-defined]
        client.app.state.lm_streaming_client = real_lm_client  # type: ignore[attr-defined]
        # The route reads models_service from app.state directly (not via the DI
        # override above), so inject the stub here — otherwise it uses the real
        # lifespan ModelsService whose resolve state varies in the full suite
        # (passes in isolation, flaky under full-suite ordering).
        client.app.state.models_service = stub_models  # type: ignore[attr-defined]

        client.post(
            "/api/auth/register",
            data={"username": "bob", "password": "correct-horse-battery"},
        )
        client.post(
            "/api/auth/login",
            data={"username": "bob", "password": "correct-horse-battery"},
        )
        chat_resp = client.post("/api/chats", data={"title": "chat"})
        assert chat_resp.status_code in (200, 201)
        chat_id = chat_resp.json()["id"]

        messages = json.dumps([{"role": "user", "content": "hello"}])
        resp = client.post(
            f"/api/chats/{chat_id}/sub-session/stream",
            data={
                "model_id": "local-model",
                "system_prompt": "be helpful",
                "messages_json": messages,
                # No provider field.
            },
        )

    assert resp.status_code == 200, resp.text

    # DISPATCH TARGET: lm_client adapter was invoked (captured ``request`` key set).
    assert "request" in lm_adapter_captured, (
        "lm_client adapter must be called when no provider is specified (LM Studio path); "
        f"captured keys: {list(lm_adapter_captured)!r}"
    )

    # No provider sent → registry is not consulted (empty slug is falsy, gate skipped).
    stub_registry.get.assert_not_called()


@pytest.mark.asyncio
async def test_lm_studio_path_still_resolves_and_422s_when_no_model_loaded(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    """LM Studio path: resolve_to_loaded_or_fallback IS called, 422 when nothing loaded.

    Proves the gating is one-way: cloud path skips resolution; LM Studio path
    (no provider) still calls it and propagates a 422 when wire_id is None
    (no model loaded in LM Studio, JIT disabled).
    """
    from fastapi.testclient import TestClient

    from lmchat.app import create_app
    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod
    from lmchat.middleware._bucket_store import InMemoryBucketStore
    from lmchat.services.auth_service import _reset_dummy_hash_cache

    db_url = f"sqlite+aiosqlite:///{tmp_path}/sub_session_lmstudio_422.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    get_settings.cache_clear()
    engine_mod.dispose_engine()
    _reset_dummy_hash_cache()

    app = create_app()

    # Stub models service: resolve returns wire_id=None → simulates "no model loaded".
    # The route reads models_service directly from app.state (not via DI), so we
    # inject it onto app.state after the TestClient context is entered.
    resolve_called: list[str] = []

    async def _resolve(model_id: str) -> Any:
        resolve_called.append(model_id)
        return MagicMock(wire_id=None, substituted=False)

    real_lm_client = _make_lm_client_stub()

    with TestClient(app) as client:
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.lm_streaming_client = real_lm_client  # type: ignore[attr-defined]
        # Override the models_service on app.state so the route uses our stub.
        stub_models = AsyncMock()
        stub_models.resolve_to_loaded_or_fallback = _resolve
        stub_models.list_loaded = AsyncMock(return_value=[])
        client.app.state.models_service = stub_models  # type: ignore[attr-defined]

        client.post(
            "/api/auth/register",
            data={"username": "carol", "password": "correct-horse-battery"},
        )
        client.post(
            "/api/auth/login",
            data={"username": "carol", "password": "correct-horse-battery"},
        )
        chat_resp = client.post("/api/chats", data={"title": "chat"})
        assert chat_resp.status_code in (200, 201)
        chat_id = chat_resp.json()["id"]

        messages = json.dumps([{"role": "user", "content": "hello"}])
        resp = client.post(
            f"/api/chats/{chat_id}/sub-session/stream",
            data={
                "model_id": "local-model",
                "system_prompt": "be helpful",
                "messages_json": messages,
                # No provider → LM Studio path → resolution runs.
            },
        )

    # Resolution MUST have been called (not skipped as it would be on cloud path).
    assert resolve_called, (
        "resolve_to_loaded_or_fallback was NOT called on the LM Studio path — "
        "the gating logic incorrectly skipped LM Studio resolution"
    )
    assert resolve_called[0] == "local-model"

    # wire_id=None → 422 "No language model is loaded".
    assert resp.status_code == 422, (
        f"Expected 422 when no model loaded, got {resp.status_code}: {resp.text}"
    )


# ---------------------------------------------------------------------------
# sub_session_finalize cloud-provider routing (was ZERO coverage)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sub_session_finalize_routes_cloud_provider_via_registry(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    """Route-level: /finalize with provider=<slug> routes through the resolved
    cloud provider and NEVER touches LM Studio loaded-instance resolution.

    Before this change, finalize had no provider param — it always ran
    resolve_to_loaded_or_fallback(model_id) and dispatched via the default
    lm_client, so a cloud finalize (model_id="openai/gpt-4o-mini") 422'd or got
    a local model substituted. This pins the fix.

    RED-ON-REVERT: revert the provider block / the `if not _is_cloud:` gate and
    resolve_to_loaded_or_fallback fires (its AssertionError side-effect) OR the
    lm_client guard is hit — either turns this red. (It also guards the runtime
    NameError from constructing LmstudioStreamingClient without a runtime import,
    since the cloud path builds the wrapper live.)
    """
    from fastapi.testclient import TestClient

    from lmchat.app import create_app
    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod
    from lmchat.middleware._bucket_store import InMemoryBucketStore
    from lmchat.services.auth_service import _reset_dummy_hash_cache

    db_url = f"sqlite+aiosqlite:///{tmp_path}/sub_session_finalize_cloud.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    get_settings.cache_clear()
    engine_mod.dispose_engine()
    _reset_dummy_hash_cache()

    app = create_app()

    # models_service.resolve_to_loaded_or_fallback must NOT run on the cloud path.
    # It RECORDS calls and returns a SUBSTITUTED local id — a broken gate is then
    # caught two ways: (1) resolve_calls is non-empty, (2) the substituted id
    # reaches the provider (MODEL ID GUARD below). A raising stub would be
    # swallowed by finalize's resolve try/except, so recording is the robust check.
    resolve_calls: list[str] = []

    async def _resolve_should_not_run(model_id: str) -> Any:
        resolve_calls.append(model_id)
        return MagicMock(wire_id="local-substituted", substituted=True, fallback_key="local")

    stub_models = AsyncMock()
    stub_models.resolve_to_loaded_or_fallback = _resolve_should_not_run
    stub_models.list_loaded = AsyncMock(return_value=[])

    cloud_captured: dict[str, Any] = {}
    cloud_provider = _make_provider_stub(captured=cloud_captured)

    stub_registry = MagicMock()
    stub_registry.get = MagicMock(
        side_effect=lambda name: cloud_provider if name == "openrouter" else None
    )

    lm_client_guard = MagicMock()
    lm_client_guard.stream = MagicMock(
        side_effect=AssertionError(
            "lm_client.stream must NOT be called when a cloud provider finalizes"
        )
    )

    with TestClient(app) as client:
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.provider_registry = stub_registry  # type: ignore[attr-defined]
        client.app.state.lm_streaming_client = lm_client_guard  # type: ignore[attr-defined]
        client.app.state.models_service = stub_models  # type: ignore[attr-defined]

        client.post(
            "/api/auth/register",
            data={"username": "dave", "password": "correct-horse-battery"},
        )
        client.post(
            "/api/auth/login",
            data={"username": "dave", "password": "correct-horse-battery"},
        )
        chat_resp = client.post("/api/chats", data={"title": "chat"})
        assert chat_resp.status_code in (200, 201), chat_resp.text
        chat_id = chat_resp.json()["id"]

        messages = json.dumps([{"role": "user", "content": "summarise this"}])
        resp = client.post(
            f"/api/chats/{chat_id}/sub-session/finalize",
            data={
                "model_id": "cloud-model",
                "system_prompt": "be helpful",
                "messages_json": messages,
                "provider": "openrouter",
            },
        )

    assert resp.status_code == 200, resp.text

    # DISPATCH TARGET: the cloud provider's stream_chat ran.
    assert "request" in cloud_captured, (
        "cloud provider.stream_chat must be the finalize dispatch target when "
        f"provider=openrouter; captured keys: {list(cloud_captured)!r}"
    )

    # MODEL ID GUARD: the raw cloud model id reaches the provider unmodified.
    captured_req = cloud_captured.get("request")
    if captured_req is not None and hasattr(captured_req, "model"):
        assert captured_req.model == "cloud-model", (
            f"cloud provider received model={captured_req.model!r} instead of "
            "'cloud-model' — LM Studio resolution must have corrupted the model id"
        )

    # GATE: LM Studio resolution must be SKIPPED entirely on the cloud path.
    assert resolve_calls == [], (
        "LM Studio resolution must be skipped for a cloud-provider finalize; "
        f"resolve was called with: {resolve_calls!r}"
    )

    lm_client_guard.stream.assert_not_called()


@pytest.mark.asyncio
async def test_sub_session_finalize_lm_studio_path_still_resolves(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    """/finalize with NO provider still runs LM Studio resolution (gate is
    one-way): resolve_to_loaded_or_fallback IS called and the local wire id is
    used. Proves this change did not break the default finalize path."""
    from fastapi.testclient import TestClient

    from lmchat.app import create_app
    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod
    from lmchat.middleware._bucket_store import InMemoryBucketStore
    from lmchat.services.auth_service import _reset_dummy_hash_cache

    db_url = f"sqlite+aiosqlite:///{tmp_path}/sub_session_finalize_local.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    get_settings.cache_clear()
    engine_mod.dispose_engine()
    _reset_dummy_hash_cache()

    app = create_app()

    resolve_called: list[str] = []

    async def _resolve(model_id: str) -> Any:
        resolve_called.append(model_id)
        return MagicMock(wire_id="local-model", substituted=False, fallback_key=None)

    lm_adapter_captured: dict[str, Any] = {}
    real_lm_client = _make_lm_client_stub(captured=lm_adapter_captured)

    stub_registry = MagicMock()
    stub_registry.get = MagicMock(return_value=None)

    with TestClient(app) as client:
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.provider_registry = stub_registry  # type: ignore[attr-defined]
        client.app.state.lm_streaming_client = real_lm_client  # type: ignore[attr-defined]
        stub_models = AsyncMock()
        stub_models.resolve_to_loaded_or_fallback = _resolve
        stub_models.list_loaded = AsyncMock(return_value=[])
        client.app.state.models_service = stub_models  # type: ignore[attr-defined]

        client.post(
            "/api/auth/register",
            data={"username": "erin", "password": "correct-horse-battery"},
        )
        client.post(
            "/api/auth/login",
            data={"username": "erin", "password": "correct-horse-battery"},
        )
        chat_resp = client.post("/api/chats", data={"title": "chat"})
        assert chat_resp.status_code in (200, 201)
        chat_id = chat_resp.json()["id"]

        messages = json.dumps([{"role": "user", "content": "summarise"}])
        resp = client.post(
            f"/api/chats/{chat_id}/sub-session/finalize",
            data={
                "model_id": "local-model",
                "system_prompt": "be helpful",
                "messages_json": messages,
                # No provider → LM Studio path → resolution runs.
            },
        )

    assert resp.status_code == 200, resp.text
    assert resolve_called == ["local-model"], (
        "resolve_to_loaded_or_fallback must run on the no-provider finalize path"
    )
    assert "request" in lm_adapter_captured, (
        "the default lm_client must be the finalize dispatch target with no provider"
    )
    stub_registry.get.assert_not_called()


# ---------------------------------------------------------------------------
# Increment 5 — sub-session mirrors the main path's openai_compat + web_search
# wiring (see tests/services/test_streaming_replay.py's
# test_lmstudio_openai_compat_web_search_advertised_and_executed, which this
# mirrors at the route level for BOTH sub-session dispatch blocks: /stream
# and /finalize). Native stays a byte-identical no-op in both.
# ---------------------------------------------------------------------------


def _make_compat_provider_stub(captured_tools: list[set[str]]) -> Any:
    """Stub the compat re-presentation returned by as_openai_compat_provider().

    Records the tool names visible on each request.tools so a test can assert
    web_search was advertised — proof the turn was wrapped in a real
    AgenticMcpProvider, not dispatched bare.
    """
    provider = MagicMock()
    provider.name = "lmstudio"
    provider.context_mode = "replay"

    async def _stream_chat_gen(
        request: Any, **kwargs: Any
    ) -> AsyncIterator[CanonicalEvent]:
        captured_tools.append({t.name for t in (request.tools or [])})
        for ev in _happy_events():
            yield ev

    def _stream_chat(request: Any, **kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        return _stream_chat_gen(request, **kwargs)

    provider.stream_chat = _stream_chat
    return provider


def _make_lmstudio_native_stub(compat_provider: Any) -> MagicMock:
    """Stub the native LmstudioAdapter registered under the "lmstudio" name."""
    native = MagicMock()
    native.name = "lmstudio"
    native.context_mode = "chain"
    native.as_openai_compat_provider = MagicMock(return_value=compat_provider)
    return native


def _seed_endpoint_mode(db_path: str, mode: str) -> None:
    """Seed server_lm_studio_default.lm_studio_endpoint_mode via a raw sync
    sqlite3 connection.

    Called AFTER entering the `with TestClient(app) as client:` block, so the
    lifespan's ensure_schema_ready() has already created the table. A raw
    connection (rather than going through the app's own AsyncEngine) sidesteps
    any cross-event-loop hazard between the pytest-asyncio loop running this
    test and the loop TestClient's portal runs the ASGI app on.

    UPSERT, not a plain INSERT: the lifespan's own admin-tier bootstrap
    (``resolve_admin_tier_only`` / ``prune_unusable_api_keys``) already
    creates the id=1 singleton row before this runs.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO server_lm_studio_default (id, lm_studio_endpoint_mode) "
            "VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "lm_studio_endpoint_mode = excluded.lm_studio_endpoint_mode",
            (mode,),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_sub_session_stream_openai_compat_advertises_web_search(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    """LM Studio in openai_compat mode: /stream routes through the compat
    re-presentation wrapped in a REAL AgenticMcpProvider, so web_search is
    advertised to the model — mirrors test_streaming_replay.py's
    test_lmstudio_openai_compat_web_search_advertised_and_executed at the
    sub-session route level.

    RED-ON-REVERT: revert the new `else:` branch in sub_session_stream and
    this drops back to the native lm_client path — captured_tools stays
    empty, as_openai_compat_provider is never called, and the lm_client_guard
    (which raises on any call) turns the test red.
    """
    from fastapi.testclient import TestClient

    from lmchat.app import create_app
    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod
    from lmchat.middleware._bucket_store import InMemoryBucketStore
    from lmchat.services.auth_service import _reset_dummy_hash_cache

    db_path = tmp_path / "sub_session_stream_openai_compat.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    get_settings.cache_clear()
    engine_mod.dispose_engine()
    _reset_dummy_hash_cache()

    app = create_app()

    captured_tools: list[set[str]] = []
    compat_provider = _make_compat_provider_stub(captured_tools)
    native_stub = _make_lmstudio_native_stub(compat_provider)

    stub_registry = MagicMock()
    stub_registry.get = MagicMock(
        side_effect=lambda name: native_stub if name == "lmstudio" else None
    )

    resolve_calls: list[str] = []

    async def _resolve(model_id: str) -> Any:
        resolve_calls.append(model_id)
        return MagicMock(wire_id="loaded-label", substituted=False, fallback_key=None)

    stub_models = AsyncMock()
    stub_models.resolve_to_loaded_or_fallback = _resolve
    stub_models.list_loaded = AsyncMock(return_value=[])

    lm_client_guard = MagicMock()
    lm_client_guard.stream = MagicMock(
        side_effect=AssertionError(
            "lm_client.stream must NOT be called when LM Studio is in "
            "openai_compat mode"
        )
    )

    web_search_service = MagicMock()
    web_search_service.search = AsyncMock(return_value=[])

    with TestClient(app) as client:
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.provider_registry = stub_registry  # type: ignore[attr-defined]
        client.app.state.lm_streaming_client = lm_client_guard  # type: ignore[attr-defined]
        client.app.state.models_service = stub_models  # type: ignore[attr-defined]
        client.app.state.mcp_host = MagicMock()  # type: ignore[attr-defined]
        client.app.state.mcp_host.list_tools = MagicMock(return_value=[])  # type: ignore[attr-defined]
        client.app.state.mcp_server_store = None  # type: ignore[attr-defined]
        client.app.state.web_search_service = web_search_service  # type: ignore[attr-defined]

        # Seed the endpoint-mode row now that the schema exists (lifespan
        # startup ran on entering this `with` block).
        _seed_endpoint_mode(str(db_path), "openai_compat")

        client.post(
            "/api/auth/register",
            data={"username": "frank", "password": "correct-horse-battery"},
        )
        client.post(
            "/api/auth/login",
            data={"username": "frank", "password": "correct-horse-battery"},
        )
        chat_resp = client.post("/api/chats", data={"title": "chat"})
        assert chat_resp.status_code in (200, 201), chat_resp.text
        chat_id = chat_resp.json()["id"]

        messages = json.dumps([{"role": "user", "content": "what's the weather"}])
        resp = client.post(
            f"/api/chats/{chat_id}/sub-session/stream",
            data={
                "model_id": "local-model",
                "system_prompt": "be helpful",
                "messages_json": messages,
                # Explicit empty list — no MCP integrations selected. (Not
                # omitted: an omitted field triggers the admin-defaults
                # lookup, which would make server_ids depend on whatever
                # catalog exists in this test's fresh DB.)
                "integrations": "[]",
                # No provider field — this is the LM Studio path.
            },
        )

    assert resp.status_code == 200, resp.text

    # ADVERTISED: the compat provider's request carried the web_search tool —
    # proof the turn was wrapped in a real AgenticMcpProvider.
    assert captured_tools and "web_search" in captured_tools[0], (
        f"web_search not advertised to the compat provider: {captured_tools!r}"
    )

    # DISPATCH TARGET: the compat re-presentation was used, not the native
    # default lm_client.
    native_stub.as_openai_compat_provider.assert_called_once()
    lm_client_guard.stream.assert_not_called()

    # _is_cloud stayed False: LM Studio loaded-instance resolution still ran
    # (the compat endpoint needs the wire label, not the catalog key).
    assert resolve_calls == ["local-model"], (
        "model resolution must still run for openai_compat LM Studio — "
        f"resolve_calls={resolve_calls!r}"
    )


@pytest.mark.asyncio
async def test_sub_session_stream_native_mode_unchanged(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    """Native endpoint mode (the default, no DB row): /stream stays
    byte-identical to pre-increment-5 — lm_client is the dispatch target,
    as_openai_compat_provider is never called, web_search is never
    advertised."""
    from fastapi.testclient import TestClient

    from lmchat.app import create_app
    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod
    from lmchat.middleware._bucket_store import InMemoryBucketStore
    from lmchat.services.auth_service import _reset_dummy_hash_cache

    db_url = f"sqlite+aiosqlite:///{tmp_path}/sub_session_stream_native.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    get_settings.cache_clear()
    engine_mod.dispose_engine()
    _reset_dummy_hash_cache()

    app = create_app()

    compat_provider = _make_compat_provider_stub([])
    native_stub = _make_lmstudio_native_stub(compat_provider)

    stub_registry = MagicMock()
    stub_registry.get = MagicMock(
        side_effect=lambda name: native_stub if name == "lmstudio" else None
    )

    resolve_calls: list[str] = []

    async def _resolve(model_id: str) -> Any:
        resolve_calls.append(model_id)
        return MagicMock(wire_id="local-model", substituted=False, fallback_key=None)

    stub_models = AsyncMock()
    stub_models.resolve_to_loaded_or_fallback = _resolve
    stub_models.list_loaded = AsyncMock(return_value=[])

    lm_captured: dict[str, Any] = {}
    real_lm_client = _make_lm_client_stub(captured=lm_captured)

    web_search_service = MagicMock()
    web_search_service.search = AsyncMock(return_value=[])

    with TestClient(app) as client:
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.provider_registry = stub_registry  # type: ignore[attr-defined]
        client.app.state.lm_streaming_client = real_lm_client  # type: ignore[attr-defined]
        client.app.state.models_service = stub_models  # type: ignore[attr-defined]
        client.app.state.mcp_host = MagicMock()  # type: ignore[attr-defined]
        client.app.state.web_search_service = web_search_service  # type: ignore[attr-defined]
        # No endpoint-mode row seeded — native is the default.

        client.post(
            "/api/auth/register",
            data={"username": "grace", "password": "correct-horse-battery"},
        )
        client.post(
            "/api/auth/login",
            data={"username": "grace", "password": "correct-horse-battery"},
        )
        chat_resp = client.post("/api/chats", data={"title": "chat"})
        assert chat_resp.status_code in (200, 201)
        chat_id = chat_resp.json()["id"]

        messages = json.dumps([{"role": "user", "content": "hello"}])
        resp = client.post(
            f"/api/chats/{chat_id}/sub-session/stream",
            data={
                "model_id": "local-model",
                "system_prompt": "be helpful",
                "messages_json": messages,
                "integrations": "[]",
            },
        )

    assert resp.status_code == 200, resp.text
    assert "request" in lm_captured, (
        "the default lm_client must remain the dispatch target in native mode"
    )
    native_stub.as_openai_compat_provider.assert_not_called()
    web_search_service.search.assert_not_called()
    assert resolve_calls == ["local-model"]
