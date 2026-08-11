# SPDX-License-Identifier: Apache-2.0
"""Quality-mode dispatch tests for ``StreamingService.stream_chat``.

Covers the wiring that connects the per-chat ``self_consistency_enabled`` /
``chain_of_verification_enabled`` settings (persisted by the chat-settings
route, toggled by the FE ChatSettingsRail) to the already-implemented
``QualityModeService`` methods.

Matrix (per the task brief):
  (a) self_consistency_enabled True → QualityModeService.self_consistency
      called with the right prompt/model and its result is persisted/emitted.
  (b) chain_of_verification_enabled True → chain_of_verification called, the
      revised_answer is what gets persisted/emitted.
  (c) both flags False → quality methods NOT called; normal path runs and the
      quality service is untouched.
  (d) non-lmstudio provider + flag True → quality methods NOT called.
  (e) quality method raises → graceful fallback to the normal generation;
      no unhandled exception; row still reaches FINAL with the fallback
      answer.

``QualityModeService`` is mocked in every test.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import chats, messages, metadata
from lmchat.lmstudio.types import (
    CanonicalChatRequest,
    CanonicalEvent,
    CanonicalInputBlock,
)
from lmchat.services._stream_state import PersistState
from lmchat.services.quality_modes import ChainOfVerificationResult
from lmchat.services.streaming_service import ChatStreamRequest, StreamingService

# ---------------------------------------------------------------------------
# Helpers (mirrors test_streaming_service.py patterns)
# ---------------------------------------------------------------------------


async def _make_engine() -> AsyncEngine:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return engine


def _mock_user(user_id: int = 1) -> MagicMock:
    user = MagicMock()
    user.id = user_id
    return user


def _mock_request(disconnected: bool = False) -> AsyncMock:
    from tests.services.conftest import make_disconnect_receive

    request = AsyncMock()
    request.receive = make_disconnect_receive(disconnected)
    return request


def _make_request_payload(
    model: str = "test-model",
    chat_text: str = "what is the capital of france?",
    integrations: list[str] | None = None,
) -> ChatStreamRequest:
    return ChatStreamRequest(
        chat_id=1,
        payload=CanonicalChatRequest(
            model=model,
            input=[CanonicalInputBlock(type="text", content=chat_text)],
            integrations=integrations or [],
        ),
    )


def _normal_events(
    content: str = "normal answer", response_id: str = "rid-normal"
) -> list[CanonicalEvent]:
    return [
        CanonicalEvent(type="chat.start"),
        CanonicalEvent(type="message.start"),
        CanonicalEvent(type="message.delta", content=content),
        CanonicalEvent(type="message.end"),
        CanonicalEvent(type="chat.end", response_id=response_id),
    ]


def _fake_stream_factory(events: list[CanonicalEvent]):  # noqa: ANN202
    async def _fake_stream(*args: object, **kwargs: object) -> AsyncIterator[CanonicalEvent]:
        for ev in events:
            yield ev
    return _fake_stream


async def _drain(gen: AsyncIterator[bytes]) -> list[bytes]:
    frames: list[bytes] = []
    async for frame in gen:
        frames.append(frame)
    return frames


def _parse_frames(frames: list[bytes]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for frame in frames:
        for line in frame.decode("utf-8").splitlines():
            if line.startswith("data:"):
                results.append(json.loads(line[5:].strip()))
    return results


async def _insert_chat(engine: AsyncEngine, *, settings: dict[str, Any]) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            chats.insert().values(user_id=1, title="test", settings=settings)
        )


async def _assistant_row(engine: AsyncEngine) -> Any:
    async with engine.connect() as conn:
        result = await conn.execute(
            select(messages.c.state, messages.c.content, messages.c.response_id).where(
                messages.c.chat_id == 1,
                messages.c.role == "assistant",
            )
        )
        return result.fetchone()


def _build_service(
    engine: AsyncEngine,
    *,
    lm_client: Any,
    quality_mode_service: Any,
) -> StreamingService:
    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)
    return StreamingService(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        chat_locks={},
        idle_timeout_sec=60,
        quality_mode_service=quality_mode_service,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = await _make_engine()
    yield eng
    await eng.dispose()


# ---------------------------------------------------------------------------
# (a) self_consistency_enabled → self_consistency called + result persisted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_consistency_enabled_calls_method_and_persists_result(
    engine: AsyncEngine,
) -> None:
    """SC flag on → self_consistency(prompt, model) called; chosen draft is
    the persisted + emitted answer; the normal lm_client.stream is NOT used."""
    qms = MagicMock()
    qms.self_consistency = AsyncMock(return_value="the most central draft")
    qms.chain_of_verification = AsyncMock()

    lm_client = MagicMock()
    # If the normal path were taken it would yield this — assert it is NOT.
    lm_client.stream = _fake_stream_factory(_normal_events(content="SHOULD-NOT-APPEAR"))

    svc = _build_service(engine, lm_client=lm_client, quality_mode_service=qms)
    await _insert_chat(engine, settings={"self_consistency_enabled": True})

    frames = await _drain(
        svc.stream_chat(
            chat_id=1,
            user=_mock_user(1),
            payload=_make_request_payload(model="local-model"),
            request=_mock_request(),
        )
    )

    qms.self_consistency.assert_awaited_once()
    _, kwargs = qms.self_consistency.call_args
    assert kwargs["prompt"] == "what is the capital of france?"
    assert kwargs["model_id"] == "local-model"
    qms.chain_of_verification.assert_not_called()

    row = await _assistant_row(engine)
    assert row is not None
    assert row[0] == PersistState.FINAL.value
    assert row[1] == "the most central draft"

    # The chosen draft is what reaches the client via a message.delta frame.
    parsed = _parse_frames(frames)
    deltas = [d for d in parsed if d.get("type") == "message.delta"]
    assert any(d.get("content") == "the most central draft" for d in deltas)
    assert not any(d.get("content") == "SHOULD-NOT-APPEAR" for d in parsed)


# ---------------------------------------------------------------------------
# (a1) Regression pin: SC dispatch never threads integrations, even when the
# turn has them configured — asymmetric with the CoVe dispatch on purpose.
# See lmchat.services.quality_modes module docstring (CHANGELOG-0.5.3
# contract section) for the rationale: SC is a deliberately tool-free
# consistency measurement.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_consistency_dispatch_never_threads_integrations(
    engine: AsyncEngine,
) -> None:
    """Even with integrations configured on the wire payload, the
    self_consistency dispatch call carries no 'integrations' kwarg.

    Mirrors test_cove_enabled_calls_method_and_persists_revised_answer,
    which asserts the OPPOSITE for CoVe (kwargs["integrations"] ==
    ["mcp/searxng"]). The asymmetry is intentional, not a bug.
    """
    qms = MagicMock()
    qms.self_consistency = AsyncMock(return_value="the most central draft")
    qms.chain_of_verification = AsyncMock()

    lm_client = MagicMock()
    lm_client.stream = _fake_stream_factory(_normal_events(content="SHOULD-NOT-APPEAR"))

    svc = _build_service(engine, lm_client=lm_client, quality_mode_service=qms)
    await _insert_chat(engine, settings={"self_consistency_enabled": True})

    await _drain(
        svc.stream_chat(
            chat_id=1,
            user=_mock_user(1),
            payload=_make_request_payload(
                model="local-model", integrations=["mcp/searxng"]
            ),
            request=_mock_request(),
        )
    )

    qms.self_consistency.assert_awaited_once()
    _, kwargs = qms.self_consistency.call_args
    assert "integrations" not in kwargs, (
        f"self_consistency was dispatched with integrations={kwargs.get('integrations')!r}; "
        "SC is deliberately tool-free (see quality_modes.py module docstring). "
        "If this is an intentional product change, update the rationale in "
        "quality_modes.py, this test, and test_quality_sc_no_integrations.py together."
    )


# ---------------------------------------------------------------------------
# (a2) T1-7: the assembled system_prompt reaches the quality method
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quality_mode_receives_assembled_system_prompt(
    engine: AsyncEngine,
) -> None:
    """T1-7: quality modes answer WITH the assembled turn context, not the bare
    user text. An incoming system_prompt sentinel (stand-in for project
    instructions / date / persona / RAG) must appear in the system_prompt the
    quality method is dispatched with. Before the fix the QM service got no
    system_prompt at all (context-blind / amnesiac answers).
    """
    qms = MagicMock()
    qms.self_consistency = AsyncMock(return_value="draft")
    qms.chain_of_verification = AsyncMock()

    lm_client = MagicMock()
    lm_client.stream = _fake_stream_factory(_normal_events(content="x"))

    svc = _build_service(engine, lm_client=lm_client, quality_mode_service=qms)
    await _insert_chat(engine, settings={"self_consistency_enabled": True})

    payload = ChatStreamRequest(
        chat_id=1,
        payload=CanonicalChatRequest(
            model="local-model",
            system_prompt="SENTINEL-PROJECT-CONTEXT",
            input=[CanonicalInputBlock(type="text", content="hello there")],
            integrations=[],
        ),
    )

    await _drain(
        svc.stream_chat(
            chat_id=1,
            user=_mock_user(1),
            payload=payload,
            request=_mock_request(),
        )
    )

    qms.self_consistency.assert_awaited_once()
    _, kwargs = qms.self_consistency.call_args
    assert "system_prompt" in kwargs, "system_prompt not threaded to the quality mode"
    sys_prompt = kwargs["system_prompt"]
    assert sys_prompt is not None, "quality mode received no system_prompt (amnesiac)"
    assert "SENTINEL-PROJECT-CONTEXT" in sys_prompt, (
        f"assembled turn context did not reach the quality mode; got {sys_prompt!r}"
    )


# ---------------------------------------------------------------------------
# (b) chain_of_verification_enabled → CoVe called + revised_answer persisted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cove_enabled_calls_method_and_persists_revised_answer(
    engine: AsyncEngine,
) -> None:
    """CoVe flag on → chain_of_verification called with prompt/model/integrations;
    the revised_answer is persisted + emitted."""
    cove_result = ChainOfVerificationResult(
        initial_answer="paris is the capital",
        verification_questions=["is paris the capital?"],
        verification_answers=["yes"],
        revised_answer="The capital of France is Paris.",
        converged=False,
    )
    qms = MagicMock()
    qms.chain_of_verification = AsyncMock(return_value=cove_result)
    qms.self_consistency = AsyncMock()

    lm_client = MagicMock()
    lm_client.stream = _fake_stream_factory(_normal_events(content="SHOULD-NOT-APPEAR"))

    svc = _build_service(engine, lm_client=lm_client, quality_mode_service=qms)
    await _insert_chat(engine, settings={"chain_of_verification_enabled": True})

    frames = await _drain(
        svc.stream_chat(
            chat_id=1,
            user=_mock_user(1),
            payload=_make_request_payload(
                model="local-model", integrations=["mcp/searxng"]
            ),
            request=_mock_request(),
        )
    )

    qms.chain_of_verification.assert_awaited_once()
    _, kwargs = qms.chain_of_verification.call_args
    assert kwargs["prompt"] == "what is the capital of france?"
    assert kwargs["model_id"] == "local-model"
    assert kwargs["integrations"] == ["mcp/searxng"]
    qms.self_consistency.assert_not_called()

    row = await _assistant_row(engine)
    assert row is not None
    assert row[0] == PersistState.FINAL.value
    assert row[1] == "The capital of France is Paris."

    parsed = _parse_frames(frames)
    deltas = [d for d in parsed if d.get("type") == "message.delta"]
    assert any(d.get("content") == "The capital of France is Paris." for d in deltas)


# ---------------------------------------------------------------------------
# (f) Empty-answer salvage/fallback — quality modes must never persist ""
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cove_falls_back_to_initial_answer_when_revised_is_empty(
    engine: AsyncEngine,
) -> None:
    """CoVe with an empty revised_answer salvages the earlier initial_answer.

    A reasoning-heavy model can leave revised_answer empty (Step 4's
    generation parked its whole answer in reasoning_content, which
    quality_modes._stream_to_string discards) while the earlier
    initial_answer (Step 1, a separate generation) still carries real text.

    RED-ON-REVERT: reading only revised_answer persists "" instead of the
    initial_answer text.
    """
    cove_result = ChainOfVerificationResult(
        initial_answer="Paris is the capital of France.",
        verification_questions=["is paris the capital?"],
        verification_answers=["yes"],
        revised_answer="",
        converged=False,
    )
    qms = MagicMock()
    qms.chain_of_verification = AsyncMock(return_value=cove_result)
    qms.self_consistency = AsyncMock()

    lm_client = MagicMock()
    lm_client.stream = _fake_stream_factory(_normal_events(content="SHOULD-NOT-APPEAR"))

    svc = _build_service(engine, lm_client=lm_client, quality_mode_service=qms)
    await _insert_chat(engine, settings={"chain_of_verification_enabled": True})

    await _drain(
        svc.stream_chat(
            chat_id=1,
            user=_mock_user(1),
            payload=_make_request_payload(model="local-model"),
            request=_mock_request(),
        )
    )

    row = await _assistant_row(engine)
    assert row is not None
    assert row[0] == PersistState.FINAL.value
    assert row[1] == "Paris is the capital of France."


@pytest.mark.asyncio
async def test_cove_both_answers_empty_persists_fallback_not_empty_string(
    engine: AsyncEngine,
) -> None:
    """CoVe with BOTH initial_answer and revised_answer empty persists an
    honest fallback message, never an empty stored answer.

    RED-ON-REVERT: without the fallback, the assistant row's content is ""
    — a silently-dropped turn with no way for the user to know why.
    """
    cove_result = ChainOfVerificationResult(
        initial_answer="",
        verification_questions=[],
        verification_answers=[],
        revised_answer="",
        converged=True,
    )
    qms = MagicMock()
    qms.chain_of_verification = AsyncMock(return_value=cove_result)
    qms.self_consistency = AsyncMock()

    lm_client = MagicMock()
    lm_client.stream = _fake_stream_factory(_normal_events(content="SHOULD-NOT-APPEAR"))

    svc = _build_service(engine, lm_client=lm_client, quality_mode_service=qms)
    await _insert_chat(engine, settings={"chain_of_verification_enabled": True})

    await _drain(
        svc.stream_chat(
            chat_id=1,
            user=_mock_user(1),
            payload=_make_request_payload(model="local-model"),
            request=_mock_request(),
        )
    )

    row = await _assistant_row(engine)
    assert row is not None
    assert row[0] == PersistState.FINAL.value
    assert row[1], "quality mode persisted an EMPTY answer with no fallback"
    assert "Chain-of-Verification" in row[1]


@pytest.mark.asyncio
async def test_self_consistency_empty_draft_persists_fallback_not_empty_string(
    engine: AsyncEngine,
) -> None:
    """Self-consistency returning an empty/whitespace draft persists an
    honest fallback message, never an empty stored answer.

    RED-ON-REVERT: without the fallback, the assistant row's content is ""
    — a silently-dropped turn with no way for the user to know why.
    """
    qms = MagicMock()
    qms.self_consistency = AsyncMock(return_value="   ")
    qms.chain_of_verification = AsyncMock()

    lm_client = MagicMock()
    lm_client.stream = _fake_stream_factory(_normal_events(content="SHOULD-NOT-APPEAR"))

    svc = _build_service(engine, lm_client=lm_client, quality_mode_service=qms)
    await _insert_chat(engine, settings={"self_consistency_enabled": True})

    await _drain(
        svc.stream_chat(
            chat_id=1,
            user=_mock_user(1),
            payload=_make_request_payload(model="local-model"),
            request=_mock_request(),
        )
    )

    row = await _assistant_row(engine)
    assert row is not None
    assert row[0] == PersistState.FINAL.value
    assert row[1], "quality mode persisted an EMPTY answer with no fallback"
    assert "Self-Consistency" in row[1]


@pytest.mark.asyncio
async def test_both_flags_set_cove_wins(engine: AsyncEngine) -> None:
    """Precedence: when both flags are set, chain_of_verification wins."""
    cove_result = ChainOfVerificationResult(
        initial_answer="x",
        verification_questions=[],
        verification_answers=[],
        revised_answer="cove won",
        converged=True,
    )
    qms = MagicMock()
    qms.chain_of_verification = AsyncMock(return_value=cove_result)
    qms.self_consistency = AsyncMock(return_value="sc ran")

    lm_client = MagicMock()
    lm_client.stream = _fake_stream_factory(_normal_events())

    svc = _build_service(engine, lm_client=lm_client, quality_mode_service=qms)
    await _insert_chat(
        engine,
        settings={
            "self_consistency_enabled": True,
            "chain_of_verification_enabled": True,
        },
    )

    await _drain(
        svc.stream_chat(
            chat_id=1,
            user=_mock_user(1),
            payload=_make_request_payload(model="local-model"),
            request=_mock_request(),
        )
    )

    qms.chain_of_verification.assert_awaited_once()
    qms.self_consistency.assert_not_called()
    row = await _assistant_row(engine)
    assert row is not None
    assert row[1] == "cove won"


# ---------------------------------------------------------------------------
# (c) both flags False → quality methods NOT called; normal path runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_flags_runs_normal_path_quality_service_untouched(
    engine: AsyncEngine,
) -> None:
    """Both flags absent/false → the quality service is never touched and the
    ordinary lm_client.stream path produces the answer."""
    qms = MagicMock()
    qms.self_consistency = AsyncMock()
    qms.chain_of_verification = AsyncMock()

    lm_client = MagicMock()
    lm_client.stream = _fake_stream_factory(
        _normal_events(content="ordinary answer", response_id="rid-ord")
    )

    svc = _build_service(engine, lm_client=lm_client, quality_mode_service=qms)
    await _insert_chat(engine, settings={})

    await _drain(
        svc.stream_chat(
            chat_id=1,
            user=_mock_user(1),
            payload=_make_request_payload(model="local-model"),
            request=_mock_request(),
        )
    )

    qms.self_consistency.assert_not_called()
    qms.chain_of_verification.assert_not_called()

    row = await _assistant_row(engine)
    assert row is not None
    assert row[0] == PersistState.FINAL.value
    assert row[1] == "ordinary answer"
    assert row[2] == "rid-ord"


# ---------------------------------------------------------------------------
# (d) non-lmstudio provider + flag True → quality methods NOT called
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_lmstudio_provider_ignores_quality_flags(
    engine: AsyncEngine,
) -> None:
    """A chat whose provider is not 'lmstudio' must IGNORE the quality flags
    and run the normal path — quality methods are never called.

    With ``provider_registry=None`` an unknown provider name keeps the default
    chain path BUT the dispatch guard checks ``_provider_name != 'lmstudio'``
    directly, so a non-lmstudio provider short-circuits before any method call.
    """
    qms = MagicMock()
    qms.self_consistency = AsyncMock()
    qms.chain_of_verification = AsyncMock()

    lm_client = MagicMock()
    lm_client.stream = _fake_stream_factory(
        _normal_events(content="cloud answer", response_id="rid-cloud")
    )

    svc = _build_service(engine, lm_client=lm_client, quality_mode_service=qms)
    await _insert_chat(
        engine,
        settings={
            "provider": "openrouter",
            "self_consistency_enabled": True,
            "chain_of_verification_enabled": True,
        },
    )

    await _drain(
        svc.stream_chat(
            chat_id=1,
            user=_mock_user(1),
            payload=_make_request_payload(model="cloud-model"),
            request=_mock_request(),
        )
    )

    qms.self_consistency.assert_not_called()
    qms.chain_of_verification.assert_not_called()

    row = await _assistant_row(engine)
    assert row is not None
    assert row[0] == PersistState.FINAL.value
    assert row[1] == "cloud answer"


# ---------------------------------------------------------------------------
# (e) quality method raises → graceful fallback to the normal generation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quality_method_raises_falls_back_to_normal_path(
    engine: AsyncEngine,
) -> None:
    """When the quality method raises, the stream degrades to the normal
    generation — no unhandled exception, row reaches FINAL with the
    fallback answer."""
    from lmchat.services.quality_modes import QualityModeUpstreamError

    qms = MagicMock()
    qms.self_consistency = AsyncMock(
        side_effect=QualityModeUpstreamError(
            error_code="upstream_unavailable", message="boom"
        )
    )
    qms.chain_of_verification = AsyncMock()

    lm_client = MagicMock()
    lm_client.stream = _fake_stream_factory(
        _normal_events(content="fallback answer", response_id="rid-fb")
    )

    svc = _build_service(engine, lm_client=lm_client, quality_mode_service=qms)
    await _insert_chat(engine, settings={"self_consistency_enabled": True})

    frames = await _drain(
        svc.stream_chat(
            chat_id=1,
            user=_mock_user(1),
            payload=_make_request_payload(model="local-model"),
            request=_mock_request(),
        )
    )

    qms.self_consistency.assert_awaited_once()

    row = await _assistant_row(engine)
    assert row is not None
    assert row[0] == PersistState.FINAL.value
    assert row[1] == "fallback answer"
    assert row[2] == "rid-fb"

    # No error frame should have surfaced — the fallback is transparent.
    parsed = _parse_frames(frames)
    assert not any(d.get("type") == "error" for d in parsed)


@pytest.mark.asyncio
async def test_quality_service_none_runs_normal_path(engine: AsyncEngine) -> None:
    """When no QualityModeService is wired (legacy/test paths), the flags are
    inert and the normal path runs — proves the None-guard."""
    lm_client = MagicMock()
    lm_client.stream = _fake_stream_factory(
        _normal_events(content="legacy answer", response_id="rid-legacy")
    )

    svc = _build_service(engine, lm_client=lm_client, quality_mode_service=None)
    await _insert_chat(engine, settings={"chain_of_verification_enabled": True})

    await _drain(
        svc.stream_chat(
            chat_id=1,
            user=_mock_user(1),
            payload=_make_request_payload(model="local-model"),
            request=_mock_request(),
        )
    )

    row = await _assistant_row(engine)
    assert row is not None
    assert row[0] == PersistState.FINAL.value
    assert row[1] == "legacy answer"


# ---------------------------------------------------------------------------
# (f) quality method HANGS → watchdog timeout → graceful fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quality_method_hangs_timeout_falls_back_to_normal_path(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuinely-hung quality call must NOT block the turn forever.

    With ``lm_chat_quality_mode_timeout_sec`` overridden to a tiny value and a
    quality method that never completes (sleeps far past the timeout), the
    watchdog cancels the hung task and DELEGATES to the normal upstream — the
    row still reaches FINAL with the fallback answer, no error frame surfaces,
    and the stream does not hang.
    """
    import lmchat.config as _config_mod

    # Tiny watchdog so the loop fires after ~one poll interval instead of 2 h.
    # Mutate the live cached Settings instance (Pydantic v2 models are mutable
    # here); monkeypatch restores the original value after the test.
    _settings = _config_mod.get_settings()
    monkeypatch.setattr(_settings, "lm_chat_quality_mode_timeout_sec", 0.1)

    async def _never_completes(*_a: object, **_k: object) -> str:
        # Sleeps well past the (tiny) configured timeout so the task is still
        # pending when the watchdog fires and cancels it.
        await asyncio.sleep(30)
        return "should never be reached"

    qms = MagicMock()
    qms.self_consistency = AsyncMock(side_effect=_never_completes)
    qms.chain_of_verification = AsyncMock()

    lm_client = MagicMock()
    lm_client.stream = _fake_stream_factory(
        _normal_events(content="timeout fallback answer", response_id="rid-to")
    )

    svc = _build_service(engine, lm_client=lm_client, quality_mode_service=qms)
    await _insert_chat(engine, settings={"self_consistency_enabled": True})

    frames = await asyncio.wait_for(
        _drain(
            svc.stream_chat(
                chat_id=1,
                user=_mock_user(1),
                payload=_make_request_payload(model="local-model"),
                request=_mock_request(),
            )
        ),
        # Outer guard: if the watchdog regressed, the inner stream would block
        # on the 30 s sleep — fail fast instead of hanging the suite.
        timeout=10.0,
    )

    # The quality method was invoked (and then cancelled by the watchdog).
    qms.self_consistency.assert_awaited_once()

    # The turn fell back to the normal upstream answer and reached FINAL.
    row = await _assistant_row(engine)
    assert row is not None
    assert row[0] == PersistState.FINAL.value
    assert row[1] == "timeout fallback answer"
    assert row[2] == "rid-to"

    # Fallback is transparent — no error frame reaches the client.
    parsed = _parse_frames(frames)
    assert not any(d.get("type") == "error" for d in parsed)
    deltas = [d for d in parsed if d.get("type") == "message.delta"]
    assert any(d.get("content") == "timeout fallback answer" for d in deltas)
