# SPDX-License-Identifier: Apache-2.0
"""Unit tests for IntegrationSetEntry input validation.

LLM07 #169 — ``IntegrationSetEntry.value`` previously accepted script tags,
shell metachars, path traversal, null bytes, CRLF, oversized values, and
whitespace-only strings.  The ``field_validator("value")`` added by fix #169
enforces a conservative allowlist regex; these tests cover every adversarial
payload class identified by the P15e harness.

Payload classes (mirrored from test_llm07_mcp_preset_injection.py):
  1.  Script-tag injection
  2.  HTML attribute injection
  3.  Shell metachar — backtick
  4.  Shell metachar — dollar-paren
  5.  Shell metachar — pipe
  6.  Shell metachar — semicolon
  7.  Path traversal — relative (..)
  8.  Path traversal — absolute (/)
  9.  Null byte
  10. Newline injection (log forgery)
  11. URL with credentials
  12. Pure whitespace
  13. Empty string
  14. Excessive length (>256)
  15. CRLF injection

Benign classes that MUST be accepted:
  - mcp/<name> style IDs
  - hyphenated names
  - single-char names
  - names with dots, colons, underscores
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from lmchat.services.integrations_service import IntegrationSetEntry

# ---------------------------------------------------------------------------
# Payload sets — reuse the same sets from test_llm07_mcp_preset_injection
# to guarantee parity.
# ---------------------------------------------------------------------------

DANGEROUS_PRESETS = [
    "mcp/<script>alert(1)</script>",
    'mcp/x" onerror="alert(1)',
    "mcp/`whoami`",
    "mcp/$(whoami)",
    "mcp/foo | rm -rf /",
    "mcp/foo; cat /etc/passwd",
    "mcp/../../etc/passwd",
    "/etc/passwd",
    "mcp/foo\x00bar",
    "mcp/foo\nFAKE: forged log line",
    "http://user:pass@evil.test/mcp",
    "   ",
    "",
    "mcp/" + ("a" * 4096),
    "mcp/foo\r\nSet-Cookie: x=evil",
]

BENIGN_PRESETS = [
    "mcp/searxng",
    "mcp/filesystem",
    "lmstudio-default",
    "company-internal-mcp",
    "x",
    "mcp/foo_bar-baz.123",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _try_create(value: str) -> bool:
    """Return True if IntegrationSetEntry(value=value) validates."""
    try:
        IntegrationSetEntry(value=value, sort_order=0)
        return True
    except (ValidationError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Dangerous payloads — all MUST be rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", DANGEROUS_PRESETS)
def test_rejects_dangerous_value(payload: str) -> None:
    """IntegrationSetEntry MUST reject every dangerous payload with ValidationError."""
    with pytest.raises((ValidationError, ValueError)):
        IntegrationSetEntry(value=payload, sort_order=0)


# ---------------------------------------------------------------------------
# Benign values — all MUST be accepted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", BENIGN_PRESETS)
def test_accepts_benign_value(payload: str) -> None:
    """IntegrationSetEntry MUST accept legitimate MCP integration names."""
    entry = IntegrationSetEntry(value=payload, sort_order=0)
    assert entry.value == payload


# ---------------------------------------------------------------------------
# Individual invariant checks (explicit coverage, not just parametric)
# ---------------------------------------------------------------------------


def test_rejects_empty_string() -> None:
    with pytest.raises((ValidationError, ValueError)):
        IntegrationSetEntry(value="", sort_order=0)


def test_rejects_whitespace_only() -> None:
    with pytest.raises((ValidationError, ValueError)):
        IntegrationSetEntry(value="   ", sort_order=0)


def test_rejects_null_byte() -> None:
    with pytest.raises((ValidationError, ValueError)):
        IntegrationSetEntry(value="mcp/foo\x00bar", sort_order=0)


def test_rejects_newline() -> None:
    with pytest.raises((ValidationError, ValueError)):
        IntegrationSetEntry(value="mcp/foo\nbar", sort_order=0)


def test_rejects_crlf() -> None:
    with pytest.raises((ValidationError, ValueError)):
        IntegrationSetEntry(value="mcp/foo\r\nbar", sort_order=0)


def test_rejects_script_tag() -> None:
    with pytest.raises((ValidationError, ValueError)):
        IntegrationSetEntry(value="mcp/<script>", sort_order=0)


def test_rejects_path_traversal_relative() -> None:
    with pytest.raises((ValidationError, ValueError)):
        IntegrationSetEntry(value="mcp/../../etc/passwd", sort_order=0)


def test_rejects_path_traversal_absolute() -> None:
    with pytest.raises((ValidationError, ValueError)):
        IntegrationSetEntry(value="/etc/passwd", sort_order=0)


def test_rejects_oversized_value() -> None:
    with pytest.raises((ValidationError, ValueError)):
        IntegrationSetEntry(value="a" * 257, sort_order=0)


def test_accepts_boundary_length_256() -> None:
    """Exactly 256 chars of allowed characters must pass."""
    value = ("mcp/" + "a" * 252)[:256]
    entry = IntegrationSetEntry(value=value, sort_order=0)
    assert len(entry.value) == 256


def test_accepts_single_char() -> None:
    entry = IntegrationSetEntry(value="x", sort_order=0)
    assert entry.value == "x"


def test_rejects_shell_pipe() -> None:
    with pytest.raises((ValidationError, ValueError)):
        IntegrationSetEntry(value="mcp/foo | bar", sort_order=0)


def test_rejects_shell_semicolon() -> None:
    with pytest.raises((ValidationError, ValueError)):
        IntegrationSetEntry(value="mcp/foo;bar", sort_order=0)


def test_rejects_backtick() -> None:
    with pytest.raises((ValidationError, ValueError)):
        IntegrationSetEntry(value="mcp/`whoami`", sort_order=0)


def test_rejects_dollar_paren() -> None:
    with pytest.raises((ValidationError, ValueError)):
        IntegrationSetEntry(value="mcp/$(id)", sort_order=0)
