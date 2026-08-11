# SPDX-License-Identifier: Apache-2.0
"""At-rest encryption using the versioned ``enc$v1$`` envelope.

Construction:
    KDF:     SHAKE-256(LM_CHAT_SECRET | "|" | kind | "|" | record_id) → 32 bytes
    Cipher:  AES-256-GCM, 12-byte random nonce
    AAD:     b"enc$v1$" + kind + "|" + str(record_id)
    Wire:    "enc$v1$" + base64url(nonce || ciphertext_with_tag)

The ``$`` character is not in the base64url alphabet (``A-Za-z0-9-_``),
so an enc$v1$-prefixed token can never be produced by base64url encoding
alone — the prefix is unambiguous.

Transparent migration: :func:`decrypt` called on a value that does *not*
start with ``enc$v1$`` returns the value encoded as bytes.  This allows
existing bare-text rows to pass through without a bulk re-encryption step.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from typing import Final

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import ValidationError

from lmchat.config import get_settings
from lmchat.logging import get_logger

PREFIX: Final[str] = "enc$v1$"
_NONCE_BYTES: Final[int] = 12
_KEY_BYTES: Final[int] = 32

log = get_logger(__name__)


def encrypt(plaintext: bytes, *, kind: str, record_id: int) -> str:
    """Encrypt *plaintext* and return a versioned ``enc$v1$`` envelope string.

    The AES-256-GCM key is derived per-record from ``LM_CHAT_SECRET``
    using SHAKE-256.  The AAD binds the ``kind`` and ``record_id`` so
    that a ciphertext from one record cannot be transplanted into another
    without GCM tag failure.

    Args:
        plaintext:  Raw bytes to encrypt.
        kind:       Column / domain tag (e.g. ``"totp"``).  Must not
                    contain ``"|"`` to preserve the KDF domain separation.
        record_id:  Database primary key of the row being encrypted.

    Returns:
        An opaque string beginning with ``"enc$v1$"`` that can be stored
        in a text column.

    Raises:
        RuntimeError:  If ``LM_CHAT_SECRET`` is not set.
        ValueError:    If ``kind`` contains the ``"|"`` separator character.
    """
    if "|" in kind:
        raise ValueError(f"kind must not contain '|', got: {kind!r}")

    key = _derive_key(kind, record_id)
    nonce = secrets.token_bytes(_NONCE_BYTES)
    aad = f"{PREFIX}{kind}|{record_id}".encode()
    ct = AESGCM(key).encrypt(nonce, plaintext, aad)
    return PREFIX + base64.urlsafe_b64encode(nonce + ct).decode()


def decrypt(token: str, *, kind: str, record_id: int) -> bytes:
    """Decrypt an ``enc$v1$`` envelope, or pass through bare-text rows.

    If *token* does not start with ``"enc$v1$"``, the value is returned
    as ``token.encode()`` unchanged — this is the transparent-migration
    path for rows written before encryption was introduced.

    Args:
        token:      Value read from the database column.
        kind:       Must match the ``kind`` used during :func:`encrypt`.
        record_id:  Must match the ``record_id`` used during :func:`encrypt`.

    Returns:
        Decrypted plaintext bytes, or the bare-text row bytes for
        unencrypted legacy values.

    Raises:
        RuntimeError:                       If ``LM_CHAT_SECRET`` is not set.
        cryptography.exceptions.InvalidTag: If the ciphertext, tag, or AAD
                                            has been tampered with.
    """
    if not token.startswith(PREFIX):
        # Transparent migration: return bare-text as bytes, no logging needed.
        return token.encode()

    payload = base64.urlsafe_b64decode(token[len(PREFIX) :])
    nonce, ct = payload[:_NONCE_BYTES], payload[_NONCE_BYTES:]
    key = _derive_key(kind, record_id)
    aad = f"{PREFIX}{kind}|{record_id}".encode()
    return AESGCM(key).decrypt(nonce, ct, aad)


def _derive_key(kind: str, record_id: int) -> bytes:
    """Derive a 32-byte AES key via SHAKE-256.

    Domain-separates by ``kind`` and ``record_id`` so each record gets an
    independent key.  The ``LM_CHAT_SECRET`` is the root secret and must
    be set for any operation that calls this function.

    Args:
        kind:       Column / domain tag; must be stable for a given column.
        record_id:  Database primary key.

    Returns:
        32 bytes suitable for use as an AES-256 key.

    Raises:
        RuntimeError: If ``LM_CHAT_SECRET`` is falsy.
    """
    try:
        settings = get_settings()
    except ValidationError as exc:
        # LM_CHAT_SECRET is absent or empty — the validator raises ValidationError.
        # Re-raise as RuntimeError so callers (and tests) see a consistent
        # "missing secret" error type regardless of whether the setting fails
        # at the pydantic layer or the application layer.
        raise RuntimeError("LM_CHAT_SECRET required for encryption ops") from exc
    if not settings.lm_chat_secret:
        raise RuntimeError("LM_CHAT_SECRET required for encryption ops")

    shake = hashlib.shake_256()
    shake.update(settings.lm_chat_secret.encode())
    shake.update(b"|" + kind.encode() + b"|" + str(record_id).encode())
    return shake.digest(_KEY_BYTES)
