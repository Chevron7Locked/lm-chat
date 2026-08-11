# SPDX-License-Identifier: Apache-2.0
"""TOTP utilities wrapping pyotp.

TOTP secrets are stored encrypted in the database using the
``enc$v1$`` envelope from :mod:`lmchat.utils.encryption` — the
encryption/decryption is performed in the auth service, not here.
This module is a thin, stateless wrapper over :mod:`pyotp` that
provides the three primitive operations needed by the auth service:
secret generation, code verification, and provisioning URI assembly
for QR code rendering.
"""
from __future__ import annotations

from typing import Final

import pyotp

_VALID_WINDOW: Final[int] = 1  # ±30 s drift tolerance (one TOTP step each side)
_DEFAULT_ISSUER: Final[str] = "lm-chat"


def generate_secret() -> str:
    """Generate a fresh random TOTP secret in base32 encoding.

    Returns a 32-character base32 string suitable for seeding a
    :class:`pyotp.TOTP` instance and for storage (after encryption)
    in ``users.totp_secret``.

    Returns:
        A 32-character uppercase base32 string.
    """
    return pyotp.random_base32()


def verify(secret: str, code: str) -> bool:
    """Return ``True`` if *code* is a valid TOTP token for *secret*.

    Allows a valid-window of ±1 step (``valid_window=1``) to tolerate
    up to 30 seconds of client-clock drift in either direction.

    Args:
        secret:  Base32-encoded TOTP secret.
        code:    6-digit code supplied by the user.

    Returns:
        ``True`` if the code matches the current or immediately
        adjacent TOTP step, ``False`` otherwise.
    """
    return bool(pyotp.TOTP(secret).verify(code, valid_window=_VALID_WINDOW))


def provisioning_uri(
    secret: str,
    *,
    name: str,
    issuer: str = _DEFAULT_ISSUER,
) -> str:
    """Return an ``otpauth://totp/`` URI for QR code generation.

    The URI is passed to a QR code library in the auth route to let
    the user scan it with an authenticator app.

    Args:
        secret:  Base32-encoded TOTP secret.
        name:    Account name shown in the authenticator (typically
                 the username).
        issuer:  Issuer label displayed by the authenticator app.
                 Defaults to ``"lm-chat"``.

    Returns:
        A string of the form
        ``"otpauth://totp/<issuer>:<name>?secret=<secret>&issuer=<issuer>"``.
    """
    return pyotp.TOTP(secret).provisioning_uri(name=name, issuer_name=issuer)
