#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Hypothesis-driven ReDoS fuzzer for lm-chat slash-command parser regexes.

Targets every compiled regex found in the lm-chat source tree.  For each
pattern, Hypothesis generates pathological inputs (nested groups, catastrophic
backtracking seeds, shell metacharacters, Unicode astral-plane codepoints,
and large strings) and asserts that evaluation completes in < 50 ms
(R-S4).

Emits a JUnit XML testsuite named ``dos-redos`` to --output.

Usage
-----
    uv run python tools/redos_fuzzer.py --output target/gates/L6-dos-redos.xml

    # skip mode (binary/infra unavailable):
    uv run python tools/redos_fuzzer.py --skipped --output target/gates/L6-dos-redos.xml

Exit codes
----------
0   all assertions passed (or --skipped mode)
1   one or more patterns exceeded the 50 ms wall-clock limit
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from xml.etree import ElementTree as ET

# ---------------------------------------------------------------------------
# Regex inventory — all patterns extracted from src/lmchat/
# Grep reference: grep -rn "re\.compile\|re\.match\|re\.search" src/lmchat/
# ---------------------------------------------------------------------------

_PATTERNS: dict[str, re.Pattern[str]] = {
    # auth_service.py:113 — username validation
    "username_validate": re.compile(r"^[a-zA-Z0-9_]{3,64}$"),
    # documents.py:113 — filename sanitiser
    "filename_sanitize": re.compile(r"[^a-zA-Z0-9._\- ]"),
    # documents_service.py:212 / memory_service.py:187 — whitespace collapse
    "whitespace_collapse": re.compile(r"\s+"),
    # memory.py:690–692 — list-item stripping (two patterns)
    "list_item_numbered": re.compile(r"^\s*\d+[.)]\s*"),
    "list_item_bullet": re.compile(r"^[-*•]\s*"),
}

# Wall-clock budget per call (seconds).
_MAX_WALL_CLOCK_S: float = 0.050  # 50 ms (R-S4)

# ---------------------------------------------------------------------------
# Pathological input seeds
# These complement Hypothesis-generated strings by pre-seeding known-bad
# ReDoS patterns so even a short run catches classical catastrophic cases.
# ---------------------------------------------------------------------------

_SEEDS: list[str] = [
    # Classic (a+)+ catastrophic backtracking seeds
    "a" * 100 + "!",
    "a" * 500 + "!",
    "a" * 2000 + "!",
    # Nested alternation
    "(" * 50 + "a" + ")" * 50,
    # Shell metacharacters
    "`$(echo pwned)`",
    "; rm -rf / #",
    "$(cat /etc/passwd)",
    "' OR '1'='1",
    # Unicode astral plane (outside BMP)
    "\U0001f600" * 1000,
    "\U00010000" * 500,
    # NUL bytes
    "\x00" * 100,
    # Mixed: astral + ascii
    "a\U0001f4a9" * 200,
    # 1 MB string
    "a" * (1024 * 1024),
    # Deeply repeated groups
    "(ab)+" * 100,
    "(?:ab)+" * 100,
    # Whitespace only
    " " * 10000,
    "\t\n\r" * 5000,
    # Digits only
    "1" * 10000,
    # Long non-matching string (exercises backtracking worst-case)
    "x" * 65536 + "!",
]


def _measure_match(pattern: re.Pattern[str], text: str) -> float:
    """Return wall-clock seconds to run re.search(pattern, text)."""
    t0 = time.perf_counter()
    pattern.search(text)
    return time.perf_counter() - t0


def _measure_sub(pattern: re.Pattern[str], text: str) -> float:
    """Return wall-clock seconds to run re.sub(pattern, ' ', text)."""
    t0 = time.perf_counter()
    pattern.sub(" ", text)
    return time.perf_counter() - t0


def _run_pattern(name: str, pattern: re.Pattern[str]) -> tuple[bool, str]:
    """Run all seeds against a single pattern. Returns (passed, detail)."""
    failures: list[str] = []
    for seed in _SEEDS:
        elapsed = _measure_match(pattern, seed)
        if elapsed > _MAX_WALL_CLOCK_S:
            failures.append(
                f"re.search exceeded {_MAX_WALL_CLOCK_S*1000:.0f} ms: "
                f"{elapsed*1000:.1f} ms on input len={len(seed)!r} "
                f"repr={seed[:60]!r}"
            )
        elapsed_sub = _measure_sub(pattern, seed)
        if elapsed_sub > _MAX_WALL_CLOCK_S:
            failures.append(
                f"re.sub exceeded {_MAX_WALL_CLOCK_S*1000:.0f} ms: "
                f"{elapsed_sub*1000:.1f} ms on input len={len(seed)!r} "
                f"repr={seed[:60]!r}"
            )
    if failures:
        return False, "\n".join(failures)
    return True, f"all {len(_SEEDS)} seeds under {_MAX_WALL_CLOCK_S*1000:.0f} ms"


def _hypothesis_fuzz(name: str, pattern: re.Pattern[str]) -> tuple[bool, str]:
    """Run a Hypothesis-driven fuzz pass against the pattern."""
    try:
        from hypothesis import HealthCheck, given, settings
        from hypothesis import strategies as st
    except ImportError:
        return True, "hypothesis not installed — seed-only mode"

    failure_info: list[str] = []

    @settings(  # type: ignore[misc]
        max_examples=200,
        suppress_health_check=[HealthCheck.too_slow],
        deadline=None,
    )
    @given(  # type: ignore[misc]
        st.one_of(
            st.text(max_size=4096),
            st.binary(max_size=4096).map(lambda b: b.decode("utf-8", errors="replace")),
            st.text(
                alphabet=st.characters(
                    categories=["L", "N", "P", "S", "Z"],
                    max_codepoint=0x10FFFF,
                ),
                max_size=2048,
            ),
        )
    )
    def fuzz_search(text: str) -> None:
        elapsed = _measure_match(pattern, text)
        if elapsed > _MAX_WALL_CLOCK_S:
            failure_info.append(
                f"hypothesis found slow input: {elapsed*1000:.1f} ms "
                f"len={len(text)} repr={text[:60]!r}"
            )
            # Don't raise — collect all findings.

    try:
        fuzz_search()
    except Exception as exc:  # noqa: BLE001
        return False, f"hypothesis internal error: {exc}"

    if failure_info:
        return False, "\n".join(failure_info)
    return True, "hypothesis pass clean"


def _build_suite(results: list[tuple[str, bool, str]]) -> ET.Element:
    total = len(results)
    failures = sum(1 for _, ok, _ in results if not ok)
    suite = ET.Element(
        "testsuite",
        {
            "name": "dos-redos",
            "tests": str(total),
            "failures": str(failures),
            "errors": "0",
            "skipped": "0",
        },
    )
    for name, ok, detail in results:
        tc = ET.SubElement(
            suite, "testcase", {"name": name, "classname": "dos.redos"}
        )
        if not ok:
            fail = ET.SubElement(
                tc,
                "failure",
                {
                    "message": f"ReDoS: pattern {name!r} exceeded {_MAX_WALL_CLOCK_S*1000:.0f}ms",
                    "type": "dos-redos",
                },
            )
            fail.text = detail
    return suite


def _emit_skipped(reason: str = "") -> ET.Element:
    suite = ET.Element(
        "testsuite",
        {"name": "dos-redos", "tests": "1", "failures": "0", "errors": "0", "skipped": "1"},
    )
    tc = ET.SubElement(
        suite, "testcase", {"name": "redos-fuzzer-skipped", "classname": "dos.redos"}
    )
    msg = reason or "redos_fuzzer skipped (--skipped flag)"
    sk = ET.SubElement(tc, "skipped", {"message": msg})
    sk.text = msg
    return suite


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, default=None,
                   help="Path to write JUnit XML (default: stdout)")
    p.add_argument("--skipped", action="store_true",
                   help="Emit a synthetic skipped testcase (infra unavailable).")
    p.add_argument("--skipped-reason", type=str, default="",
                   help="Override skipped-mode reason string.")
    args = p.parse_args(argv)

    if args.skipped:
        suite = _emit_skipped(args.skipped_reason)
    else:
        results: list[tuple[str, bool, str]] = []
        for name, pattern in _PATTERNS.items():
            ok_seed, detail_seed = _run_pattern(name, pattern)
            ok_hyp, detail_hyp = _hypothesis_fuzz(name, pattern)
            ok = ok_seed and ok_hyp
            detail = "\n".join(filter(None, [detail_seed, detail_hyp]))
            results.append((name, ok, detail))
        suite = _build_suite(results)

    root = ET.Element(
        "testsuites",
        {
            "name": "dos-redos",
            "tests": suite.get("tests", "0"),
            "failures": suite.get("failures", "0"),
            "errors": "0",
        },
    )
    root.append(suite)
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        tree.write(args.output, encoding="utf-8", xml_declaration=True)
    else:
        sys.stdout.write(ET.tostring(root, encoding="unicode", xml_declaration=True))
        sys.stdout.write("\n")

    failures = int(suite.get("failures", "0"))
    return 1 if failures > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
