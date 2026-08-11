# SPDX-License-Identifier: Apache-2.0
"""Smoke test for the mock LM Studio server — §1A.

Boots the mock server, exercises each script, and asserts expected
SSE frame sequences are produced.
"""
from __future__ import annotations

import json

import httpx
import pytest

pytestmark = pytest.mark.asyncio(loop_scope="session")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iter_sse_frames(raw: bytes) -> list[tuple[str, dict[str, object]]]:
    """Parse raw SSE bytes into (event_name, data_dict) pairs.

    Mirrors the parser in ``native.py`` but returns dicts instead of
    CanonicalEvent objects so the smoke test has no dependency on
    production types.
    """
    events: list[tuple[str, dict[str, object]]] = []
    current_event: str | None = None
    current_data: str | None = None

    for raw_line in raw.decode("utf-8", errors="replace").splitlines():
        line = raw_line.rstrip("\r")
        if line.startswith("event:"):
            current_event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_part = line[len("data:"):].strip()
            if current_data is None:
                current_data = data_part
            else:
                current_data += "\n" + data_part
        elif line == "":
            if current_event is not None and current_data is not None:
                events.append((current_event, json.loads(current_data)))
            current_event = None
            current_data = None

    if current_event is not None and current_data is not None:
        events.append((current_event, json.loads(current_data)))

    return events


async def _post_chat(base_url: str) -> httpx.Response:
    """POST to /api/v1/chat with a minimal payload and collect the response."""
    payload: dict[str, object] = {
        "model": "test-model",
        "input": [{"type": "text", "content": "hello"}],
        "stream": True,
    }
    async with httpx.AsyncClient(base_url=base_url, timeout=10) as client:
        resp = await client.post("/api/v1/chat", json=payload)
        return resp


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMockSmoke:
    """Smoke tests for each mock LM Studio script."""

    @pytest.mark.parametrize(
        "script_name, expected_types",
        [
            (
                "happy_text",
                [
                    "chat.start",
                    "message.start",
                    "message.delta",
                    "message.delta",
                    "message.delta",
                    "message.end",
                    "chat.end",
                ],
            ),
            (
                "reasoning_then_text",
                [
                    "chat.start",
                    "reasoning.start",
                    "reasoning.delta",
                    "reasoning.delta",
                    "reasoning.end",
                    "message.start",
                    "message.delta",
                    "message.end",
                    "chat.end",
                ],
            ),
            (
                "tool_call_xml",
                [
                    "chat.start",
                    "message.start",
                    "message.delta",
                    "message.delta",
                    "message.end",
                    "chat.end",
                ],
            ),
            (
                "system_prompt_leak",
                [
                    "chat.start",
                    "message.start",
                    "message.delta",
                    "message.delta",
                    "message.end",
                    "chat.end",
                ],
            ),
        ],
    )
    async def test_script_events(
        self,
        mock_lmstudio_server: str,
        mock_lmstudio_script: dict,
        script_name: str,
        expected_types: list[str],
    ) -> None:
        """Each script produces the expected SSE event sequence."""
        mock_lmstudio_script["switch"](script_name)
        resp = await _post_chat(mock_lmstudio_server)
        assert resp.status_code == 200
        ct = resp.headers.get("content-type", "")
        assert ct.startswith("text/event-stream"), f"Expected text/event-stream, got {ct!r}"

        raw = resp.content
        events = _iter_sse_frames(raw)
        types = [ev[0] for ev in events]

        assert types == expected_types, (
            f"Script {script_name!r}: expected types {expected_types}, "
            f"got {types}"
        )

    async def test_happy_text_content(
        self,
        mock_lmstudio_server: str,
        mock_lmstudio_script: dict,
    ) -> None:
        """happy_text's message.delta events carry expected content."""
        mock_lmstudio_script["switch"]("happy_text")
        resp = await _post_chat(mock_lmstudio_server)
        events = _iter_sse_frames(resp.content)

        deltas = [
            str(ev[1]["content"]) for ev in events if ev[0] == "message.delta"
        ]
        assert deltas == ["Hello", " world", "!"]

    async def test_reasoning_content(
        self,
        mock_lmstudio_server: str,
        mock_lmstudio_script: dict,
    ) -> None:
        """reasoning.delta events carry expected content."""
        mock_lmstudio_script["switch"]("reasoning_then_text")
        resp = await _post_chat(mock_lmstudio_server)
        events = _iter_sse_frames(resp.content)

        reasoning_deltas = [
            str(ev[1]["content"]) for ev in events if ev[0] == "reasoning.delta"
        ]
        assert reasoning_deltas == [
            "Let me think step by step.",
            " First, I need to understand the question.",
        ]

    async def test_tool_call_xml_content(
        self,
        mock_lmstudio_server: str,
        mock_lmstudio_script: dict,
    ) -> None:
        """tool_call_xml script carries tool_call XML in message.delta."""
        from lmchat.services.tool_args import recover_xml_tool_calls

        mock_lmstudio_script["switch"]("tool_call_xml")
        resp = await _post_chat(mock_lmstudio_server)
        events = _iter_sse_frames(resp.content)

        deltas = [str(ev[1]["content"]) for ev in events if ev[0] == "message.delta"]
        assert len(deltas) == 2
        assert deltas[0] == "I'll search for that."
        assert "<tool_call>" in deltas[1]
        assert "<function=search>" in deltas[1]
        # Verify the XML round-trips through recover_xml_tool_calls.
        result = recover_xml_tool_calls(deltas[1])
        assert result is not None, "recover_xml_tool_calls returned None"
        calls, _cleaned = result
        assert len(calls) > 0, "recover_xml_tool_calls returned empty calls list"
        assert calls[0]["function"]["name"] == "search"

    async def test_system_prompt_leak_content(
        self,
        mock_lmstudio_server: str,
        mock_lmstudio_script: dict,
    ) -> None:
        """system_prompt_leak script emits system prompt in message.delta."""
        mock_lmstudio_script["switch"]("system_prompt_leak")
        resp = await _post_chat(mock_lmstudio_server)
        events = _iter_sse_frames(resp.content)

        deltas = [str(ev[1]["content"]) for ev in events if ev[0] == "message.delta"]
        assert len(deltas) == 2
        # The first delta contains leaked system prompt content
        assert "You are a helpful assistant" in deltas[0]
        assert "system prompt" in deltas[1]

    async def test_script_switching_reset(
        self,
        mock_lmstudio_server: str,
        mock_lmstudio_script: dict,
    ) -> None:
        """Switching between scripts resets the event sequence."""
        # Load reasoning_then_text first
        mock_lmstudio_script["switch"]("reasoning_then_text")
        resp1 = await _post_chat(mock_lmstudio_server)
        events1 = _iter_sse_frames(resp1.content)
        types1 = [ev[0] for ev in events1]
        assert "reasoning.start" in types1

        # Switch to happy_text
        mock_lmstudio_script["switch"]("happy_text")
        resp2 = await _post_chat(mock_lmstudio_server)
        events2 = _iter_sse_frames(resp2.content)
        types2 = [ev[0] for ev in events2]
        assert "reasoning.start" not in types2
        assert types2.count("message.delta") == 3

    async def test_missing_model_returns_400(
        self,
        mock_lmstudio_server: str,
    ) -> None:
        """A request without 'model' is rejected with 400."""
        payload: dict[str, object] = {
            "input": [{"type": "text", "content": "hello"}],
            "stream": True,
        }
        async with httpx.AsyncClient(base_url=mock_lmstudio_server, timeout=10) as client:
            resp = await client.post("/api/v1/chat", json=payload)
        assert resp.status_code == 400
        assert "model" in resp.text.lower()

    async def test_stalled_stream_returns_chat_start(
        self,
        mock_lmstudio_server: str,
        mock_lmstudio_script: dict,
    ) -> None:
        """stalled_stream sends chat.start then hangs (timeout expected)."""
        mock_lmstudio_script["switch"]("stalled_stream")
        payload = {
            "model": "test-model",
            "input": [{"type": "text", "content": "hello"}],
            "stream": True,
        }
        async with httpx.AsyncClient(
            base_url=mock_lmstudio_server, timeout=3
        ) as client:
            with pytest.raises((httpx.ReadTimeout, httpx.ConnectError)):
                async with client.stream("POST", "/api/v1/chat", json=payload) as resp:
                    assert resp.status_code == 200
                    async for _ in resp.aiter_bytes():
                        pass
                    # Should never get here — connection should timeout.
                    raise AssertionError("stalled_stream should not complete")

    async def test_truncated_handshake_raises(
        self,
        mock_lmstudio_server: str,
        mock_lmstudio_script: dict,
    ) -> None:
        """truncated_handshake sends partial data then raises ConnectionResetError."""
        mock_lmstudio_script["switch"]("truncated_handshake")
        payload = {
            "model": "test-model",
            "input": [{"type": "text", "content": "hello"}],
            "stream": True,
        }
        async with httpx.AsyncClient(
            base_url=mock_lmstudio_server, timeout=5
        ) as client:
            # We expect an error when the connection is reset mid-stream.
            # httpx may raise RemoteProtocolError, ConnectError, or ReadError.
            with pytest.raises(
                (httpx.RemoteProtocolError, httpx.ConnectError, httpx.ReadError)
            ):
                async with client.stream("POST", "/api/v1/chat", json=payload) as resp:
                    async for _ in resp.aiter_bytes():
                        pass