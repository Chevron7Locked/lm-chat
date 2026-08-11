# SPDX-License-Identifier: Apache-2.0
"""Auth-service exception hierarchy for lm-chat.

All exceptions derive from :class:`AuthError` so routes can catch the
base type for uniform 401 handling without leaking which specific field
(username vs. password vs. TOTP code) was wrong — per OWASP guidance on
user enumeration prevention.
"""
from __future__ import annotations


class AuthError(Exception):
    """Base class for all authentication / authorisation failures."""


class UsernameTakenError(AuthError):
    """Raised by ``register()`` when the chosen username is already in use."""


class BadCredentialsError(AuthError):
    """Raised when username, password, or TOTP code is incorrect.

    Intentionally unspecific: routes map this to a uniform 401 so that
    response-timing or error-text inspection cannot determine *which*
    credential was wrong (username enumeration mitigation).
    """


class TotpRequiredError(AuthError):
    """Raised by ``login()`` when the user has TOTP configured but no code was supplied."""


class TotpAlreadyConfiguredError(AuthError):
    """Raised when a TOTP setup is attempted on a user who already has TOTP active."""


class TotpNotConfiguredError(AuthError):
    """Raised when a TOTP disable is attempted on a user who has no TOTP configured."""
