# SPDX-License-Identifier: Apache-2.0
"""Tests for C3 — model-decided role adoption (out-of-band, 2026-08-14).

Spec: after a completed MAIN-chat assistant turn, a separate lightweight
call (``_infer_mode_oob``) asks the chat's own model which role preset (if
any) the NEXT turn should run under. Mirrors the OOB-followups decoupling
exactly: the main generation's system_prompt must NEVER carry a
mode-selection directive (that's the failure mode the followups decoupling
fixed — an injected directive measured ~30x local-model reasoning inflation);
the verdict is a separate call, gated by ``lm_chat_mode_adoption_enabled``,
emitted as a synthetic ``mode_adopt`` SSE frame after ``chat.end``.

The classifier's WIRE vocabulary is ``mode_<id>``-PREFIXED tokens (e.g.
``mode_research``), never the bare preset ids — ``general`` in particular is
an ordinary English word (and the default persona), so scanning for the bare
id would false-match prose like "...but in general, ..." and silently drop
the user out of an adopted mode. See ``_MODE_TOKEN_PREFIX``'s doc comment in
streaming_service.py.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import chats, metadata
from lmchat.lmstudio.types import (
    CanonicalChatRequest,
    CanonicalEvent,
    CanonicalInputBlock,
)
from lmchat.services.preset_catalog import (
    DEFAULT_PRESET_ID,
    list_adoptable_preset_ids,
    list_preset_ids,
)
from lmchat.services.streaming_service import (
    _MODE_TOKEN_PREFIX,
    ChatStreamRequest,
    StreamingService,
    _format_mode_adopt_frame,
    _infer_mode_oob,
    _last_valid_mode_id,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers — mirrors tests/services/test_followups_oob.py exactly.
# ---------------------------------------------------------------------------


@pytest.fixture
async def engine() -> AsyncEngine:
    e = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with e.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return e


def _make_events(content: str = "hello") -> list[CanonicalEvent]:
    """Minimal stream — start / message / end."""
    return [
        CanonicalEvent(type="chat.start"),
        CanonicalEvent(type="message.start"),
        CanonicalEvent(type="message.delta", content=content),
        CanonicalEvent(type="message.end"),
        CanonicalEvent(type="chat.end", response_id="r-mode-adopt-test"),
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


def _make_payload(content: str = "say hello") -> ChatStreamRequest:
    canonical = CanonicalChatRequest(
        model="test-model",
        input=[CanonicalInputBlock(type="text", content=content)],
    )
    return ChatStreamRequest(chat_id=1, payload=canonical)


async def _seed_chat(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(insert(chats).values(id=1, user_id=1, title="t"))


async def _build_service(engine: AsyncEngine, lm_client: Any) -> StreamingService:
    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)
    return StreamingService(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        chat_locks={},
        idle_timeout_sec=60,
    )


async def _drain_events(stream: AsyncIterator[bytes]) -> list[dict]:  # type: ignore[type-arg]
    """Drain a streaming_service byte stream; decode SSE frames to dicts."""
    result: list[dict] = []  # type: ignore[type-arg]
    buf = b""
    async for chunk in stream:
        buf += chunk
    for block in buf.split(b"\n\n"):
        lines = block.strip().split(b"\n")
        data_line = next((ln[5:] for ln in lines if ln.startswith(b"data: ")), None)
        if data_line:
            try:
                result.append(json.loads(data_line))
            except json.JSONDecodeError:
                pass
    return result


def _mock_adapter(post_fn: Any) -> MagicMock:
    """Build a MagicMock(spec=LmstudioAdapter) wired to a fake httpx post."""
    from lmchat.services.lmstudio_adapter import LmstudioAdapter

    mock_http = MagicMock()
    mock_http.post = post_fn
    adapter = MagicMock(spec=LmstudioAdapter)
    adapter._http_client = mock_http
    adapter._base_url = "http://lm-studio.local"
    return adapter


class _Resp:
    def __init__(self, content: str = "", reasoning_content: str = "") -> None:
        self._content = content
        self._reasoning = reasoning_content

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:  # type: ignore[type-arg]
        return {
            "choices": [
                {
                    "message": {
                        "content": self._content,
                        "reasoning_content": self._reasoning,
                    }
                }
            ]
        }


# ---------------------------------------------------------------------------
# Test 1: main system_prompt must NEVER contain a mode-adoption directive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_main_system_prompt_never_contains_mode_adopt_directive(
    engine: AsyncEngine,
) -> None:
    """LOCKED constraint: the main answer's system_prompt must stay clean.

    Mirrors test_followups_oob.py's identically-named guard for the
    followups directive — the same incident (an injected directive on the
    MAIN prompt inflating local-model reasoning ~30x) is exactly why C3
    mode adoption is a separate out-of-band call and must never ride the
    main prompt, regardless of whether the feature is enabled.
    """
    captured: dict[str, Any] = {}

    async def _fake_stream(*args: Any, **kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        req = kwargs.get("request") or (args[0] if args else None)
        captured["system_prompt"] = getattr(req, "system_prompt", None) or ""
        for ev in _make_events():
            yield ev

    lm_client = MagicMock()
    lm_client.stream = _fake_stream
    lm_client._adapter = None  # makes _infer_mode_oob return None cheaply

    await _seed_chat(engine)
    svc = await _build_service(engine, lm_client)

    for mode_adoption_enabled in (True, False):
        captured.clear()
        with patch("lmchat.config.get_settings") as mock_settings:
            cfg = MagicMock()
            cfg.lm_chat_followups_enabled = False
            cfg.lm_chat_mode_adoption_enabled = mode_adoption_enabled
            mock_settings.return_value = cfg

            async for _ in svc.stream_chat(
                chat_id=1,
                user=_mock_user(1),
                payload=_make_payload(),
                request=_mock_request(),
            ):
                pass

        sys_prompt = captured.get("system_prompt", "")
        for marker in ("mode_adopt", "persona token", "classify"):
            assert marker not in sys_prompt.lower(), (
                f"mode-adoption directive leaked into main system_prompt "
                f"(mode_adoption_enabled={mode_adoption_enabled}): {sys_prompt!r}"
            )


# ---------------------------------------------------------------------------
# Test 2: `mode_adopt` SSE frame emitted when enabled, absent when disabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mode_adopt_sse_frame_emitted_when_enabled(engine: AsyncEngine) -> None:
    """A `mode_adopt` SSE frame is yielded after chat.end when enabled."""

    async def _fake_stream(*args: Any, **kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        for ev in _make_events():
            yield ev

    lm_client = MagicMock()
    lm_client.stream = _fake_stream
    lm_client._adapter = None  # -> _infer_mode_oob returns None (no model call)

    await _seed_chat(engine)
    svc = await _build_service(engine, lm_client)

    with patch("lmchat.config.get_settings") as mock_settings:
        cfg = MagicMock()
        cfg.lm_chat_followups_enabled = False
        cfg.lm_chat_mode_adoption_enabled = True
        mock_settings.return_value = cfg

        events = await _drain_events(
            svc.stream_chat(
                chat_id=1,
                user=_mock_user(1),
                payload=_make_payload(),
                request=_mock_request(),
            )
        )

    types = [e.get("type") for e in events]
    assert "mode_adopt" in types, f"No mode_adopt frame in events when enabled: {types}"
    frame = next(e for e in events if e.get("type") == "mode_adopt")
    # adapter=None -> the OOB call never fires -> preset_id is None this turn.
    assert frame.get("preset_id") is None


@pytest.mark.asyncio
async def test_mode_adopt_sse_frame_absent_when_disabled(engine: AsyncEngine) -> None:
    """No `mode_adopt` SSE frame when lm_chat_mode_adoption_enabled=False (the default)."""

    async def _fake_stream(*args: Any, **kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        for ev in _make_events():
            yield ev

    lm_client = MagicMock()
    lm_client.stream = _fake_stream
    lm_client._adapter = None

    await _seed_chat(engine)
    svc = await _build_service(engine, lm_client)

    with patch("lmchat.config.get_settings") as mock_settings:
        cfg = MagicMock()
        cfg.lm_chat_followups_enabled = False
        cfg.lm_chat_mode_adoption_enabled = False
        mock_settings.return_value = cfg

        events = await _drain_events(
            svc.stream_chat(
                chat_id=1,
                user=_mock_user(1),
                payload=_make_payload(),
                request=_mock_request(),
            )
        )

    types = [e.get("type") for e in events]
    assert "mode_adopt" not in types, f"mode_adopt frame present when disabled: {types}"


# ---------------------------------------------------------------------------
# Test 3: _infer_mode_oob — the trust gate (never trust free text)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_infer_mode_oob_valid_token_reply_returns_the_id() -> None:
    adapter = _mock_adapter(AsyncMock(return_value=_Resp(content="mode_coder")))
    lm_client = MagicMock()
    lm_client._adapter = adapter

    result = await _infer_mode_oob(
        lm_client=lm_client,
        model="bg@q4",
        conversation_messages=[{"role": "user", "content": "fix this bug"}],
        assistant_answer="Here's the patch.",
    )
    assert result == "coder"


@pytest.mark.asyncio
async def test_infer_mode_oob_none_token_reply_returns_none() -> None:
    adapter = _mock_adapter(AsyncMock(return_value=_Resp(content="mode_none")))
    lm_client = MagicMock()
    lm_client._adapter = adapter

    result = await _infer_mode_oob(
        lm_client=lm_client,
        model="bg@q4",
        conversation_messages=[{"role": "user", "content": "what's the weather like"}],
        assistant_answer="I don't have real-time weather access.",
    )
    assert result is None


@pytest.mark.asyncio
async def test_infer_mode_oob_never_returns_general_even_when_model_says_mode_general() -> None:
    """RED-ON-REVERT for the operator-reported live defect (2026-08-14): a
    local model returned `general` deterministically (8/8) for a clear
    /research-shaped exchange — worse than not adopting a mode, since
    `general` is the DEFAULT persona and this silently clears whatever
    mode the user was already in. `general` must never be offered as an
    option, and even a model that answers `mode_general` anyway (ignoring
    the instructions) must resolve to `None`, not `"general"` — the trust
    gate rejects it exactly like any other token this classifier doesn't
    actually offer."""
    adapter = _mock_adapter(AsyncMock(return_value=_Resp(content="mode_general")))
    lm_client = MagicMock()
    lm_client._adapter = adapter

    result = await _infer_mode_oob(
        lm_client=lm_client,
        model="bg@q4",
        conversation_messages=[
            {
                "role": "user",
                "content": "Look into how vector databases handle high-dimensional indexing.",
            }
        ],
        assistant_answer="Vector DBs use ANN indexes like HNSW and IVF...",
    )
    assert result is None
    assert result != "general"


@pytest.mark.asyncio
async def test_infer_mode_oob_hallucinated_token_returns_none() -> None:
    """A token-shaped reply that isn't a real catalog token is NEVER
    trusted — RED-ON-REVERT for the validation gate (a hallucinated/
    invented id must not apply, even dressed up in the right prefix)."""
    adapter = _mock_adapter(AsyncMock(return_value=_Resp(content="mode_wizard")))
    lm_client = MagicMock()
    lm_client._adapter = adapter

    result = await _infer_mode_oob(
        lm_client=lm_client,
        model="bg@q4",
        conversation_messages=[],
        assistant_answer="anything",
    )
    assert result is None


@pytest.mark.asyncio
async def test_infer_mode_oob_prose_reply_with_no_token_mention_returns_none() -> None:
    """A verbose sentence (ignoring the one-token instruction) that never
    mentions any wire token anywhere is rejected, not partially trusted."""
    adapter = _mock_adapter(
        AsyncMock(
            return_value=_Resp(content="I think this is casual small talk, nothing specific.")
        )
    )
    lm_client = MagicMock()
    lm_client._adapter = adapter

    result = await _infer_mode_oob(
        lm_client=lm_client,
        model="bg@q4",
        conversation_messages=[],
        assistant_answer="anything",
    )
    assert result is None


@pytest.mark.asyncio
async def test_infer_mode_oob_bare_id_words_in_prose_never_match_without_token_prefix() -> None:
    """RED-ON-REVERT for the operator-flagged design fix (2026-08-14): the
    classifier's reply vocabulary is the `mode_`-PREFIXED tokens, never the
    bare preset ids. `general` in particular is an ordinary English word AND
    the default persona — a reply that uses "general"/"research"/"coder" as
    plain English, with the `mode_` prefix nowhere in the text, must resolve
    to None, not silently match and (worst case) wipe an adopted mode back
    to General. This is exactly the false-positive class a bare-id scan
    would have caught here — it must NOT.
    """
    adapter = _mock_adapter(
        AsyncMock(
            return_value=_Resp(
                content="",
                reasoning_content=(
                    "In general, I'd say this research helped the coder "
                    "understand the codebase better, but nothing here calls "
                    "for a specialized mode."
                ),
            )
        )
    )
    lm_client = MagicMock()
    lm_client._adapter = adapter

    result = await _infer_mode_oob(
        lm_client=lm_client,
        model="bg@q4",
        conversation_messages=[],
        assistant_answer="anything",
    )
    assert result is None


@pytest.mark.asyncio
async def test_infer_mode_oob_bare_word_after_label_returns_none_without_token_prefix() -> None:
    """A verbose model that ignores the one-token instruction and answers
    with a bare label ("Mode: coder") — no `mode_` prefix anywhere — is
    rejected. Before the token-vocabulary fix this bare word salvaged to
    "coder"; that is now exactly the behavior the fix removes."""
    adapter = _mock_adapter(AsyncMock(return_value=_Resp(content="Mode: coder")))
    lm_client = MagicMock()
    lm_client._adapter = adapter

    result = await _infer_mode_oob(
        lm_client=lm_client,
        model="bg@q4",
        conversation_messages=[],
        assistant_answer="anything",
    )
    assert result is None


@pytest.mark.asyncio
async def test_infer_mode_oob_salvages_from_reasoning_when_content_has_no_token() -> None:
    """RED-ON-REVERT for the latent bug the C3 mode path inherited from using
    ``oob_message_text``'s WEAKER "field empty" rule directly instead of
    :func:`lmchat.lmstudio.oob_text.oob_salvage`'s "extraction empty" rule.

    ``content`` here is NON-empty ("I'm not sure") but contains no valid
    ``mode_`` token; the real answer is in ``reasoning_content``.
    ``oob_message_text`` alone would return the (non-empty) content and
    the caller would never even look at reasoning_content — silently
    dropping a mode the model actually decided on. Must resolve via
    ``oob_salvage``, which falls back whenever EXTRACTION comes up empty,
    not merely whenever the field itself is empty.
    """
    adapter = _mock_adapter(
        AsyncMock(
            return_value=_Resp(
                content="I'm not sure",
                reasoning_content="On reflection this calls for mode_research.",
            )
        )
    )
    lm_client = MagicMock()
    lm_client._adapter = adapter

    result = await _infer_mode_oob(
        lm_client=lm_client,
        model="bg@q4",
        conversation_messages=[],
        assistant_answer="anything",
    )
    assert result == "research"


@pytest.mark.asyncio
async def test_infer_mode_oob_salvages_from_reasoning_content_when_content_empty() -> None:
    """Reasoning background models can leave `content` empty and put the
    answer in `reasoning_content` — same salvage shape as followups."""
    adapter = _mock_adapter(
        AsyncMock(return_value=_Resp(content="", reasoning_content="mode_research"))
    )
    lm_client = MagicMock()
    lm_client._adapter = adapter

    result = await _infer_mode_oob(
        lm_client=lm_client,
        model="bg@q4",
        conversation_messages=[],
        assistant_answer="anything",
    )
    assert result == "research"


@pytest.mark.asyncio
async def test_infer_mode_oob_salvages_conclusion_from_long_reasoning_prose() -> None:
    """RED-ON-REVERT for the live dogfood failure (2026-08-14): two identical
    requests at temperature=0.0 against a real local model — one returned
    `content="research"` directly, the other left `content=""` and put a
    6306-char (live re-probe: 9212-char) deliberation in `reasoning_content`
    that CORRECTLY concluded research, but the original last-whitespace-token
    implementation took the last token of that PROSE (not a catalog id) and
    silently returned None, killing the flagship /research use case
    ~intermittently. This test uses realistic multi-paragraph deliberation —
    including a bare, unprefixed mention of "research" mid-argument that must
    be ignored — ending on the real `mode_research` wire token, to pin both
    fixes at once (reasoning salvage AND the token-vocabulary guard).
    """
    reasoning = (
        "Let me think through this exchange carefully.\n\n"
        "The user is asking about recent developments and wants me to verify "
        "claims against current sources rather than rely purely on training "
        "knowledge. This isn't a coding task — there's no code being read or "
        "written. It isn't creative writing either, and there's no architecture "
        "or systems-design tradeoff being discussed here.\n\n"
        "Could this be analyst mode? Analyst mode works from material the user "
        "already provided, but here the user is asking me to go FIND new "
        "information and verify it against sources, which is a different "
        "process — that's squarely what the research persona is for: "
        "decompose, search, cite, and flag confidence on every claim.\n\n"
        "Weighing it once more: general conversation doesn't fit, since the "
        "user explicitly wants verified, sourced claims. Coder doesn't fit. "
        "Creative doesn't fit. Analyst is close but the emphasis on external "
        "verification tips it.\n\n"
        "Final answer: mode_research"
    )
    adapter = _mock_adapter(
        AsyncMock(return_value=_Resp(content="", reasoning_content=reasoning))
    )
    lm_client = MagicMock()
    lm_client._adapter = adapter

    result = await _infer_mode_oob(
        lm_client=lm_client,
        model="bg@q4",
        conversation_messages=[{"role": "user", "content": "verify the latest release notes"}],
        assistant_answer="Let me check current sources for that.",
    )
    assert result == "research"


@pytest.mark.asyncio
async def test_infer_mode_oob_reasoning_concludes_none_after_mentioning_tokens() -> None:
    """Deliberation that WEIGHS several personas (via real wire tokens) but
    concludes `mode_none` must return None — mentioning tokens while
    reasoning is not the same as adopting one; only the LAST token counts."""
    reasoning = (
        "This could be mode_coder, since code was mentioned in passing. Or "
        "maybe mode_research, since a fact was checked. But on reflection "
        "this exchange is mostly small talk with a passing reference to "
        "both — neither dominates. This doesn't clearly call for a "
        "specialized mode. Final answer: mode_none"
    )
    adapter = _mock_adapter(
        AsyncMock(return_value=_Resp(content="", reasoning_content=reasoning))
    )
    lm_client = MagicMock()
    lm_client._adapter = adapter

    result = await _infer_mode_oob(
        lm_client=lm_client,
        model="bg@q4",
        conversation_messages=[],
        assistant_answer="anything",
    )
    assert result is None


@pytest.mark.asyncio
async def test_infer_mode_oob_reasoning_with_no_valid_token_anywhere_returns_none() -> None:
    reasoning = (
        "I'm weighing several options here but none of the standard "
        "categories quite fit this exchange, so I'll just answer plainly "
        "without picking a special mode."
    )
    adapter = _mock_adapter(
        AsyncMock(return_value=_Resp(content="", reasoning_content=reasoning))
    )
    lm_client = MagicMock()
    lm_client._adapter = adapter

    result = await _infer_mode_oob(
        lm_client=lm_client,
        model="bg@q4",
        conversation_messages=[],
        assistant_answer="anything",
    )
    assert result is None


@pytest.mark.asyncio
async def test_infer_mode_oob_token_substring_words_do_not_match() -> None:
    """`mode_researcher` must NOT match `mode_research` — the word-boundary
    regex is the guard; a prose reply that ONLY contains a substring
    overlap (no standalone real token anywhere) resolves to None, not a
    false-positive match."""
    adapter = _mock_adapter(
        AsyncMock(
            return_value=_Resp(
                content="",
                reasoning_content=(
                    "Perhaps mode_researcher applies best, though that "
                    "isn't a real option, so nothing here applies cleanly."
                ),
            )
        )
    )
    lm_client = MagicMock()
    lm_client._adapter = adapter

    result = await _infer_mode_oob(
        lm_client=lm_client,
        model="bg@q4",
        conversation_messages=[],
        assistant_answer="anything",
    )
    assert result is None


@pytest.mark.asyncio
async def test_infer_mode_oob_non_lmstudio_adapter_returns_none_without_a_call() -> None:
    """Replay / cloud provider path — skip OOB mode adoption, no HTTP call."""
    lm_client = MagicMock()
    lm_client._adapter = None  # not an LmstudioAdapter instance

    result = await _infer_mode_oob(
        lm_client=lm_client,
        model="bg@q4",
        conversation_messages=[],
        assistant_answer="anything",
    )
    assert result is None


@pytest.mark.asyncio
async def test_infer_mode_oob_upstream_failure_never_raises() -> None:
    async def _raising_post(*_args: Any, **_kwargs: Any) -> Any:
        raise TimeoutError("upstream timed out")

    adapter = _mock_adapter(_raising_post)
    lm_client = MagicMock()
    lm_client._adapter = adapter

    result = await _infer_mode_oob(
        lm_client=lm_client,
        model="bg@q4",
        conversation_messages=[],
        assistant_answer="anything",
    )
    assert result is None


@pytest.mark.asyncio
async def test_infer_mode_oob_empty_reply_returns_none() -> None:
    adapter = _mock_adapter(AsyncMock(return_value=_Resp(content="")))
    lm_client = MagicMock()
    lm_client._adapter = adapter

    result = await _infer_mode_oob(
        lm_client=lm_client,
        model="bg@q4",
        conversation_messages=[],
        assistant_answer="anything",
    )
    assert result is None


@pytest.mark.asyncio
async def test_infer_mode_oob_prompt_carries_every_adoptable_token() -> None:
    """Structural check (mirrors test_distill_oob_labels_assistant_and_scopes_to_user):
    the classifier prompt built for the model must list every ADOPTABLE
    preset's `mode_<id>` wire token, derived from the live catalog — not a
    hardcoded/stale copy — so the model is never asked to choose among
    tokens this codebase doesn't actually have.

    Also pins the operator-reported live defect (2026-08-14): `mode_general`
    must NEVER be offered as an option, and the prompt must not use the
    bare word "general" at all — a local model deterministically (8/8)
    picked the default persona for a clear /research-shaped exchange,
    most likely because the catalog's own "general" entry (and the
    original "reply none for general conversation" hedge) semantically
    primed the model toward that token. See
    preset_catalog.list_adoptable_preset_ids's docstring.
    """
    captured: dict[str, object] = {}

    async def _fake_post(_url: str, **kwargs: object) -> _Resp:
        captured["body"] = kwargs.get("json")
        return _Resp(content="mode_none")

    adapter = _mock_adapter(_fake_post)
    lm_client = MagicMock()
    lm_client._adapter = adapter

    await _infer_mode_oob(
        lm_client=lm_client,
        model="bg@q4",
        conversation_messages=[{"role": "user", "content": "hello"}],
        assistant_answer="hi there",
    )

    body = captured["body"]
    assert isinstance(body, dict)
    messages = body["messages"]
    user_content = str(messages[-1]["content"])
    system_content = str(messages[0]["content"])
    for preset_id in list_adoptable_preset_ids():
        token = f"{_MODE_TOKEN_PREFIX}{preset_id}"
        assert token in user_content, (
            f"catalog token {token!r} missing from the mode-adoption prompt"
        )
    assert DEFAULT_PRESET_ID not in list_adoptable_preset_ids(), (
        "test premise: the default persona must be excluded from the adoptable set"
    )
    assert "mode_general" not in user_content, (
        f"the default persona must never be offered as an adoptable option: {user_content!r}"
    )
    # The bare word "general" must not appear anywhere in the prompt either
    # — not just as a missing token, but as prose ("general conversation")
    # that could still semantically prime the model toward it.
    assert "general" not in user_content.lower(), (
        f"the word 'general' must not appear in the classifier prompt at all: {user_content!r}"
    )
    assert "general" not in system_content.lower()


# ---------------------------------------------------------------------------
# Test 3b: _last_valid_mode_id — direct unit tests (mirrors how
# _last_json_array_of_strings is tested directly in test_followups_oob.py)
# ---------------------------------------------------------------------------


def test_last_valid_mode_id_token_form() -> None:
    assert _last_valid_mode_id("mode_coder", list_preset_ids()) == "coder"


def test_last_valid_mode_id_takes_the_last_match() -> None:
    text = "First I considered mode_coder, then mode_creative, but settled on mode_research."
    assert _last_valid_mode_id(text, list_preset_ids()) == "research"


def test_last_valid_mode_id_none_wins_when_it_is_last() -> None:
    text = "Considered mode_coder and mode_research, but the final answer is mode_none."
    assert _last_valid_mode_id(text, list_preset_ids()) == "none"


def test_last_valid_mode_id_token_substring_words_excluded() -> None:
    text = "A mode_researcher wrote this, referencing mode_codebase notes."
    assert _last_valid_mode_id(text, list_preset_ids()) is None


def test_last_valid_mode_id_bare_ids_without_prefix_do_not_match() -> None:
    """RED-ON-REVERT for the operator-flagged fix: bare "general"/"coder"/
    "research" (no `mode_` prefix) must never match, however plausible the
    surrounding prose looks."""
    text = "In general, this coder found the research helpful."
    assert _last_valid_mode_id(text, list_preset_ids()) is None


def test_last_valid_mode_id_general_word_in_prose_does_not_match() -> None:
    """The specific collision that motivated the fix: `general` is both a
    real preset id AND an ordinary English word used constantly in prose."""
    text = "I'd say that, in general, this conversation went fine."
    assert _last_valid_mode_id(text, list_preset_ids()) is None


def test_last_valid_mode_id_case_insensitive() -> None:
    assert _last_valid_mode_id("MODE_RESEARCH", list_preset_ids()) == "research"
    assert _last_valid_mode_id("Mode_Architect", list_preset_ids()) == "architect"


def test_last_valid_mode_id_empty_text_returns_none() -> None:
    assert _last_valid_mode_id("", list_preset_ids()) is None


def test_last_valid_mode_id_no_match_returns_none() -> None:
    assert _last_valid_mode_id("just a plain reply", list_preset_ids()) is None


def test_last_valid_mode_id_mode_general_never_matches_the_adoptable_list() -> None:
    """RED-ON-REVERT for the operator-reported live defect: when scanned
    against `list_adoptable_preset_ids()` (what `_infer_mode_oob` actually
    passes), `mode_general` cannot match anything — it isn't in the
    vocabulary at all — even when it's the ONLY token-shaped text present."""
    adoptable = list_adoptable_preset_ids()
    assert "general" not in adoptable
    assert _last_valid_mode_id("mode_general", adoptable) is None
    # Even with other real tokens present, mode_general is simply never a
    # candidate — the LAST-match logic can't select it because it never
    # entered the alternation.
    text = "First mode_general, then mode_general again, but really mode_research."
    assert _last_valid_mode_id(text, adoptable) == "research"


# ---------------------------------------------------------------------------
# Test 4: _format_mode_adopt_frame wire format
# ---------------------------------------------------------------------------


def test_format_mode_adopt_frame_wire_format_with_preset() -> None:
    frame = _format_mode_adopt_frame(preset_id="architect", msg_id=42)
    assert isinstance(frame, bytes)
    text = frame.decode("utf-8")
    assert text.startswith("event: mode_adopt\n")
    data_line = next(line for line in text.splitlines() if line.startswith("data: "))
    payload = json.loads(data_line[len("data: "):])
    assert payload["type"] == "mode_adopt"
    assert payload["msg_id"] == 42
    assert payload["preset_id"] == "architect"


def test_format_mode_adopt_frame_wire_format_none() -> None:
    """None is a valid, expected payload (the common no-adoption case)."""
    frame = _format_mode_adopt_frame(preset_id=None, msg_id=1)
    text = frame.decode("utf-8")
    data_line = next(line for line in text.splitlines() if line.startswith("data: "))
    payload = json.loads(data_line[len("data: "):])
    assert payload["preset_id"] is None
