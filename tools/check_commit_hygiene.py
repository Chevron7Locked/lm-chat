#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Pre-commit guardrail: fail if a commit message violates LMChat hygiene rules.

Wire-up:
- `tools/git-hooks/commit-msg` calls this with the commit-message file
  path as argv[1]. Activated per clone via `make install-hooks` (sets
  `core.hooksPath tools/git-hooks`).
- `make gates` runs `--check-head` against `HEAD`'s commit body so CI
  catches a commit that slipped through a clone without the hook.

Rules (sourced from
`feedback_lm_chat_no_claude_attribution_in_commits_2026_05_16.md`):

1. No AI-attribution trailers:
   `Co-Authored-By:` (any AI attribution), `🤖 Generated`,
   `Generated with [Claude...`.
2. No AI-prose filler words (whole-word, case-insensitive):
   comprehensive, robust, seamless, leverages, delivers, ensures,
   facilitates. Word-boundary semantics are pinned so common stems
   like `robustness`, `delivery`, `leveraging` don't false-positive
  .
3. No emoji glyphs anywhere in subject or body.

Exit 0 = clean; exit 1 = at least one rule fired (each hit printed to
stderr with its file:line + the pattern that matched).
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from re import Pattern
from typing import Final

_PATTERNS: Final[list[tuple[Pattern[str], str]]] = [
    (re.compile(r"Co-Authored-By", re.IGNORECASE),
     "AI-attribution trailer (Co-Authored-By:)"),
    (re.compile(br"\xf0\x9f\xa4\x96\s*Generated".decode("utf-8"), 0),
     "AI-attribution marker (🤖 Generated)"),
    (re.compile(r"Generated with \[?Claude", re.IGNORECASE),
     "AI-attribution marker (Generated with Claude)"),
    (re.compile(
        r"\b(comprehensive|robust|seamless|leverages|delivers|"
        r"ensures|facilitates)\b",
        re.IGNORECASE,
     ),
     "AI-prose filler word"),
    (re.compile(r"[\U0001F300-\U0001F9FF☀-➿]"),
     "emoji glyph"),
]


def _scan(text: str, *, source_label: str) -> list[str]:
    hits: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if line.startswith("#"):
            continue  # git-commented lines are not committed
        for pat, label in _PATTERNS:
            m = pat.search(line)
            if m:
                hits.append(
                    f"{source_label}:{lineno}: {label} -> "
                    f"{m.group(0)!r}"
                )
    return hits


def _read_msg_file(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _read_head_msg() -> str:
    out = subprocess.run(
        ["git", "log", "-1", "--format=%B", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "msg_file",
        nargs="?",
        help="commit-message file path (commit-msg hook contract)",
    )
    p.add_argument(
        "--check-head",
        action="store_true",
        help="scan HEAD's commit body instead of a message file",
    )
    args = p.parse_args(argv)

    if args.check_head:
        text = _read_head_msg()
        label = "HEAD"
    else:
        if not args.msg_file:
            p.error("either msg_file or --check-head is required")
        text = _read_msg_file(Path(args.msg_file))
        label = args.msg_file

    hits = _scan(text, source_label=label)
    if hits:
        print(
            "commit-hygiene check failed — fix the message and retry:",
            file=sys.stderr,
        )
        for h in hits:
            print(f"  {h}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
