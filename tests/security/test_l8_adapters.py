# SPDX-License-Identifier: Apache-2.0
"""Unit tests for P15g tool scripts: parsing, aggregation, and skip paths.

Tests the logic of the four emitter tools without requiring external network,
running services, or the lm-chat application stack.

Per task spec: 15-20 tests, covering missing-dep skip paths, JUnit XML
structure, combine logic, and the tamper/flip helpers.

All tests pass without external network or services.
"""
from __future__ import annotations

import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

# Ensure repo root on sys.path so tools/ imports work
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------


def _parse_xml(path: Path) -> ET.Element:
    return ET.parse(path).getroot()


def _make_simple_junit(name: str, tests: int, failures: int) -> str:
    return (
        f'<?xml version="1.0" encoding="utf-8"?>'
        f'<testsuites name="{name}" tests="{tests}" failures="{failures}" errors="0">'
        f'<testsuite name="{name}" tests="{tests}" failures="{failures}" errors="0" skipped="0">'
        + (
            "".join(
                f'<testcase name="tc{i}" classname="cls"/>'
                for i in range(tests - failures)
            )
        )
        + (
            "".join(
                f'<testcase name="fail{i}" classname="cls">'
                f'<failure message="bad" type="A">bad</failure></testcase>'
                for i in range(failures)
            )
        )
        + "</testsuite></testsuites>"
    )


# ---------------------------------------------------------------------------
# Tests: enc_envelope_tamper_test helpers
# ---------------------------------------------------------------------------


def test_flip_byte_produces_different_envelope() -> None:
    """_flip_byte_in_ciphertext must change the envelope value."""
    import os
    os.environ.setdefault("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.utils.encryption import encrypt
    from tools.enc_envelope_tamper_test import _flip_byte_in_ciphertext

    envelope = encrypt(b"hello world", kind="test", record_id=1)
    tampered = _flip_byte_in_ciphertext(envelope, byte_offset=0)
    assert tampered != envelope


def test_flip_byte_preserves_prefix() -> None:
    """Tampered envelope must still start with enc$v1$ prefix."""
    import os
    os.environ.setdefault("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.utils.encryption import encrypt
    from tools.enc_envelope_tamper_test import _flip_byte_in_ciphertext

    envelope = encrypt(b"test data", kind="test", record_id=2)
    tampered = _flip_byte_in_ciphertext(envelope, byte_offset=4)
    assert tampered.startswith("enc$v1$")


def test_flip_byte_non_prefix_raises_valueerror() -> None:
    """_flip_byte_in_ciphertext must raise ValueError for non-envelope input."""
    from tools.enc_envelope_tamper_test import _flip_byte_in_ciphertext

    with pytest.raises(ValueError, match="enc\\$v1\\$"):
        _flip_byte_in_ciphertext("not-an-envelope", byte_offset=0)


def test_is_tag_failure_on_invalid_tag() -> None:
    """_is_tag_failure must return True for cryptography.exceptions.InvalidTag."""
    from cryptography.exceptions import InvalidTag

    from tools.enc_envelope_tamper_test import _is_tag_failure

    assert _is_tag_failure(InvalidTag())


def test_is_tag_failure_on_value_error_with_authentication() -> None:
    """_is_tag_failure must return True for ValueError mentioning 'authentication'."""
    from tools.enc_envelope_tamper_test import _is_tag_failure

    assert _is_tag_failure(ValueError("authentication tag mismatch"))


def test_is_tag_failure_on_unrelated_exception() -> None:
    """_is_tag_failure must return False for unrelated exceptions."""
    from tools.enc_envelope_tamper_test import _is_tag_failure

    assert not _is_tag_failure(ValueError("some other error"))
    assert not _is_tag_failure(RuntimeError("connection refused"))


def test_tamper_test_emits_skipped_when_app_unavailable(tmp_path: Path) -> None:
    """When _APP_AVAILABLE is False, main() emits skipped XML and returns 0."""
    import unittest.mock as mock

    # Patch _APP_AVAILABLE to False in the module
    import tools.enc_envelope_tamper_test as mod
    with mock.patch.object(mod, "_APP_AVAILABLE", False):
        with mock.patch.object(
            mod, "_REPO_ROOT", tmp_path
        ):
            rc = mod.main()
    assert rc == 0
    out = tmp_path / "target" / "gates" / "crypto-aesgcm-tamper.xml"
    assert out.is_file()
    root = _parse_xml(out)
    skipped = root.findall(".//skipped")
    assert len(skipped) > 0


# ---------------------------------------------------------------------------
# Tests: session_fixation_test helpers
# ---------------------------------------------------------------------------


def test_session_fixation_emits_skipped_when_app_unavailable(tmp_path: Path) -> None:
    """When _APP_AVAILABLE is False, session fixation emits skipped XML."""
    import unittest.mock as mock

    import tools.session_fixation_test as mod

    with mock.patch.object(mod, "_APP_AVAILABLE", False):
        with mock.patch.object(mod, "_REPO_ROOT", tmp_path):
            rc = mod.main()
    assert rc == 0
    out = tmp_path / "target" / "gates" / "auth-session-fixation.xml"
    assert out.is_file()
    root = _parse_xml(out)
    assert root.findall(".//skipped")


# ---------------------------------------------------------------------------
# Tests: totp_race_test helpers
# ---------------------------------------------------------------------------


def test_totp_race_emits_skipped_when_app_unavailable(tmp_path: Path) -> None:
    """When _APP_AVAILABLE is False, TOTP race test emits skipped XML."""
    import unittest.mock as mock

    import tools.totp_race_test as mod

    with mock.patch.object(mod, "_APP_AVAILABLE", False):
        with mock.patch.object(mod, "_REPO_ROOT", tmp_path):
            rc = mod.main()
    assert rc == 0
    out = tmp_path / "target" / "gates" / "auth-totp-race.xml"
    assert out.is_file()
    root = _parse_xml(out)
    assert root.findall(".//skipped")


# ---------------------------------------------------------------------------
# Tests: junit_from_auth_session combiner
# ---------------------------------------------------------------------------


def test_combiner_merges_all_sub_xmls(tmp_path: Path) -> None:
    """Combiner merges three sub-XML files into a single testsuites root."""
    import unittest.mock as mock

    gates = tmp_path / "target" / "gates"
    gates.mkdir(parents=True)

    # Write four synthetic sub-XMLs (matching _TOOLS in junit_from_auth_session.py)
    for fname, tests, failures in [
        ("auth-session-fixation.xml", 2, 0),
        ("auth-totp-race.xml", 3, 0),
        ("crypto-aesgcm-tamper.xml", 4, 0),
        ("L8-xff-default-deny.xml", 1, 0),
    ]:
        (gates / fname).write_text(_make_simple_junit(fname, tests, failures),
                                   encoding="utf-8")

    import tools.junit_from_auth_session as mod

    with mock.patch.object(mod, "_GATES_DIR", gates):
        with mock.patch.object(mod, "_OUT_XML", gates / "L8-auth-session.xml"):
            # Patch subprocess to avoid running the real tools
            with mock.patch("tools.junit_from_auth_session.subprocess.run") as mr:
                mr.return_value = mock.Mock(returncode=0)
                rc = mod.main()

    assert rc == 0
    out = gates / "L8-auth-session.xml"
    assert out.is_file()
    root = _parse_xml(out)
    total_tests = int(root.get("tests", "0"))
    assert total_tests == 10  # 2 + 3 + 4 + 1


def test_combiner_reports_failure_when_tool_fails(tmp_path: Path) -> None:
    """Combiner returns non-zero exit when a sub-tool exits non-zero."""
    import unittest.mock as mock

    gates = tmp_path / "target" / "gates"
    gates.mkdir(parents=True)

    for fname, tests, failures in [
        ("auth-session-fixation.xml", 2, 1),
        ("auth-totp-race.xml", 3, 0),
        ("crypto-aesgcm-tamper.xml", 4, 0),
    ]:
        (gates / fname).write_text(_make_simple_junit(fname, tests, failures),
                                   encoding="utf-8")

    import tools.junit_from_auth_session as mod

    with mock.patch.object(mod, "_GATES_DIR", gates):
        with mock.patch.object(mod, "_OUT_XML", gates / "L8-auth-session.xml"):
            with mock.patch("tools.junit_from_auth_session.subprocess.run") as mr:
                mr.return_value = mock.Mock(returncode=1)
                rc = mod.main()

    assert rc != 0


def test_combiner_handles_missing_sub_xml(tmp_path: Path) -> None:
    """Combiner emits a skipped placeholder when a sub-XML is missing."""
    import unittest.mock as mock

    gates = tmp_path / "target" / "gates"
    gates.mkdir(parents=True)

    # Only write one of three sub-XMLs
    (gates / "auth-session-fixation.xml").write_text(
        _make_simple_junit("auth-session-fixation.xml", 2, 0), encoding="utf-8"
    )

    import tools.junit_from_auth_session as mod

    with mock.patch.object(mod, "_GATES_DIR", gates):
        with mock.patch.object(mod, "_OUT_XML", gates / "L8-auth-session.xml"):
            with mock.patch("tools.junit_from_auth_session.subprocess.run") as mr:
                mr.return_value = mock.Mock(returncode=0)
                mod.main()

    out = gates / "L8-auth-session.xml"
    assert out.is_file()
    root = _parse_xml(out)
    skipped = root.findall(".//skipped")
    assert len(skipped) >= 2  # two missing files


def test_combiner_output_is_valid_junit_xml(tmp_path: Path) -> None:
    """Combined output must have <testsuites> root with required attributes."""
    import unittest.mock as mock

    gates = tmp_path / "target" / "gates"
    gates.mkdir(parents=True)
    for fname in [
        "auth-session-fixation.xml",
        "auth-totp-race.xml",
        "crypto-aesgcm-tamper.xml",
    ]:
        (gates / fname).write_text(
            _make_simple_junit(fname, 1, 0), encoding="utf-8"
        )

    import tools.junit_from_auth_session as mod

    with mock.patch.object(mod, "_GATES_DIR", gates):
        with mock.patch.object(mod, "_OUT_XML", gates / "L8-auth-session.xml"):
            with mock.patch("tools.junit_from_auth_session.subprocess.run") as mr:
                mr.return_value = mock.Mock(returncode=0)
                mod.main()

    root = _parse_xml(gates / "L8-auth-session.xml")
    assert root.tag == "testsuites"
    assert "tests" in root.attrib
    assert "failures" in root.attrib
    assert "errors" in root.attrib


# ---------------------------------------------------------------------------
# Tests: scrypt constant
# ---------------------------------------------------------------------------


def test_scrypt_constant_value_is_131072() -> None:
    """DEFAULT_N must equal 131072 (2^17) — matches test_scrypt_cost_factor."""
    from lmchat.utils.hashing import DEFAULT_N

    assert DEFAULT_N == 131072


def test_enc_prefix_constant() -> None:
    """enc$v1$ PREFIX must be the known value."""
    import os
    os.environ.setdefault("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.utils.encryption import PREFIX

    assert PREFIX == "enc$v1$"
