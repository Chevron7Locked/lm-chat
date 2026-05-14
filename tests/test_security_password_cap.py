"""Password-length DoS guard tests.

Without an upper bound on password length, the 50 MB body limit lets an
attacker burn many seconds of CPU per login attempt — scrypt processes the
full byte string of every candidate.  The cap at ``PASSWORD_MAX_LENGTH``
keeps the cost bounded and is enforced in three places:

  * ``validate_password`` — pre-flight gate used by setup/change endpoints
  * ``hash_password`` — defensive raise on misuse
  * ``verify_password`` — fast False without ever calling scrypt

These tests assert all three, including a DoS-time-budget assertion that
catches a regression where any one of them stops enforcing the cap.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request

import pytest


@pytest.fixture
def server(inproc_server):
    return inproc_server.module


# ---------------------------------------------------------------------------
# validate_password contract
# ---------------------------------------------------------------------------

def test_validate_password_rejects_under_min(server):
    assert not server.validate_password("short")
    assert not server.validate_password("a" * (server.PASSWORD_MIN_LENGTH - 1))


def test_validate_password_accepts_at_min(server):
    assert server.validate_password("a" * server.PASSWORD_MIN_LENGTH)


def test_validate_password_accepts_at_max(server):
    assert server.validate_password("a" * server.PASSWORD_MAX_LENGTH)


def test_validate_password_rejects_over_max(server):
    assert not server.validate_password("a" * (server.PASSWORD_MAX_LENGTH + 1))


def test_validate_password_rejects_non_str(server):
    assert not server.validate_password(None)
    assert not server.validate_password(b"a" * 16)
    assert not server.validate_password(12345678)


# ---------------------------------------------------------------------------
# hash_password / verify_password direct cap enforcement
# ---------------------------------------------------------------------------

def test_hash_password_raises_on_oversize(server):
    with pytest.raises(ValueError):
        server.hash_password("a" * (server.PASSWORD_MAX_LENGTH + 1))


def test_hash_password_accepts_max_length(server):
    h, salt = server.hash_password("a" * server.PASSWORD_MAX_LENGTH)
    # Composite format: scrypt$n=...$r=...$p=...$<128 hex chars>
    assert isinstance(h, str) and h.startswith("scrypt$") and h.endswith("$" + h.rsplit("$", 1)[1])
    assert len(h.rsplit("$", 1)[1]) == 128  # 64-byte digest in hex
    assert isinstance(salt, str) and len(salt) == 32  # 16-byte salt in hex


def test_verify_password_returns_false_without_hashing_oversize(server):
    """An oversize candidate must short-circuit to False — and do so cheaply."""
    real_hash, real_salt = server.hash_password("correct horse battery")
    # 1 MB candidate, way over cap.  This should be ~instant — no scrypt run.
    oversize = "x" * (1024 * 1024)
    start = time.monotonic()
    ok = server.verify_password(oversize, real_hash, real_salt)
    elapsed = time.monotonic() - start
    assert ok is False
    # scrypt at the configured cost is ~30-80 ms on M-class silicon.  Bound
    # of 50 ms is generous and catches a regression where the cap is removed.
    assert elapsed < 0.05, (
        f"oversize verify took {elapsed:.3f}s — scrypt is still running on it"
    )


# ---------------------------------------------------------------------------
# End-to-end: login DoS guard
# ---------------------------------------------------------------------------

def _login_attempt(base_url: str, username: str, password: str):
    body = ('{"username": "' + username + '", "password": "' + password + '"}').encode()
    req = urllib.request.Request(
        base_url + "/api/auth/login",
        data=body,
        headers={
            "Content-Type":     "application/json",
            "X-Requested-With": "lm-chat",
        },
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_login_with_oversize_password_is_bounded(inproc_server):
    """1 MB password in /api/auth/login must return non-200 quickly.

    The pre-flight cap is what bounds CPU.  A regression that removes the
    cap from validate_password but leaves it on hash_password would still
    pass the unit test above — but here the full request path is exercised.
    """
    payload_pw = "y" * (1024 * 1024)
    start = time.monotonic()
    code, _ = _login_attempt(inproc_server.url, "admin", payload_pw)
    elapsed = time.monotonic() - start
    # Rate limit (429) or invalid creds (401) are both acceptable — the only
    # thing that's not acceptable is success or excessive CPU time.
    assert code != 200
    # Generous bound — local DB query + a single scrypt run = ~100 ms.
    # If this test takes more than 1.5s the cap is gone somewhere.
    assert elapsed < 1.5, (
        f"oversize login took {elapsed:.2f}s — DoS guard missing on a code path"
    )


def test_setup_rejects_oversize_password(inproc_server):
    """Auth setup with oversize password is bounded as well.

    Setup runs before any user exists, but the server-side admin bootstrap
    in our fixture has already populated one — so setup will return either
    400 (oversize) or 400 (already complete).  Either is fine; the only
    failure mode is success or a long delay.
    """
    payload_pw = "z" * (1024 * 1024)
    body = (
        '{"username": "newadmin", "password": "' + payload_pw + '"}'
    ).encode()
    req = urllib.request.Request(
        inproc_server.url + "/api/auth/setup",
        data=body,
        headers={
            "Content-Type":     "application/json",
            "X-Requested-With": "lm-chat",
        },
        method="POST",
    )
    start = time.monotonic()
    try:
        urllib.request.urlopen(req, timeout=5)
        ok = True
    except urllib.error.HTTPError as e:
        ok = e.code == 200
    elapsed = time.monotonic() - start
    assert not ok, "setup with 1MB password succeeded — cap is missing"
    assert elapsed < 1.5, f"setup took {elapsed:.2f}s — cap not enforced"
