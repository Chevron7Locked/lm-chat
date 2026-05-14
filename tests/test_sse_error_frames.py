"""SSE error-frame contract tests.

These tests assert that every error path in ``_handle_chat_stream`` emits a
frame the *client* will actually deliver to its handler.  ``app.js``'s
``processSSEBlock`` returns early on any block without an ``event:`` line, so
data-only error frames are silently dropped — the user sees a dead spinner
instead of an error message.

We drive the server in-process (so coverage tracing sees the branches) and
parse the response with a Python port of the JS parser so the contract is
checked from the *client's* point of view rather than the server's.

Each test exercises one of the three error paths in ``_handle_chat_stream``:

1. **Upstream open failure** — LM Studio returns non-200 or the connection
   fails.  The handler emits ``event: error\\n`` with a structured message.
2. **Stream collect exception** — ``_collect_stream`` raises.  The handler
   used to emit a ``data:``-only frame (silently dropped by the client);
   the source-level invariant test below makes that regression a CI failure.
3. **Empty stream** — LM Studio closes the connection without any
   ``message.delta`` or ``chat.end``.  Same bug, same fix.
"""

from __future__ import annotations

import json
import re
import urllib.request
import urllib.error

import pytest

from conftest import (
    CSRF_HEADER,
    AuthedClient,
    all_raw_frames,
    parse_sse_like_client,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _post_chat_stream_raw(base_url: str, cookie: str, body: dict) -> bytes:
    """POST to /api/chat/stream and return the raw response body bytes.

    We can't use _Client.post — that one buffers via urllib.request.urlopen
    and doesn't expose status code on non-2xx without unwrapping.  We need
    raw bytes either way to feed the SSE parser.
    """
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        base_url + "/api/chat/stream",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Cookie": cookie,
            **CSRF_HEADER,
        },
        method="POST",
    )
    # Streaming responses: read() pulls until the server closes the connection.
    # The server sets Connection: close on the SSE response so this is bounded.
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        body_bytes = resp.read()
    except urllib.error.HTTPError as e:
        body_bytes = e.read()
    return body_bytes


def _new_chat_id(client) -> str:
    resp = client.post("/api/chats", {"title": "sse-error-test"})
    return json.loads(resp.read())["id"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def authed(inproc_server) -> AuthedClient:
    return AuthedClient(inproc_server.url)


# ---------------------------------------------------------------------------
# Path 1 — upstream open failure (already correct: emits event: error)
# ---------------------------------------------------------------------------

def test_upstream_5xx_emits_named_event_frame(mock_lmstudio, inproc_server, authed):
    """LM Studio returns 500 → client receives a parseable error frame.

    Covers server.py:1944.  This path is already correct today; the test
    locks in the contract so future refactors don't regress it.
    """
    mock_lmstudio.configure(status_code=500)
    chat_id = _new_chat_id(authed.user)

    raw = _post_chat_stream_raw(
        inproc_server.url,
        authed.user.cookie,
        {"input": "hello", "model": "test-model", "chat_id": chat_id, "stream": True},
    )

    delivered = parse_sse_like_client(raw)
    error_frames = [f for f in delivered if f.event == "error"]
    assert error_frames, (
        f"client received no event=error frame on upstream 500. "
        f"Raw frames: {all_raw_frames(raw)[:10]}"
    )
    err = error_frames[0].data
    assert err.get("type") == "error", f"unexpected payload: {err}"
    assert "message" in err.get("error", {}), f"no error.message: {err}"

    # The terminator must be present so the client unwinds its read loop.
    assert b"data: [DONE]" in raw, "missing [DONE] terminator after error"


def test_upstream_4xx_emits_named_event_frame(mock_lmstudio, inproc_server, authed):
    """Same as 5xx but for 4xx (e.g. model unloaded, auth issue)."""
    mock_lmstudio.configure(status_code=404)
    chat_id = _new_chat_id(authed.user)

    raw = _post_chat_stream_raw(
        inproc_server.url,
        authed.user.cookie,
        {"input": "hi", "model": "test-model", "chat_id": chat_id, "stream": True},
    )

    error_frames = [f for f in parse_sse_like_client(raw) if f.event == "error"]
    assert error_frames, f"no event=error frame; raw: {all_raw_frames(raw)[:10]}"


# ---------------------------------------------------------------------------
# Path 3 — empty stream (the silent-dropper)
#
# We get here by configuring mock_lmstudio to return a 200 with zero content
# chunks AND no chat.end event.  The server's _collect_stream returns
# stream_complete=False and content_parts=[].  Today the handler emits
# data: ... without event: ... — silently dropped by the client.
# ---------------------------------------------------------------------------

def test_empty_stream_emits_named_event_frame(mock_lmstudio, inproc_server, authed):
    """LM Studio returns 200 but disconnects before sending any payload.

    The server reaches the "no response from model" branch (server.py around
    line 1967).  Before the fix this branch wrote a data-only frame that
    ``app.js:processSSEBlock`` silently dropped — the user saw a dead
    spinner instead of the error message.  After the fix it emits
    ``event: error`` so the client's parser actually delivers it.
    """
    # disconnect_after=0 → mock skips the chunk loop entirely.
    # skip_chat_end=True → mock closes the connection without chat.end.
    # Together these reproduce a crashed-upstream / no-response stream.
    mock_lmstudio.configure(
        chunks=[], disconnect_after=0,
        reasoning_chunks=[], tool_calls=[],
        skip_chat_end=True,
    )
    chat_id = _new_chat_id(authed.user)
    raw = _post_chat_stream_raw(
        inproc_server.url,
        authed.user.cookie,
        {"input": "trigger empty", "model": "test-model", "chat_id": chat_id, "stream": True},
    )
    delivered = parse_sse_like_client(raw)
    error_frames = [f for f in delivered if f.event == "error"]
    assert error_frames, (
        f"server emitted error frame the client can't parse. "
        f"All raw frames (including event-less, which client drops): "
        f"{all_raw_frames(raw)}"
    )
    err = error_frames[0].data
    assert "error" in err
    msg = err.get("error", {}).get("message", "")
    assert "no response" in msg.lower(), f"unexpected error msg: {msg!r}"
    assert b"data: [DONE]" in raw, "missing [DONE] terminator after no-response error"


def test_collect_exception_emits_named_event_frame(mock_lmstudio, inproc_server, authed):
    """_collect_stream raises → server.py:1957 must use event: error.

    _collect_stream catches most exceptions internally so the surrounding
    try block at 1957 is only reachable when the tuple unpacking itself
    fails (e.g. _collect_stream returns the wrong shape).  We can't easily
    inject that from a black-box test, so this case shares the unit-level
    contract guard at ``test_parser_drops_event_less_data_frames`` and
    the source-level inspection below.
    """
    # Source-level invariant: there is no bare ``data:`` SSE error frame
    # written from _handle_chat_stream.  Every error path emits ``event:``.
    src_path = inproc_server.module.__file__
    with open(src_path) as f:
        src = f.read()
    # Locate the _handle_chat_stream body
    start = src.index("def _handle_chat_stream(")
    end = src.index("\n    def ", start + 1)
    body = src[start:end]
    # Walk every wfile.write in the handler body — any frame written as
    # bare ``data:`` (without an accompanying ``event:`` line in the same
    # write) is a regression.  ``data: [DONE]`` is the standard SSE
    # terminator and is intentionally event-less.
    offending: list[str] = []
    for line in body.split("\n"):
        if "wfile.write" not in line:
            continue
        if 'b"data: [DONE]' in line or "b'data: [DONE]" in line:
            continue
        # f-string writes embed both lines in a single literal.
        if re.search(r'wfile\.write\(\s*[bf]?"\s*data:', line):
            offending.append(line.strip())
    assert not offending, (
        "server.py _handle_chat_stream writes data-only SSE frames that the "
        "client will drop: " + repr(offending)
    )


# ---------------------------------------------------------------------------
# Direct unit test: write the broken frame directly from a helper.
#
# The integration tests above are limited by what the mock can express.  This
# one drives just the parser to lock in the contract: any frame the server
# emits must survive parse_sse_like_client.  It documents the rule a reviewer
# can apply by eye when reading new SSE-emitting code in server.py.
# ---------------------------------------------------------------------------

def test_parser_drops_event_less_data_frames():
    """The contract: ``data:`` without ``event:`` is invisible to the browser.

    This is the rule the broken code at server.py:1957/1967 violates.
    """
    # What server.py currently writes for the two broken paths:
    broken = b'data: {"type":"error","error":{"message":"x"}}\n\ndata: [DONE]\n\n'
    frames = parse_sse_like_client(broken)
    assert frames == [], (
        "parser delivered frames it shouldn't have — the JS client drops "
        "event-less data blocks, and tests must match that behaviour."
    )


def test_parser_accepts_named_event_error_frame():
    """Locks in the format Phase 1 will use to fix server.py:1957/1967."""
    good = b'event: error\ndata: {"type":"error","error":{"message":"x"}}\n\ndata: [DONE]\n\n'
    frames = parse_sse_like_client(good)
    assert len(frames) == 1
    assert frames[0].event == "error"
    assert frames[0].data["error"]["message"] == "x"


def test_parser_handles_crlf_line_endings():
    """SSE permits CRLF as well as LF — the parser must accept both."""
    crlf = b"event: ping\r\ndata: {}\r\n\r\nevent: ping\ndata: {}\n\n"
    frames = parse_sse_like_client(crlf)
    assert [f.event for f in frames] == ["ping", "ping"]


def test_parser_concatenates_multiple_data_lines():
    """SSE allows multi-line data; the JS client joins them with newlines."""
    multi = b"event: msg\ndata: line1\ndata: line2\n\n"
    frames = parse_sse_like_client(multi)
    assert len(frames) == 1
    # JS does `data += (data ? "\n" : "") + line.slice(5).trim()`
    assert frames[0].data_raw == "line1\nline2"


# ---------------------------------------------------------------------------
# Happy path roundtrip — locks in that normal streams parse cleanly.
# ---------------------------------------------------------------------------

def test_happy_path_stream_parses_into_expected_events(mock_lmstudio, inproc_server, authed):
    """Sanity: a successful stream contains the events app.js expects."""
    mock_lmstudio.configure(chunks=["Hello", " ", "world"])
    chat_id = _new_chat_id(authed.user)

    raw = _post_chat_stream_raw(
        inproc_server.url,
        authed.user.cookie,
        {"input": "hi", "model": "test-model", "chat_id": chat_id, "stream": True},
    )

    frames = parse_sse_like_client(raw)
    event_names = [f.event for f in frames]
    assert "message.delta" in event_names, event_names
    assert "chat.end" in event_names, event_names

    # message.delta payloads concatenate to the original chunks.
    chunks = [f.data.get("content", "") for f in frames if f.event == "message.delta"]
    assert "".join(chunks) == "Hello world"

    # chat.end always wraps the stream.
    assert event_names[-1] == "chat.end" or "[DONE]" in raw.decode("utf-8", errors="replace")
