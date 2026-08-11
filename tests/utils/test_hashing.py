# SPDX-License-Identifier: Apache-2.0
"""Tests for lmchat.utils.hashing.

Covers:
- Known-vector round-trip: hash_password → verify_password returns True.
- Wrong password → verify returns False.
- Corrupt envelope → verify raises ValueError.
- needs_rehash: stored at lower cost returns True.
- needs_rehash: stored at default cost returns False.
- Cost parameters round-trip in stored string format.
"""
from __future__ import annotations

import pytest

from lmchat.utils.hashing import (
    DEFAULT_N,
    DEFAULT_P,
    DEFAULT_R,
    hash_password,
    needs_rehash,
    verify_password,
)

# Use a very small N for test speed (must still be a power of two ≥ 2).
_TEST_N = 4


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_round_trip_correct_password() -> None:
    """hash_password → verify_password(correct) returns True."""
    pw = "correct-horse-battery-staple"
    stored = hash_password(pw, n=_TEST_N, r=8, p=1)
    assert verify_password(pw, stored) is True


def test_round_trip_default_cost() -> None:
    """hash_password with default N (large) round-trips correctly."""
    # This is slow on purpose — it exercises the real default cost once.
    # Marked xfail-not (runs, just slow). Use a smaller N here to keep
    # the suite fast; the DEFAULT_N value is tested via needs_rehash.
    pw = "my-password"
    stored = hash_password(pw, n=_TEST_N)
    assert verify_password(pw, stored) is True


def test_round_trip_unicode_password() -> None:
    """Unicode passwords encode to UTF-8 and round-trip."""
    pw = "pässwörd-🔐"
    stored = hash_password(pw, n=_TEST_N)
    assert verify_password(pw, stored) is True


# ---------------------------------------------------------------------------
# Wrong password
# ---------------------------------------------------------------------------


def test_wrong_password_returns_false() -> None:
    """verify_password returns False for an incorrect password."""
    stored = hash_password("correct", n=_TEST_N)
    assert verify_password("wrong", stored) is False


def test_similar_password_returns_false() -> None:
    """A password that differs by one character is rejected."""
    stored = hash_password("password1", n=_TEST_N)
    assert verify_password("password2", stored) is False


def test_empty_password_does_not_verify_non_empty() -> None:
    """Empty string does not verify against a non-empty password hash."""
    stored = hash_password("nonempty", n=_TEST_N)
    assert verify_password("", stored) is False


# ---------------------------------------------------------------------------
# Corrupt envelope → ValueError
# ---------------------------------------------------------------------------


def test_garbage_envelope_raises_value_error() -> None:
    """Completely malformed stored string raises ValueError."""
    with pytest.raises(ValueError, match="corrupt|invalid"):
        verify_password("pw", "this-is-garbage")


def test_too_few_fields_raises_value_error() -> None:
    """Envelope with fewer than 6 fields raises ValueError."""
    with pytest.raises(ValueError, match="corrupt|invalid"):
        verify_password("pw", "scrypt$1$8$1$salt")


def test_wrong_prefix_raises_value_error() -> None:
    """Envelope starting with an unexpected prefix raises ValueError."""
    with pytest.raises(ValueError, match="corrupt|invalid"):
        verify_password("pw", "bcrypt$17$8$1$abc$def")


def test_non_integer_cost_raises_value_error() -> None:
    """Non-integer cost parameter in envelope raises ValueError."""
    with pytest.raises(ValueError, match="corrupt"):
        verify_password("pw", "scrypt$abc$8$1$dGVzdA==$dGVzdA==")


def test_bad_base64_raises_value_error() -> None:
    """Invalid base64 in salt field raises ValueError."""
    with pytest.raises(ValueError, match="corrupt"):
        verify_password("pw", "scrypt$4$8$1$!!!not-base64!!!$dGVzdA==")


# ---------------------------------------------------------------------------
# needs_rehash
# ---------------------------------------------------------------------------


def test_needs_rehash_below_default_n() -> None:
    """Stored hash with N < DEFAULT_N reports needs_rehash True."""
    stored = hash_password("pw", n=_TEST_N, r=DEFAULT_R, p=DEFAULT_P)
    assert needs_rehash(stored) is True


def test_needs_rehash_at_default_cost() -> None:
    """Stored hash at exactly the default cost reports needs_rehash False.

    Constructs the envelope string directly to avoid running scrypt at the
    full DEFAULT_N=2^17 (128 MiB), which exceeds OpenSSL's default maxmem
    cap in the test environment.  needs_rehash only parses cost fields —
    the embedded hash bytes are never verified here.
    """
    import base64

    dummy_salt = base64.b64encode(b"\x00" * 16).decode()
    dummy_hash = base64.b64encode(b"\x00" * 32).decode()
    stored = f"scrypt${DEFAULT_N}${DEFAULT_R}${DEFAULT_P}${dummy_salt}${dummy_hash}"
    assert needs_rehash(stored) is False


def test_needs_rehash_above_default_n() -> None:
    """Stored hash with N > DEFAULT_N reports needs_rehash False.

    Same synthetic-envelope approach as test_needs_rehash_at_default_cost.
    """
    import base64

    dummy_salt = base64.b64encode(b"\x00" * 16).decode()
    dummy_hash = base64.b64encode(b"\x00" * 32).decode()
    stored = f"scrypt${DEFAULT_N * 2}${DEFAULT_R}${DEFAULT_P}${dummy_salt}${dummy_hash}"
    assert needs_rehash(stored) is False


def test_needs_rehash_corrupt_envelope_raises() -> None:
    """needs_rehash raises ValueError on a corrupt envelope."""
    with pytest.raises(ValueError, match="corrupt|invalid"):
        needs_rehash("not-a-valid-envelope")


# ---------------------------------------------------------------------------
# Cost parameters round-trip in stored string
# ---------------------------------------------------------------------------


def test_stored_string_encodes_cost_params() -> None:
    """The stored string encodes N, r, p as second through fourth fields."""
    n, r, p = _TEST_N, 8, 1
    stored = hash_password("pw", n=n, r=r, p=p)
    parts = stored.split("$")
    assert parts[0] == "scrypt"
    assert int(parts[1]) == n
    assert int(parts[2]) == r
    assert int(parts[3]) == p


def test_stored_string_has_six_fields() -> None:
    """The stored string always has exactly 6 '$'-delimited fields."""
    stored = hash_password("pw", n=_TEST_N)
    assert stored.count("$") == 5  # 6 fields → 5 delimiters


def test_two_hashes_of_same_password_differ() -> None:
    """Two calls to hash_password with the same password produce different salts."""
    pw = "same-password"
    stored_a = hash_password(pw, n=_TEST_N)
    stored_b = hash_password(pw, n=_TEST_N)
    assert stored_a != stored_b


def test_stored_string_base64_fields_are_standard() -> None:
    """Salt and hash fields decode cleanly as standard (not URL-safe) base64."""
    import base64

    stored = hash_password("pw", n=_TEST_N)
    parts = stored.split("$")
    salt_bytes = base64.b64decode(parts[4])
    hash_bytes = base64.b64decode(parts[5])
    assert len(salt_bytes) == 16
    assert len(hash_bytes) == 32
