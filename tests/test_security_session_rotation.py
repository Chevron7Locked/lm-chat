"""Session rotation policy tests.

A stolen session cookie persists for ``SESSION_EXPIRY`` (30 days) regardless
of subsequent logins by the legitimate user.  The ``LM_CHAT_SINGLE_SESSION``
env flag enforces that every fresh login invalidates the user's other
sessions atomically, so the legitimate user re-logging in kicks any
attacker-held cookie off the system.

Default is multi-device (the 0.5.7 default-ON flip was reverted in 0.5.9
after it surfaced as the dominant cause of "I had to re-login again"
complaints on personal-use deployments).  Operators with a stricter
threat model opt in with ``LM_CHAT_SINGLE_SESSION=true``.

These tests assert both modes from end to end.
"""

from __future__ import annotations

import json
import urllib.request

import pytest

from conftest import ADMIN_PASS, ADMIN_USER, CSRF_HEADER


def _login(base_url: str, username: str, password: str) -> str:
    """POST /api/auth/login and return the Set-Cookie cookie header value.

    Strips down to ``name=value`` so it can be sent back as a Cookie: header.
    """
    data = json.dumps({"username": username, "password": password}).encode()
    req = urllib.request.Request(
        base_url + "/api/auth/login",
        data=data,
        headers={"Content-Type": "application/json", **CSRF_HEADER},
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=5)
    raw_cookie = resp.headers.get("Set-Cookie", "")
    return raw_cookie.split(";")[0].strip()


import urllib.error  # noqa: E402


def _whoami(base_url: str, cookie: str) -> int:
    """Probe whether ``cookie`` authenticates against the server.

    /api/auth/me intentionally returns 200 with ``user: null`` when no
    session is present (it's the "who am I?" endpoint the SPA polls before
    rendering its login screen), so we can't use its status code alone.
    Hit a real protected endpoint instead — anything wrapped in
    ``_require_auth`` returns 401 on a missing or invalidated session.

    Returns:
        200 if the cookie still authorises, 401 if it no longer does.
    """
    req = urllib.request.Request(
        base_url + "/api/chats",
        headers={"Cookie": cookie},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        return resp.status
    except urllib.error.HTTPError as e:
        return e.code


# ---------------------------------------------------------------------------
# Default (0.5.9+): multi-device — concurrent sessions remain valid
# ---------------------------------------------------------------------------

def test_default_policy_allows_concurrent_sessions(make_inproc_server):
    """Without LM_CHAT_SINGLE_SESSION set, two logins both stay valid."""
    srv = make_inproc_server()  # LM_CHAT_SINGLE_SESSION unset → default OFF
    cookie_a = _login(srv.url, ADMIN_USER, ADMIN_PASS)
    cookie_b = _login(srv.url, ADMIN_USER, ADMIN_PASS)
    assert cookie_a != cookie_b, "second login must produce a new token"
    assert _whoami(srv.url, cookie_a) == 200
    assert _whoami(srv.url, cookie_b) == 200


def test_explicit_false_keeps_concurrent_sessions(make_inproc_server):
    """LM_CHAT_SINGLE_SESSION=false: two logins both remain valid."""
    srv = make_inproc_server(env={"LM_CHAT_SINGLE_SESSION": "false"})
    cookie_a = _login(srv.url, ADMIN_USER, ADMIN_PASS)
    cookie_b = _login(srv.url, ADMIN_USER, ADMIN_PASS)
    assert cookie_a != cookie_b, "second login must produce a new token"
    assert _whoami(srv.url, cookie_a) == 200
    assert _whoami(srv.url, cookie_b) == 200


# ---------------------------------------------------------------------------
# Explicit single-session: second login kills the first
# ---------------------------------------------------------------------------

def test_single_session_mode_invalidates_prior_cookie(make_inproc_server):
    """LM_CHAT_SINGLE_SESSION=true: the second login revokes the first."""
    srv = make_inproc_server(env={"LM_CHAT_SINGLE_SESSION": "true"})
    cookie_a = _login(srv.url, ADMIN_USER, ADMIN_PASS)
    assert _whoami(srv.url, cookie_a) == 200, "first cookie should authorise"

    cookie_b = _login(srv.url, ADMIN_USER, ADMIN_PASS)
    assert cookie_a != cookie_b

    # The old cookie must now be revoked.
    assert _whoami(srv.url, cookie_a) == 401, (
        "first cookie should be invalidated under single-session policy"
    )
    # The new cookie must still authorise.
    assert _whoami(srv.url, cookie_b) == 200


@pytest.mark.parametrize("value", ["true", "1", "on", "YES"])
def test_single_session_accepts_truthy_env_values(make_inproc_server, value):
    srv = make_inproc_server(env={"LM_CHAT_SINGLE_SESSION": value})
    cookie_a = _login(srv.url, ADMIN_USER, ADMIN_PASS)
    _login(srv.url, ADMIN_USER, ADMIN_PASS)
    assert _whoami(srv.url, cookie_a) == 401, f"value={value!r} should enable rotation"


@pytest.mark.parametrize("value", ["", "false", "0", "off", "no", "yolo"])
def test_single_session_rejects_falsy_or_unknown(make_inproc_server, value):
    srv = make_inproc_server(env={"LM_CHAT_SINGLE_SESSION": value})
    cookie_a = _login(srv.url, ADMIN_USER, ADMIN_PASS)
    _login(srv.url, ADMIN_USER, ADMIN_PASS)
    assert _whoami(srv.url, cookie_a) == 200, f"value={value!r} should keep concurrent"


# ---------------------------------------------------------------------------
# Per-user rotation: other users are not affected
# ---------------------------------------------------------------------------

def test_single_session_rotation_is_scoped_to_user(make_inproc_server):
    """Logging in as admin must not invalidate other users' sessions."""
    srv = make_inproc_server(env={"LM_CHAT_SINGLE_SESSION": "true"})

    # Create a second user via the admin's invite endpoint.
    admin_cookie = _login(srv.url, ADMIN_USER, ADMIN_PASS)
    invite_body = json.dumps(
        {"username": "alice", "password": "alicepassword123"}
    ).encode()
    req = urllib.request.Request(
        srv.url + "/api/auth/invite",
        data=invite_body,
        headers={
            "Content-Type": "application/json",
            "Cookie":       admin_cookie,
            **CSRF_HEADER,
        },
        method="POST",
    )
    urllib.request.urlopen(req, timeout=5)

    alice_cookie = _login(srv.url, "alice", "alicepassword123")
    assert _whoami(srv.url, alice_cookie) == 200

    # Admin logs in again — alice's cookie must survive.
    _login(srv.url, ADMIN_USER, ADMIN_PASS)
    assert _whoami(srv.url, alice_cookie) == 200, (
        "admin's rotation invalidated alice's session — scoping bug"
    )


# ---------------------------------------------------------------------------
# Change-password always rotates regardless of policy
# ---------------------------------------------------------------------------

def test_password_change_always_invalidates_other_sessions(make_inproc_server):
    """Independent of LM_CHAT_SINGLE_SESSION, password change must revoke all
    siblings — that's a hard security invariant, not a UX preference.

    Opt out of single-session (the 0.5.7 default) so we can construct two
    valid sibling cookies; the test is that change-password kills them
    regardless of the rotation policy.
    """
    srv = make_inproc_server(env={"LM_CHAT_SINGLE_SESSION": "false"})

    cookie_a = _login(srv.url, ADMIN_USER, ADMIN_PASS)
    cookie_b = _login(srv.url, ADMIN_USER, ADMIN_PASS)
    assert _whoami(srv.url, cookie_a) == 200
    assert _whoami(srv.url, cookie_b) == 200

    new_pass = "newpassword456!"
    body = json.dumps(
        {"current_password": ADMIN_PASS, "new_password": new_pass}
    ).encode()
    req = urllib.request.Request(
        srv.url + "/api/auth/change-password",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Cookie":       cookie_b,
            **CSRF_HEADER,
        },
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=5)
    new_cookie = resp.headers.get("Set-Cookie", "").split(";")[0].strip()

    # The cookie that performed the change-password gets refreshed (200).
    assert _whoami(srv.url, new_cookie) == 200
    # The sibling cookie must be invalidated.
    assert _whoami(srv.url, cookie_a) == 401, "sibling cookie not revoked"
    # The cookie used in the request itself is also rotated to the new token.
    assert _whoami(srv.url, cookie_b) == 401, (
        "the change-password cookie should also have been invalidated"
    )
