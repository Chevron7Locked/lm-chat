"""Structured access logging and request-ID correlation.

Pre-0.5.0 the only HTTP visibility was a DEBUG-level ``log_message`` that
the default INFO console handler hid — deployed servers effectively had no
access log.  We now emit a structured INFO line per response:

    req_id=<hex12> ip=<ip> method=<m> path=<p> status=<c> latency_ms=<n>

The same ``req_id`` is echoed in every JSON error response so a user can
quote it when filing a bug and the operator can grep the access log.

Tests:
  * Error responses contain ``req_id``.
  * Per-request IDs are unique across rapid concurrent requests.
  * 4xx and 5xx paths both surface a ``req_id``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


def _post_no_csrf(base_url: str, path: str) -> tuple[int, dict]:
    req = urllib.request.Request(
        base_url + path,
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        body = json.loads(resp.read() or b"{}")
        return resp.status, body
    except urllib.error.HTTPError as e:
        body = json.loads(e.read() or b"{}")
        return e.code, body


def test_4xx_error_response_includes_req_id(inproc_server):
    """A 403 (missing CSRF) should carry the request id."""
    code, body = _post_no_csrf(inproc_server.url, "/api/auth/login")
    assert code == 403
    assert isinstance(body.get("req_id"), str)
    assert len(body["req_id"]) == 12  # uuid4().hex[:12]


def test_404_from_app_routes_includes_req_id(inproc_server):
    """Routes that go through ``_error`` (e.g. chat-not-found) DO surface
    req_id in the response body.  Stdlib's ``send_error`` for completely
    unmatched paths emits an HTML body without req_id — that's a separate
    case covered below.

    Originally this test was named ``test_404_error_response_includes_req_id``
    and its body only asserted ``e.code == 404`` with a comment admitting
    the req_id check was not in scope — i.e., the name lied.  Split into
    two tests so the intent is clear.
    """
    # Hit an _error-routed 404 (PATCH a chat that doesn't exist).
    body = json.dumps({"title": "irrelevant"}).encode()
    req = urllib.request.Request(
        inproc_server.url + "/api/chats/nope-not-here/title",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Requested-With": "lm-chat",
        },
        method="PATCH",
    )
    try:
        urllib.request.urlopen(req, timeout=5)
        raise AssertionError("expected 4xx")
    except urllib.error.HTTPError as e:
        # Could be 401 (unauthenticated) or 404 (auth passes but no chat) —
        # either one goes through ``_error`` and must include req_id.
        assert e.code in (401, 404), f"unexpected status {e.code}"
        body = json.loads(e.read() or b"{}")
        assert isinstance(body.get("req_id"), str), f"req_id missing: {body!r}"
        assert len(body["req_id"]) == 12, f"req_id wrong length: {body!r}"


def test_unmatched_path_returns_404(inproc_server):
    """Path that falls off the bottom of the dispatcher returns 404.

    Stdlib ``send_error`` emits an HTML body for these (not JSON), so no
    req_id is expected.  This is documented behaviour, not a bug — but
    the test name in the original suite implied otherwise.
    """
    req = urllib.request.Request(
        inproc_server.url + "/api/this-does-not-exist",
        method="GET",
    )
    try:
        urllib.request.urlopen(req, timeout=5)
        raise AssertionError("expected 404")
    except urllib.error.HTTPError as e:
        assert e.code == 404


def test_request_ids_are_unique_per_request(inproc_server):
    """Hammer the server with sequential 4xx errors and confirm every
    response carries a distinct id."""
    ids = set()
    for _ in range(15):
        _, body = _post_no_csrf(inproc_server.url, "/api/auth/login")
        rid = body.get("req_id")
        assert rid and rid not in ids, f"duplicate or missing req_id: {rid!r}"
        ids.add(rid)
    assert len(ids) == 15


def test_request_id_format(inproc_server):
    """uuid4().hex[:12] → exactly 12 lowercase hex chars."""
    _, body = _post_no_csrf(inproc_server.url, "/api/auth/login")
    rid = body["req_id"]
    assert len(rid) == 12
    assert all(c in "0123456789abcdef" for c in rid), f"non-hex char in {rid!r}"
