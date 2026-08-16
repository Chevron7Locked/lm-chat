# SPDX-License-Identifier: Apache-2.0
"""Per-turn layer relocation.

``encode_native`` drops ``system_prompt`` whenever ``previous_response_id``
is set (the strict-template fix), so every per-turn layer baked
into ``system_prompt`` silently died on follow-up turns — most
importantly RAG retrieval, which is queried fresh from the current
message every turn. ``relocate_per_turn_layers`` moves the RAG block
(plus a tools-now-available corrective) into ``input[0]`` on follow-ups.

Three layers pinned here:
1. Pure-helper unit tests (first-turn no-op, RAG strip+relocate,
   tools-corrective, wire shape through ``encode_native``).
2. Stream-level: a follow-up request with a RAG hit produces a wire
   body with ``previous_response_id``, NO ``system_prompt``, and the
   retrieval block in ``input[0]``. Turn-1 behaviour is guarded by the
   existing ``test_streaming_a1_composition.py`` corners.
3. Budget-gate call site: on follow-up turns the gate is fed
   ``system_prompt=None`` (the encoder will drop it) while the
   relocated block arrives via ``input_text`` automatically.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import chats, messages, metadata
from lmchat.lmstudio.native import encode_native
from lmchat.lmstudio.types import (
    CanonicalChatRequest,
    CanonicalEvent,
    CanonicalInputBlock,
)
from lmchat.services.prompt_assembly import (
    INJECTED_CLOSE_MARKER,
    INJECTED_OPEN_MARKER,
    RAG_CLOSE_MARKER,
    RAG_HARDENING_CLAUSE,
    RAG_OPEN_MARKER,
    TOOLS_NOW_AVAILABLE_LINE,
    apply_tools_unavailable_corrective,
    format_per_turn_date_line,
    format_tools_unavailable_line,
    relocate_per_turn_layers,
    serialize_prior_turns,
)
from lmchat.services.streaming_service import ChatStreamRequest, StreamingService

RAG_MARKER = "RAG_CONTEXT_BLOCK_MARKER_777"
CHAT_MARKER = "CHAT_PROMPT_MARKER_99"


# ─── Pure helper ──────────────────────────────────────────────────────────


def _req(**overrides: Any) -> CanonicalChatRequest:
    base: dict[str, Any] = {
        "model": "qwen3.6-35b-a3b",
        "input": [CanonicalInputBlock(type="text", content="ping")],
        "system_prompt": f"{RAG_MARKER}\n\n{CHAT_MARKER}",
    }
    base.update(overrides)
    return CanonicalChatRequest(**base)


def test_first_turn_is_a_noop() -> None:
    """No ``previous_response_id`` → the system_prompt path is correct."""
    payload = _req(previous_response_id=None)
    out = relocate_per_turn_layers(
        payload, rag_block=RAG_MARKER, tools_now_available=True
    )
    # Turn 1 with RAG: hardening clause is appended to system_prompt once.
    assert out is not payload
    assert out.system_prompt is not None
    assert RAG_HARDENING_CLAUSE in out.system_prompt
    assert out.system_prompt.count(RAG_HARDENING_CLAUSE) == 1


def test_first_turn_noop_without_rag() -> None:
    """Turn 1 without rag_block IS a pure identity no-op."""
    payload = _req(previous_response_id=None)
    out = relocate_per_turn_layers(
        payload, rag_block=None, tools_now_available=True
    )
    assert out is payload


def test_followup_relocates_rag_to_input0() -> None:
    payload = _req(previous_response_id="resp-prev")
    out = relocate_per_turn_layers(
        payload, rag_block=RAG_MARKER, tools_now_available=False
    )
    # input[0] is the wrapped retrieval block; the user text follows.
    assert out.input[0].type == "text"
    content = out.input[0].content or ""
    assert content.startswith(f"{RAG_OPEN_MARKER}\n{RAG_MARKER}\n{RAG_CLOSE_MARKER}")
    assert TOOLS_NOW_AVAILABLE_LINE not in content
    assert out.input[1].content == "ping"
    # The RAG block is stripped from system_prompt; the chain-persistent
    # remainder stays (the encoder drops the field on follow-ups anyway).
    assert out.system_prompt == CHAT_MARKER
    # The hardening clause is in the per-turn input block (not system_prompt)
    # so it reaches the model this turn.
    assert RAG_HARDENING_CLAUSE.strip() in content
    # Immutability: the inbound payload is untouched.
    assert payload.system_prompt == f"{RAG_MARKER}\n\n{CHAT_MARKER}"
    assert len(payload.input) == 1


def test_followup_rag_only_system_prompt_becomes_none() -> None:
    payload = _req(previous_response_id="resp-prev", system_prompt=RAG_MARKER)
    out = relocate_per_turn_layers(
        payload, rag_block=RAG_MARKER, tools_now_available=False
    )
    assert out.system_prompt is None
    assert RAG_MARKER in (out.input[0].content or "")


def test_followup_tools_corrective_without_rag() -> None:
    payload = _req(previous_response_id="resp-prev")
    out = relocate_per_turn_layers(
        payload, rag_block=None, tools_now_available=True
    )
    content = out.input[0].content or ""
    assert TOOLS_NOW_AVAILABLE_LINE in content
    assert RAG_OPEN_MARKER not in content
    # No RAG strip requested — system_prompt untouched.
    assert out.system_prompt == payload.system_prompt


def test_followup_rag_and_tools_share_one_block() -> None:
    payload = _req(previous_response_id="resp-prev")
    out = relocate_per_turn_layers(
        payload, rag_block=RAG_MARKER, tools_now_available=True
    )
    content = out.input[0].content or ""
    assert RAG_OPEN_MARKER in content
    assert TOOLS_NOW_AVAILABLE_LINE in content
    # One prepended block + the original user block.
    assert len(out.input) == 2


def test_followup_nothing_to_relocate_is_a_noop() -> None:
    payload = _req(previous_response_id="resp-prev")
    out = relocate_per_turn_layers(
        payload, rag_block=None, tools_now_available=False
    )
    assert out is payload


# ─── Tools-unavailable corrective (post-gate, separate from
# relocate_per_turn_layers — see apply_tools_unavailable_corrective's
# docstring for why it can't run through that function: the dropped/
# trimmed set isn't known until AFTER the integrations gate, which runs
# later than relocate_per_turn_layers' own call site) ──────────────────


def test_format_tools_unavailable_line_names_every_dropped_tool() -> None:
    line = format_tools_unavailable_line(["mcp/searxng", "mcp/filesystem"])
    assert "mcp/searxng" in line
    assert "mcp/filesystem" in line
    assert "NOT available this turn" in line


def test_tools_unavailable_corrective_empty_dropped_is_noop() -> None:
    payload = _req(previous_response_id=None)
    out = apply_tools_unavailable_corrective(payload, [])
    assert out is payload
    out2 = apply_tools_unavailable_corrective(payload, [])
    assert out2 is payload


def test_tools_unavailable_corrective_appends_to_system_prompt_on_turn1() -> None:
    """Turn 1: system_prompt is the only vehicle that reaches the wire —
    the corrective is appended there, alongside the (already-stale)
    legend text it corrects."""
    payload = _req(previous_response_id=None, system_prompt=CHAT_MARKER)
    out = apply_tools_unavailable_corrective(payload, ["mcp/searxng"])
    assert out is not payload
    assert out.system_prompt is not None
    assert CHAT_MARKER in out.system_prompt
    assert "mcp/searxng" in out.system_prompt
    assert "NOT available this turn" in out.system_prompt
    # Immutability: inbound payload untouched.
    assert payload.system_prompt == CHAT_MARKER


def test_tools_unavailable_corrective_handles_empty_system_prompt_on_turn1() -> None:
    payload = _req(previous_response_id=None, system_prompt=None)
    out = apply_tools_unavailable_corrective(payload, ["mcp/searxng"])
    assert out.system_prompt is not None
    assert "mcp/searxng" in out.system_prompt


def test_tools_unavailable_corrective_prepends_input_block_on_followup() -> None:
    """Follow-up turn: encode_native drops system_prompt entirely, so the
    corrective must ride input[0] instead — mirroring how
    relocate_per_turn_layers already routes RAG/tools-now-available/date
    correctives there for the identical reason."""
    payload = _req(previous_response_id="resp-prev")
    out = apply_tools_unavailable_corrective(payload, ["mcp/searxng"])
    assert out is not payload
    content = out.input[0].content or ""
    assert "mcp/searxng" in content
    assert "NOT available this turn" in content
    # System_prompt is untouched by this corrective (it never reaches the
    # wire on a follow-up turn anyway).
    assert out.system_prompt == payload.system_prompt
    # Original input is preserved after the new per-turn block.
    assert out.input[-1].content == "ping"


def test_wire_body_shape_through_encode_native() -> None:
    """The actual wire body: previous_response_id present, NO
    system_prompt (the strict-template XOR untouched), retrieval block in input[0]."""
    payload = _req(previous_response_id="resp-prev")
    out = relocate_per_turn_layers(
        payload, rag_block=RAG_MARKER, tools_now_available=False
    )
    body = encode_native(out)
    assert body["previous_response_id"] == "resp-prev"
    assert "system_prompt" not in body
    assert RAG_MARKER in body["input"][0]["content"]
    assert body["input"][1]["content"] == "ping"


# ─── Stream-level (harness mirrors test_streaming_a1_composition.py) ──────


@pytest.fixture
async def engine() -> AsyncEngine:
    e = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with e.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return e


def _make_events() -> list[CanonicalEvent]:
    return [
        CanonicalEvent(type="chat.start"),
        CanonicalEvent(type="message.start"),
        CanonicalEvent(type="message.delta", content="ack"),
        CanonicalEvent(type="message.end"),
        CanonicalEvent(type="chat.end", response_id="r-t02"),
    ]


def _mock_request() -> MagicMock:
    from tests.services.conftest import make_disconnect_receive

    r = MagicMock()
    r.receive = make_disconnect_receive(False)
    return r


def _mock_user(user_id: int = 1) -> MagicMock:
    u = MagicMock()
    u.id = user_id
    return u


def _models_service_mock(
    *, trained_for_tool_use: bool = True, max_ctx: int = 8000
) -> MagicMock:
    from lmchat.services.models_service import ResolvedModel  # noqa: PLC0415

    ms = MagicMock()
    ms.resolve_to_loaded_or_fallback = AsyncMock(
        side_effect=lambda mid, **_kw: ResolvedModel(wire_id=mid, requested=mid)
    )
    ms.get_capabilities = AsyncMock(
        return_value=SimpleNamespace(
            trained_for_tool_use=trained_for_tool_use, reasoning=None
        )
    )
    ms.get_max_context_length = AsyncMock(return_value=max_ctx)
    return ms


async def _seed(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            insert(chats).values(id=1, user_id=1, title="t", project_id=None)
        )


async def _run_stream(
    engine: AsyncEngine,
    *,
    previous_response_id: str | None,
    integrations: list[str] | None = None,
    seed_injected: str | None = None,
) -> tuple[CanonicalChatRequest, dict[str, Any]]:
    """Drive one stream with a stubbed RAG hit. Returns the request the
    lm_client received and the kwargs the budget estimator was fed
    (empty dict when the gate never ran)."""
    captured: dict[str, Any] = {}
    budget_kwargs: dict[str, Any] = {}

    async def _fake_stream(*args: Any, **kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        captured["payload"] = kwargs.get("request") or (args[0] if args else None)
        for ev in _make_events():
            yield ev

    lm_client = MagicMock()
    lm_client.stream = _fake_stream

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    svc = StreamingService(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        chat_locks={},
        idle_timeout_sec=60,
        projects_service=None,
        embedding_client=MagicMock(),
        models_service=_models_service_mock(),
    )
    await _seed(engine)
    if previous_response_id is not None:
        # Hybrid compaction's chain-reset backstop cross-checks
        # an incoming previous_response_id against a real message row before
        # honouring it — an unbacked rid is treated as unknown and dropped.
        # Seed the prior assistant turn that "produced" this rid so these
        # follow-up-turn tests keep exercising RAG relocation, not the
        # backstop itself (no compactions exist here, so the backstop is a
        # pure pass-through once the anchor row is found).
        async with engine.begin() as conn:
            await conn.execute(
                insert(messages).values(
                    chat_id=1,
                    role="assistant",
                    content="prior turn",
                    state="final",
                    response_id=previous_response_id,
                )
            )
    if seed_injected is not None:
        # Mimic inject_message: an assistant row with response_id=NULL (never
        # went through LM Studio's chain) sitting in the thread.
        async with engine.begin() as conn:
            await conn.execute(
                insert(messages).values(
                    chat_id=1,
                    role="assistant",
                    content=seed_injected,
                    state="final",
                    response_id=None,
                )
            )

    canonical = CanonicalChatRequest(
        model="qwen3.6-35b-a3b",
        input=[CanonicalInputBlock(type="text", content="ping")],
        system_prompt=CHAT_MARKER,
        previous_response_id=previous_response_id,
        integrations=integrations or [],
    )
    payload = ChatStreamRequest(chat_id=1, payload=canonical)

    async def _stub_augment(*args: Any, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            context_block=RAG_MARKER, memory_hits=1, doc_hits=1, ctx_window=0
        )

    from lmchat.services._token_budget import estimate_context_budget as _real_gate

    def _spy_gate(**kwargs: Any) -> Any:
        budget_kwargs.update(kwargs)
        return _real_gate(**kwargs)

    settings_patch = patch("lmchat.config.get_settings")
    rag_patch = patch(
        "lmchat.services.rag_service.augment_prompt", side_effect=_stub_augment
    )
    gate_patch = patch(
        "lmchat.services._token_budget.estimate_context_budget",
        side_effect=_spy_gate,
    )
    with settings_patch as mock_settings, rag_patch, gate_patch:
        cfg = MagicMock()
        cfg.lm_chat_followups_enabled = False
        mock_settings.return_value = cfg
        async for _ in svc.stream_chat(
            chat_id=1,
            user=_mock_user(1),
            payload=payload,
            request=_mock_request(),
        ):
            pass

    sent = captured.get("payload")
    assert sent is not None, "lm_client.stream was not called"
    return sent, budget_kwargs


@pytest.mark.asyncio
async def test_followup_stream_relocates_rag_into_wire_input(
    engine: AsyncEngine,
) -> None:
    """Follow-up + RAG hit: the wire body carries previous_response_id,
    NO system_prompt, and the retrieval block in input[0]."""
    sent, _ = await _run_stream(engine, previous_response_id="resp-prev")

    body = encode_native(sent)
    assert body["previous_response_id"] == "resp-prev"
    assert "system_prompt" not in body
    first_block = body["input"][0]["content"]
    assert RAG_OPEN_MARKER in first_block
    assert RAG_MARKER in first_block
    # The user's actual message follows the relocated block.
    assert body["input"][1]["content"] == "ping"
    # The canonical request's remaining system_prompt no longer carries
    # the RAG block (it was relocated, not duplicated).
    assert RAG_MARKER not in (sent.system_prompt or "")


@pytest.mark.asyncio
async def test_turn1_stream_keeps_rag_in_system_prompt(
    engine: AsyncEngine,
) -> None:
    """Turn 1 (no chain anchor): unchanged behaviour — RAG stays in
    system_prompt and reaches the wire there."""
    sent, _ = await _run_stream(engine, previous_response_id=None)

    body = encode_native(sent)
    assert "previous_response_id" not in body
    assert RAG_MARKER in body["system_prompt"]
    # No relocated block was prepended.
    assert body["input"][0]["content"] == "ping"
    assert RAG_OPEN_MARKER not in body["input"][0]["content"]


@pytest.mark.asyncio
async def test_budget_gate_excludes_system_tokens_on_followup(
    engine: AsyncEngine,
) -> None:
    """On follow-up turns the gate must not count a system_prompt the
    encoder will drop; the relocated block arrives via input_text."""
    _, budget_kwargs = await _run_stream(
        engine,
        previous_response_id="resp-prev",
        integrations=["mcp/searxng"],
    )
    assert budget_kwargs, "budget gate did not run"
    assert budget_kwargs["system_prompt"] is None
    # The relocated per-turn block is counted via input_text.
    assert RAG_MARKER in budget_kwargs["input_text"]
    # tools_now_available corrective rides in the same block.
    assert TOOLS_NOW_AVAILABLE_LINE in budget_kwargs["input_text"]
    assert budget_kwargs["integrations"] == ["mcp/searxng"]


@pytest.mark.asyncio
async def test_budget_gate_counts_system_tokens_on_turn1(
    engine: AsyncEngine,
) -> None:
    """Turn 1: the system_prompt genuinely goes on the wire — the gate
    keeps counting it."""
    _, budget_kwargs = await _run_stream(
        engine,
        previous_response_id=None,
        integrations=["mcp/searxng"],
    )
    assert budget_kwargs, "budget gate did not run"
    assert budget_kwargs["system_prompt"] is not None
    assert RAG_MARKER in budget_kwargs["system_prompt"]
    assert RAG_OPEN_MARKER not in budget_kwargs["input_text"]


# ─── Injected sub-session summary visibility ────────────────────
#
# Injected assistant messages (response_id=NULL, appended via
# ``inject_message``) are NOT in LM Studio's server-side chain state.
# On follow-up turns, their content must be placed in the per-turn
# ``input`` block so the model sees it.


def test_followup_injected_messages_prepended() -> None:
    """Follow-up with injected messages: the input block contains the
    injected content wrapped in [Earlier in this conversation] markers."""
    payload = _req(previous_response_id="resp-prev", system_prompt=CHAT_MARKER)
    injected = ["sub-session summary: research complete."]
    out = relocate_per_turn_layers(
        payload,
        rag_block=None,
        tools_now_available=False,
        injected_messages=injected,
    )
    assert out is not payload
    content = out.input[0].content or ""
    assert INJECTED_OPEN_MARKER in content, (
        f"INJECTED_OPEN_MARKER missing: {content!r}"
    )
    assert INJECTED_CLOSE_MARKER in content, (
        f"INJECTED_CLOSE_MARKER missing: {content!r}"
    )
    assert "sub-session summary: research complete." in content
    # Immutability: inbound payload untouched.
    assert payload.system_prompt == CHAT_MARKER
    assert len(payload.input) == 1


def test_followup_multiple_injected_messages() -> None:
    """Multiple injected messages are joined and all appear in the block."""
    payload = _req(previous_response_id="resp-prev")
    injected = ["summary A", "summary B"]
    out = relocate_per_turn_layers(
        payload,
        rag_block=None,
        tools_now_available=False,
        injected_messages=injected,
    )
    content = out.input[0].content or ""
    assert INJECTED_OPEN_MARKER in content
    assert "summary A" in content
    assert "summary B" in content
    assert INJECTED_CLOSE_MARKER in content


def test_followup_no_injected_messages_noop() -> None:
    """Follow-up with no injected messages: behaviour unchanged (no
    spurious block)."""
    payload = _req(previous_response_id="resp-prev")
    out = relocate_per_turn_layers(
        payload,
        rag_block=None,
        tools_now_available=False,
        injected_messages=None,
    )
    assert out is payload
    # Also test with empty list.
    out2 = relocate_per_turn_layers(
        payload,
        rag_block=None,
        tools_now_available=False,
        injected_messages=[],
    )
    assert out2 is payload


@pytest.mark.asyncio
async def test_turn1_stream_relocates_injected_summary_into_wire_input(
    engine: AsyncEngine,
) -> None:
    """Fresh chat / no chain anchor (the /research → Add to main chat case):
    an injected assistant summary (response_id NULL) is relocated into the wire
    input so the model sees it. Previously the orphan query was
    skipped on previous_response_id=None and the summary never reached the
    model — the follow-up answered with no knowledge of it."""
    sent, _ = await _run_stream(
        engine,
        previous_response_id=None,
        seed_injected="SERVER_FARM_SUMMARY_MARKER",
    )
    wire_input = "".join((blk.content or "") for blk in sent.input)
    assert "SERVER_FARM_SUMMARY_MARKER" in wire_input, (
        "injected summary must be relocated into wire input on a fresh chat"
    )
    assert INJECTED_OPEN_MARKER in wire_input


def test_turn1_injected_messages_relocated_into_input() -> None:
    """Turn 1 / no chain anchor (fresh chat: /research → Add to main chat):
    the injected summary MUST still reach the model — relocated into input[0],
    since there's no chain to carry it and no message replay."""
    payload = _req(previous_response_id=None)
    out = relocate_per_turn_layers(
        payload,
        rag_block=None,
        tools_now_available=False,
        injected_messages=["server-farm summary"],
    )
    content = out.input[0].content or ""
    assert content.startswith(INJECTED_OPEN_MARKER)
    assert "server-farm summary" in content
    assert INJECTED_CLOSE_MARKER in content
    # Original input is preserved after the per-turn block.
    assert len(out.input) == len(payload.input) + 1
    assert out.input[-1].content == "ping"


def test_turn1_no_injected_no_rag_is_noop() -> None:
    """Turn 1 with neither injected messages nor RAG block: pure no-op."""
    payload = _req(previous_response_id=None, system_prompt="BASE")
    out = relocate_per_turn_layers(
        payload,
        rag_block=None,
        tools_now_available=False,
        injected_messages=None,
    )
    assert out is payload


def test_followup_injected_and_rag_together() -> None:
    """Both injected messages and RAG block present: both appear in
    input[0], injected first, then RAG + hardening, then tools note."""
    payload = _req(previous_response_id="resp-prev")
    injected = ["injected summary"]
    out = relocate_per_turn_layers(
        payload,
        rag_block=RAG_MARKER,
        tools_now_available=True,
        injected_messages=injected,
    )
    content = out.input[0].content or ""
    # Injected block comes first.
    assert content.startswith(INJECTED_OPEN_MARKER)
    assert "injected summary" in content
    assert INJECTED_CLOSE_MARKER in content
    # RAG block follows.
    rag_idx = content.index(RAG_OPEN_MARKER)
    injected_close_idx = content.index(INJECTED_CLOSE_MARKER)
    assert injected_close_idx < rag_idx, (
        f"Injected block should precede RAG block; "
        f"injected_close at {injected_close_idx}, RAG at {rag_idx}"
    )
    assert TOOLS_NOW_AVAILABLE_LINE in content
    # System prompt still had RAG stripped.
    assert RAG_MARKER not in (out.system_prompt or "")


# ─── RAG relocation content-decoupling ────────────────


def test_followup_rag_strip_survives_prepend_format_drift() -> None:
    """The RAG block is stripped from system_prompt by the sentinel
    markers, not by matching rag_block's raw text. If the prepended
    block's exact formatting ever drifts from what the caller passes as
    ``rag_block`` (e.g. re-wrapped, whitespace-normalized), the strip
    must still find the marker-delimited span and must NOT leave the
    block duplicated in system_prompt — the failure mode startswith/in
    matching alone is exposed to."""
    drifted_system_prompt = (
        f"{RAG_OPEN_MARKER}\nsome DRIFTED formatting of the retrieval "
        f"text, not byte-identical to rag_block\n{RAG_CLOSE_MARKER}"
        f"\n\n{CHAT_MARKER}"
    )
    payload = _req(
        previous_response_id="resp-prev",
        system_prompt=drifted_system_prompt,
    )
    out = relocate_per_turn_layers(
        payload, rag_block=RAG_MARKER, tools_now_available=False
    )
    # The sentinel-delimited span is gone from system_prompt even though
    # its content never matched `rag_block` — startswith/in matching
    # alone would have left it behind (the duplication risk this closes).
    assert out.system_prompt == CHAT_MARKER, (
        f"drifted RAG span must still be stripped via markers: {out.system_prompt!r}"
    )
    assert "DRIFTED formatting" not in (out.system_prompt or "")
    # The per-turn input block still carries the caller-supplied
    # rag_block content, wrapped in the same markers.
    content = out.input[0].content or ""
    assert content.startswith(f"{RAG_OPEN_MARKER}\n{RAG_MARKER}\n{RAG_CLOSE_MARKER}")


# ─── shared "## Prior turns" serialization ────────────


def test_serialize_prior_turns_exact_format() -> None:
    """Pins the exact ``## Prior turns`` suffix shared by all three call
    sites (chain tool-turn replay, quality-mode replay fold-in,
    sub-session bridge)."""
    block = serialize_prior_turns(
        [("user", "what is the weather?"), ("assistant", "let me search for that")]
    )
    assert block == (
        "\n\n## Prior turns\nuser: what is the weather?\n"
        "assistant: let me search for that"
    )


def test_serialize_prior_turns_empty_is_empty_string() -> None:
    """Empty history -> "" (not the bare header), so callers can
    unconditionally append without a separate emptiness guard."""
    assert serialize_prior_turns([]) == ""


def test_serialize_prior_turns_composes_onto_existing_system_prompt() -> None:
    """All three call sites do `existing_sys + serialize_prior_turns(...)`
    — pin that composition directly."""
    composed = CHAT_MARKER + serialize_prior_turns([("user", "hi")])
    assert composed == f"{CHAT_MARKER}\n\n## Prior turns\nuser: hi"


# ─── fresh date on chain follow-ups ───────────────────


def test_followup_per_turn_date_appended_to_input() -> None:
    """Follow-up turn with per_turn_date set: a fresh-date corrective is
    appended to input[0] — the only thing that reaches the model this
    turn, since encode_native drops system_prompt on follow-ups."""
    payload = _req(previous_response_id="resp-prev")
    out = relocate_per_turn_layers(
        payload,
        rag_block=None,
        tools_now_available=False,
        per_turn_date="Thursday, July 17, 2031 at 09:42 CST (UTC-06:00)",
    )
    content = out.input[0].content or ""
    assert "Thursday, July 17, 2031 at 09:42 CST (UTC-06:00)" in content
    assert content.startswith(format_per_turn_date_line(
        "Thursday, July 17, 2031 at 09:42 CST (UTC-06:00)"
    ))


def test_turn1_per_turn_date_ignored() -> None:
    """Turn 1: per_turn_date must be a no-op even if a caller passes one
    — the fresh [Context] block already reaches the wire whole via
    system_prompt; a per-turn corrective would just duplicate it."""
    payload = _req(previous_response_id=None)
    out = relocate_per_turn_layers(
        payload,
        rag_block=None,
        tools_now_available=False,
        per_turn_date="SHOULD_NOT_APPEAR",
    )
    assert out is payload


@pytest.mark.asyncio
async def test_followup_stream_carries_fresh_date_in_wire_input(
    engine: AsyncEngine,
) -> None:
    """Chain mode bakes the [Context] block's date into system_prompt on
    turn 1 only (chain-persistent). encode_native drops system_prompt on
    every follow-up, and pre-fix relocate_per_turn_layers never
    re-emitted the date — so a long-lived chain-mode chat reported turn
    1's date forever. On a follow-up turn the freshly-computed date must
    reach input[0], the only thing that actually reaches the model this
    turn."""
    sent, _ = await _run_stream(engine, previous_response_id="resp-prev")

    body = encode_native(sent)
    assert "system_prompt" not in body, (
        "sanity: system_prompt must be dropped on follow-ups (a45c2ca XOR)"
    )
    wire_input = "".join(blk.get("content", "") for blk in body["input"])
    assert "[Runtime update: the current date and time is now " in wire_input, (
        f"fresh date corrective missing from follow-up wire input: {wire_input!r}"
    )
