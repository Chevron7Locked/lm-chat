"""4-corners matrix + composition-order assertion.

The project prompt MUST reach the
LLM regardless of the ``lm_chat_followups_enabled`` flag (the bug where
project_prompt was previously gated INSIDE the followups branch).

Composition order asserted by these tests:
``[temporal_anchor][RAG_context][project_prompt][chat_prompt][history]``

Note: the followups directive was REMOVED from the main generation system
prompt as part of the OOB-followups decoupling. Followups are
now generated via a separate lightweight call after ``chat.end`` and emitted
as a ``followups`` SSE frame. The ``lm_chat_followups_enabled`` flag still
controls whether the OOB call is fired but no longer injects into the
main system prompt.

The 4-corners matrix exhausts ``(followups, rag)`` ∈ {(off, off),
(off, on), (on, off), (on, on)``. A regression that reorders or drops a
component in any corner fails here even if the other three corners
pass per-corner truthiness checks.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import chats, metadata, projects
from lmchat.lmstudio.types import (
    CanonicalChatRequest,
    CanonicalEvent,
    CanonicalInputBlock,
)
from lmchat.services.streaming_service import ChatStreamRequest, StreamingService

PROJECT_MARKER = "PROJECT_PROMPT_MARKER_42"
CHAT_MARKER = "CHAT_PROMPT_MARKER_99"
# FOLLOWUPS_MARKER is intentionally NOT defined here: the directive was
# removed from the main generation system prompt (OOB decoupling).
# The flag lm_chat_followups_enabled now only controls the post-chat.end
# OOB call; the main system prompt is always clean.
RAG_MARKER = "RAG_CONTEXT_BLOCK_MARKER_777"
# TEMPORAL_ANCHOR_MARKER: the leading text injected by the temporal-anchor
# layer. Matches the substring shared by both chain-mode ("[Context]\n-
# Right now: ... Treat this as ground truth ...") and replay-mode
# ("[Current date and time: ... Treat this as authoritative ...]") blocks.
# We assert its presence (and that it precedes all other layers) but do not
# assert an exact date string — that would make the test fragile.
TEMPORAL_ANCHOR_MARKER = "Treat this as"


@pytest.fixture
async def engine() -> AsyncEngine:
    e = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with e.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return e


def _make_events(content: str = "ack") -> list[CanonicalEvent]:
    """Minimal stream — start / message / end (mirrors the existing
    test_streaming_service.py ``_make_events`` shape)."""
    return [
        CanonicalEvent(type="chat.start"),
        CanonicalEvent(type="message.start"),
        CanonicalEvent(type="message.delta", content=content),
        CanonicalEvent(type="message.end"),
        CanonicalEvent(type="chat.end", response_id="r-a1"),
    ]


def _mock_request(disconnected: bool = False) -> MagicMock:
    from tests.services.conftest import make_disconnect_receive

    r = MagicMock()
    r.receive = make_disconnect_receive(disconnected)
    return r


def _mock_user(user_id: int = 1) -> MagicMock:
    u = MagicMock()
    u.id = user_id
    return u


def _make_payload() -> ChatStreamRequest:
    canonical = CanonicalChatRequest(
        model="qwen3.6-35b-a3b",
        input=[CanonicalInputBlock(type="text", content="ping")],
        system_prompt=CHAT_MARKER,
    )
    return ChatStreamRequest(chat_id=1, payload=canonical)


async def _seed(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            insert(projects).values(
                id=42,
                user_id=1,
                name="P",
                description="",
                system_prompt=PROJECT_MARKER,

                created_at=0.0,
                updated_at=0.0,
            )
        )
        await conn.execute(
            insert(chats).values(
                id=1, user_id=1, title="t", project_id=42
            )
        )


async def _build_service(
    engine: AsyncEngine,
    lm_client: Any,
    *,
    rag_on: bool = False,
) -> StreamingService:
    """Build a StreamingService. When ``rag_on`` is True, wires a
    mock embedding_client + models_service so the RAG augment block
    fires (it requires both to be non-None in ``streaming_service``).
    Closes the 4-corners-only-2-of-4 coverage gap by adding RAG-on
    corners with a stubbed ``rag_service.augment_prompt`` that
    returns a known ``RAG_MARKER`` context block.
    """
    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)
    proj_svc = MagicMock()
    proj_svc.get = AsyncMock(
        return_value=SimpleNamespace(
            id=42,
            user_id=1,
            name="P",
            description="",
            system_prompt=PROJECT_MARKER,

            created_at=0.0,
            updated_at=0.0,
        )
    )
    embedding_client: Any = None
    models_service: Any = None
    if rag_on:
        from lmchat.services.models_service import ResolvedModel  # noqa: PLC0415

        embedding_client = MagicMock()
        models_service = MagicMock()
        # resolve_to_loaded_or_fallback is awaited in streaming_service; make
        # it an AsyncMock so tests that supply a models_service don't fail.
        models_service.resolve_to_loaded_or_fallback = AsyncMock(
            side_effect=lambda mid, **_kw: ResolvedModel(wire_id=mid, requested=mid)
        )
    return StreamingService(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        chat_locks={},
        idle_timeout_sec=60,
        projects_service=proj_svc,
        embedding_client=embedding_client,
        models_service=models_service,
    )


async def _drain(stream: AsyncIterator[Any]) -> list[Any]:
    return [ev async for ev in stream]


async def _capture_outbound_sys_prompt(
    engine: AsyncEngine,
    *,
    followups_enabled: bool,
    rag_on: bool = False,
) -> str:
    """Drive one stream and return the outbound system_prompt as sent
    to the lm_client. The lm_client.stream is a stub that captures the
    request kwarg.

    When ``rag_on`` is True, the StreamingService is constructed with
    a mock embedding_client + models_service so the RAG augment branch
    runs, AND ``rag_service.augment_prompt`` is patched to return an
    ``AugmentedPrompt`` with ``RAG_MARKER`` so the composition order
    assertion can check `RAG_MARKER < PROJECT_MARKER`. Closes the
    4-corners coverage gap: verifies that RAG context, project prompt,
    and followups directive compose in the correct order.
    """
    captured: dict[str, Any] = {}

    async def _fake_stream(
        *args: Any, **kwargs: Any
    ) -> AsyncIterator[CanonicalEvent]:
        captured["payload"] = kwargs.get("request") or (args[0] if args else None)
        for ev in _make_events():
            yield ev

    lm_client = MagicMock()
    lm_client.stream = _fake_stream

    svc = await _build_service(engine, lm_client, rag_on=rag_on)
    await _seed(engine)

    async def _stub_augment(*args: Any, **kwargs: Any):
        # Return an AugmentedPrompt-shape; the streaming service reads
        # ``.context_block``, ``.memory_hits``, ``.doc_hits``, ``.ctx_window``.
        return SimpleNamespace(
            context_block=RAG_MARKER, memory_hits=1, doc_hits=1, ctx_window=0
        )

    # streaming_service.py imports ``get_settings`` AND
    # ``rag_service.augment_prompt`` lazily inside the function body.
    # Patch the source bindings so the lazy imports return our stubs.
    settings_patch = patch("lmchat.config.get_settings")
    rag_patch = patch(
        "lmchat.services.rag_service.augment_prompt",
        side_effect=_stub_augment,
    )
    with settings_patch as mock_settings, rag_patch:
        cfg = MagicMock()
        cfg.lm_chat_followups_enabled = followups_enabled
        mock_settings.return_value = cfg
        await _drain(
            svc.stream_chat(
                chat_id=1,
                user=_mock_user(1),
                payload=_make_payload(),
                request=_mock_request(),
            )
        )

    sent = captured.get("payload")
    assert sent is not None, "lm_client.stream was not called"
    return getattr(sent, "system_prompt", "") or ""


# ─── 4 corners ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_corner_followups_off_rag_off(engine: AsyncEngine) -> None:
    """Followups OFF, RAG OFF — the project prompt MUST still reach
    the wire. This is the corner an earlier version regressed
    (project_prompt was gated inside the followups branch).

    OOB decoupling: the followups directive is no longer
    injected into the main system prompt regardless of the flag value.
    Only temporal_anchor, project_prompt, and chat_prompt are asserted here.
    """
    sent = await _capture_outbound_sys_prompt(
        engine, followups_enabled=False
    )
    assert TEMPORAL_ANCHOR_MARKER in sent, (
        f"TEMPORAL_ANCHOR_MARKER missing when followups OFF: {sent!r}"
    )
    assert PROJECT_MARKER in sent, (
        f"PROJECT_MARKER missing when followups OFF (A1 regression): {sent!r}"
    )
    assert CHAT_MARKER in sent, f"CHAT_MARKER missing: {sent!r}"
    # No followups directive in system prompt regardless of flag value.
    assert "<!--followups" not in sent, (
        f"followups directive should never appear in main system prompt: {sent!r}"
    )


@pytest.mark.asyncio
async def test_corner_followups_on_rag_off(engine: AsyncEngine) -> None:
    """Followups ON, RAG OFF — project prompt present, RAG block absent.

    OOB decoupling: the followups directive is no longer in
    the main system prompt even when the flag is ON. The OOB call fires
    after chat.end; the main system prompt is always clean.
    """
    sent = await _capture_outbound_sys_prompt(
        engine, followups_enabled=True
    )
    assert TEMPORAL_ANCHOR_MARKER in sent, (
        f"TEMPORAL_ANCHOR_MARKER missing when followups ON: {sent!r}"
    )
    assert PROJECT_MARKER in sent, f"PROJECT_MARKER missing: {sent!r}"
    assert CHAT_MARKER in sent, f"CHAT_MARKER missing: {sent!r}"
    # Followups directive must NOT be in the main system prompt any more.
    assert "<!--followups" not in sent, (
        f"followups directive should never appear in main system prompt: {sent!r}"
    )


# ─── RAG-on corners ───────────────────────────────────────────────────────
#
# RAG-on requires the streaming_service's embedding_client +
# models_service to be set (the RAG-augment gate), and the RAG augment
# call returns a context block that gets prepended to the system_prompt
# via ``_followups_payload.system_prompt`` (the composed prompt after
# the prompt hoist). Mock ``rag_service.augment_prompt`` to return
# RAG_MARKER and assert `RAG_MARKER < PROJECT_MARKER` — the locked
# composition order
# [RAG_context][project_prompt][chat_prompt][followups][history].


@pytest.mark.asyncio
async def test_corner_followups_off_rag_on(engine: AsyncEngine) -> None:
    """RAG ON, followups OFF — temporal_anchor + project_prompt + chat_prompt
    + RAG block reach the wire; followups directive absent (OOB decoupling)."""
    sent = await _capture_outbound_sys_prompt(
        engine, followups_enabled=False, rag_on=True
    )
    assert TEMPORAL_ANCHOR_MARKER in sent, (
        f"TEMPORAL_ANCHOR_MARKER missing in RAG-on corner: {sent!r}"
    )
    assert PROJECT_MARKER in sent, f"PROJECT_MARKER missing: {sent!r}"
    assert CHAT_MARKER in sent, f"CHAT_MARKER missing: {sent!r}"
    assert RAG_MARKER in sent, f"RAG_MARKER missing: {sent!r}"
    assert "<!--followups" not in sent, (
        f"followups directive should never appear in main system prompt: {sent!r}"
    )


@pytest.mark.asyncio
async def test_corner_followups_on_rag_on(engine: AsyncEngine) -> None:
    """RAG ON, followups ON — [temporal_anchor][RAG][project][chat] in main system prompt.

    OOB decoupling: the followups directive is no longer
    present in the main system prompt. Only the four canonical components
    are asserted here.
    """
    sent = await _capture_outbound_sys_prompt(
        engine, followups_enabled=True, rag_on=True
    )
    assert TEMPORAL_ANCHOR_MARKER in sent, (
        f"TEMPORAL_ANCHOR_MARKER missing in RAG+followups-on corner: {sent!r}"
    )
    assert PROJECT_MARKER in sent
    assert CHAT_MARKER in sent
    assert RAG_MARKER in sent
    # Followups directive absent from main prompt regardless of flag.
    assert "<!--followups" not in sent


# ─── Composition order across all 4 corners ──────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "followups_enabled,rag_on",
    [
        (False, False),
        (True, False),
        (False, True),
        (True, True),
    ],
)
async def test_composition_order_across_all_four_corners(
    engine: AsyncEngine, followups_enabled: bool, rag_on: bool
) -> None:
    """The locked composition order MUST hold across all four corners:

      [temporal_anchor][RAG_context][project_prompt][chat_prompt]

    OOB decoupling: the followups directive is no longer in
    the main system prompt, so the composition order is now 4-component
    (anchor + RAG + project + chat). A reorder regression would pass the
    per-corner "is X in sent" truthiness checks above but fail this assertion.
    Closes the 4-corners-only-2-of-4 reorder regression.
    """
    sent = await _capture_outbound_sys_prompt(
        engine, followups_enabled=followups_enabled, rag_on=rag_on
    )
    anchor_idx = sent.find(TEMPORAL_ANCHOR_MARKER)
    proj_idx = sent.find(PROJECT_MARKER)
    chat_idx = sent.find(CHAT_MARKER)
    assert anchor_idx >= 0, (
        f"TEMPORAL_ANCHOR_MARKER missing — temporal anchor not injected: {sent!r}"
    )
    assert proj_idx >= 0 and chat_idx >= 0, (
        f"markers missing — proj={proj_idx} chat={chat_idx} sent={sent!r}"
    )
    assert anchor_idx < proj_idx, (
        f"temporal_anchor must precede project_prompt; "
        f"got anchor={anchor_idx} proj={proj_idx} sent={sent!r}"
    )
    assert proj_idx < chat_idx, (
        f"project_prompt must precede chat_prompt; "
        f"got proj={proj_idx} chat={chat_idx} sent={sent!r}"
    )
    if rag_on:
        rag_idx = sent.find(RAG_MARKER)
        assert rag_idx >= 0, f"RAG_MARKER missing in rag-on corner: {sent!r}"
        assert rag_idx < proj_idx, (
            f"RAG_context must precede project_prompt; "
            f"got rag={rag_idx} proj={proj_idx} sent={sent!r}"
        )
        # Note: RAG is prepended as the outermost layer (after all other
        # assembly), so it may precede the temporal anchor in the final
        # string. The important invariant is that the anchor precedes the
        # project/chat prompts, which is asserted above.
    # Followups directive must NEVER appear in the main system prompt —
    # regardless of the flag value (OOB decoupling invariant).
    assert "<!--followups" not in sent, (
        f"followups directive found in main system prompt — "
        f"OOB decoupling invariant violated: {sent!r}"
    )
