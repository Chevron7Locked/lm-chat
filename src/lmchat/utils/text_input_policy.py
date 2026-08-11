"""Shared text-input validation for user-supplied content fields.

Memory pins, prompt names, prompt bodies, document titles — all of
these have the same defense profile:

* not empty / not whitespace-only
* under a size cap (defended at the API layer, not just the UI)
* no NUL bytes (truncates downstream str→bytes conversions in C)
* optional: reject tab/newline as control chars (most callers want
  them for multiline content; only single-line fields like "name"
  set ``allow_newlines=False``)

``POST /api/memory/pin`` previously accepted 500KB pins, whitespace-only
pins, and pins containing NUL bytes — the UI gated against empty input
but the API gated nothing. Same class of bug appears in any write path
with only frontend validation, so we extract a single helper.
"""
from __future__ import annotations

from typing import Final

# Defaults sized for chat-context-friendly fields.
DEFAULT_MAX_LENGTH: Final[int] = 8192
# Tighter caps for short identifiers (names, titles).
SHORT_FIELD_MAX_LENGTH: Final[int] = 256

# Per-field caps surfaced through this module so the route layer
# doesn't sprinkle magic numbers. Mirror these in the frontend
# `maxLength` HTML attribute so the admin hits the limit before
# the round trip.
PIN_TEXT_MAX_LENGTH: Final[int] = 8192
PROMPT_NAME_MAX_LENGTH: Final[int] = SHORT_FIELD_MAX_LENGTH
PROMPT_CONTENT_MAX_LENGTH: Final[int] = 16384


class TextInputPolicyError(ValueError):
    """Raised when text input fails the policy.

    Route layers map this to HTTP 422. Detail messages are admin-
    safe (no candidate echo).
    """


def validate_text(
    candidate: str,
    *,
    field: str = "text",
    max_length: int = DEFAULT_MAX_LENGTH,
    min_length: int = 1,
    allow_newlines: bool = True,
    allow_tabs: bool = True,
) -> str:
    """Raise :class:`TextInputPolicyError` on policy failure; return cleaned.

    The "clean" return strips trailing whitespace by default — most
    callers want a normalized form to store. If the caller wants to
    preserve the original bytes verbatim, they should pass the raw
    candidate to whatever downstream expects it and use this helper
    only for validation.

    Args:
        candidate:       The raw user input.
        field:           Field name surfaced in the error detail.
        max_length:      Length cap in characters (NOT bytes).
        min_length:      Minimum length in characters AFTER stripping
                         trailing whitespace. Default 1 preserves the
                         historical "not empty / not whitespace-only"
                         contract. Pass a higher value (e.g. 3) for
                         fields that need a meaningful minimum (project
                         names, chat titles).
        allow_newlines:  When False, ``\\n`` / ``\\r`` in input is rejected.
        allow_tabs:      When False, ``\\t`` in input is rejected.

    Returns:
        The candidate with trailing whitespace stripped.
    """
    stripped = candidate.strip()
    if stripped == "":
        raise TextInputPolicyError(
            f"{field} must not be empty or whitespace-only"
        )
    if len(stripped) < min_length:
        raise TextInputPolicyError(
            f"{field} must be at least {min_length} characters "
            f"(got {len(stripped)})"
        )
    if len(candidate) > max_length:
        raise TextInputPolicyError(
            f"{field} must be at most {max_length} characters "
            f"(got {len(candidate)})"
        )
    if "\x00" in candidate:
        raise TextInputPolicyError(
            f"{field} must not contain null bytes"
        )
    if not allow_newlines and ("\n" in candidate or "\r" in candidate):
        raise TextInputPolicyError(
            f"{field} must not contain newline characters"
        )
    if not allow_tabs and "\t" in candidate:
        raise TextInputPolicyError(
            f"{field} must not contain tab characters"
        )
    # Reject control bytes other than the explicitly-allowed whitespace.
    # cat-all guard so anything outside printable ASCII / common whitespace
    # gets caught even when the specific check above doesn't fire.
    for ch in candidate:
        cp = ord(ch)
        if cp < 0x20 and ch not in ("\t", "\n", "\r"):
            raise TextInputPolicyError(
                f"{field} must not contain control characters"
            )
    return candidate.rstrip()
