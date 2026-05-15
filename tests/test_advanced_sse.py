"""
Advanced SSE tests: SC/CoVe multi-call behavior, reasoning.delta events,
tool_call.* events.

SC/CoVe are server-side features enabled either via per-chat settings
(PATCH /api/chats/{id}/settings) or directly in the stream body.
Reasoning and tool_call events are mock-injected to test the server's
proxy-and-persist pipeline.

Architecture notes:
- SC (_self_consistency): makes N=3 parallel non-streaming calls to upstream,
  call_count will be >= 3.
- CoVe (_chain_of_verification): makes >= 2 sequential non-streaming calls
  (draft + verification questions + optional answer verification + synthesis).
- Per-chat settings (PATCH) are merged into the request body by
  _resolve_chat_settings before the SC/CoVe check, so setting sc_enabled=True
  in the DB is equivalent to sending it in the stream body.
- incognito is a stream body field; when True, messages are not persisted.
"""

import json
import time
import urllib.error
import urllib.request

import pytest

from conftest import _create_chat


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_sse(base_url: str, body: dict, cookie: str = "") -> list[dict]:
    """POST to /api/chat/stream, return parsed SSE events with _event field."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        base_url + "/api/chat/stream",
        data=data,
        headers={"Content-Type": "application/json", "X-Requested-With": "lm-chat"},
        method="POST",
    )
    if cookie:
        req.add_header("Cookie", cookie)
    resp = urllib.request.urlopen(req, timeout=20)
    events = []
    current_type = ""
    for raw_line in resp:
        line = raw_line.decode("utf-8", errors="replace").rstrip("\n\r")
        if line.startswith("event:"):
            current_type = line[6:].strip()
        elif line.startswith("data:"):
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                break
            try:
                obj = json.loads(data_str)
                obj["_event"] = current_type
                events.append(obj)
            except json.JSONDecodeError:
                pass
        elif not line:
            current_type = ""
    return events


def _stream_body(chat_id=None, **kwargs):
    body = {"model": "test-model", "input": "Hello", **kwargs}
    if chat_id:
        body["chat_id"] = chat_id
    return body


# ---------------------------------------------------------------------------
# SC / CoVe multi-call tests
# ---------------------------------------------------------------------------

class TestSCCoVe:
    def test_sc_enabled_triggers_multiple_upstream_calls(self, app_server, client, mock_lmstudio):
        """Self-consistency mode issues N=3 parallel non-streaming calls
        then a synthesis call — total >= 2 upstream calls per request.

        Previously this test had ``pytest.skip("SC/CoVe not triggering
        multiple calls")`` when ``call_count < 2``, which masked the exact
        regression it was supposed to catch.  Skip removed: if SC doesn't
        fire, that IS the bug.
        """
        mock_lmstudio.configure(chunks=["result"])
        chat_id = _create_chat(client)
        client.patch(f"/api/chats/{chat_id}/settings", {"sc_enabled": True})
        mock_lmstudio.reset()
        mock_lmstudio.configure(chunks=["result"])
        _read_sse(app_server, _stream_body(chat_id=chat_id), cookie=client.cookie)
        time.sleep(0.3)
        assert mock_lmstudio.call_count >= 2, (
            f"sc_enabled should fan out to >=2 upstream calls; got {mock_lmstudio.call_count}"
        )

    def test_cove_enabled_triggers_multiple_upstream_calls(self, app_server, client, mock_lmstudio):
        """CoVe makes >= 2 sequential calls (draft + verification questions
        + optional final).  Skip + bare except removed for the same reason
        as the SC test above."""
        mock_lmstudio.configure(chunks=["draft answer"])
        chat_id = _create_chat(client)
        client.patch(f"/api/chats/{chat_id}/settings", {"cove_enabled": True})
        mock_lmstudio.reset()
        mock_lmstudio.configure(chunks=["draft answer"])
        _read_sse(app_server, _stream_body(chat_id=chat_id), cookie=client.cookie)
        time.sleep(0.3)
        assert mock_lmstudio.call_count >= 2, (
            f"cove_enabled should fan out to >=2 upstream calls; got {mock_lmstudio.call_count}"
        )

    def test_sc_disabled_single_upstream_call(self, app_server, client, mock_lmstudio):
        mock_lmstudio.configure(chunks=["hello"])
        chat_id = _create_chat(client)
        client.patch(f"/api/chats/{chat_id}/settings", {"sc_enabled": False})
        mock_lmstudio.reset()
        mock_lmstudio.configure(chunks=["hello"])
        _read_sse(app_server, _stream_body(chat_id=chat_id), cookie=client.cookie)
        time.sleep(0.1)
        assert mock_lmstudio.call_count == 1

    def test_fresh_chat_single_upstream_call(self, app_server, client, mock_lmstudio):
        mock_lmstudio.configure(chunks=["hi"])
        chat_id = _create_chat(client)
        _read_sse(app_server, _stream_body(chat_id=chat_id), cookie=client.cookie)
        time.sleep(0.1)
        assert mock_lmstudio.call_count == 1

    def test_sc_result_persisted_to_db(self, app_server, client, mock_lmstudio):
        # SC makes N=3 parallel non-streaming calls then returns the consensus.
        # We verify: (a) SC ran — call_count >= 3; (b) the result was persisted.
        mock_lmstudio.configure(chunks=["SC response"])
        chat_id = _create_chat(client)
        client.patch(f"/api/chats/{chat_id}/settings", {"sc_enabled": True})
        mock_lmstudio.reset()
        mock_lmstudio.configure(chunks=["SC response"])
        try:
            _read_sse(app_server, _stream_body(chat_id=chat_id), cookie=client.cookie)
        except Exception:
            pass
        time.sleep(0.5)
        assert mock_lmstudio.call_count >= 3, \
            f"SC should have made at least 3 upstream calls, got {mock_lmstudio.call_count}"
        msgs = json.loads(client.get(f"/api/chats/{chat_id}/messages").read())
        asst_msgs = [m for m in msgs if m["role"] == "assistant"]
        assert len(asst_msgs) >= 1, "SC result should have been persisted as an assistant message"
        assert asst_msgs[-1].get("content"), "Persisted SC message should have non-empty content"

    def test_sc_incognito_not_persisted(self, app_server, client, mock_lmstudio):
        mock_lmstudio.configure(chunks=["incognito SC"])
        chat_id = _create_chat(client)
        client.patch(f"/api/chats/{chat_id}/settings", {"sc_enabled": True})
        mock_lmstudio.reset()
        mock_lmstudio.configure(chunks=["incognito SC"])
        try:
            _read_sse(
                app_server,
                _stream_body(chat_id=chat_id, incognito=True),
                cookie=client.cookie,
            )
        except Exception:
            pass
        time.sleep(0.5)
        msgs = json.loads(client.get(f"/api/chats/{chat_id}/messages").read())
        assert len(msgs) == 0


# ---------------------------------------------------------------------------
# Reasoning events
# ---------------------------------------------------------------------------

class TestSSEReasoning:
    def test_reasoning_delta_events_proxied(self, app_server, mock_lmstudio):
        mock_lmstudio.configure(
            reasoning_chunks=["Step 1: think...", "Step 2: conclude..."],
            chunks=["Final answer"],
        )
        events = _read_sse(app_server, _stream_body())
        reasoning_events = [e for e in events if e.get("_event") == "reasoning.delta"]
        assert len(reasoning_events) == 2
        assert reasoning_events[0]["content"] == "Step 1: think..."

    def test_reasoning_events_precede_message_events(self, app_server, mock_lmstudio):
        mock_lmstudio.configure(
            reasoning_chunks=["thinking"],
            chunks=["answer"],
        )
        events = _read_sse(app_server, _stream_body())
        types = [
            e.get("_event") for e in events
            if e.get("_event") in ("reasoning.delta", "message.delta")
        ]
        seen_message = False
        for t in types:
            if t == "message.delta":
                seen_message = True
            if t == "reasoning.delta" and seen_message:
                pytest.fail("reasoning.delta appeared after message.delta")

    def test_reasoning_parts_persisted(self, app_server, client, mock_lmstudio):
        """The reasoning text emitted in reasoning.delta events must be
        persisted alongside the assistant message.  Previously the fallback
        assertion was ``"thought" in str(asst)`` which stringifies the entire
        row dict — passes if the literal "thought" appears anywhere, including
        content metadata.  This rewrite checks the dedicated field directly
        (the server stores reasoning as a ``<think>...</think>`` prefix in the
        assistant content column when no separate column exists).
        """
        mock_lmstudio.configure(reasoning_chunks=["thought-marker-r9"], chunks=["answer"])
        chat_id = _create_chat(client)
        _read_sse(app_server, _stream_body(chat_id=chat_id), cookie=client.cookie)
        time.sleep(0.3)
        msgs = json.loads(client.get(f"/api/chats/{chat_id}/messages").read())
        asst = next((m for m in msgs if m["role"] == "assistant"), None)
        assert asst is not None, f"no assistant message persisted: {msgs!r}"
        # Reasoning lives in either a dedicated column or the content field
        # wrapped in <think>...</think>.  Either is acceptable as long as it's
        # carrying the exact reasoning text — not just incidentally containing
        # the word "thought".
        reasoning = asst.get("reasoning") or asst.get("thinking") or ""
        content = asst.get("content") or ""
        if not reasoning and "<think>" in content:
            # Strip the <think> wrapper to extract the reasoning body.
            start = content.find("<think>") + len("<think>")
            end = content.find("</think>", start)
            reasoning = content[start:end] if end > start else ""
        assert "thought-marker-r9" in reasoning, (
            f"reasoning text not persisted; got reasoning={reasoning!r} content={content!r}"
        )

    def test_reasoning_not_persisted_in_incognito(self, app_server, client, mock_lmstudio):
        mock_lmstudio.configure(reasoning_chunks=["secret thought"], chunks=["answer"])
        chat_id = _create_chat(client)
        _read_sse(
            app_server,
            _stream_body(chat_id=chat_id, incognito=True),
            cookie=client.cookie,
        )
        time.sleep(0.2)
        msgs = json.loads(client.get(f"/api/chats/{chat_id}/messages").read())
        assert len(msgs) == 0

    def test_no_reasoning_events_when_empty(self, app_server, mock_lmstudio):
        mock_lmstudio.configure(reasoning_chunks=[], chunks=["hello"])
        events = _read_sse(app_server, _stream_body())
        reasoning_events = [e for e in events if e.get("_event") == "reasoning.delta"]
        assert reasoning_events == []

    def test_multiple_reasoning_chunks_all_delivered(self, app_server, mock_lmstudio):
        chunks = [f"thought {i}" for i in range(5)]
        mock_lmstudio.configure(reasoning_chunks=chunks, chunks=["done"])
        events = _read_sse(app_server, _stream_body())
        reasoning_events = [e for e in events if e.get("_event") == "reasoning.delta"]
        assert len(reasoning_events) == 5


# ---------------------------------------------------------------------------
# Tool call events
# ---------------------------------------------------------------------------

class TestSSEToolCalls:
    def _tc(self, **kwargs):
        return {
            "id": "tc1",
            "tool": "search",
            "arguments": '{"q":"test"}',
            "output": "search result",
            **kwargs,
        }

    def test_tool_call_start_proxied(self, app_server, mock_lmstudio):
        mock_lmstudio.configure(tool_calls=[self._tc()], chunks=["done"])
        events = _read_sse(app_server, _stream_body())
        starts = [e for e in events if e.get("_event") == "tool_call.start"]
        assert len(starts) == 1
        assert starts[0]["tool"] == "search"

    def test_tool_call_success_proxied(self, app_server, mock_lmstudio):
        mock_lmstudio.configure(tool_calls=[self._tc()], chunks=["done"])
        events = _read_sse(app_server, _stream_body())
        successes = [e for e in events if e.get("_event") == "tool_call.success"]
        assert len(successes) == 1

    def test_tool_call_failure_proxied(self, app_server, mock_lmstudio):
        mock_lmstudio.configure(
            tool_calls=[self._tc(error="Tool timed out")],
            chunks=["done"],
        )
        events = _read_sse(app_server, _stream_body())
        failures = [e for e in events if e.get("_event") == "tool_call.failure"]
        assert len(failures) == 1

    def test_tool_call_persisted(self, app_server, client, mock_lmstudio):
        # Tool calls are stored as separate messages with role="tool" and name=<tool name>.
        # They are NOT inline fields on the assistant message.
        mock_lmstudio.configure(tool_calls=[self._tc()], chunks=["after tool"])
        chat_id = _create_chat(client)
        _read_sse(app_server, _stream_body(chat_id=chat_id), cookie=client.cookie)
        time.sleep(0.3)
        msgs = json.loads(client.get(f"/api/chats/{chat_id}/messages").read())
        tool_msgs = [m for m in msgs if m.get("role") == "tool"]
        assert len(tool_msgs) >= 1
        assert tool_msgs[0].get("name") == "search"

    def test_multiple_tool_calls_all_delivered(self, app_server, mock_lmstudio):
        tcs = [
            {"id": "tc1", "tool": "search", "arguments": '{"q":"a"}', "output": "r1"},
            {"id": "tc2", "tool": "calc", "arguments": '{"expr":"1+1"}', "output": "2"},
        ]
        mock_lmstudio.configure(tool_calls=tcs, chunks=["done"])
        events = _read_sse(app_server, _stream_body())
        starts = [e for e in events if e.get("_event") == "tool_call.start"]
        assert len(starts) == 2

    def test_partial_tool_call_on_disconnect_handled(self, app_server, mock_lmstudio):
        """A mid-stream disconnect from upstream must not take the server
        down — verify the server keeps responding to /api/health after the
        bad stream attempt.  Previously the test wrapped the request in
        ``try/except: pass`` with no follow-up assertion, so it passed even
        if the server thread had crashed.
        """
        mock_lmstudio.configure(
            tool_calls=[self._tc()],
            chunks=["done"],
            disconnect_after=1,  # drop after first message.delta chunk
        )
        # The bad stream is allowed to raise on the client side (the upstream
        # closed early); what we care about is that the server stays alive.
        try:
            _read_sse(app_server, _stream_body())
        except Exception:
            pass
        import urllib.request
        with urllib.request.urlopen(app_server + "/api/health", timeout=5) as resp:
            assert resp.status == 200, "server crashed after partial-tool-call disconnect"
