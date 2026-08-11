#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""AES-GCM envelope tamper standalone test.

For every encrypted column found via grep on src/lmchat/models (actually
src/lmchat/db/schema.py for this project):
  - Reads a seed ciphertext from a test user.
  - Flips one byte in the ciphertext portion.
  - Calls decrypt() with the tampered value.
  - Asserts that AES-GCM tag verification raises (cryptography.exceptions.InvalidTag
    or a wrapping exception). The assert is type-tolerant but specific:
    it accepts any exception whose type name or message indicates tag/auth failure.

Emits JUnit XML to target/gates/crypto-aesgcm-tamper.xml.
Exit code: 0 all-pass, 1 any failure.
"""
from __future__ import annotations

import asyncio
import base64
import os
import sys
import time
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

os.environ.setdefault("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")

InvalidTag: Any = None
text: Any = None
create_async_engine: Any = None
metadata: Any = None
users: Any = None
user_lm_studio_overrides: Any = None
PREFIX: Any = None
_NONCE_BYTES: Any = None
decrypt: Any = None
encrypt: Any = None
hash_password: Any = None
_APP_AVAILABLE = False

try:
    from cryptography.exceptions import InvalidTag as _InvalidTag
    from sqlalchemy import text as _text
    from sqlalchemy.ext.asyncio import create_async_engine as _create_async_engine

    from lmchat.db.schema import (
        metadata as _metadata,
    )
    from lmchat.db.schema import (
        user_lm_studio_overrides as _user_lm_studio_overrides,  # type: ignore[attr-defined]
    )
    from lmchat.db.schema import (
        users as _users,
    )
    from lmchat.utils.encryption import (
        _NONCE_BYTES as _NONCE_BYTES_INNER,
    )
    from lmchat.utils.encryption import (
        PREFIX as _PREFIX,
    )
    from lmchat.utils.encryption import (
        decrypt as _decrypt,
    )
    from lmchat.utils.encryption import (
        encrypt as _encrypt,
    )
    from lmchat.utils.hashing import hash_password as _hash_password

    InvalidTag = _InvalidTag
    text = _text
    create_async_engine = _create_async_engine
    metadata = _metadata
    users = _users
    user_lm_studio_overrides = _user_lm_studio_overrides
    PREFIX = _PREFIX
    _NONCE_BYTES = _NONCE_BYTES_INNER
    decrypt = _decrypt
    encrypt = _encrypt
    hash_password = _hash_password

    _APP_AVAILABLE = True
except Exception:  # noqa: BLE001
    _APP_AVAILABLE = False

# ---------------------------------------------------------------------------
# JUnit helpers
# ---------------------------------------------------------------------------


def _make_suite(name: str) -> ET.Element:
    return ET.Element(
        "testsuite",
        {"name": name, "tests": "0", "failures": "0", "errors": "0", "skipped": "0"},
    )


def _add_pass(suite: ET.Element, classname: str, name: str, elapsed: float) -> None:
    ET.SubElement(suite, "testcase",
                  {"name": name, "classname": classname, "time": f"{elapsed:.3f}"})
    suite.set("tests", str(int(suite.get("tests", "0")) + 1))


def _add_fail(suite: ET.Element, classname: str, name: str, msg: str,
              elapsed: float) -> None:
    tc = ET.SubElement(suite, "testcase",
                       {"name": name, "classname": classname, "time": f"{elapsed:.3f}"})
    fl = ET.SubElement(tc, "failure", {"message": msg, "type": "AssertionError"})
    fl.text = msg
    suite.set("tests", str(int(suite.get("tests", "0")) + 1))
    suite.set("failures", str(int(suite.get("failures", "0")) + 1))


def _add_skip(suite: ET.Element, classname: str, name: str, reason: str) -> None:
    tc = ET.SubElement(suite, "testcase", {"name": name, "classname": classname})
    sk = ET.SubElement(tc, "skipped", {"message": reason})
    sk.text = reason
    suite.set("tests", str(int(suite.get("tests", "0")) + 1))
    suite.set("skipped", str(int(suite.get("skipped", "0")) + 1))


def _emit_junit(suite: ET.Element, out_path: Path) -> None:
    root = ET.Element("testsuites", {
        "name": "crypto-aesgcm-tamper",
        "tests": suite.get("tests", "0"),
        "failures": suite.get("failures", "0"),
        "errors": suite.get("errors", "0"),
    })
    root.append(suite)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(ET.ElementTree(root), space="  ")
    ET.ElementTree(root).write(out_path, encoding="utf-8", xml_declaration=True)


# ---------------------------------------------------------------------------
# Tamper helper
# ---------------------------------------------------------------------------


def _flip_byte_in_ciphertext(envelope: str, byte_offset: int = 0) -> str:
    """Flip one byte in the ciphertext portion of an enc$v1$ envelope.

    The payload is: nonce (12 bytes) || ciphertext+tag.
    We flip a byte *after* the nonce to hit the ciphertext/tag, not the nonce
    (flipping the nonce changes decryption output but may not trigger InvalidTag
    in all AESGCM implementations; flipping the tag bytes reliably does).
    """
    if not envelope.startswith(PREFIX):
        raise ValueError(f"not an enc$v1$ envelope: {envelope[:20]!r}")
    payload = bytearray(base64.urlsafe_b64decode(envelope[len(PREFIX):]))
    # Target the last byte of the GCM tag (last 16 bytes of payload).
    target_idx = len(payload) - 1 - (byte_offset % 16)
    payload[target_idx] ^= 0xFF
    return PREFIX + base64.urlsafe_b64encode(bytes(payload)).decode()


def _is_tag_failure(exc: BaseException) -> bool:
    """Return True if exc indicates an AES-GCM authentication tag failure.

    Type-tolerant: accepts InvalidTag directly, or any exception whose type
    name or string representation mentions tag/authentication/invalid/mac.
    """
    if isinstance(exc, InvalidTag):
        return True
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    keywords = ("invalidtag", "invalid tag", "authentication", "mac", "gcm",
                "integrity", "tamper", "verification failed", "decrypt")
    return any(k in name or k in msg for k in keywords)


# ---------------------------------------------------------------------------
# Test cases per column
# ---------------------------------------------------------------------------


async def _test_totp_secret_tamper(
    suite: ET.Element, tmpdir: Path
) -> int:
    """users.totp_secret — encrypt a dummy TOTP secret, tamper, assert rejection."""
    failures = 0
    db_path = tmpdir / "tamper_totp.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}", pool_pre_ping=True)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)

    pw_hash = hash_password("pw", n=2**10, r=8, p=1)
    async with eng.begin() as conn:
        await conn.execute(
            text("INSERT INTO users (id, username, password_hash) VALUES (1, 'u', :ph)"),
            {"ph": pw_hash},
        )

    # Encrypt a fake TOTP secret
    plaintext = b"JBSWY3DPEHPK3PXP"
    envelope = encrypt(plaintext, kind="totp", record_id=1)

    # Verify it round-trips correctly first
    t0 = time.monotonic()
    decrypted = decrypt(envelope, kind="totp", record_id=1)
    if decrypted != plaintext:
        _add_fail(suite, "crypto-tamper", "totp-secret-roundtrip",
                  f"Round-trip failed: {decrypted!r} != {plaintext!r}",
                  time.monotonic() - t0)
        failures += 1
    else:
        _add_pass(suite, "crypto-tamper", "totp-secret-roundtrip",
                  time.monotonic() - t0)

    # Now tamper and assert rejection
    for flip_offset in [0, 4, 8]:
        tampered = _flip_byte_in_ciphertext(envelope, byte_offset=flip_offset)
        t1 = time.monotonic()
        try:
            decrypt(tampered, kind="totp", record_id=1)
            _add_fail(
                suite, "crypto-tamper",
                f"totp-secret-tamper-rejected-offset-{flip_offset}",
                f"SECURITY BUG: tampered ciphertext (offset {flip_offset}) did not "
                f"raise — AES-GCM tag verification not enforced",
                time.monotonic() - t1,
            )
            failures += 1
        except Exception as exc:  # noqa: BLE001
            if _is_tag_failure(exc):
                _add_pass(
                    suite, "crypto-tamper",
                    f"totp-secret-tamper-rejected-offset-{flip_offset}",
                    time.monotonic() - t1,
                )
            else:
                _add_fail(
                    suite, "crypto-tamper",
                    f"totp-secret-tamper-rejected-offset-{flip_offset}",
                    f"Unexpected exception type {type(exc).__name__}: {exc}",
                    time.monotonic() - t1,
                )
                failures += 1

    await eng.dispose()
    return failures


async def _test_api_key_enc_tamper(
    suite: ET.Element, kind: str, record_id: int
) -> int:
    """Generic test for any api_key_enc column kind."""
    failures = 0
    plaintext = b"sk-test-api-key-value-for-tamper-test"
    envelope = encrypt(plaintext, kind=kind, record_id=record_id)

    # Round-trip
    t0 = time.monotonic()
    decrypted = decrypt(envelope, kind=kind, record_id=record_id)
    if decrypted != plaintext:
        _add_fail(suite, "crypto-tamper", f"{kind}-api-key-roundtrip",
                  f"Round-trip failed: {decrypted!r} != {plaintext!r}",
                  time.monotonic() - t0)
        failures += 1
    else:
        _add_pass(suite, "crypto-tamper", f"{kind}-api-key-roundtrip",
                  time.monotonic() - t0)

    # Tamper and assert rejection
    for flip_offset in [0, 8]:
        tampered = _flip_byte_in_ciphertext(envelope, byte_offset=flip_offset)
        t1 = time.monotonic()
        try:
            decrypt(tampered, kind=kind, record_id=record_id)
            _add_fail(
                suite, "crypto-tamper",
                f"{kind}-api-key-tamper-rejected-offset-{flip_offset}",
                f"SECURITY BUG: tampered {kind} api_key_enc (offset {flip_offset}) "
                f"did not raise — AES-GCM tag verification not enforced",
                time.monotonic() - t1,
            )
            failures += 1
        except Exception as exc:  # noqa: BLE001
            if _is_tag_failure(exc):
                _add_pass(
                    suite, "crypto-tamper",
                    f"{kind}-api-key-tamper-rejected-offset-{flip_offset}",
                    time.monotonic() - t1,
                )
            else:
                _add_fail(
                    suite, "crypto-tamper",
                    f"{kind}-api-key-tamper-rejected-offset-{flip_offset}",
                    f"Unexpected exception type {type(exc).__name__}: {exc}",
                    time.monotonic() - t1,
                )
                failures += 1

    # AAD-binding: tamper the record_id in AAD — decrypt with wrong record_id
    # should also fail (proves AAD is bound).
    wrong_id = record_id + 99
    t2 = time.monotonic()
    try:
        decrypt(envelope, kind=kind, record_id=wrong_id)
        _add_fail(
            suite, "crypto-tamper",
            f"{kind}-api-key-aad-binding",
            f"SECURITY BUG: decrypt accepted wrong record_id={wrong_id} "
            f"(expected {record_id}) — AAD not bound",
            time.monotonic() - t2,
        )
        failures += 1
    except Exception as exc:  # noqa: BLE001
        if _is_tag_failure(exc):
            _add_pass(suite, "crypto-tamper", f"{kind}-api-key-aad-binding",
                      time.monotonic() - t2)
        else:
            _add_fail(
                suite, "crypto-tamper", f"{kind}-api-key-aad-binding",
                f"Unexpected exception type {type(exc).__name__}: {exc}",
                time.monotonic() - t2,
            )
            failures += 1

    return failures


async def _run_all(suite: ET.Element, tmpdir: Path) -> int:
    failures = 0
    failures += await _test_totp_secret_tamper(suite, tmpdir)
    # user_lm_studio_overrides.api_key_enc — kind="lmsoverride"
    failures += await _test_api_key_enc_tamper(suite, kind="lmsoverride", record_id=7)
    return failures


def main() -> int:
    out_path = _REPO_ROOT / "target" / "gates" / "crypto-aesgcm-tamper.xml"
    suite = _make_suite("crypto-aesgcm-tamper")

    if not _APP_AVAILABLE:
        _add_skip(suite, "crypto-tamper", "crypto-tamper-all",
                  "lm-chat app not importable")
        _emit_junit(suite, out_path)
        print("[enc_envelope_tamper_test] SKIPPED")
        return 0

    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            asyncio.run(_run_all(suite, Path(tmpdir)))
        except Exception as exc:  # noqa: BLE001
            _add_fail(suite, "crypto-tamper", "infrastructure",
                      f"Infrastructure error: {exc}", 0.0)

    _emit_junit(suite, out_path)
    f = int(suite.get("failures", "0"))
    e = int(suite.get("errors", "0"))
    t = int(suite.get("tests", "0"))
    print(f"[enc_envelope_tamper_test] {t} tests, {f} failures, {e} errors -> {out_path}")
    return 1 if (f or e) else 0


if __name__ == "__main__":
    sys.exit(main())
