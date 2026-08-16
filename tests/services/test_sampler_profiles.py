# SPDX-License-Identifier: Apache-2.0
"""Tests for sampler_profiles module.

Covers:
- Profile selection logic (thinking + tool_category combinations)
- Profile ownership (returned dict is a copy)
- Model matching (patterns, exclusions)
- profile_for_request integration
- Hot-reload behavior
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from lmchat.services import sampler_profiles


def _expected(vendor: dict) -> dict:
    """Translate vendor profile constants to the wire shape.

    ``select_profile`` applies two renames at the boundary:
      - ``repetition_penalty`` → ``repeat_penalty`` (llama.cpp native name)
      - ``max_tokens`` → ``max_output_tokens`` (LM Studio native field name;
        ``max_tokens`` is compat-only — verified by live probe 2026-06-01).
    """
    out = dict(vendor)
    if "repetition_penalty" in out:
        out["repeat_penalty"] = out.pop("repetition_penalty")
    if "max_tokens" in out:
        out["max_output_tokens"] = out.pop("max_tokens")
    return out


# ---------------------------------------------------------------------------
# Profile selection tests
# ---------------------------------------------------------------------------


def test_select_profile_thinking_general() -> None:
    """thinking=True + tool_category != "code" → PROFILE_THINKING_GENERAL."""
    result = sampler_profiles.select_profile(tool_category="review", thinking=True)
    assert result == _expected(sampler_profiles.PROFILE_THINKING_GENERAL)


def test_select_profile_thinking_coding() -> None:
    """thinking=True + tool_category == "code" → PROFILE_THINKING_CODING."""
    result = sampler_profiles.select_profile(tool_category="code", thinking=True)
    assert result == _expected(sampler_profiles.PROFILE_THINKING_CODING)


def test_select_profile_instruct() -> None:
    """thinking=False → PROFILE_INSTRUCT regardless of tool_category."""
    result = sampler_profiles.select_profile(tool_category="any", thinking=False)
    assert result == _expected(sampler_profiles.PROFILE_INSTRUCT)


def test_select_profile_returns_owned_copy() -> None:
    """Mutating returned dict does NOT mutate the original profile."""
    original = sampler_profiles.PROFILE_THINKING_GENERAL.copy()
    result = sampler_profiles.select_profile(tool_category="review", thinking=True)
    result["temperature"] = 999.0
    assert sampler_profiles.PROFILE_THINKING_GENERAL == original


# ---------------------------------------------------------------------------
# is_profiled_model tests
# ---------------------------------------------------------------------------


def test_is_profiled_model_qwen35ba3b_uppercase() -> None:
    """Qwen3.6-35B-A3B-MLX matches the 35b-a3b pattern."""
    assert sampler_profiles.is_profiled_model("Qwen/Qwen3.6-35B-A3B-MLX") is True


def test_is_profiled_model_qwen122ba10b() -> None:
    """qwen3.5-122b-a10b-claude-distill matches the 122b-a10b pattern."""
    assert sampler_profiles.is_profiled_model("qwen3.5-122b-a10b-claude-distill") is True


def test_is_profiled_model_qwen36_family() -> None:
    """qwen3.6-72b-experimental matches the bare qwen3.6 substring."""
    assert sampler_profiles.is_profiled_model("qwen3.6-72b-experimental") is True


def test_is_profiled_model_scar_exclusion() -> None:
    """qwen3.6-35b-a3b-scar is excluded despite matching 35b-a3b pattern."""
    assert sampler_profiles.is_profiled_model("qwen3.6-35b-a3b-scar") is False


def test_is_profiled_model_non_qwen() -> None:
    """llama-3.1-70b does not match any pattern."""
    assert sampler_profiles.is_profiled_model("llama-3.1-70b") is False


def test_is_profiled_model_empty_id() -> None:
    """Empty model_id returns False."""
    assert sampler_profiles.is_profiled_model("") is False


# ---------------------------------------------------------------------------
# profile_for_request tests
# ---------------------------------------------------------------------------


def test_profile_for_request_non_profiled_returns_none() -> None:
    """Non-profiled model → None."""
    result = sampler_profiles.profile_for_request("llama-3", "high", True)
    assert result is None


def test_profile_for_request_profiled_with_reasoning() -> None:
    """Profiled model + reasoning support + high effort → thinking profile."""
    result = sampler_profiles.profile_for_request("Qwen3.6-35B-A3B", "high", True)
    assert result == _expected(sampler_profiles.PROFILE_THINKING_GENERAL)


def test_profile_for_request_reasoning_off_returns_instruct() -> None:
    """Profiled model + reasoning_effort="off" → PROFILE_INSTRUCT."""
    result = sampler_profiles.profile_for_request("Qwen3.6-35B-A3B", "off", True)
    assert result == _expected(sampler_profiles.PROFILE_INSTRUCT)


def test_profile_for_request_no_reasoning_support_returns_instruct() -> None:
    """Profiled model but no reasoning support → PROFILE_INSTRUCT."""
    result = sampler_profiles.profile_for_request("Qwen3.6-35B-A3B", "high", False)
    assert result == _expected(sampler_profiles.PROFILE_INSTRUCT)


# ---------------------------------------------------------------------------
# StreamingService integration — default (reasoning_effort unset) turn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_new_chat_turn_applies_thinking_profile_for_reasoning_model() -> None:
    """A profiled reasoning model must get the THINKING sampler profile on
    its default (reasoning_effort unset) turn, not INSTRUCT.

    RED-ON-REVERT: ``StreamingService._assemble_system_prompt`` used to only
    fetch model capabilities (``_caps``) when a per-chat ``reasoning_effort``
    setting was ALREADY set. On a brand-new chat — the common case, no
    per-chat override yet — ``_caps`` stayed ``None``, which forced
    ``profile_for_request``'s ``supports_reasoning=False`` branch and
    silently picked PROFILE_INSTRUCT (temperature=0.7) even though the model
    advertises reasoning and should get PROFILE_THINKING_GENERAL
    (temperature=1.0). Reverting the fix makes this assert 0.7.
    """
    from collections.abc import AsyncIterator
    from unittest.mock import AsyncMock, MagicMock

    from sqlalchemy import insert
    from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

    from lmchat.db.schema import chats, metadata
    from lmchat.lmstudio.types import (
        CanonicalChatRequest,
        CanonicalEvent,
        CanonicalInputBlock,
    )
    from lmchat.services.models_service import Capabilities, ReasoningCapability
    from lmchat.services.streaming_service import ChatStreamRequest, StreamingService

    model_id = "Qwen3.6-35B-A3B"  # profiled per is_profiled_model

    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
        # No `settings` → reasoning_effort is unset (default new-chat case).
        await conn.execute(insert(chats).values(user_id=1, title="test"))

    captured: dict[str, Any] = {}

    async def _fake_stream(*args: Any, **kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        captured["request"] = kwargs.get("request") or (args[0] if args else None)
        for ev in (
            CanonicalEvent(type="chat.start", response_id="rid-1"),
            CanonicalEvent(type="message.start"),
            CanonicalEvent(type="message.delta", content="hi"),
            CanonicalEvent(type="message.end"),
            CanonicalEvent(type="chat.end", response_id="rid-1"),
        ):
            yield ev

    lm_client = MagicMock()
    lm_client.stream = _fake_stream

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    models_service = AsyncMock()
    models_service.auth_failed = False
    res = MagicMock(wire_id=model_id, substituted=False)
    models_service.resolve_to_loaded_or_fallback = AsyncMock(return_value=res)
    models_service.get_capabilities = AsyncMock(
        return_value=Capabilities(
            vision=False,
            trained_for_tool_use=True,
            reasoning=ReasoningCapability(
                allowed_options=["off", "low", "medium", "high"], default="medium"
            ),
        )
    )
    models_service.get_max_context_length = AsyncMock(return_value=0)

    svc = StreamingService(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        chat_locks={},
        models_service=models_service,
    )

    from tests.services.conftest import make_disconnect_receive

    request = AsyncMock()
    request.receive = make_disconnect_receive(False)
    user = MagicMock()
    user.id = 1

    async for _ in svc.stream_chat(
        chat_id=1,
        user=user,
        payload=ChatStreamRequest(
            chat_id=1,
            payload=CanonicalChatRequest(
                model=model_id,
                input=[CanonicalInputBlock(type="text", content="hi")],
            ),
        ),
        request=request,
    ):
        pass

    await engine.dispose()

    sent = captured.get("request")
    assert sent is not None, "lm_client.stream was never called"
    assert sent.temperature == sampler_profiles.PROFILE_THINKING_GENERAL["temperature"], (
        "default (reasoning_effort unset) turn on a profiled reasoning model "
        f"must get the THINKING sampler profile (temperature="
        f"{sampler_profiles.PROFILE_THINKING_GENERAL['temperature']}), got "
        f"{sent.temperature} — looks like PROFILE_INSTRUCT "
        f"(temperature={sampler_profiles.PROFILE_INSTRUCT['temperature']}) "
        "was applied instead"
    )


@pytest.mark.asyncio
async def test_sampler_profile_does_not_overwrite_an_explicit_temperature() -> None:
    """A temperature the caller actually chose must survive a profiled model.

    RED-ON-REVERT: ``_assemble_system_prompt`` used to apply the profile as
    ``reasoning_payload.model_copy(update=_profile)`` — unconditional, with
    no check for whether the field already carried a value. Every sampler
    field on the payload is ``X | None = None``, so a non-None value is a
    CHOICE: the per-chat numeric rail exposes temperature/top_p/top_k/min_p/
    repeat_penalty directly, and the active preset puts its own temperature
    on the payload before send. Under the old code a profiled model silently
    replaced all of it, leaving nothing but a server-side log line — no
    warning frame, unlike the tools-trimmed path one function away.

    Reverting the fix makes this assert PROFILE_THINKING_GENERAL's 1.0
    instead of the caller's 0.2.
    """
    from collections.abc import AsyncIterator
    from unittest.mock import AsyncMock, MagicMock

    from sqlalchemy import insert
    from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

    from lmchat.db.schema import chats, metadata
    from lmchat.lmstudio.types import (
        CanonicalChatRequest,
        CanonicalEvent,
        CanonicalInputBlock,
    )
    from lmchat.services.models_service import Capabilities, ReasoningCapability
    from lmchat.services.streaming_service import ChatStreamRequest, StreamingService

    model_id = "Qwen3.6-35B-A3B"  # profiled per is_profiled_model
    chosen_temperature = 0.2

    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
        await conn.execute(insert(chats).values(user_id=1, title="test"))

    captured: dict[str, Any] = {}

    async def _fake_stream(*args: Any, **kwargs: Any) -> AsyncIterator[CanonicalEvent]:
        captured["request"] = kwargs.get("request") or (args[0] if args else None)
        for ev in (
            CanonicalEvent(type="chat.start", response_id="rid-1"),
            CanonicalEvent(type="message.start"),
            CanonicalEvent(type="message.delta", content="hi"),
            CanonicalEvent(type="message.end"),
            CanonicalEvent(type="chat.end", response_id="rid-1"),
        ):
            yield ev

    lm_client = MagicMock()
    lm_client.stream = _fake_stream

    memory_mock = AsyncMock()
    memory_mock.index_message = AsyncMock(return_value=None)

    models_service = AsyncMock()
    models_service.auth_failed = False
    res = MagicMock(wire_id=model_id, substituted=False)
    models_service.resolve_to_loaded_or_fallback = AsyncMock(return_value=res)
    models_service.get_capabilities = AsyncMock(
        return_value=Capabilities(
            vision=False,
            trained_for_tool_use=True,
            reasoning=ReasoningCapability(
                allowed_options=["off", "low", "medium", "high"], default="medium"
            ),
        )
    )
    models_service.get_max_context_length = AsyncMock(return_value=0)

    svc = StreamingService(
        engine=engine,
        lm_client=lm_client,
        memory_service=memory_mock,
        chat_locks={},
        models_service=models_service,
    )

    from tests.services.conftest import make_disconnect_receive

    request = AsyncMock()
    request.receive = make_disconnect_receive(False)
    user = MagicMock()
    user.id = 1

    async for _ in svc.stream_chat(
        chat_id=1,
        user=user,
        payload=ChatStreamRequest(
            chat_id=1,
            payload=CanonicalChatRequest(
                model=model_id,
                input=[CanonicalInputBlock(type="text", content="hi")],
                temperature=chosen_temperature,
            ),
        ),
        request=request,
    ):
        pass

    await engine.dispose()

    sent = captured.get("request")
    assert sent is not None, "lm_client.stream was never called"
    assert sent.temperature == chosen_temperature, (
        "an explicitly-chosen temperature must survive a profiled model; the "
        f"sampler profile overwrote {chosen_temperature} with {sent.temperature}"
    )
    # The profile must still fill fields the caller left unset — this is a
    # "don't clobber a choice" fix, not a "disable sampler profiles" one.
    assert sent.top_p == sampler_profiles.PROFILE_THINKING_GENERAL["top_p"], (
        "fields the caller left unset must still receive the vendor profile; "
        f"top_p is {sent.top_p}"
    )


# ---------------------------------------------------------------------------
# Hot-reload tests
# ---------------------------------------------------------------------------


def test_hot_reload_picks_up_config_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Config changes are picked up after mtime bump and cache reset."""
    # Create a temp config file
    config_path = tmp_path / "sampler_profiles_config.json"
    initial_config: dict[str, Any] = {
        "$schema_version": "1",
        "patterns": [
            {"match": "other", "profile": "instruct", "comment": "other model"}
        ],
    }
    config_path.write_text(json.dumps(initial_config), encoding="utf-8")

    # Monkeypatch _CONFIG_PATH to use our temp file
    monkeypatch.setattr(sampler_profiles, "_CONFIG_PATH", config_path)

    # Reset cache to ensure fresh load
    sampler_profiles._reset_cache_for_testing()

    # Initial state: "foobar-123" should not match
    assert sampler_profiles.is_profiled_model("foobar-123") is False

    # Update config to add "foobar" pattern
    updated_config: dict[str, Any] = {
        "$schema_version": "1",
        "patterns": [
            {"match": "foobar", "profile": "instruct", "comment": "foobar model"},
            {"match": "other", "profile": "instruct", "comment": "other model"},
        ],
    }
    config_path.write_text(json.dumps(updated_config), encoding="utf-8")

    # Bump mtime
    current_mtime = config_path.stat().st_mtime
    os.utime(config_path, (current_mtime + 1, current_mtime + 1))

    # Reset cache to force reload
    sampler_profiles._reset_cache_for_testing()

    # Now "foobar-123" should match
    assert sampler_profiles.is_profiled_model("foobar-123") is True
