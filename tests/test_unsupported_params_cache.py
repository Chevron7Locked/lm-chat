"""Universal per-model rejected-parameter cache.

LM Studio rejects certain chat parameters on a per-model basis with a
structured 400 response.  Real example from a freshly-loaded Qwen3.6 model
(May 2026):

    {"error": {
        "message": "Model '...' does not expose reasoning configuration.",
        "type": "invalid_request",
        "param": "reasoning",
        "code": "invalid_value"
    }}

Without intervention, every chat turn against an affected model burned an
extra round-trip retrying without the offending param.  The cache in
``Handler._unsupported_params`` records ``{model_id: set[param]}`` from
the first such response per (model, param) pair, then
``_build_lmstudio_payload`` strips matching params upfront on every
subsequent request.  The same record is surfaced via ``/api/models`` so
the SPA can disable the corresponding UI controls.

These tests assert the full loop end-to-end against the mock:

  1. First request: param sent, mock returns 400, server retries without,
     stream completes, cache populated.
  2. /api/models reflects the cache.
  3. Second request: server strips upfront, mock never sees the param,
     stream completes in one round-trip.
  4. Universal — same machinery handles ANY param name, not just reasoning.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from conftest import CSRF_HEADER, parse_sse_like_client


@pytest.fixture
def srv(inproc_server, inproc_client):
    """Convenience tuple: (server module, authed client, base url)."""
    return inproc_server, inproc_client.admin


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stream(base_url: str, cookie: str, body: dict) -> bytes:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        base_url + "/api/chat/stream",
        data=data,
        headers={"Content-Type": "application/json", "Cookie": cookie, **CSRF_HEADER},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        return resp.read()
    except urllib.error.HTTPError as e:
        return e.read()


def _models(base_url: str, cookie: str) -> list:
    req = urllib.request.Request(base_url + "/api/models", headers={"Cookie": cookie})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read()).get("models", []) or []


def _new_chat_id(client) -> str:
    resp = client.post("/api/chats", {"title": "cache-test"})
    return json.loads(resp.read())["id"]


# Structured 400 in LM Studio's actual wire format.  Verified against a
# live LM Studio v0.4+ server on 2026-05-15 (Qwen3.6-35B model).
LM_STUDIO_REASONING_REJECTION = {
    "error": {
        "message": "Model 'test-model' does not expose reasoning configuration.",
        "type":    "invalid_request",
        "param":   "reasoning",
        "code":    "invalid_value",
    },
}


# ---------------------------------------------------------------------------
# Initial state — cache empty, capability list present-but-empty
# ---------------------------------------------------------------------------

def test_models_endpoint_includes_empty_unsupported_params(srv):
    server, admin = srv
    models = _models(server.url, admin.cookie)
    for m in models:
        if m.get("type") != "llm":
            continue
        caps = m.get("capabilities") or {}
        assert caps.get("unsupported_params") == [], (
            f"expected empty cache for fresh server, got {caps!r}"
        )


# ---------------------------------------------------------------------------
# First-request flow: 400 → cache → retry → success
# ---------------------------------------------------------------------------

def test_first_rejection_populates_cache_and_retries(srv, mock_lmstudio):
    server, admin = srv

    # Tell the mock to reject the next /api/v1/chat with the structured 400.
    mock_lmstudio.configure(
        chat_error_body=LM_STUDIO_REASONING_REJECTION,
        chat_error_for_model="test-model",
        chunks=["after-retry"],
    )

    chat_id = _new_chat_id(admin)
    raw = _stream(server.url, admin.cookie, {
        "model": "test-model",
        "input": "trigger reject",
        "chat_id": chat_id,
        "stream": True,
        "reasoning": "off",
    })

    # Stream must complete despite the upstream 400 — the retry runs
    # transparently before the response headers reach the client.
    frames = parse_sse_like_client(raw)
    end = next((f for f in frames if f.event == "chat.end"), None)
    assert end is not None, (
        f"server didn't retry+complete after upstream 400.  Raw: {raw[:400]!r}"
    )

    # Cache must now have the (model, "reasoning") pair.
    assert "reasoning" in server.module.Handler._unsupported_params.get("test-model", set()), (
        f"cache missing the rejected param: {dict(server.module.Handler._unsupported_params)!r}"
    )


# ---------------------------------------------------------------------------
# /api/models reflects the cache
# ---------------------------------------------------------------------------

def test_models_endpoint_surfaces_cache(srv):
    server, admin = srv

    # Populate the cache directly — no need to round-trip through chat for
    # this particular assertion.
    with server.module.Handler._unsupported_params_lock:
        server.module.Handler._unsupported_params["test-model"] = {"reasoning"}

    found = None
    for m in _models(server.url, admin.cookie):
        if m.get("key") == "test-model" or m.get("id") == "test-model":
            found = m
            break
    assert found is not None, "test-model missing from /api/models"
    caps = found.get("capabilities") or {}
    assert caps.get("unsupported_params") == ["reasoning"]


# ---------------------------------------------------------------------------
# Second-request flow: server strips upfront, no retry
# ---------------------------------------------------------------------------

def test_cached_request_strips_param_upfront(srv, mock_lmstudio):
    server, admin = srv

    # Pre-populate the cache directly.
    with server.module.Handler._unsupported_params_lock:
        server.module.Handler._unsupported_params["test-model"] = {"reasoning"}

    mock_lmstudio.configure(chunks=["ok"])
    chat_id = _new_chat_id(admin)

    # Send the same payload the first request would have used.
    _stream(server.url, admin.cookie, {
        "model": "test-model",
        "input": "second-request",
        "chat_id": chat_id,
        "stream": True,
        "reasoning": "off",
    })

    # The mock recorded the last request — it must not have ``reasoning``.
    assert mock_lmstudio.last_request is not None, "mock never received a request"
    assert "reasoning" not in mock_lmstudio.last_request, (
        f"server didn't strip cached param.  Sent: {mock_lmstudio.last_request!r}"
    )
    # Single call — no retry.
    assert mock_lmstudio.call_count == 1, (
        f"expected one upstream call (no retry); got {mock_lmstudio.call_count}"
    )


# ---------------------------------------------------------------------------
# Cache is per-model — different models track independently
# ---------------------------------------------------------------------------

def test_cache_is_per_model(srv):
    server, _admin = srv
    with server.module.Handler._unsupported_params_lock:
        server.module.Handler._unsupported_params["model-a"] = {"reasoning"}
    assert "reasoning" in server.module.Handler._unsupported_params.get("model-a", set())
    assert "model-b" not in server.module.Handler._unsupported_params


# ---------------------------------------------------------------------------
# Universal — works for any param name, not just "reasoning"
# ---------------------------------------------------------------------------

def test_universal_for_arbitrary_param_name(srv, mock_lmstudio):
    """The cache key is the param name from ``error.param``, not a hard-coded
    "reasoning" constant.  This test simulates LM Studio rejecting a
    different but still real chat param (``temperature``) and confirms the
    same machinery catches it without code changes here.

    Using ``temperature`` rather than a synthetic name because the server's
    ``_build_lmstudio_payload`` only forwards whitelisted params to LM
    Studio — for the retry path to fire, the rejected param must actually
    be in the outgoing payload.  Any whitelisted param works; temperature
    is the easiest to reason about.
    """
    server, admin = srv

    mock_lmstudio.configure(
        chat_error_body={
            "error": {
                "message": "Model 'test-model' does not support temperature configuration.",
                "type":    "invalid_request",
                "param":   "temperature",
                "code":    "invalid_value",
            }
        },
        chat_error_for_model="test-model",
        chunks=["after-retry"],
    )

    chat_id = _new_chat_id(admin)
    raw = _stream(server.url, admin.cookie, {
        "model":  "test-model",
        "input":  "trigger",
        "chat_id": chat_id,
        "stream": True,
        "temperature": 0.7,
        # ``reasoning`` is also present so we verify ONLY the rejected
        # param gets cached, not every param in the request.
        "reasoning": "off",
    })

    frames = parse_sse_like_client(raw)
    assert any(f.event == "chat.end" for f in frames), (
        f"retry didn't complete the stream: {raw[:400]!r}"
    )

    cache = server.module.Handler._unsupported_params.get("test-model", set())
    assert "temperature" in cache, (
        f"temperature wasn't cached as unsupported: {cache!r}"
    )
    assert "reasoning" not in cache, (
        f"unrelated param leaked into cache: {cache!r}"
    )
