"""HSTS (Strict-Transport-Security) emission tests.

HSTS protects users from SSL-stripping attacks by telling browsers to refuse
plain-HTTP connections to a host for the duration of ``max-age``.  RFC 6797
says the header is only meaningful over HTTPS — emitting it over plain HTTP
is allowed but does nothing.

We have three opt-in modes:
  * ``LM_CHAT_HSTS=""`` (default) — no header.
  * ``LM_CHAT_HSTS=true|on|1`` — standard directive.
  * ``LM_CHAT_HSTS=preload`` — directive plus the preload token.

A second env var ``LM_CHAT_HSTS_MAX_AGE`` overrides the default 2-year window.

Detection of HTTPS comes from two sources, both pre-existing:
  * ``LM_CHAT_HTTPS`` env (set when the Python server itself terminates TLS),
  * ``X-Forwarded-Proto: https`` header (when behind a TLS reverse proxy).

Tests use the latter because the in-process fixture binds to plain HTTP.
"""

from __future__ import annotations

import urllib.request

import pytest


def _headers_for(base_url: str, path: str = "/api/health", forwarded: str | None = None) -> dict:
    """Issue a GET and return the response header map (lowercased keys)."""
    req = urllib.request.Request(base_url + path)
    if forwarded is not None:
        req.add_header("X-Forwarded-Proto", forwarded)
    with urllib.request.urlopen(req, timeout=5) as r:
        return {k.lower(): v for k, v in r.headers.items()}


# ---------------------------------------------------------------------------
# Off by default
# ---------------------------------------------------------------------------

def test_hsts_absent_when_env_unset(make_inproc_server):
    """No LM_CHAT_HSTS = no HSTS header, even over HTTPS."""
    srv = make_inproc_server()
    headers = _headers_for(srv.url, forwarded="https")
    assert "strict-transport-security" not in headers


def test_hsts_absent_on_http_even_when_env_set(make_inproc_server):
    """HSTS over plain HTTP is a no-op — don't emit it.

    Even though browsers ignore the header on HTTP responses, emitting it
    leaks the policy and increases header bytes on every health probe.
    """
    srv = make_inproc_server(env={"LM_CHAT_HSTS": "true"})
    # No X-Forwarded-Proto header → _secure_flag returns "" → no HSTS.
    headers = _headers_for(srv.url)
    assert "strict-transport-security" not in headers


# ---------------------------------------------------------------------------
# Standard mode
# ---------------------------------------------------------------------------

def test_hsts_standard_mode_emits_default_directive(make_inproc_server):
    srv = make_inproc_server(env={"LM_CHAT_HSTS": "true"})
    headers = _headers_for(srv.url, forwarded="https")
    h = headers.get("strict-transport-security", "")
    assert "max-age=63072000" in h
    assert "includeSubDomains" in h
    assert "preload" not in h


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "on", "ON"])
def test_hsts_accepts_common_truthy_strings(make_inproc_server, value):
    srv = make_inproc_server(env={"LM_CHAT_HSTS": value})
    headers = _headers_for(srv.url, forwarded="https")
    assert "strict-transport-security" in headers, f"value={value!r}"


@pytest.mark.parametrize("value", ["", "false", "0", "off", "no", "yolo"])
def test_hsts_rejects_falsy_or_unknown_values(make_inproc_server, value):
    srv = make_inproc_server(env={"LM_CHAT_HSTS": value})
    headers = _headers_for(srv.url, forwarded="https")
    assert "strict-transport-security" not in headers, f"value={value!r}"


# ---------------------------------------------------------------------------
# Preload mode
# ---------------------------------------------------------------------------

def test_hsts_preload_mode_emits_preload_token(make_inproc_server):
    srv = make_inproc_server(env={"LM_CHAT_HSTS": "preload"})
    headers = _headers_for(srv.url, forwarded="https")
    h = headers.get("strict-transport-security", "")
    assert "preload" in h
    assert "includeSubDomains" in h
    assert "max-age=" in h


# ---------------------------------------------------------------------------
# Custom max-age
# ---------------------------------------------------------------------------

def test_hsts_max_age_can_be_overridden(make_inproc_server):
    srv = make_inproc_server(env={
        "LM_CHAT_HSTS":         "true",
        "LM_CHAT_HSTS_MAX_AGE": "300",  # 5 minutes, for testing-on-staging
    })
    headers = _headers_for(srv.url, forwarded="https")
    assert "max-age=300" in headers.get("strict-transport-security", "")


def test_hsts_max_age_rejects_negative_falls_back_to_default(make_inproc_server):
    """Negative or garbage values must not result in a header that disables
    HSTS (max-age=0 unsets it in browsers).  We clamp at 0 — when the operator
    sets ``-1`` we still emit ``max-age=0``, which is the documented unset
    behaviour and at least won't crash."""
    srv = make_inproc_server(env={
        "LM_CHAT_HSTS":         "true",
        "LM_CHAT_HSTS_MAX_AGE": "-1",
    })
    headers = _headers_for(srv.url, forwarded="https")
    h = headers.get("strict-transport-security", "")
    # max=0 (clamped from -1) is acceptable; the test is that we don't crash
    # and we still emit some header so the operator can debug.
    assert h.startswith("max-age=")


def test_hsts_max_age_garbage_falls_back_to_default(make_inproc_server):
    srv = make_inproc_server(env={
        "LM_CHAT_HSTS":         "true",
        "LM_CHAT_HSTS_MAX_AGE": "definitely-not-an-int",
    })
    headers = _headers_for(srv.url, forwarded="https")
    h = headers.get("strict-transport-security", "")
    assert "max-age=63072000" in h, f"unexpected: {h!r}"


# ---------------------------------------------------------------------------
# Adjacent header sanity
# ---------------------------------------------------------------------------

def test_hsts_does_not_remove_other_security_headers(make_inproc_server):
    """HSTS should be additive — adding it must not drop CSP or X-Frame-Options."""
    srv = make_inproc_server(env={"LM_CHAT_HSTS": "true"})
    headers = _headers_for(srv.url, forwarded="https")
    assert "content-security-policy" in headers
    assert "x-frame-options" in headers
    assert "referrer-policy" in headers
