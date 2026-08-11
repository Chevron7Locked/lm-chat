# SPDX-License-Identifier: Apache-2.0
"""Integration tests for sampler profile wire-in (PR-S1).

Tests that:
- Sampler profiles are applied for profiled models (Qwen3.6-35B-A3B, etc.)
- Sampler profiles are NOT applied for non-profiled models
- LM_CHAT_DISABLE_SAMPLER_PROFILES=1 env var disables the profile
- The sampler_profile_applied log fires for profiled models
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from typing import Any

import pytest
import structlog
from structlog.contextvars import clear_contextvars

from tests.integration.conftest import get_stub_last_chat_body, register_and_login


@pytest.fixture(autouse=True)
def _stdlib_logging() -> Iterator[None]:
    """Wire structlog through stdlib so caplog captures structured events."""
    from lmchat.logging import configure_logging
    configure_logging(console=False, log_file=None)
    yield
    structlog.reset_defaults()
    root = logging.getLogger()
    for handler in list(root.handlers):
        try:
            handler.flush()
            handler.close()
        finally:
            root.removeHandler(handler)
    root.setLevel(logging.WARNING)
    clear_contextvars()


def _stream_body(chat_id: int, model_id: str) -> dict[str, Any]:
    """Return a minimal valid ChatStreamRequest as a JSON-serialisable dict."""
    return {
        "chat_id": chat_id,
        "payload": {
            "model": model_id,
            "input": [{"type": "text", "content": "hello"}],
        },
    }


@pytest.mark.asyncio
async def test_profile_keys_in_outbound_request_for_qwen_model(
    client: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Profile keys are present in the outbound CanonicalChatRequest for Qwen models."""
    # Use a model_id that matches the Qwen pattern
    model_id = "Qwen3.6-35B-A3B"

    # Authenticate
    _, cookie = await register_and_login(client)

    # Create a chat with this model
    create_resp = await client.post(
        "/api/chats",
        data={"title": "test"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert create_resp.status_code == 201
    chat_id = create_resp.json()["id"]

    # Use the streaming endpoint with the model_id
    stream_resp = await client.post(
        "/api/chat/stream",
        json=_stream_body(chat_id, model_id),
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert stream_resp.status_code == 200
    assert "text/event-stream" in stream_resp.headers.get("content-type", "")

    # Wait for the streaming to complete
    await asyncio.sleep(0.5)

    # Get the last chat body sent to the stub LM Studio
    body = get_stub_last_chat_body()

    # Assert all 7 vendor profile keys reach the wire body.
    # The stub env has no models_service capabilities → `_caps` is None →
    # `supports_reasoning=False` → `profile_for_request` returns PROFILE_INSTRUCT
    # (temperature=0.7, top_p=0.80, ...) not PROFILE_THINKING_GENERAL.
    # The native encoder allowlist was widened 2026-06-01 (live-probed) after
    # the initial PR-S1 review found 4 keys were being dropped.
    assert body.get("temperature") == 0.7, f"temperature: {body.get('temperature')}"
    assert body.get("top_p") == 0.80, f"top_p: {body.get('top_p')}"
    assert body.get("top_k") == 20, f"top_k: {body.get('top_k')}"
    assert body.get("min_p") == 0.0, f"min_p: {body.get('min_p')}"
    assert body.get("presence_penalty") == 1.5, (
        f"presence_penalty: {body.get('presence_penalty')}"
    )
    assert body.get("repeat_penalty") == 1.0, (
        f"repeat_penalty: {body.get('repeat_penalty')}"
    )
    assert body.get("max_output_tokens") == 32768, (
        f"max_output_tokens: {body.get('max_output_tokens')}"
    )

    # Check the log fired
    with caplog.at_level(logging.INFO):
        logs = [r for r in caplog.records if "sampler_profile_applied" in r.getMessage()]
        assert len(logs) >= 1, f"Expected sampler_profile_applied log, got: {caplog.text}"
        log = logs[0]
        assert model_id in log.getMessage()


@pytest.mark.asyncio
async def test_no_profile_keys_for_non_qwen_model(
    client: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Profile keys are NOT present for non-profiled models."""
    # Use a model_id that does NOT match any profile pattern
    model_id = "some-non-qwen-model"

    # Authenticate
    _, cookie = await register_and_login(client)

    # Create a chat with this model
    create_resp = await client.post(
        "/api/chats",
        data={"title": "test"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert create_resp.status_code == 201
    chat_id = create_resp.json()["id"]

    # Use the streaming endpoint with the model_id
    stream_resp = await client.post(
        "/api/chat/stream",
        json=_stream_body(chat_id, model_id),
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert stream_resp.status_code == 200
    assert "text/event-stream" in stream_resp.headers.get("content-type", "")

    # Wait for the streaming to complete
    await asyncio.sleep(0.5)

    # Get the last chat body sent to the stub LM Studio
    body = get_stub_last_chat_body()

    # Assert the profile keys are NOT present (profile not applied).
    # Only check the wire fields the native encoder actually emits.
    assert body.get("temperature") is None, (
        f"temperature should be None, got: {body.get('temperature')}"
    )
    assert body.get("top_p") is None, f"top_p should be None, got: {body.get('top_p')}"
    assert body.get("top_k") is None, f"top_k should be None, got: {body.get('top_k')}"
    assert body.get("repeat_penalty") is None, (
        f"repeat_penalty should be None, got: {body.get('repeat_penalty')}"
    )
    assert body.get("max_output_tokens") is None, (
        f"max_output_tokens should be None, got: {body.get('max_output_tokens')}"
    )

    # Check the log did NOT fire
    with caplog.at_level(logging.INFO):
        logs = [r for r in caplog.records if "sampler_profile_applied" in r.getMessage()]
        assert len(logs) == 0, (
            f"Expected no sampler_profile_applied log, got: {caplog.text}"
        )


@pytest.mark.asyncio
async def test_disable_env_makes_wire_a_noop(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """LM_CHAT_DISABLE_SAMPLER_PROFILES=1 disables the profile."""
    # Set the env var to disable the profile
    monkeypatch.setenv("LM_CHAT_DISABLE_SAMPLER_PROFILES", "1")

    # Use a model_id that matches the Qwen pattern
    model_id = "Qwen3.6-35B-A3B"

    # Authenticate
    _, cookie = await register_and_login(client)

    # Create a chat with this model
    create_resp = await client.post(
        "/api/chats",
        data={"title": "test"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert create_resp.status_code == 201
    chat_id = create_resp.json()["id"]

    # Use the streaming endpoint with the model_id
    stream_resp = await client.post(
        "/api/chat/stream",
        json=_stream_body(chat_id, model_id),
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert stream_resp.status_code == 200
    assert "text/event-stream" in stream_resp.headers.get("content-type", "")

    # Wait for the streaming to complete
    await asyncio.sleep(0.5)

    # Get the last chat body sent to the stub LM Studio
    body = get_stub_last_chat_body()

    # Assert the profile keys are NOT present (env var disabled the profile).
    # Only check the wire fields the native encoder actually emits.
    assert body.get("temperature") is None, (
        f"temperature should be None, got: {body.get('temperature')}"
    )
    assert body.get("top_p") is None, f"top_p should be None, got: {body.get('top_p')}"
    assert body.get("top_k") is None, f"top_k should be None, got: {body.get('top_k')}"
    assert body.get("repeat_penalty") is None, (
        f"repeat_penalty should be None, got: {body.get('repeat_penalty')}"
    )
    assert body.get("max_output_tokens") is None, (
        f"max_output_tokens should be None, got: {body.get('max_output_tokens')}"
    )

    # Check the log did NOT fire
    with caplog.at_level(logging.INFO):
        logs = [r for r in caplog.records if "sampler_profile_applied" in r.getMessage()]
        assert len(logs) == 0, f"Expected no sampler_profile_applied log, got: {caplog.text}"


@pytest.mark.asyncio
async def test_sampler_profile_applied_log_fires(
    client: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The sampler_profile_applied log fires for profiled models."""
    # Use a model_id that matches the Qwen pattern
    model_id = "Qwen3.6-35B-A3B"

    # Authenticate
    _, cookie = await register_and_login(client)

    # Create a chat with this model
    create_resp = await client.post(
        "/api/chats",
        data={"title": "test"},
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert create_resp.status_code == 201
    chat_id = create_resp.json()["id"]

    # Use the streaming endpoint with the model_id
    stream_resp = await client.post(
        "/api/chat/stream",
        json=_stream_body(chat_id, model_id),
        headers={"Cookie": f"lmchat_session={cookie}"},
    )
    assert stream_resp.status_code == 200
    assert "text/event-stream" in stream_resp.headers.get("content-type", "")

    # Wait for the streaming to complete
    await asyncio.sleep(0.5)

    # Get the last chat body sent to the stub LM Studio
    body = get_stub_last_chat_body()

    # Assert the profile keys are present. Stub env → PROFILE_INSTRUCT values.
    assert body.get("temperature") == 0.7
    assert body.get("top_p") == 0.80
    assert body.get("repeat_penalty") == 1.0
    assert body.get("max_output_tokens") == 32768

    # Check the log fired
    with caplog.at_level(logging.INFO):
        logs = [r for r in caplog.records if "sampler_profile_applied" in r.getMessage()]
        assert len(logs) >= 1, f"Expected sampler_profile_applied log, got: {caplog.text}"
        log = logs[0]
        assert model_id in log.getMessage()
        assert "profile_keys" in log.getMessage()
