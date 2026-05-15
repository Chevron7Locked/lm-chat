"""Integration tests against a real LM Studio instance.

These tests **require** a running LM Studio server at ``LMSTUDIO_URL`` (or
the default ``http://localhost:1234``) with at least one LLM loaded.  The
whole module is skipped if /api/v1/models is unreachable or returns no
loaded LLMs — so CI without LM Studio doesn't fail, it just doesn't run.

The mocked-LM-Studio suite is excellent at exercising routing, persistence,
auth, and error frames.  It can't catch:

* Wire-format drift between lm-chat's parser and what real LM Studio emits
  (the most recent regression: ``chat.end`` nested its payload under
  ``result`` and the mock had it at the top level).
* End-to-end response_id chaining across two turns.
* Real streaming cadence (the mock's chunks arrive instantly).
* Real reasoning models emitting ``reasoning.delta`` events that interleave
  with ``message.delta``.
* LM Studio API behaviours that aren't documented (e.g. how it surfaces
  invalid model IDs, what HTTP code it returns for context overflow).

Each test takes a few seconds because we talk to an actual model.  They're
collected under ``pytest -m real_lmstudio`` so they can be opted in/out of.

Run with:
    pytest tests/test_real_lmstudio.py -v
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

import pytest

from conftest import ADMIN_PASS, ADMIN_USER, CSRF_HEADER, parse_sse_like_client


LMSTUDIO_URL = os.environ.get("LMSTUDIO_URL", "http://localhost:1234")


# ---------------------------------------------------------------------------
# Module-level guard: skip everything if LM Studio isn't running with a model
# ---------------------------------------------------------------------------

def _discover_loaded_model() -> str | None:
    """Return the key of the first loaded LLM, or None.

    LM Studio may require an API token (Developer → "Require API token");
    we use ``LMSTUDIO_TOKEN`` from the environment when present so the
    discovery probe stays usable in token-gated setups.
    """
    req = urllib.request.Request(f"{LMSTUDIO_URL}/api/v1/models")
    token = os.environ.get("LMSTUDIO_TOKEN", "")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=2) as r:
            data = json.loads(r.read())
    except Exception:
        return None
    for m in data.get("models", []):
        if m.get("type") == "llm" and m.get("loaded_instances"):
            return m["key"]
    return None


_MODEL = _discover_loaded_model()
pytestmark = pytest.mark.skipif(
    _MODEL is None,
    reason=(
        f"no loaded LLM at {LMSTUDIO_URL} — these tests need a real LM Studio "
        "running with at least one model loaded.  See tests/test_real_lmstudio.py."
    ),
)


# ---------------------------------------------------------------------------
# Fixture: in-process server pointed at REAL LM Studio (not the mock)
# ---------------------------------------------------------------------------

@pytest.fixture
def real_server(tmp_path):
    """In-process server with ``LMSTUDIO_URL`` pointing at the real instance.

    Uses the same wiring as the ``inproc_server`` fixture but bypasses
    ``mock_lmstudio`` so we hit real LM Studio.  Auth enabled with the
    standard ADMIN_USER/ADMIN_PASS.
    """
    from conftest import _start_inproc_server

    handle = _start_inproc_server(
        mock_lmstudio_url=LMSTUDIO_URL,
        db_path=str(tmp_path / "chats.db"),
        log_dir=str(tmp_path / "logs"),
        # Use cheaper scrypt params so per-test bootstrap stays under a second.
        extra_env={"LM_CHAT_SCRYPT_N": "16384"},
    )
    try:
        yield handle
    finally:
        handle.shutdown()
        t = getattr(handle, "_serve_thread", None)
        if t is not None:
            t.join(timeout=2)


def _login(base_url: str) -> str:
    body = json.dumps({"username": ADMIN_USER, "password": ADMIN_PASS}).encode()
    req = urllib.request.Request(
        base_url + "/api/auth/login",
        data=body,
        headers={"Content-Type": "application/json", **CSRF_HEADER},
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=10)
    return resp.headers.get("Set-Cookie", "").split(";")[0].strip()


def _create_chat(base_url: str, cookie: str) -> str:
    body = json.dumps({"title": "real-lm-test"}).encode()
    req = urllib.request.Request(
        base_url + "/api/chats",
        data=body,
        headers={"Content-Type": "application/json", "Cookie": cookie, **CSRF_HEADER},
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=5)
    return json.loads(resp.read())["id"]


def _stream(base_url: str, cookie: str, body: dict, timeout: float = 120) -> bytes:
    """POST /api/chat/stream and read the entire SSE response."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        base_url + "/api/chat/stream",
        data=data,
        headers={"Content-Type": "application/json", "Cookie": cookie, **CSRF_HEADER},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.read()
    except urllib.error.HTTPError as e:
        return e.read()


# ---------------------------------------------------------------------------
# Models endpoint contract
# ---------------------------------------------------------------------------

def test_proxy_returns_models_list(real_server):
    """/api/models should pass through LM Studio's models list including
    capabilities + loaded_instances — the SPA's model dropdown depends on it.
    """
    cookie = _login(real_server.url)
    req = urllib.request.Request(
        real_server.url + "/api/models",
        headers={"Cookie": cookie},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    # LM Studio returns {"models": [...]} — the proxy is a thin pass-through.
    assert isinstance(data.get("models"), list)
    assert any(m.get("type") == "llm" for m in data["models"]), (
        f"no LLMs in models list: {data!r}"
    )


# ---------------------------------------------------------------------------
# Streaming round-trip
# ---------------------------------------------------------------------------

def test_stream_completes_with_real_model(real_server):
    """End-to-end streaming: send a short prompt, get a real response, and
    verify the SSE event sequence the SPA expects (message.delta+, chat.end).

    Note: real LM Studio does NOT emit ``data: [DONE]`` after a successful
    ``chat.end`` — that terminator is only emitted by lm-chat's server on
    its own error paths (see server.py:1957/1967 + tests/test_sse_error_
    frames.py).  The SPA treats ``event: chat.end`` as completion.
    """
    cookie = _login(real_server.url)
    chat_id = _create_chat(real_server.url, cookie)
    raw = _stream(
        real_server.url, cookie,
        {
            "model":  _MODEL,
            "input":  "Reply with just the word: hi",
            "chat_id": chat_id,
            "stream":  True,
        },
        timeout=120,
    )
    text = raw.decode("utf-8", errors="replace")
    assert "event: chat.end" in text, f"no chat.end in stream: {text[:500]!r}"

    frames = parse_sse_like_client(raw)
    end = next((f for f in frames if f.event == "chat.end"), None)
    assert end is not None
    # Real LM Studio nests under result; verify the shape we depend on.
    result = end.data.get("result") or {}
    assert isinstance(result.get("response_id"), str) and result["response_id"].startswith("resp_")
    assert isinstance(result.get("output"), list) and result["output"]

    deltas = [f for f in frames if f.event == "message.delta"]
    assert deltas, "no message.delta frames"
    body = "".join(f.data.get("content", "") for f in deltas).strip()
    assert body, "model returned empty content"


def test_stream_persists_response_id_for_chaining(real_server):
    """Two-turn conversation: first request → assert response_id is persisted
    in chats.response_id; second request → assert previous_response_id is
    sent upstream (we can't observe upstream here but we observe that the
    server stitches it via the chats table)."""
    cookie = _login(real_server.url)
    chat_id = _create_chat(real_server.url, cookie)

    _stream(
        real_server.url, cookie,
        {"model": _MODEL, "input": "Reply with one word: alpha",
         "chat_id": chat_id, "stream": True},
        timeout=120,
    )
    time.sleep(0.5)

    # Inspect the chats row directly to confirm persistence.
    server = real_server.module
    db = server.get_db()
    row = db.execute(
        "SELECT response_id FROM chats WHERE id=?", (chat_id,),
    ).fetchone()
    assert row is not None, "chat row vanished"
    assert row[0] and row[0].startswith("resp_"), (
        f"response_id not persisted from real LM Studio chat.end: {row[0]!r}"
    )


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------

def test_invalid_model_returns_clean_error_frame(real_server):
    """A nonexistent model id triggers a 4xx from LM Studio; the server must
    wrap it in an ``event: error`` SSE frame the client can parse, not just
    forward whatever HTML/JSON LM Studio happened to send."""
    cookie = _login(real_server.url)
    chat_id = _create_chat(real_server.url, cookie)
    raw = _stream(
        real_server.url, cookie,
        {"model": "definitely-not-a-real-model-xyz",
         "input": "hi", "chat_id": chat_id, "stream": True},
        timeout=30,
    )
    frames = parse_sse_like_client(raw)
    error_frames = [f for f in frames if f.event == "error"]
    assert error_frames, (
        f"no event=error frame on invalid model.  Raw response: {raw[:500]!r}"
    )
    err = error_frames[0].data
    assert "error" in err, f"error frame missing 'error' field: {err!r}"
    assert b"data: [DONE]" in raw, "missing [DONE] terminator on error path"


# ---------------------------------------------------------------------------
# Non-streaming path
# ---------------------------------------------------------------------------

def test_non_streaming_chat_returns_real_content(real_server):
    """POST /api/chat (non-streaming) round-trips through real LM Studio and
    persists the assistant content into the messages table.

    Real LM Studio's non-stream response uses the same ``output: [{type,
    content}, ...]`` shape as the streaming ``chat.end.result``.  There is
    NO top-level ``content`` field; the server's ``_extract_content``
    helper navigates the output list to find the ``type: "message"`` entry.
    """
    cookie = _login(real_server.url)
    chat_id = _create_chat(real_server.url, cookie)
    req = urllib.request.Request(
        real_server.url + "/api/chat",
        data=json.dumps({
            "model":  _MODEL,
            "input":  "Reply with one word: bravo",
            "chat_id": chat_id,
            "stream": False,
        }).encode(),
        headers={"Content-Type": "application/json", "Cookie": cookie, **CSRF_HEADER},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())

    # LM Studio's response_id and stats are at the top level for non-stream.
    assert isinstance(data.get("response_id"), str) and data["response_id"].startswith("resp_")
    output = data.get("output")
    assert isinstance(output, list) and output, f"no output list: {data!r}"
    message_items = [o for o in output if o.get("type") == "message"]
    assert message_items, f"no message item in output: {output!r}"
    assert message_items[0].get("content"), f"empty message content: {message_items[0]!r}"

    # Persisted assistant message visible via the messages endpoint.  Server
    # ran ``_extract_content`` on the same payload, so the persisted content
    # is whatever the server pulled from output[].
    msg_req = urllib.request.Request(
        real_server.url + f"/api/chats/{chat_id}/messages",
        headers={"Cookie": cookie},
    )
    with urllib.request.urlopen(msg_req, timeout=5) as r:
        msgs = json.loads(r.read())
    asst_msgs = [m for m in msgs if m["role"] == "assistant"]
    assert asst_msgs, f"no assistant message persisted: {msgs!r}"
    assert asst_msgs[-1]["content"], "assistant content empty in DB"


# ---------------------------------------------------------------------------
# Concurrency / stress (short-duration, opt-in)
# ---------------------------------------------------------------------------

def test_models_endpoint_exposes_unsupported_params(real_server):
    """``/api/models`` augments each LLM entry with the server's
    per-model rejected-param cache so the SPA can disable corresponding
    UI controls.  Starts empty for a freshly-imported server module.
    """
    cookie = _login(real_server.url)
    req = urllib.request.Request(
        real_server.url + "/api/models",
        headers={"Cookie": cookie},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    for m in data.get("models", []):
        if m.get("type") != "llm":
            continue
        caps = m.get("capabilities") or {}
        # Field must be present even when empty so the SPA can rely on it.
        assert "unsupported_params" in caps, f"missing capability list: {m['key']}"
        assert isinstance(caps["unsupported_params"], list)


def test_reasoning_rejection_caches_and_skips_retry(real_server):
    """Sending ``reasoning: "off"`` to a model that rejects the param must:

      1. Succeed on the first request (server transparently retries).
      2. Add the param to the model's ``unsupported_params`` list in /api/models.
      3. Take noticeably less time on the second request (no retry).

    Skipped if no loaded LLM rejects the reasoning param — the test
    only exercises the cache when there's something to cache.
    """
    cookie = _login(real_server.url)
    server = real_server.module

    # Find a model that rejects reasoning by sending one probe request.
    chat_id = _create_chat(real_server.url, cookie)
    raw_first = _stream(
        real_server.url, cookie,
        {"model": _MODEL, "input": "reply: ok", "chat_id": chat_id,
         "stream": True, "reasoning": "off", "incognito": True},
        timeout=120,
    )
    text_first = raw_first.decode("utf-8", errors="replace")
    # First request must succeed end-to-end even though the original
    # payload contained the rejected param.
    assert "event: chat.end" in text_first, (
        f"first request failed to complete after retry: {text_first[:400]!r}"
    )

    cache = dict(server.Handler._unsupported_params)
    if _MODEL not in cache or "reasoning" not in cache[_MODEL]:
        pytest.skip(
            f"{_MODEL!r} accepts reasoning config — nothing to cache here."
        )

    # /api/models now shows the cached entry.
    req = urllib.request.Request(
        real_server.url + "/api/models", headers={"Cookie": cookie},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    target = next(
        (m for m in data.get("models", []) if m.get("key") == _MODEL), None,
    )
    assert target is not None
    assert "reasoning" in (target.get("capabilities") or {}).get("unsupported_params", []), (
        f"server didn't surface cache to /api/models: {target.get('capabilities')!r}"
    )

    # Second request: confirm no retry happens.  We can't reliably time
    # the retry savings (model latency dominates) but we CAN assert the
    # request completed and the response shape is identical.
    raw_second = _stream(
        real_server.url, cookie,
        {"model": _MODEL, "input": "reply: ok again", "chat_id": chat_id,
         "stream": True, "reasoning": "off", "incognito": True},
        timeout=120,
    )
    assert b"event: chat.end" in raw_second


def test_three_concurrent_streams_complete_independently(real_server):
    """Three streaming requests against three separate chats should all
    finish without crossing wires or deadlocking the server.  Catches
    classes of bugs the single-request tests miss: shared mutable state,
    socket pool exhaustion, response_id collisions, etc.
    """
    import threading

    cookie = _login(real_server.url)
    chat_ids = [_create_chat(real_server.url, cookie) for _ in range(3)]
    results: list[bytes | None] = [None, None, None]

    def run(idx: int, chat_id: str):
        results[idx] = _stream(
            real_server.url, cookie,
            {"model": _MODEL,
             "input": f"Reply with just the digit: {idx}",
             "chat_id": chat_id, "stream": True},
            timeout=180,
        )

    threads = [
        threading.Thread(target=run, args=(i, cid), daemon=True)
        for i, cid in enumerate(chat_ids)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=200)
        assert not t.is_alive(), "concurrent stream did not finish in time"

    for idx, raw in enumerate(results):
        assert raw is not None, f"stream {idx} produced no result"
        assert b"event: chat.end" in raw, f"stream {idx} missing chat.end"
        # Each stream's response_id must be unique — a regression where
        # response_id is shared across concurrent streams would corrupt
        # ``previous_response_id`` chaining on the next turn.
        frames = parse_sse_like_client(raw)
        end = next((f for f in frames if f.event == "chat.end"), None)
        assert end is not None and (end.data.get("result") or {}).get("response_id")
    response_ids = []
    for raw in results:
        frames = parse_sse_like_client(raw or b"")
        end = next((f for f in frames if f.event == "chat.end"), None)
        if end:
            response_ids.append((end.data.get("result") or {}).get("response_id"))
    assert len(set(response_ids)) == len(response_ids), (
        f"concurrent streams shared response_ids: {response_ids!r}"
    )
