# SPDX-License-Identifier: Apache-2.0
"""LLM07 Insecure Plugin Design — MCP integrations preset name injection.

LLM07 — MCP preset injection tests:

    Test:           MCP preset name injection via bleach.clean() invariant
    Pass criterion: sanitization preserves visible text; strips dangerous markup

The MCP integrations list is **admin-supplied** (admin-only write path
at ``PUT /api/integrations/available`` per ``src/lmchat/routes/integrations.py``).
But the list is rendered in the chat composer's picker UI and the values
flow into the ``integrations`` field of the LM Studio ``/api/v1/chat``
request body.  Any preset ``value`` that contains shell metachars,
``<script>`` tags, traversal sequences, or null bytes is a vector for:

    1. XSS in the picker UI (if the renderer doesn't escape).
    2. Smuggled MCP server names into LM Studio.
    3. Log-injection in our structured logger.

This file asserts the **input validation contract** on
``IntegrationSetEntry.value``: the admin PUT MUST reject these payloads.

If the validator doesn't currently exist (the production model permits
``value: str`` with no regex), this test surfaces it as a finding — which
is the L7 gate working as intended.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from lmchat.services.integrations_service import IntegrationSetEntry

# ---------------------------------------------------------------------------
# Adversarial preset names that MUST be rejected.
# ---------------------------------------------------------------------------

DANGEROUS_PRESETS = [
    # 1. Script tag injection.
    "mcp/<script>alert(1)</script>",
    # 2. HTML attribute injection.
    'mcp/x" onerror="alert(1)',
    # 3. Shell metachars — backtick.
    "mcp/`whoami`",
    # 4. Shell metachars — dollar-paren.
    "mcp/$(whoami)",
    # 5. Shell metachars — pipe.
    "mcp/foo | rm -rf /",
    # 6. Shell metachars — semicolon.
    "mcp/foo; cat /etc/passwd",
    # 7. Path traversal — relative.
    "mcp/../../etc/passwd",
    # 8. Path traversal — absolute.
    "/etc/passwd",
    # 9. Null byte.
    "mcp/foo\x00bar",
    # 10. Newline injection (log forgery).
    "mcp/foo\nFAKE: forged log line",
    # 11. URL with credentials.
    "http://user:pass@evil.test/mcp",
    # 12. Pure whitespace.
    "   ",
    # 13. Empty.
    "",
    # 14. Excessive length (>256).
    "mcp/" + ("a" * 4096),
    # 15. CRLF injection.
    "mcp/foo\r\nSet-Cookie: x=evil",
]


# ---------------------------------------------------------------------------
# Benign preset names that MUST be accepted.
# ---------------------------------------------------------------------------

BENIGN_PRESETS = [
    "mcp/searxng",
    "mcp/filesystem",
    "lmstudio-default",
    "company-internal-mcp",
    "x",  # short single-token names
    "mcp/foo_bar-baz.123",
]


def _validates(value: str) -> bool:
    """Return True if the value passes the IntegrationSetEntry validator."""
    try:
        IntegrationSetEntry(value=value, sort_order=0)
    except (ValidationError, ValueError):
        return False
    return True


def _reference_is_safe(value: str) -> bool:
    """The contract: a preset value is safe iff it matches a conservative regex.

    The validator that ``IntegrationSetEntry.value`` SHOULD enforce.
    """
    import re

    if not value or not value.strip():
        return False
    if len(value) > 256:
        return False
    # Whitelist: alnum + a small set of separators commonly used in MCP IDs.
    if not re.fullmatch(r"[A-Za-z0-9_\-./:]{1,256}", value):
        return False
    # Block traversal.
    if "../" in value or value.startswith("/"):
        return False
    # Block control bytes.
    if any(c in value for c in ("\x00", "\n", "\r", "\t")):
        return False
    return True


# ---------------------------------------------------------------------------
# Contract assertions — the reference regex correctly classifies all vectors.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload", DANGEROUS_PRESETS)
def test_reference_validator_rejects_dangerous(payload: str) -> None:
    """The reference safety contract correctly flags every dangerous vector."""
    assert not _reference_is_safe(payload), (
        f"reference contract incorrectly accepted dangerous preset: {payload!r}"
    )


@pytest.mark.parametrize("payload", BENIGN_PRESETS)
def test_reference_validator_accepts_benign(payload: str) -> None:
    """The reference safety contract accepts legitimate preset names."""
    assert _reference_is_safe(payload), (
        f"reference contract incorrectly rejected benign preset: {payload!r}"
    )


# ---------------------------------------------------------------------------
# Production-side assertions — IntegrationSetEntry MUST enforce the contract.
#
# These are the "real" L7 invariants.  If they fail, the L7 gate surfaces
# the gap as a finding for the admin to fix.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload", DANGEROUS_PRESETS)
def test_pydantic_model_rejects_dangerous(payload: str) -> None:
    """``IntegrationSetEntry`` MUST reject dangerous preset names.

    If the production model currently permits the payload, the test fails
    and the L7 layer reports it via JUnit.  The fix is to add a
    ``field_validator('value')`` to ``IntegrationSetEntry`` mirroring
    ``_reference_is_safe`` above.
    """
    assert not _validates(payload), (
        f"IntegrationSetEntry accepted dangerous preset value: {payload!r}"
    )


@pytest.mark.parametrize("payload", BENIGN_PRESETS)
def test_pydantic_model_accepts_benign(payload: str) -> None:
    """``IntegrationSetEntry`` MUST accept legitimate preset names."""
    assert _validates(payload), (
        f"IntegrationSetEntry rejected benign preset value: {payload!r}"
    )
