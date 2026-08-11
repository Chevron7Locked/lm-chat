#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Verify ADR consistency.

ADR consistency check: walk
``docs/adr/*.md``, parse each file's ``**Status:**`` line, and
assert every ADR is in one of the accepted statuses
(``Accepted`` / ``Superseded`` / ``LOCKED``). Any ADR whose
Status line is missing, malformed, or carries a non-canonical
value is a hard failure.

Exit codes:
    0 — every ADR has a valid Status line.
    1 — at least one ADR is missing or has a bad Status line.

Usage:
    uv run python tools/check_adr_consistency.py
    make check-adr-consistency
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Final

_ADR_DIR: Final[Path] = Path(__file__).resolve().parent.parent / "docs" / "adr"
# A status line begins with a markdown bold ``**Status:**`` and is
# followed by free text. The first non-whitespace word after the
# colon must be one of the accepted statuses (case-sensitive on
# the keyword, lenient on surrounding punctuation).
_STATUS_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\*\*Status:\*\*\s+(?P<status>\w+)",
    re.MULTILINE,
)
_ACCEPTED_STATUSES: Final[frozenset[str]] = frozenset(
    {"Accepted", "Superseded", "LOCKED", "Locked"},
)


def _check_one(path: Path) -> str | None:
    """Return None if the ADR has a valid Status line, else a failure message."""
    text = path.read_text(encoding="utf-8")
    match = _STATUS_PATTERN.search(text)
    if match is None:
        return f"{path.name}: no `**Status:**` line found"
    status = match.group("status")
    if status not in _ACCEPTED_STATUSES:
        return f"{path.name}: invalid status {status!r} (accepted: {sorted(_ACCEPTED_STATUSES)})"
    return None


def main() -> int:
    """Walk the ADR directory and report any inconsistencies."""
    if not _ADR_DIR.is_dir():
        print(f"ERROR: ADR directory not found: {_ADR_DIR}", file=sys.stderr)
        return 1

    failures: list[str] = []
    checked = 0
    for path in sorted(_ADR_DIR.glob("*.md")):
        # Skip README / index files; ADRs are numbered or LIC/W0-prefixed
        if path.name in {"README.md", "index.md"}:
            continue
        checked += 1
        failure = _check_one(path)
        if failure is not None:
            failures.append(failure)

    if failures:
        print(f"ADR consistency FAILED ({len(failures)} of {checked} ADRs):", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print(f"ADR consistency OK ({checked} ADRs checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
