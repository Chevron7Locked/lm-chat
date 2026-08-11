"""Server-side password policy validation.

Single source of truth for password strength rules. Called from both
register and change_password so the frontend's `minLength={8}` HTML
attribute cannot be bypassed by direct API calls (a UI audit caught
`POST /api/auth/password` accepting a one-character new password
because validation was client-only).

The rules deliberately stay simple:

* MIN_LENGTH = 8 — universally cited floor
* MAX_LENGTH = 4096 — defends against memory/CPU exhaustion via giant
  bcrypt inputs while leaving room for memorable passphrases
* whitespace-only strings are rejected
* leading/trailing whitespace is preserved (some password managers
  generate trailing-newline strings; we don't strip silently)

NIST SP 800-63B does NOT require complexity rules (digits, symbols);
those tend to push users toward predictable patterns. Length is the
primary defense. If the admin wants stricter complexity later, add
a setting + extend `validate_new_password`.
"""
from __future__ import annotations

from typing import Final

MIN_LENGTH: Final[int] = 8
MAX_LENGTH: Final[int] = 4096


class PasswordPolicyError(ValueError):
    """Raised when a candidate password fails the policy.

    The route layer maps this to HTTP 422 with the message as detail.
    The message is admin-safe (no candidate echo, no PII).
    """


def validate_new_password(candidate: str) -> None:
    """Raise :class:`PasswordPolicyError` if *candidate* fails the policy.

    Returns ``None`` on success. The caller proceeds to hash + store
    only after this returns cleanly.
    """
    if candidate == "" or candidate.strip() == "":
        raise PasswordPolicyError("password must not be empty or whitespace-only")
    if len(candidate) < MIN_LENGTH:
        raise PasswordPolicyError(
            f"password must be at least {MIN_LENGTH} characters"
        )
    if len(candidate) > MAX_LENGTH:
        raise PasswordPolicyError(
            f"password must be at most {MAX_LENGTH} characters"
        )
    # Block control characters except common whitespace inside the body
    # (a tab or newline mid-password is a footgun some managers emit; we
    # explicitly tolerate them so we don't lock anyone out of an existing
    # hash). NUL bytes are unconditionally rejected — they truncate
    # downstream str→bytes conversions in C code.
    if "\x00" in candidate:
        raise PasswordPolicyError(
            "password must not contain null bytes"
        )
