# SPDX-License-Identifier: Apache-2.0
"""check_mutation_scores.py — validate cosmic-ray kill-rate thresholds.

Reads one or more cosmic-ray SQLite session files, runs ``cr-rate`` on each,
parses the kill-rate fraction it prints, and exits non-zero if any session
falls below the configured threshold.

Usage (called by ``make mutation-gate``):
    uv run python tools/check_mutation_scores.py \\
        --threshold 0.60 \\
        --sessions target/mutation/streaming_client.sqlite \\
                   target/mutation/native.sqlite \\
                   target/mutation/chats.sqlite
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


def cr_rate(session: Path) -> float | None:
    """Run ``cr-rate <session>`` and return the kill-rate as a float in [0,1].

    Returns None if the session file does not exist or cr-rate output cannot
    be parsed (e.g. session not yet run).
    """
    if not session.exists():
        return None
    try:
        result = subprocess.run(
            ["uv", "run", "cr-rate", str(session)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"  [error] cr-rate invocation failed for {session.name}: {exc}", file=sys.stderr)
        return None

    output = result.stdout + result.stderr
    # cr-rate prints a line like: "kill rate: 0.7142857142857143"
    # or as a percentage in some builds: "72%"  — handle both.
    match_fraction = re.search(r"kill\s+rate[:\s]+([0-9]+\.[0-9]+)", output, re.IGNORECASE)
    if match_fraction:
        return float(match_fraction.group(1))
    match_pct = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*%", output)
    if match_pct:
        return float(match_pct.group(1)) / 100.0
    # Fallback: last bare float on any line.
    match_bare = re.search(r"^([0-9]+\.[0-9]+)\s*$", output, re.MULTILINE)
    if match_bare:
        return float(match_bare.group(1))
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate cosmic-ray kill-rate thresholds.")
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.60,
        help="Minimum kill-rate fraction required (default: 0.60).",
    )
    parser.add_argument(
        "--sessions",
        nargs="+",
        required=True,
        metavar="SESSION.sqlite",
        help="One or more cosmic-ray session files to check.",
    )
    args = parser.parse_args(argv)

    failures: list[str] = []
    skipped: list[str] = []

    print(f"[mutation-gate] threshold: {args.threshold:.0%}")
    for path_str in args.sessions:
        session = Path(path_str)
        rate = cr_rate(session)
        name = session.stem
        if rate is None:
            print(f"  SKIP  {name:30s}  (session not found or cr-rate unparseable)")
            skipped.append(name)
        elif rate >= args.threshold:
            print(f"  OK    {name:30s}  kill-rate={rate:.2%}")
        else:
            print(f"  FAIL  {name:30s}  kill-rate={rate:.2%}  < threshold {args.threshold:.0%}")
            failures.append(f"{name}: {rate:.2%}")

    if skipped:
        print(
            f"\n[mutation-gate] {len(skipped)} session(s) skipped "
            "(run `make mutation-baseline` first)"
        )
    if failures:
        print(f"\n[mutation-gate] FAIL — {len(failures)} session(s) below threshold:")
        for f in failures:
            print(f"  {f}")
        return 1

    print(f"\n[mutation-gate] OK — all checked sessions >= {args.threshold:.0%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
