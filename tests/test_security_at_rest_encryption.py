"""At-rest encryption for ``user_settings`` (lm_apikey, remote_mcps).

A DB-only compromise (read-only filesystem access, leaked backup, sloppy
volume mount) gives an attacker the LM Studio API key and every remote
MCP auth token stored in plaintext.  The fix uses stdlib-only authenticated
encryption: SHAKE-256 stream cipher with HMAC-SHA256, keys derived from
``LM_CHAT_SECRET`` via HKDF.

Tests cover:
  * Crypto primitive round-trip and failure modes.
  * Storage layer always writes ciphertext.
  * Read layer transparently handles legacy plaintext rows so existing DBs
    keep working (the next write upgrades them).
  * Tampered or wrong-key ciphertexts fail closed (return empty/[]).
  * The MCP auth-preservation flow still works after the crypto wrap.
"""

from __future__ import annotations

import base64
import json
import urllib.request

import pytest

from conftest import CSRF_HEADER


@pytest.fixture
def server(inproc_server):
    return inproc_server.module


# ---------------------------------------------------------------------------
# Primitive round-trip
# ---------------------------------------------------------------------------

def test_encrypt_decrypt_roundtrip_basic(server):
    ct = server.encrypt_at_rest("hello", "ctx")
    assert ct.startswith("enc$v1$")
    assert server.decrypt_at_rest(ct, "ctx") == "hello"


def test_encrypt_empty_returns_empty(server):
    """Empty plaintext means "unset" — no ciphertext, no nonce burn."""
    assert server.encrypt_at_rest("", "ctx") == ""
    assert server.decrypt_at_rest("", "ctx") == ""


def test_encrypt_is_nondeterministic_under_same_input(server):
    """Random nonce per encryption → two ciphertexts for the same plaintext."""
    a = server.encrypt_at_rest("token-abc-123", "ctx")
    b = server.encrypt_at_rest("token-abc-123", "ctx")
    assert a != b
    assert server.decrypt_at_rest(a, "ctx") == "token-abc-123"
    assert server.decrypt_at_rest(b, "ctx") == "token-abc-123"


def test_encrypt_context_domain_separation(server):
    """A ciphertext made under context A must not decrypt under context B."""
    ct = server.encrypt_at_rest("apikey-xyz", "context-A")
    assert server.decrypt_at_rest(ct, "context-A") == "apikey-xyz"
    assert server.decrypt_at_rest(ct, "context-B") is None


def test_encrypt_handles_unicode(server):
    plaintext = "naïve résumé — 你好 — 🔐"
    ct = server.encrypt_at_rest(plaintext, "ctx")
    assert server.decrypt_at_rest(ct, "ctx") == plaintext


def test_encrypt_handles_long_payload(server):
    """A 64 KB payload round-trips — bigger than any realistic mcp config."""
    plaintext = "x" * (64 * 1024)
    ct = server.encrypt_at_rest(plaintext, "ctx")
    assert server.decrypt_at_rest(ct, "ctx") == plaintext


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------

def test_decrypt_legacy_plaintext_returned_unchanged(server):
    """No prefix → the value is a pre-encryption plaintext.  Pass it through
    so reads keep working; the next write encrypts it transparently."""
    assert server.decrypt_at_rest("legacy-bare-token", "ctx") == "legacy-bare-token"


def test_decrypt_tampered_ciphertext_returns_none(server):
    """Flipping any single bit in the MAC or ciphertext must fail closed."""
    ct = server.encrypt_at_rest("secret-value", "ctx")
    raw = base64.b64decode(ct[len("enc$v1$"):])
    # Flip a bit in the ciphertext portion (skip the 16-byte nonce prefix).
    flipped = bytearray(raw)
    flipped[20] ^= 0x01
    tampered = "enc$v1$" + base64.b64encode(bytes(flipped)).decode()
    assert server.decrypt_at_rest(tampered, "ctx") is None


def test_decrypt_truncated_blob_returns_none(server):
    """Less than nonce(16) + mac(32) bytes can't be a valid ciphertext."""
    short = "enc$v1$" + base64.b64encode(b"\x00" * 30).decode()
    assert server.decrypt_at_rest(short, "ctx") is None


def test_decrypt_garbage_base64_returns_none(server):
    """Anything that fails base64 decoding fails closed, not raises."""
    assert server.decrypt_at_rest("enc$v1$@@@not-base64@@@", "ctx") is None


def test_decrypt_wrong_key_returns_none(make_inproc_server):
    """Reading a ciphertext under a different LM_CHAT_SECRET → None.

    This exercises the key-derivation path: HKDF over a different secret
    yields different MAC keys, so the integrity check rejects the blob.
    """
    srv_a = make_inproc_server(env={"LM_CHAT_SECRET": "secret-A" * 8})
    ct = srv_a.module.encrypt_at_rest("apikey-from-A", "user_settings.lm_apikey")

    srv_b = make_inproc_server(env={"LM_CHAT_SECRET": "secret-B" * 8})
    assert srv_b.module.decrypt_at_rest(ct, "user_settings.lm_apikey") is None


# ---------------------------------------------------------------------------
# End-to-end: settings storage actually writes ciphertext
# ---------------------------------------------------------------------------

def _save(base_url: str, cookie: str, body: dict):
    req = urllib.request.Request(
        base_url + "/api/auth/settings",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Cookie": cookie, **CSRF_HEADER},
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=5)
    return json.loads(resp.read())


def _read_db_value(server_mod, user_id: str, key: str) -> str:
    db = server_mod.get_db()
    row = db.execute(
        "SELECT value FROM user_settings WHERE user_id=? AND key=?",
        (user_id, key),
    ).fetchone()
    return row[0] if row else ""


def test_save_lm_apikey_encrypts_in_db(inproc_server, inproc_client):
    """The raw column must not contain the plaintext apikey."""
    client = inproc_client.admin
    apikey = "sk-test-PLAINTEXT-must-not-leak-1234567890"
    _save(inproc_server.url, client.cookie, {"lm_apikey": apikey})

    user = client.json(client.get("/api/auth/me"))["user"]
    raw = _read_db_value(inproc_server.module, user["id"], "lm_apikey")
    assert raw.startswith("enc$v1$"), f"value not encrypted: {raw!r}"
    assert apikey not in raw, "plaintext apikey present in stored value"

    # The server's internal accessor still returns the original plaintext.
    # The handler doesn't touch ``self`` for this method — module-level
    # ``get_db()`` resolves the connection — so we can pass None safely.
    Handler = inproc_server.module.Handler
    assert Handler._get_user_lm_apikey(None, user["id"]) == apikey


def test_save_remote_mcps_encrypts_auth_in_db(inproc_server, inproc_client):
    client = inproc_client.admin
    mcps = [{"label": "mcp-x", "url": "https://example.test/mcp",
             "on": True, "auth": "Bearer SECRET-MCP-TOKEN-9XmK"}]
    _save(inproc_server.url, client.cookie, {"remote_mcps": mcps})

    user = client.json(client.get("/api/auth/me"))["user"]
    raw = _read_db_value(inproc_server.module, user["id"], "remote_mcps")
    assert raw.startswith("enc$v1$"), f"value not encrypted: {raw!r}"
    assert "SECRET-MCP-TOKEN" not in raw, "MCP token present in raw column"

    # API GET only exposes has_auth, never the token.
    resp = client.json(client.get("/api/auth/settings"))
    assert resp["remote_mcps"][0]["has_auth"] is True
    assert "auth" not in resp["remote_mcps"][0]


def test_get_settings_returns_apikey_bool_correctly(inproc_server, inproc_client):
    client = inproc_client.admin
    _save(inproc_server.url, client.cookie, {"lm_apikey": "anything"})
    resp = client.json(client.get("/api/auth/settings"))
    assert resp["lm_apikey"] is True

    _save(inproc_server.url, client.cookie, {"lm_apikey": ""})
    resp = client.json(client.get("/api/auth/settings"))
    assert resp["lm_apikey"] is False


# ---------------------------------------------------------------------------
# Migration: legacy plaintext rows still work, get upgraded on next save
# ---------------------------------------------------------------------------

def test_legacy_plaintext_row_still_readable(inproc_server, inproc_client):
    """A row written before the encryption rollout should still authorise."""
    client = inproc_client.admin
    user = client.json(client.get("/api/auth/me"))["user"]

    # Inject a legacy plaintext row directly.
    db = inproc_server.module.get_db()
    db.execute(
        "INSERT OR REPLACE INTO user_settings (user_id, key, value) VALUES (?, ?, ?)",
        (user["id"], "lm_apikey", "sk-legacy-plaintext"),
    )
    db.commit()

    # Settings API reports apikey-is-set.
    resp = client.json(client.get("/api/auth/settings"))
    assert resp["lm_apikey"] is True


def test_legacy_plaintext_row_upgraded_on_next_save(inproc_server, inproc_client):
    """Writing a new value over a legacy plaintext row encrypts it."""
    client = inproc_client.admin
    user = client.json(client.get("/api/auth/me"))["user"]

    db = inproc_server.module.get_db()
    db.execute(
        "INSERT OR REPLACE INTO user_settings (user_id, key, value) VALUES (?, ?, ?)",
        (user["id"], "lm_apikey", "sk-legacy-plaintext"),
    )
    db.commit()
    raw_before = _read_db_value(inproc_server.module, user["id"], "lm_apikey")
    assert not raw_before.startswith("enc$v1$"), "test setup mistake"

    _save(inproc_server.url, client.cookie, {"lm_apikey": "sk-new-encrypted"})
    raw_after = _read_db_value(inproc_server.module, user["id"], "lm_apikey")
    assert raw_after.startswith("enc$v1$")
    assert "sk-new-encrypted" not in raw_after


def test_mcp_auth_preservation_works_after_encryption(inproc_server, inproc_client):
    """If the client re-saves an MCP entry without the ``auth`` field, the
    server preserves the previously-stored auth value — this must still
    work after we wrapped the read/write paths in encryption."""
    client = inproc_client.admin
    _save(inproc_server.url, client.cookie, {"remote_mcps": [
        {"label": "x", "url": "https://example.test", "on": True,
         "auth": "Bearer PRESERVED-TOKEN-7Z"},
    ]})

    # Re-save without the auth field; the server should keep the old token.
    _save(inproc_server.url, client.cookie, {"remote_mcps": [
        {"label": "x", "url": "https://example.test", "on": True},
    ]})

    # Reach into the server's accessor — confirm token survived.
    user = client.json(client.get("/api/auth/me"))["user"]
    Handler = inproc_server.module.Handler
    mcps = Handler._get_user_remote_mcps(None, user["id"])
    assert mcps[0]["auth"] == "Bearer PRESERVED-TOKEN-7Z"


def test_corrupted_apikey_value_returns_empty(inproc_server, inproc_client):
    """Tampered ciphertext must not surface as an authentication credential."""
    client = inproc_client.admin
    user = client.json(client.get("/api/auth/me"))["user"]

    # Save a valid encrypted apikey, then corrupt it on disk.
    _save(inproc_server.url, client.cookie, {"lm_apikey": "sk-valid"})
    raw = _read_db_value(inproc_server.module, user["id"], "lm_apikey")
    # Append junk bytes — destroys both MAC and content.
    corrupted = raw + "AAAAAAAA"
    db = inproc_server.module.get_db()
    db.execute(
        "UPDATE user_settings SET value=? WHERE user_id=? AND key='lm_apikey'",
        (corrupted, user["id"]),
    )
    db.commit()

    # The accessor must NOT bubble the corrupted bytes anywhere — empty string
    # is the right "no apikey" sentinel.
    Handler = inproc_server.module.Handler
    result = Handler._get_user_lm_apikey(None, user["id"])
    assert result == ""
