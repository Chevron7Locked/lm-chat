#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""P15h — JUnit XML emitter for soak-test results.

Reads ``target/gates/soak-checkpoints.jsonl`` (produced by
``tools/soak_test.py``) and final locust aggregate stats (if present),
then emits ``target/gates/soak-test.xml`` with testsuite name
``soak-test`` and one testcase per drift assertion:

  * ``memory_growth``   — RSS at finalize ≤ baseline × 1.10
  * ``p99_stability``   — final-window p99 ≤ initial-window p99 × 1.50
  * ``error_rate``      — cumulative failures / requests < 0.001 (0.1 %)

Failing assertions get ``<failure>`` elements with actual vs expected.
Passing assertions get a plain passing testcase (no child elements).

Skipped mode
------------
When ``--skipped`` is passed (or when the checkpoints file is missing),
the tool emits a synthetic ``soak-not-run`` testsuite with one
``<skipped>`` testcase.  Use ``--reason`` to set the skip message.
The recipe in the Makefile invokes this unconditionally; when the soak
wasn't run the output is a documented skip, not a missing file.

Usage
-----
  uv run python tools/junit_from_soak.py \\
      --checkpoints target/gates/soak-checkpoints.jsonl \\
      --output target/gates/soak-test.xml

  uv run python tools/junit_from_soak.py \\
      --skipped --reason "soak not run (compose-target not up)" \\
      --output target/gates/soak-test.xml
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

_SUITE_NAME = "soak-test"

# Drift thresholds (must match soak_test.py).
MEMORY_GROWTH_MAX: float = 1.10
P99_STABILITY_MAX: float = 1.50
ERROR_RATE_MAX: float = 0.001


# ---------------------------------------------------------------------------
# JSONL parsing
# ---------------------------------------------------------------------------

def load_checkpoints(path: Path) -> list[dict[str, Any]]:
    """Parse a soak-checkpoints.jsonl file.

    Args:
        path: Path to the ``.jsonl`` file.

    Returns:
        List of checkpoint dicts in file order.

    Raises:
        FileNotFoundError: when ``path`` does not exist.
        ValueError: when any line is not valid JSON.
    """
    records: list[dict[str, Any]] = []
    text = path.read_text(encoding="utf-8")
    for i, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"line {i}: invalid JSON — {exc}") from exc
        if not isinstance(obj, dict):
            raise ValueError(f"line {i}: expected JSON object, got {type(obj).__name__}")
        records.append(obj)
    return records


# ---------------------------------------------------------------------------
# Drift assertion logic (stateless — testable without file I/O)
# ---------------------------------------------------------------------------

def assess_drift(
    checkpoints: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Evaluate drift assertions.  Returns a list of result dicts.

    Each result dict has keys:
        name       — assertion name (e.g. ``memory_growth``)
        passed     — bool
        actual     — float (the measured ratio/rate)
        threshold  — float (the limit)
        message    — human-readable outcome string

    Always returns exactly 3 entries (one per assertion) regardless of
    how many checkpoints are present.  When there are fewer than 2
    checkpoints, all assertions are marked as ``skipped`` (not enough
    data).
    """
    results: list[dict[str, Any]] = []

    def _skipped(name: str, threshold: float, reason: str) -> dict[str, Any]:
        return {
            "name": name,
            "passed": True,
            "skipped": True,
            "actual": None,
            "threshold": threshold,
            "message": f"skipped — {reason}",
        }

    if len(checkpoints) < 2:
        reason = (
            "only one checkpoint recorded (soak duration too short to measure drift)"
            if len(checkpoints) == 1
            else "no checkpoints recorded"
        )
        results.append(_skipped("memory_growth", MEMORY_GROWTH_MAX, reason))
        results.append(_skipped("p99_stability", P99_STABILITY_MAX, reason))
        results.append(_skipped("error_rate", ERROR_RATE_MAX, reason))
        return results

    first = checkpoints[0]
    last = checkpoints[-1]

    # --- memory_growth ---
    rss_first = first.get("rss_mb")
    rss_last = last.get("rss_mb")
    if rss_first is not None and rss_last is not None and rss_first > 0:
        ratio = rss_last / rss_first
        passed = ratio <= MEMORY_GROWTH_MAX
        results.append({
            "name": "memory_growth",
            "passed": passed,
            "skipped": False,
            "actual": round(ratio, 4),
            "threshold": MEMORY_GROWTH_MAX,
            "message": (
                f"RSS ratio {ratio:.4f} "
                f"(baseline {rss_first:.1f} MB → final {rss_last:.1f} MB); "
                f"limit {MEMORY_GROWTH_MAX:.2f}"
            ),
        })
    else:
        results.append(
            _skipped("memory_growth", MEMORY_GROWTH_MAX, "rss_mb not available in checkpoints")
        )

    # --- p99_stability ---
    p99_first = first.get("p99_ms")
    p99_last = last.get("p99_ms")
    if p99_first is not None and p99_last is not None and p99_first > 0:
        ratio = p99_last / p99_first
        passed = ratio <= P99_STABILITY_MAX
        results.append({
            "name": "p99_stability",
            "passed": passed,
            "skipped": False,
            "actual": round(ratio, 4),
            "threshold": P99_STABILITY_MAX,
            "message": (
                f"p99 ratio {ratio:.4f} "
                f"(initial {p99_first:.1f} ms → final {p99_last:.1f} ms); "
                f"limit {P99_STABILITY_MAX:.2f}"
            ),
        })
    elif p99_first == 0.0 and p99_last == 0.0:
        # Zero latency means no requests were recorded — treat as skip.
        results.append(_skipped("p99_stability", P99_STABILITY_MAX, "no requests recorded (p99=0)"))
    else:
        results.append(
            _skipped("p99_stability", P99_STABILITY_MAX, "p99_ms not available in checkpoints")
        )

    # --- error_rate ---
    total_req = last.get("total_requests")
    total_fail = last.get("total_failures")
    if total_req is not None and total_req > 0:
        rate = (total_fail or 0) / total_req
        passed = rate < ERROR_RATE_MAX
        results.append({
            "name": "error_rate",
            "passed": passed,
            "skipped": False,
            "actual": round(rate, 6),
            "threshold": ERROR_RATE_MAX,
            "message": (
                f"error rate {rate:.6f} "
                f"({int(total_fail or 0)} / {int(total_req)} requests); "
                f"limit {ERROR_RATE_MAX:.4f}"
            ),
        })
    else:
        results.append(_skipped("error_rate", ERROR_RATE_MAX, "no requests recorded"))

    return results


# ---------------------------------------------------------------------------
# JUnit XML builder
# ---------------------------------------------------------------------------

def build_junit(
    assertion_results: list[dict[str, Any]],
    *,
    soak_duration_hint: str = "",
) -> ET.Element:
    """Build the JUnit XML ``<testsuites>`` element.

    Args:
        assertion_results: Output of :func:`assess_drift`.
        soak_duration_hint: Optional human-readable duration string for the
            suite's ``properties`` block.

    Returns:
        The ``<testsuites>`` root element.
    """
    total = len(assertion_results)
    failures = sum(1 for r in assertion_results if not r["passed"] and not r.get("skipped"))
    skipped = sum(1 for r in assertion_results if r.get("skipped"))

    suite = ET.Element("testsuite", {
        "name": _SUITE_NAME,
        "tests": str(total),
        "failures": str(failures),
        "errors": "0",
        "skipped": str(skipped),
    })

    if soak_duration_hint:
        props = ET.SubElement(suite, "properties")
        prop = ET.SubElement(props, "property")
        prop.set("name", "soak_duration")
        prop.set("value", soak_duration_hint)

    for r in assertion_results:
        tc = ET.SubElement(suite, "testcase", {
            "name": r["name"],
            "classname": _SUITE_NAME,
        })
        if r.get("skipped"):
            sk = ET.SubElement(tc, "skipped", {"message": r["message"]})
            sk.text = r["message"]
        elif not r["passed"]:
            fail = ET.SubElement(tc, "failure", {
                "message": r["message"][:200],
                "type": f"drift-{r['name']}",
            })
            actual = r.get("actual")
            threshold = r.get("threshold")
            fail.text = (
                f"DRIFT ASSERTION FAILED: {r['name']}\n"
                f"  actual:    {actual}\n"
                f"  threshold: {threshold}\n"
                f"  detail:    {r['message']}"
            )

    root = ET.Element("testsuites", {
        "name": _SUITE_NAME,
        "tests": str(total),
        "failures": str(failures),
        "errors": "0",
        "skipped": str(skipped),
    })
    root.append(suite)
    return root


def _emit_skipped_suite(reason: str) -> ET.Element:
    """Emit a synthetic skipped suite when the soak was not run."""
    suite = ET.Element("testsuite", {
        "name": _SUITE_NAME,
        "tests": "1",
        "failures": "0",
        "errors": "0",
        "skipped": "1",
    })
    tc = ET.SubElement(suite, "testcase", {
        "name": "soak-not-run",
        "classname": _SUITE_NAME,
    })
    sk = ET.SubElement(tc, "skipped", {"message": reason})
    sk.text = reason

    root = ET.Element("testsuites", {
        "name": _SUITE_NAME,
        "tests": "1",
        "failures": "0",
        "errors": "0",
        "skipped": "1",
    })
    root.append(suite)
    return root


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--checkpoints", type=Path,
        default=Path("target/gates/soak-checkpoints.jsonl"),
        help="Path to soak-checkpoints.jsonl (default: %(default)s).",
    )
    p.add_argument(
        "--output", type=Path,
        default=Path("target/gates/soak-test.xml"),
        help="Destination JUnit XML path (default: %(default)s).",
    )
    p.add_argument(
        "--skipped", action="store_true",
        help="Emit a synthetic 'soak-not-run' skipped suite.",
    )
    p.add_argument(
        "--reason", type=str, default="",
        help="Reason string for --skipped mode (default: generic message).",
    )
    args = p.parse_args(argv)

    # Determine skip condition.
    skip_reason: str = ""
    if args.skipped:
        skip_reason = args.reason or "soak-test was not run"
    elif not args.checkpoints.is_file():
        skip_reason = (
            args.reason
            or f"soak-checkpoints.jsonl not found at {args.checkpoints} "
               "(run `make soak-test` to generate)"
        )

    if skip_reason:
        root = _emit_skipped_suite(skip_reason)
    else:
        try:
            checkpoints = load_checkpoints(args.checkpoints)
        except ValueError as exc:
            print(f"junit_from_soak: parse error — {exc}", file=sys.stderr)
            return 1

        results = assess_drift(checkpoints)

        # Build a duration hint from the JSONL if available.
        duration_hint = ""
        if checkpoints:
            elapsed = checkpoints[-1].get("elapsed_seconds", 0)
            h = int(elapsed // 3600)
            m = int((elapsed % 3600) // 60)
            s = int(elapsed % 60)
            duration_hint = f"{h:02d}:{m:02d}:{s:02d}"

        root = build_junit(results, soak_duration_hint=duration_hint)

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    tree.write(args.output, encoding="utf-8", xml_declaration=True)
    print(f"junit_from_soak: wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
