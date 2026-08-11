# SPDX-License-Identifier: Apache-2.0
"""Password hashing using scrypt (stdlib hashlib).

Storage format:
    ``scrypt$N$r$p$<salt-b64-standard>$<hash-b64-standard>``

Standard (not URL-safe) base64 is used for both salt and hash.  The
``$`` field delimiter is not in the base64 alphabet (``A-Za-z0-9+/``),
so there is no ambiguity between the delimiter and the encoded bytes.
This diverges from :mod:`lmchat.utils.encryption` which uses URL-safe
base64; the choice is intentional (standard base64 for ``$``-delimited
storage formats avoids ambiguity with the field delimiter).

Cost parameters follow the OWASP Password Storage Cheat Sheet (2024+):
N=2^17, r=8, p=1 gives ~128 MiB working memory and ≈50 ms on commodity
2026 hardware.  The :func:`needs_rehash` check enables transparent
migration to higher defaults on successful login.
"""
from __future__ import annotations

import base64
import secrets
from hashlib import scrypt
from typing import Final

from lmchat.logging import get_logger

# OWASP 2024+ minimum profile: N=2^17, r=8, p=1.
# Higher N => more memory hardness; p multiplies compute without adding
# memory — p=1 is standard; p=3 is the alternative profile.
DEFAULT_N: Final[int] = 2**17
DEFAULT_R: Final[int] = 8
DEFAULT_P: Final[int] = 1

_SALT_BYTES: Final[int] = 16
_HASH_DKLEN: Final[int] = 32
_ENVELOPE_FIELDS: Final[int] = 6  # "scrypt" + N + r + p + salt + hash

# Python's hashlib.scrypt defaults maxmem=32 MiB. The OWASP 2024 profile
# (N=2^17, r=8) needs 128 * N * r = 128 * 131072 * 8 = 128 MiB. Without
# an explicit maxmem the call fails with "memory limit exceeded"
# ([digital envelope routines] error from OpenSSL). 256 MiB gives
# headroom for higher-N profiles admins may dial up via the (n, r, p)
# kwargs. Caught during admin-equivalent validation of v1.0.1
# register flow.
_MAXMEM: Final[int] = 256 * 1024 * 1024

log = get_logger(__name__)


def hash_password(
    password: str,
    *,
    n: int = DEFAULT_N,
    r: int = DEFAULT_R,
    p: int = DEFAULT_P,
) -> str:
    """Hash *password* with scrypt and return a self-describing envelope string.

    The envelope encodes all cost parameters and the salt so that
    :func:`verify_password` and :func:`needs_rehash` need no external
    metadata.

    Args:
        password:  Plaintext password to hash.
        n:         CPU/memory cost factor.  Must be a power of two.
        r:         Block size parameter.
        p:         Parallelisation parameter.

    Returns:
        A string of the form
        ``"scrypt$N$r$p$<base64-salt>$<base64-hash>"``.
    """
    salt = secrets.token_bytes(_SALT_BYTES)
    h = scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=_HASH_DKLEN, maxmem=_MAXMEM)
    salt_b64 = base64.b64encode(salt).decode()
    hash_b64 = base64.b64encode(h).decode()
    return f"scrypt${n}${r}${p}${salt_b64}${hash_b64}"


def verify_password(password: str, stored: str) -> bool:
    """Return ``True`` if *password* matches the *stored* hash envelope.

    Uses :func:`secrets.compare_digest` to prevent timing attacks.

    Args:
        password:  Plaintext candidate password.
        stored:    Envelope string produced by :func:`hash_password`.

    Returns:
        ``True`` if the password matches, ``False`` otherwise.

    Raises:
        ValueError:  If *stored* is not a valid scrypt envelope (wrong
                     number of fields, non-integer cost params, or
                     bad base64).
    """
    try:
        fields = stored.split("$")
        if len(fields) != _ENVELOPE_FIELDS or fields[0] != "scrypt":
            raise ValueError(f"invalid scrypt envelope: wrong format ({len(fields)} fields)")
        _, n_str, r_str, p_str, salt_b64, hash_b64 = fields
        n = int(n_str)
        r = int(r_str)
        p = int(p_str)
        salt = base64.b64decode(salt_b64)
        expected_hash = base64.b64decode(hash_b64)
    except (ValueError, UnicodeDecodeError, Exception) as exc:
        raise ValueError(f"corrupt scrypt envelope: {exc}") from exc

    candidate = scrypt(
        password.encode(), salt=salt, n=n, r=r, p=p, dklen=_HASH_DKLEN, maxmem=_MAXMEM,
    )
    return secrets.compare_digest(expected_hash, candidate)


def needs_rehash(stored: str) -> bool:
    """Return ``True`` if *stored* was hashed at lower cost than the current defaults.

    Called by the auth service after a successful password verification.
    When this returns ``True``, the service re-hashes the password at
    the current default cost and updates the database row transparently.

    Args:
        stored:  Envelope string produced by :func:`hash_password`.

    Returns:
        ``True`` if any cost parameter is strictly below the current
        default; ``False`` if all parameters are at or above defaults.

    Raises:
        ValueError:  If *stored* is not a valid scrypt envelope.
    """
    try:
        fields = stored.split("$")
        if len(fields) != _ENVELOPE_FIELDS or fields[0] != "scrypt":
            raise ValueError(f"invalid scrypt envelope: wrong format ({len(fields)} fields)")
        _, n_str, r_str, p_str, _salt, _hash = fields
        n = int(n_str)
        r = int(r_str)
        p = int(p_str)
    except (ValueError, UnicodeDecodeError, Exception) as exc:
        raise ValueError(f"corrupt scrypt envelope: {exc}") from exc

    stored_cost = (n, r, p)
    default_cost = (DEFAULT_N, DEFAULT_R, DEFAULT_P)
    if stored_cost < default_cost:
        log.warning(
            "password needs rehash",
            stored_n=n,
            stored_r=r,
            stored_p=p,
            default_n=DEFAULT_N,
            default_r=DEFAULT_R,
            default_p=DEFAULT_P,
        )
        return True
    return False
