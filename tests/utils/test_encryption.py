# SPDX-License-Identifier: Apache-2.0
"""Tests for lmchat.utils.encryption.

Covers:
- Round-trip: encrypt → decrypt returns original plaintext.
- Tag validation: tampered ciphertext raises InvalidTag.
- Cross-record-id rejection: wrong record_id raises InvalidTag.
- Cross-kind rejection: wrong kind raises InvalidTag.
- Bare-text passthrough: unencrypted rows returned as bytes.
- No LM_CHAT_SECRET: RuntimeError raised.
- Nonce uniqueness: same plaintext encrypted twice yields different tokens.
"""
from __future__ import annotations

from collections.abc import Generator

import pytest
from cryptography.exceptions import InvalidTag
from pytest import MonkeyPatch

from lmchat.utils.encryption import PREFIX, decrypt, encrypt


@pytest.fixture(autouse=True)
def patch_secret(monkeypatch: MonkeyPatch) -> Generator[None]:
    """Set LM_CHAT_SECRET for all tests and clear lru_cache between tests."""
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-value-for-unit-tests-32b!")
    # Clear the lru_cache so the freshly set env var is picked up.
    from lmchat.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_round_trip_basic() -> None:
    """encrypt → decrypt returns the original plaintext bytes."""
    plaintext = b"super-secret-totp-seed"
    token = encrypt(plaintext, kind="totp", record_id=42)
    assert token.startswith(PREFIX)
    result = decrypt(token, kind="totp", record_id=42)
    assert result == plaintext


def test_round_trip_empty_bytes() -> None:
    """Round-trip with empty plaintext succeeds."""
    token = encrypt(b"", kind="totp", record_id=1)
    assert decrypt(token, kind="totp", record_id=1) == b""


def test_round_trip_binary_data() -> None:
    """Round-trip preserves arbitrary binary data unchanged."""
    plaintext = bytes(range(256))
    token = encrypt(plaintext, kind="api_token", record_id=999)
    assert decrypt(token, kind="api_token", record_id=999) == plaintext


# ---------------------------------------------------------------------------
# Tag validation: tampered ciphertext
# ---------------------------------------------------------------------------


def test_tampered_ciphertext_raises_invalid_tag() -> None:
    """Flipping one byte in the ciphertext body causes InvalidTag."""
    import base64

    token = encrypt(b"my-secret", kind="totp", record_id=7)
    payload = base64.urlsafe_b64decode(token[len(PREFIX) :])

    # Flip the first byte of the ciphertext (byte 12, after the 12-byte nonce).
    flipped = bytearray(payload)
    flipped[12] ^= 0xFF
    bad_token = PREFIX + base64.urlsafe_b64encode(bytes(flipped)).decode()

    with pytest.raises(InvalidTag):
        decrypt(bad_token, kind="totp", record_id=7)


def test_tampered_tag_raises_invalid_tag() -> None:
    """Flipping the last byte (in the GCM tag) causes InvalidTag."""
    import base64

    token = encrypt(b"my-secret", kind="totp", record_id=7)
    payload = base64.urlsafe_b64decode(token[len(PREFIX) :])

    flipped = bytearray(payload)
    flipped[-1] ^= 0x01
    bad_token = PREFIX + base64.urlsafe_b64encode(bytes(flipped)).decode()

    with pytest.raises(InvalidTag):
        decrypt(bad_token, kind="totp", record_id=7)


# ---------------------------------------------------------------------------
# Cross-record-id rejection
# ---------------------------------------------------------------------------


def test_cross_record_id_rejected() -> None:
    """Ciphertext encrypted for record_id=1 cannot be decrypted as record_id=2."""
    plaintext = b"sensitive-value"
    token = encrypt(plaintext, kind="totp", record_id=1)
    with pytest.raises(InvalidTag):
        decrypt(token, kind="totp", record_id=2)


def test_cross_record_id_large_values() -> None:
    """Cross-record rejection holds for large primary key values."""
    token = encrypt(b"data", kind="totp", record_id=1_000_000)
    with pytest.raises(InvalidTag):
        decrypt(token, kind="totp", record_id=1_000_001)


# ---------------------------------------------------------------------------
# Cross-kind rejection
# ---------------------------------------------------------------------------


def test_cross_kind_rejected() -> None:
    """Ciphertext encrypted as kind='totp' cannot be decrypted as kind='api_key'."""
    plaintext = b"sensitive-value"
    token = encrypt(plaintext, kind="totp", record_id=1)
    with pytest.raises(InvalidTag):
        decrypt(token, kind="api_key", record_id=1)


def test_cross_kind_and_record_id_rejected() -> None:
    """Swapping both kind and record_id is still rejected."""
    token = encrypt(b"value", kind="totp", record_id=5)
    with pytest.raises(InvalidTag):
        decrypt(token, kind="api_key", record_id=9)


# ---------------------------------------------------------------------------
# Bare-text passthrough
# ---------------------------------------------------------------------------


def test_bare_text_passthrough() -> None:
    """A value without the enc$v1$ prefix is returned as raw bytes."""
    raw = "plaintext_totp_seed"
    result = decrypt(raw, kind="totp", record_id=1)
    assert result == raw.encode()


def test_bare_text_passthrough_empty() -> None:
    """Empty string bare-text passthrough returns empty bytes."""
    result = decrypt("", kind="x", record_id=1)
    assert result == b""


def test_bare_text_passthrough_does_not_require_secret(monkeypatch: MonkeyPatch) -> None:
    """Bare-text passthrough succeeds even when LM_CHAT_SECRET is empty."""
    from lmchat.config import get_settings

    monkeypatch.setenv("LM_CHAT_SECRET", "")
    get_settings.cache_clear()

    result = decrypt("unencrypted_value", kind="totp", record_id=1)
    assert result == b"unencrypted_value"

    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# No LM_CHAT_SECRET
# ---------------------------------------------------------------------------


def test_encrypt_no_secret_raises(monkeypatch: MonkeyPatch) -> None:
    """encrypt() raises RuntimeError when LM_CHAT_SECRET is not set."""
    from lmchat.config import get_settings

    monkeypatch.setenv("LM_CHAT_SECRET", "")
    get_settings.cache_clear()

    with pytest.raises(RuntimeError, match="LM_CHAT_SECRET required"):
        encrypt(b"data", kind="totp", record_id=1)

    get_settings.cache_clear()


def test_decrypt_encrypted_no_secret_raises(monkeypatch: MonkeyPatch) -> None:
    """decrypt() of an enc$v1$ token raises RuntimeError when LM_CHAT_SECRET is empty."""
    # First encrypt with a valid secret.
    token = encrypt(b"data", kind="totp", record_id=1)

    # Then clear the secret.
    from lmchat.config import get_settings

    monkeypatch.setenv("LM_CHAT_SECRET", "")
    get_settings.cache_clear()

    with pytest.raises(RuntimeError, match="LM_CHAT_SECRET required"):
        decrypt(token, kind="totp", record_id=1)

    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Nonce uniqueness
# ---------------------------------------------------------------------------


def test_nonce_uniqueness() -> None:
    """Encrypting the same plaintext twice produces different tokens."""
    plaintext = b"same-plaintext"
    token_a = encrypt(plaintext, kind="totp", record_id=1)
    token_b = encrypt(plaintext, kind="totp", record_id=1)
    assert token_a != token_b


def test_nonce_uniqueness_many() -> None:
    """Encrypting the same plaintext many times produces all-distinct tokens."""
    tokens = {encrypt(b"fixed", kind="totp", record_id=1) for _ in range(20)}
    assert len(tokens) == 20


# ---------------------------------------------------------------------------
# Kind validation
# ---------------------------------------------------------------------------


def test_kind_with_pipe_raises() -> None:
    """Kind containing '|' is rejected to prevent KDF domain collision."""
    with pytest.raises(ValueError, match="kind must not contain"):
        encrypt(b"data", kind="bad|kind", record_id=1)
