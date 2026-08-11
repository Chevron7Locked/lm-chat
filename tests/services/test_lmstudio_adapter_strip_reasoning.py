# SPDX-License-Identifier: Apache-2.0
"""Tests for §1.3 — strip reasoning_content from sent history.

LMSTUDIO-MULTIMODEL-HARDENING-PLAN-2026-06-06 §1.3 acceptance.

Note (defense-in-depth): the compat + responses encoders already drop
``reasoning_content`` when serializing to the wire (see
``src/lmchat/lmstudio/compat.py:170,194``: "reasoning and integrations
are native-only — explicitly excluded here"). The §1.3 helper is one
layer up: it clears the field on the CanonicalMessage history list
BEFORE the encoder runs. If a future encoder change accidentally lets
reasoning_content through, the helper catches it and keeps the wire
clean for quirk-profile models. The acceptance tests pin the helper's
behavior + the wiring into ``stream_chat``.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from lmchat.lmstudio.types import (
    CanonicalChatRequest,
    CanonicalInputBlock,
    CanonicalMessage,
    CanonicalTool,
)
from lmchat.services.lmstudio_adapter import (
    LmstudioAdapter,
    _strip_reasoning,
)
from lmchat.services.params_service import ParamsService

# ─── Unit — _strip_reasoning ────────────────────────────────────────────────


def test_strip_reasoning_clears_assistant_reasoning_content() -> None:
    history = [
        CanonicalMessage(role="user", content="hi"),
        CanonicalMessage(
            role="assistant",
            content="my reply",
            reasoning_content="my hidden thoughts",
        ),
        CanonicalMessage(role="user", content="next q"),
    ]
    stripped = _strip_reasoning(history)
    assert len(stripped) == 3
    assert stripped[0].content == "hi"
    assert stripped[0].reasoning_content is None
    assert stripped[1].content == "my reply"
    assert stripped[1].reasoning_content is None
    assert stripped[2].content == "next q"


def test_strip_reasoning_does_not_mutate_source() -> None:
    msg = CanonicalMessage(
        role="assistant",
        content="reply",
        reasoning_content="thoughts",
    )
    history = [msg]
    _stripped = _strip_reasoning(history)
    assert msg.reasoning_content == "thoughts"
    assert history[0].reasoning_content == "thoughts"


def test_strip_reasoning_skips_user_and_tool_rows() -> None:
    history = [
        CanonicalMessage(
            role="user",
            content="hello",
            reasoning_content="user-side hint",
        ),
    ]
    stripped = _strip_reasoning(history)
    assert stripped[0].reasoning_content == "user-side hint"


# ─── Wiring — adapter calls strip helper iff profile says so ────────────────


def _make_history() -> list[CanonicalMessage]:
    return [
        CanonicalMessage(role="user", content="first q"),
        CanonicalMessage(
            role="assistant",
            content="first reply",
            reasoning_content="LONG THOUGHTS",
        ),
    ]


def _make_request(model: str) -> CanonicalChatRequest:
    return CanonicalChatRequest(
        model=model,
        input=[CanonicalInputBlock(type="text", content="next q")],
        tools=[
            CanonicalTool(
                name="x",
                description="x",
                parameters={},
            )
        ],
    )


@pytest.mark.asyncio
async def test_quirk_profile_passes_stripped_history_to_encoder() -> None:
    """A quirk-profile request → encoder receives history with
    ``reasoning_content`` cleared on the assistant row."""
    captured_history: list[CanonicalMessage] | None = None

    def _capture_responses(req, history):  # type: ignore[no-untyped-def]
        nonlocal captured_history
        captured_history = history
        return {"model": req.model, "input": []}

    async def _fake_post_stream(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return None  # short-circuit the stream entirely

    with patch(
        "lmchat.services.lmstudio_adapter.encode_responses",
        side_effect=_capture_responses,
    ):
        async with httpx.AsyncClient() as client:
            adapter = LmstudioAdapter(
                http_client=client,
                base_url="http://lm-studio.test:1234",
                params_service=ParamsService(),
            )
            adapter._post_stream = MagicMock(side_effect=_fake_post_stream)  # type: ignore[method-assign]
            req = _make_request("qwen3.6-35b-a3b")
            try:
                async for _ in adapter.stream_chat(req, history=_make_history()):
                    pass
            except Exception:
                # _post_stream short-circuits — surface any decode error and
                # ignore; what matters is that encode_responses was called
                # with the stripped history before the stream started.
                pass

    assert captured_history is not None
    assert captured_history[0].role == "user"
    assert captured_history[1].role == "assistant"
    assert captured_history[1].content == "first reply"
    assert captured_history[1].reasoning_content is None


@pytest.mark.asyncio
async def test_default_profile_passes_unstripped_history_to_encoder() -> None:
    """An untracked model → DEFAULT_PROFILE → encoder receives history
    with ``reasoning_content`` INTACT. Regression guard for the
    default-off case."""
    captured_history: list[CanonicalMessage] | None = None

    def _capture_responses(req, history):  # type: ignore[no-untyped-def]
        nonlocal captured_history
        captured_history = history
        return {"model": req.model, "input": []}

    async def _fake_post_stream(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return None

    with patch(
        "lmchat.services.lmstudio_adapter.encode_responses",
        side_effect=_capture_responses,
    ):
        async with httpx.AsyncClient() as client:
            adapter = LmstudioAdapter(
                http_client=client,
                base_url="http://lm-studio.test:1234",
                params_service=ParamsService(),
            )
            adapter._post_stream = MagicMock(side_effect=_fake_post_stream)  # type: ignore[method-assign]
            req = _make_request("some-untracked-model")
            try:
                async for _ in adapter.stream_chat(req, history=_make_history()):
                    pass
            except Exception:
                pass

    assert captured_history is not None
    # Default profile = no strip → reasoning bytes ride forward into encoder.
    assert captured_history[1].reasoning_content == "LONG THOUGHTS"
