#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Pre-commit guardrail: fail if transient working documents are tracked in git.

Some documents are local scratch produced while developing a change — interim
findings, review notes, draft proposals, fix narratives. They live on disk
during the work but are never committed. The only docs the repo tracks are
curated: PLAN.md, ADRs, README, CHANGELOG, INTEGRATION_MAP.md, ARCHITECTURE.md,
the harness probes, etc.

This script runs `git ls-files` and matches against the scratch-doc patterns
below. If any tracked path matches, it prints the offenders and exits non-zero.
A passing run is silent unless --verbose.

Wire-up:
- production-gate L0 depends on this.
- Optional pre-commit hook: `.git/hooks/pre-commit` calls it.
"""
from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys

# Paths that should never be tracked. Each pattern is fnmatch-style (glob);
# we test ls-files output against each one. The docs/ tree is gitignored
# wholesale; these patterns are a belt-and-suspenders check for stray adds.
SCRATCH_DOC_PATTERNS: list[str] = [
    "docs/panels/*",                       # interim review trees
    "docs/phases/*/FINDINGS*.md",          # interim findings notes
    "docs/phases/*/*-FINDINGS.md",         # named-prefix findings (e.g. P15g-FINDINGS.md)
    "docs/phases/*/DESIGN_PROPOSAL.md",    # draft design proposals
    "docs/phases/*/AUDIT-*-FIX.md",        # audit-driven fix narratives
    "docs/phases/*/*-FIX-FINDINGS.md",     # named-prefix fix narratives
    "docs/phases/*/*-FIX.md",              # bare fix narratives
    "docs/phases/*/*-REPORT.md",           # interim investigation reports
    "docs/phases/*/*-FIX-REPORT.md",       # interim fix narratives
]


def _git_ls_files() -> list[str]:
    """Return repo-relative paths of every tracked file."""
    result = subprocess.run(
        ["git", "ls-files"],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def _match_offenders(paths: list[str], patterns: list[str]) -> list[tuple[str, str]]:
    """Return (path, matched_pattern) pairs for every tracked scratch doc."""
    offenders: list[tuple[str, str]] = []
    for path in paths:
        for pat in patterns:
            if fnmatch.fnmatch(path, pat):
                offenders.append((path, pat))
                break
    return offenders


def _explain(offenders: list[tuple[str, str]]) -> str:
    lines = [
        "BLOCKED: transient working docs are tracked in the repo.",
        "",
        "These paths are local scratch produced during development and must",
        "not be committed. See .gitignore for the ignored docs/ tree.",
        "",
        "Offenders:",
    ]
    for path, pat in offenders:
        lines.append(f"  {path}    (matched: {pat})")
    lines.extend([
        "",
        "Fix:",
        "  git rm --cached <each-path>   # preserves the file on disk",
        "  git commit -m 'chore(docs): untrack scratch'",
        "",
        "If a file in the offenders list should genuinely ship, RENAME it so",
        "it no longer matches the patterns, or narrow the pattern with",
        "explicit justification.",
    ])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail if transient working docs are tracked in git.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print 'clean' line on success (default: silent unless failing).",
    )
    parser.add_argument(
        "--patterns",
        action="store_true",
        help="Print the scratch-doc pattern list and exit 0.",
    )
    args = parser.parse_args(argv)

    if args.patterns:
        print("Scratch-doc patterns enforced by check_no_scratch_docs.py:")
        for pat in SCRATCH_DOC_PATTERNS:
            print(f"  {pat}")
        return 0

    paths = _git_ls_files()
    offenders = _match_offenders(paths, SCRATCH_DOC_PATTERNS)

    if offenders:
        print(_explain(offenders), file=sys.stderr)
        return 1

    if args.verbose:
        scanned = len(paths)
        print(f"[check-no-scratch-docs] clean — {scanned} tracked paths, 0 scratch-doc matches.")
    return 0


# Re-export the regex form some callers may want (CI grep gates etc.)
SCRATCH_DOC_REGEX = re.compile(
    "|".join(
        # Convert each fnmatch glob to a regex anchored to start.
        fnmatch.translate(pat).removesuffix(r"\Z")
        for pat in SCRATCH_DOC_PATTERNS
    )
)


if __name__ == "__main__":
    raise SystemExit(main())
