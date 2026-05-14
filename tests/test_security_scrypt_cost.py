"""scrypt cost parameters, hash format, and rehash-on-login.

Pre-0.5.0 stored bare-hex scrypt hashes with the 2017 OWASP defaults
(``n=16384, r=8, p=1``).  The 2024 OWASP floor is ``n=131072``, and modern
silicon makes the older cost roughly 4–8× weaker than the current minimum.

We bumped the default cost and switched to a composite hash format that
carries its own ``n/r/p`` so:
  * Existing DBs keep working — verify against the legacy params.
  * Old hashes are silently upgraded on the next successful login.
  * Operators can tune cost via ``LM_CHAT_SCRYPT_N``/``_R``/``_P`` without
    breaking older rows.

These tests assert all three properties from end to end.
"""

from __future__ import annotations

import hashlib
import json
import urllib.request

import pytest

from conftest import ADMIN_USER, CSRF_HEADER


@pytest.fixture
def server(inproc_server):
    return inproc_server.module


# ---------------------------------------------------------------------------
# Hash format / parser
# ---------------------------------------------------------------------------

def test_new_hashes_use_composite_format(server):
    """``hash_password`` always emits ``scrypt$n=...$r=...$p=...$<hex>``."""
    h, _ = server.hash_password("correct horse battery staple")
    assert h.startswith("scrypt$n=")
    parts = h.split("$")
    # ["scrypt", "n=N", "r=R", "p=P", "<128 hex chars>"]
    assert len(parts) == 5
    assert parts[0] == "scrypt"
    assert parts[1].startswith("n=") and parts[1][2:].isdigit()
    assert parts[2].startswith("r=") and parts[2][2:].isdigit()
    assert parts[3].startswith("p=") and parts[3][2:].isdigit()
    assert len(parts[4]) == 128  # 64-byte digest, hex-encoded


def test_default_cost_meets_owasp_2024_floor(server):
    """Default ``n`` must be at least 2^17 (131072) per OWASP 2024.

    A regression that drops the floor would silently weaken every new
    password without raising any obvious error in normal use.
    """
    h, _ = server.hash_password("xxxxxxxx")
    n, r, p, _ = server._parse_password_hash(h)
    assert n >= 131072, f"n={n} is below OWASP 2024 floor"
    assert r == 8
    assert p == 1


def test_parser_accepts_legacy_bare_hex(server):
    """Bare-hex hashes from pre-0.5.0 are interpreted as legacy params."""
    # 128 hex chars — what the old format wrote.
    legacy = "a" * 128
    n, r, p, raw = server._parse_password_hash(legacy)
    assert (n, r, p) == server._SCRYPT_LEGACY_PARAMS
    assert raw == legacy


def test_parser_rejects_malformed_composite(server):
    """A composite-prefixed but broken string raises, not silently lets in."""
    for bad in [
        "scrypt$broken",
        "scrypt$n=abc$r=8$p=1$0",
        "scrypt$n=131072$r=8$0",  # missing p=
        "scrypt$xx=1$yy=2$zz=3$hex",
        "scrypt$$$$",
    ]:
        with pytest.raises(ValueError):
            server._parse_password_hash(bad)


# ---------------------------------------------------------------------------
# Backward-compat verify
# ---------------------------------------------------------------------------

def _legacy_hash(password: str, salt: bytes) -> str:
    """Compute a pre-0.5.0 bare-hex hash with the 2017 params."""
    return hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=64).hex()


def test_verify_password_accepts_legacy_hashes(server):
    """A legacy bare-hex hash must still authenticate the correct password."""
    import os
    salt = os.urandom(16)
    h_legacy = _legacy_hash("hunter2hunter", salt)
    assert server.verify_password("hunter2hunter", h_legacy, salt.hex()) is True
    assert server.verify_password("wrong-password", h_legacy, salt.hex()) is False


def test_verify_password_accepts_composite_hashes(server):
    h, salt = server.hash_password("hunter2hunter")
    assert server.verify_password("hunter2hunter", h, salt) is True
    assert server.verify_password("wrong-password", h, salt) is False


def test_verify_returns_false_on_malformed_hash(server):
    """No matter what nonsense is in the DB column, verify never raises."""
    import os
    salt = os.urandom(16).hex()
    for bad in ["", "not-hex-not-prefixed", "scrypt$n=abc$$"]:
        assert server.verify_password("any", bad, salt) is False


# ---------------------------------------------------------------------------
# needs_rehash
# ---------------------------------------------------------------------------

def test_needs_rehash_true_for_legacy(server):
    legacy = "a" * 128
    assert server.password_needs_rehash(legacy) is True


def test_needs_rehash_false_for_current(server):
    h, _ = server.hash_password("anything")
    assert server.password_needs_rehash(h) is False


def test_needs_rehash_true_for_below_floor_composite(server):
    """A composite hash with weaker params is still flagged for upgrade."""
    h, _ = server.hash_password("anything", params=(16384, 8, 1))
    assert server.password_needs_rehash(h) is True


# ---------------------------------------------------------------------------
# End-to-end: login transparently upgrades a legacy hash
# ---------------------------------------------------------------------------

def _login(base_url: str, username: str, password: str) -> int:
    body = json.dumps({"username": username, "password": password}).encode()
    req = urllib.request.Request(
        base_url + "/api/auth/login",
        data=body,
        headers={"Content-Type": "application/json", **CSRF_HEADER},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.status
    except urllib.error.HTTPError as e:  # type: ignore[name-defined]
        return e.code


import urllib.error  # noqa: E402


def test_login_rehashes_legacy_stored_hash(make_inproc_server):
    """A user whose row has the legacy bare-hex hash + legacy salt is
    upgraded to the composite form on the next successful login."""
    srv = make_inproc_server(bootstrap_admin=False)
    server = srv.module

    # Build a user row by hand with a legacy-format hash, simulating an
    # upgrade from a pre-0.5.0 deployment.
    import os, time, uuid
    salt = os.urandom(16)
    legacy = _legacy_hash("legacy-pass-1234", salt)
    user_id = uuid.uuid4().hex
    db = server.get_db()
    db.execute(
        "INSERT INTO users (id,username,password_hash,salt,display_name,is_admin,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (user_id, ADMIN_USER, legacy, salt.hex(), ADMIN_USER, 1, time.time()),
    )
    db.commit()

    # Verify pre-conditions: row has legacy bare-hex hash.
    stored = db.execute(
        "SELECT password_hash FROM users WHERE id=?", (user_id,)
    ).fetchone()[0]
    assert not stored.startswith(server._SCRYPT_PREFIX), "test setup mistake"

    # Log in.  Should succeed AND silently upgrade the hash.
    assert _login(srv.url, ADMIN_USER, "legacy-pass-1234") == 200

    upgraded = db.execute(
        "SELECT password_hash FROM users WHERE id=?", (user_id,)
    ).fetchone()[0]
    assert upgraded.startswith(server._SCRYPT_PREFIX), (
        f"hash was not upgraded: {upgraded[:40]!r}"
    )
    n, _, _, _ = server._parse_password_hash(upgraded)
    assert n >= 131072

    # Subsequent login still works against the upgraded hash.
    assert _login(srv.url, ADMIN_USER, "legacy-pass-1234") == 200


def test_login_does_not_rehash_if_password_wrong(make_inproc_server):
    """A failed login must not touch the password_hash column."""
    srv = make_inproc_server(bootstrap_admin=False)
    server = srv.module

    import os, time, uuid
    salt = os.urandom(16)
    legacy = _legacy_hash("legacy-pass-1234", salt)
    user_id = uuid.uuid4().hex
    db = server.get_db()
    db.execute(
        "INSERT INTO users (id,username,password_hash,salt,display_name,is_admin,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (user_id, ADMIN_USER, legacy, salt.hex(), ADMIN_USER, 1, time.time()),
    )
    db.commit()

    # Wrong password
    assert _login(srv.url, ADMIN_USER, "definitely-wrong") == 401

    still = db.execute(
        "SELECT password_hash FROM users WHERE id=?", (user_id,)
    ).fetchone()[0]
    assert still == legacy, "failed login should NOT touch the hash"


# ---------------------------------------------------------------------------
# Env-driven cost override (lets test runs pick lighter params for speed)
# ---------------------------------------------------------------------------

def test_env_n_override_takes_effect(make_inproc_server):
    """``LM_CHAT_SCRYPT_N`` controls the cost parameter for new hashes."""
    srv = make_inproc_server(env={"LM_CHAT_SCRYPT_N": "32768"})
    n, r, p = srv.module.SCRYPT_PARAMS_CURRENT
    assert (n, r, p) == (32768, 8, 1)
