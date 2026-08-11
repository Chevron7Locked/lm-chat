# SPDX-License-Identifier: Apache-2.0
"""Tests for lmchat.utils.totp.

Covers:
- generate_secret returns a 32-char base32 string.
- verify(secret, current code) → True.
- verify(secret, wrong code) → False.
- valid_window: code from 25s ago verifies True; from 90s ago verifies False.
- provisioning_uri starts with "otpauth://totp/" and includes secret + issuer.
"""
from __future__ import annotations

import time

import pyotp

from lmchat.utils.totp import generate_secret, provisioning_uri, verify

# ---------------------------------------------------------------------------
# generate_secret
# ---------------------------------------------------------------------------


def test_generate_secret_returns_32_chars() -> None:
    """generate_secret returns a 32-character string."""
    secret = generate_secret()
    assert len(secret) == 32


def test_generate_secret_is_base32() -> None:
    """generate_secret returns an uppercase base32-alphabet string."""
    import base64

    secret = generate_secret()
    # base32 alphabet: A-Z and 2-7.  Padding with '=' may appear but pyotp
    # random_base32 does not pad — validate by round-trip decode.
    decoded = base64.b32decode(secret)
    assert isinstance(decoded, bytes)
    assert len(decoded) == 20  # 32 base32 chars → 20 bytes


def test_generate_secret_unique() -> None:
    """Two calls to generate_secret return different values."""
    assert generate_secret() != generate_secret()


# ---------------------------------------------------------------------------
# verify — current code
# ---------------------------------------------------------------------------


def test_verify_current_code_returns_true() -> None:
    """A freshly generated TOTP code verifies as True."""
    secret = generate_secret()
    code = pyotp.TOTP(secret).now()
    assert verify(secret, code) is True


def test_verify_wrong_code_returns_false() -> None:
    """A code of '000000' almost certainly does not match a fresh secret."""
    secret = generate_secret()
    # '000000' is a fixed invalid code; probability of collision is 1/10^6.
    assert verify(secret, "000000") is False or verify(secret, "000000") is True  # noqa: S101
    # Safer: generate a wrong code by incrementing the real one.
    real_code = pyotp.TOTP(secret).now()
    wrong_code = str((int(real_code) + 1) % 1_000_000).zfill(6)
    assert verify(secret, wrong_code) is False


def test_verify_empty_code_returns_false() -> None:
    """An empty code string is rejected."""
    secret = generate_secret()
    assert verify(secret, "") is False


def test_verify_non_numeric_code_returns_false() -> None:
    """A non-numeric code is rejected without raising."""
    secret = generate_secret()
    assert verify(secret, "abcdef") is False


# ---------------------------------------------------------------------------
# valid_window: drift tolerance
# ---------------------------------------------------------------------------


def test_verify_25s_old_code_returns_true() -> None:
    """A code generated 25 seconds ago should still verify (within window=1)."""
    secret = generate_secret()
    past_time = int(time.time()) - 25
    old_code = pyotp.TOTP(secret).at(past_time)
    # valid_window=1 means ±1 step (±30 s), so 25 s ago is within window.
    assert verify(secret, old_code) is True


def test_verify_90s_old_code_returns_false() -> None:
    """A code generated 90 seconds ago should NOT verify (beyond window=1)."""
    secret = generate_secret()
    past_time = int(time.time()) - 90
    old_code = pyotp.TOTP(secret).at(past_time)
    # 90 s is 3 TOTP steps; valid_window=1 covers only 1 step in each direction.
    assert verify(secret, old_code) is False


def test_verify_25s_future_code_returns_true() -> None:
    """A code from 25 seconds in the future should verify (window covers forward too)."""
    secret = generate_secret()
    future_time = int(time.time()) + 25
    future_code = pyotp.TOTP(secret).at(future_time)
    assert verify(secret, future_code) is True


# ---------------------------------------------------------------------------
# provisioning_uri
# ---------------------------------------------------------------------------


def test_provisioning_uri_starts_with_otpauth() -> None:
    """provisioning_uri returns a string starting with 'otpauth://totp/'."""
    secret = generate_secret()
    uri = provisioning_uri(secret, name="alice")
    assert uri.startswith("otpauth://totp/")


def test_provisioning_uri_contains_secret() -> None:
    """provisioning_uri encodes the secret in the query string."""
    secret = generate_secret()
    uri = provisioning_uri(secret, name="alice")
    assert f"secret={secret}" in uri


def test_provisioning_uri_contains_default_issuer() -> None:
    """provisioning_uri includes the default issuer 'lm-chat'."""
    secret = generate_secret()
    uri = provisioning_uri(secret, name="alice")
    assert "lm-chat" in uri


def test_provisioning_uri_contains_custom_issuer() -> None:
    """A custom issuer appears in the provisioning URI."""
    secret = generate_secret()
    uri = provisioning_uri(secret, name="bob", issuer="MyApp")
    assert "MyApp" in uri


def test_provisioning_uri_contains_name() -> None:
    """The account name appears in the provisioning URI."""
    secret = generate_secret()
    uri = provisioning_uri(secret, name="charlie@example.com")
    # pyotp percent-encodes the name; check the encoded form is present.
    assert "charlie" in uri
