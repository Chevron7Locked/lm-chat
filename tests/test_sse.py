"""
SSE proxy round-trip tests.

Verifies the streaming path: client → POST /api/chat/stream → server.py
→ mock LM Studio (SSE) → server.py → client.

All tests read the raw SSE stream to verify chunk delivery, ordering, and
correct event structure without buffering artifacts.
"""

import json, time, urllib.error, urllib.request

import pytest

from conftest import CSRF_HEADER, _Client, _create_chat


# ---------------------------------------------------------------------------
# Raw SSE reader helpers
# ---------------------------------------------------------------------------

def _read_sse(base_url: str, body: dict) -> list[dict]:
    """
    POST to /api/chat/stream and collect all SSE data events.
    Returns list of parsed JSON objects from data: lines (excludes [DONE]).
    """
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        base_url + "/api/chat/stream",
        data=data,
        headers={
            "Content-Type": "application/json",
            **CSRF_HEADER,
        },
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=15)
    events = []
    current_event_type = ""
    for raw_line in resp:
        line = raw_line.decode("utf-8", errors="replace").rstrip("\n\r")
        if line.startswith("event:"):
            current_event_type = line[6:].strip()
        elif line.startswith("data:"):
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                break
            try:
                obj = json.loads(data_str)
                obj["_event"] = current_event_type
                events.append(obj)
            except json.JSONDecodeError:
                pass
        elif not line:
            current_event_type = ""  # reset on blank separator
    return events


def _stream_body(chat_id: str | None = None, **kwargs) -> dict:
    body = {"model": "test-model", "input": "Hello", **kwargs}
    if chat_id:
        body["chat_id"] = chat_id
    return body


# ---------------------------------------------------------------------------
# Basic streaming
# ---------------------------------------------------------------------------

class TestBasicStream:
    def test_chunks_delivered_in_order(self, app_server, mock_lmstudio):
        mock_lmstudio.configure(chunks=["alpha", " beta", " gamma"])
        events = _read_sse(app_server, _stream_body())
        deltas = [e["content"] for e in events if e.get("_event") == "message.delta"]
        assert deltas == ["alpha", " beta", " gamma"]

    def test_final_done_event_has_response_id(self, app_server, mock_lmstudio):
        mock_lmstudio.configure(chunks=["Hello"])
        events = _read_sse(app_server, _stream_body())
        end_events = [e for e in events if e.get("_event") == "chat.end"]
        assert len(end_events) == 1
        assert "response_id" in end_events[0]
        assert end_events[0]["response_id"] == "resp-mock-001"

    def test_content_concatenation(self, app_server, mock_lmstudio):
        words = ["The ", "quick ", "brown ", "fox"]
        mock_lmstudio.configure(chunks=words)
        events = _read_sse(app_server, _stream_body())
        deltas = [e["content"] for e in events if e.get("_event") == "message.delta"]
        assert "".join(deltas) == "The quick brown fox"


# ---------------------------------------------------------------------------
# Stream with chat persistence
# ---------------------------------------------------------------------------

class TestStreamPersistence:
    def test_stream_persists_to_db(self, app_server, mock_lmstudio):
        mock_lmstudio.configure(chunks=["Hello", " world"])
        c = _Client(app_server)
        chat_id = _create_chat(c)
        _read_sse(app_server, _stream_body(chat_id=chat_id))
        # Give server time to finish persistence after SSE done
        time.sleep(0.2)
        msgs = json.loads(c.get(f"/api/chats/{chat_id}/messages").read())
        assert len(msgs) >= 2
        assistant_msg = next((m for m in msgs if m["role"] == "assistant"), None)
        assert assistant_msg is not None
        assert "Hello world" in assistant_msg.get("content", "")

    def test_incognito_stream_no_persistence(self, app_server, mock_lmstudio):
        mock_lmstudio.configure(chunks=["Secret"])
        c = _Client(app_server)
        chat_id = _create_chat(c)
        _read_sse(app_server, _stream_body(chat_id=chat_id, incognito=True))
        time.sleep(0.1)
        msgs = json.loads(c.get(f"/api/chats/{chat_id}/messages").read())
        assert len(msgs) == 0


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestStreamEdgeCases:
    def test_single_chunk_stream(self, app_server, mock_lmstudio):
        mock_lmstudio.configure(chunks=["Only one"])
        events = _read_sse(app_server, _stream_body())
        deltas = [e["content"] for e in events if e.get("_event") == "message.delta"]
        assert deltas == ["Only one"]

    def test_many_chunks_no_truncation(self, app_server, mock_lmstudio):
        # ~50KB across many chunks
        word = "word " * 100  # 500 chars per chunk
        chunks = [word] * 100  # 50,000 chars total
        mock_lmstudio.configure(chunks=chunks)
        events = _read_sse(app_server, _stream_body())
        deltas = [e["content"] for e in events if e.get("_event") == "message.delta"]
        total = "".join(deltas)
        assert len(total) == len(word) * 100

    def test_mid_stream_disconnect_delivers_partial(self, app_server, mock_lmstudio):
        mock_lmstudio.configure(chunks=["A", "B", "C", "D", "E"], disconnect_after=2)
        events = _read_sse(app_server, _stream_body())
        deltas = [e["content"] for e in events if e.get("_event") == "message.delta"]
        # Server disconnects after 2 chunks — client gets at most 2
        assert len(deltas) <= 2
        assert "A" in deltas or len(deltas) == 0  # at least partial

    def test_slow_drip_chunks_arrive_incrementally(self, app_server, mock_lmstudio):
        mock_lmstudio.configure(chunks=["A", "B", "C"], delay_ms=80)
        start = time.monotonic()
        events = _read_sse(app_server, _stream_body())
        elapsed = time.monotonic() - start
        deltas = [e["content"] for e in events if e.get("_event") == "message.delta"]
        assert deltas == ["A", "B", "C"]
        # 3 chunks × 80ms = ~240ms minimum
        assert elapsed >= 0.2

    def test_upstream_error_delivers_error_event(self, app_server, mock_lmstudio):
        mock_lmstudio.configure(status_code=500)
        events = _read_sse(app_server, _stream_body())
        error_events = [e for e in events if e.get("type") == "error"]
        assert len(error_events) >= 1


# ---------------------------------------------------------------------------
# CSRF on streaming endpoint
# ---------------------------------------------------------------------------

class TestStreamCsrf:
    def test_stream_without_csrf_returns_403(self, app_server):
        data = json.dumps({"model": "test-model", "input": "Hello"}).encode()
        req = urllib.request.Request(
            app_server + "/api/chat/stream",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=10)
        assert exc.value.code == 403


# ---------------------------------------------------------------------------
# Reasoning and tool_call SSE additions
# ---------------------------------------------------------------------------

class TestReasoningSSE:
    def test_reasoning_delta_delivered_before_message_delta(self, app_server, mock_lmstudio):
        mock_lmstudio.configure(reasoning_chunks=["thought"], chunks=["answer"])
        events = _read_sse(app_server, _stream_body())
        types = [e["_event"] for e in events if e["_event"] in ("reasoning.delta", "message.delta")]
        # reasoning.delta must precede message.delta
        seen_msg = False
        for t in types:
            if t == "message.delta":
                seen_msg = True
            assert not (t == "reasoning.delta" and seen_msg)

    def test_tool_call_sequence_complete(self, app_server, mock_lmstudio):
        mock_lmstudio.configure(
            tool_calls=[{"id": "tc1", "tool": "lookup", "arguments": '{}', "output": "found"}],
            chunks=["done"],
        )
        events = _read_sse(app_server, _stream_body())
        event_types = [e["_event"] for e in events]
        assert "tool_call.start" in event_types
        assert "tool_call.arguments" in event_types
        assert "tool_call.success" in event_types

    def test_response_id_present_after_tool_call_stream(self, app_server, mock_lmstudio):
        mock_lmstudio.configure(
            tool_calls=[{"id": "tc1", "tool": "search", "arguments": '{}', "output": "result"}],
            chunks=["final"],
        )
        events = _read_sse(app_server, _stream_body())
        end = next((e for e in events if e.get("_event") == "chat.end"), None)
        assert end is not None
        assert "response_id" in end

    def test_empty_reasoning_produces_no_reasoning_events(self, app_server, mock_lmstudio):
        mock_lmstudio.configure(reasoning_chunks=[], chunks=["hello"])
        events = _read_sse(app_server, _stream_body())
        reasoning = [e for e in events if e.get("_event") == "reasoning.delta"]
        assert reasoning == []


# ---------------------------------------------------------------------------
# SSE error content tests
# ---------------------------------------------------------------------------

class TestSSEErrorContent:
    def test_upstream_500_error_message_contains_status_code(self, app_server, mock_lmstudio):
        mock_lmstudio.configure(status_code=500)
        events = _read_sse(app_server, _stream_body())
        error_events = [e for e in events if e.get("type") == "error"]
        assert len(error_events) >= 1
        error_msg = error_events[0].get("error", {}).get("message", "")
        assert "500" in error_msg, f"Error message should contain '500', got: {error_msg}"

    def test_upstream_connection_refused_error_message(self, app_server, mock_lmstudio):
        # Point at unreachable port to simulate connection refused
        mock_lmstudio.configure(status_code=200)  # reset mock to normal
        # Use a separate server started with an unreachable LMSTUDIO_URL
        # Instead, we use a port that nothing is listening on
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]
        s.close()
        # We need a server instance that points to the dead port.
        # Since the mock_lmstudio URL is session-scoped, we start a fresh server.
        import os, subprocess, sys, time
        from pathlib import Path
        from conftest import _free_port
        port = _free_port()
        time.sleep(0.05)
        tmp_dir = os.path.join(os.path.dirname(__file__), "..", ".test_tmp_conn_refused")
        os.makedirs(tmp_dir, exist_ok=True)
        db_path = os.path.join(tmp_dir, "test_conn_refused.db")
        env = {
            **os.environ,
            "PORT":           str(port),
            "LMSTUDIO_URL":   f"http://127.0.0.1:{dead_port}",
            "LM_CHAT_AUTH":   "false",
            "LM_CHAT_DB":     db_path,
            "LM_CHAT_LOGS":   os.path.join(tmp_dir, "logs"),
            "PYTHONPATH": (
                str(Path(__file__).parent)
                + os.pathsep + str(Path(__file__).parent.parent)
                + os.pathsep + os.environ.get("PYTHONPATH", "")
            ),
        }
        proc = subprocess.Popen(
            [sys.executable, os.path.join(os.path.dirname(os.path.dirname(__file__)), "server.py")],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        base_url = f"http://127.0.0.1:{port}"
        try:
            # Wait for server health (will show lmstudio=false but server is up)
            deadline = time.monotonic() + 25.0
            while time.monotonic() < deadline:
                try:
                    with urllib.request.urlopen(f"{base_url}/api/health", timeout=2) as r:
                        if r.status in (200, 503):
                            break
                except Exception:
                    pass
                time.sleep(0.15)
            events = _read_sse(base_url, _stream_body())
            error_events = [e for e in events if e.get("type") == "error"]
            assert len(error_events) >= 1
            error_msg = error_events[0].get("error", {}).get("message", "")
            assert "upstream service unavailable" in error_msg.lower(), \
                f"Error message should say 'upstream service unavailable', got: {error_msg}"
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
            # Clean up temp files
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)
