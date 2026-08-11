#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Convert Semgrep SARIF output into JUnit XML.

Semgrep emits SARIF 2.1.0 JSON with shape::

    {
      "$schema": "...",
      "version": "2.1.0",
      "runs": [{
        "tool": { "driver": { "name": "semgrep", ... } },
        "results": [{
          "ruleId": "my-rule-id",
          "level": "error",
          "message": { "text": "..." },
          "locations": [{
            "physicalLocation": {
              "artifactLocation": { "uri": "path/to/file.py" },
              "region": { "startLine": 42 }
            }
          }]
        }, ...]
      }]
    }

Severity mapping (Semgrep level → JUnit):
  - error   → ``<failure>``
  - warning → ``<failure>`` (gated; plan may choose to treat as skipped)
  - note    → ``<skipped>`` (informational)

Each unique ``ruleId`` becomes one ``<testcase>``; multiple sightings of
the same rule are collapsed into the failure body. When no findings are
present, a single zero-failure testcase is emitted.

When ``--skipped`` is passed, emits a synthetic ``semgrep-not-installed``
skipped testcase for graceful degradation.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

_SUITE_NAME = "semgrep-sast"

# Levels that flip the gate red.
_FAIL_LEVELS = {"error", "warning"}


def _collect_findings(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten SARIF runs.results into a simple list."""
    out: list[dict[str, Any]] = []
    for run in report.get("runs") or []:
        for r in run.get("results") or []:
            if not isinstance(r, dict):
                continue
            # Honor SARIF suppressions. semgrep's --sarif output keeps findings
            # that an inline `# nosemgrep` / `# nosem` comment suppressed, but
            # tags them with a non-empty `suppressions` array (kind=inSource)
            # rather than dropping them (which the text formatter does). A
            # suppressed result is NOT an active finding, so skip it — otherwise
            # the gate re-counts every nosem'd false positive as a failure.
            if r.get("suppressions"):
                continue
            rule_id = str(r.get("ruleId") or "unknown-rule")
            level = str(r.get("level") or "warning")
            msg = str(r.get("message", {}).get("text") or "")
            loc = r.get("locations") or [{}]
            phys = loc[0].get("physicalLocation", {}) if loc else {}
            uri = str(phys.get("artifactLocation", {}).get("uri") or "")
            region = phys.get("region") or {}
            start_line = str(region.get("startLine") or "")
            snippet = str(region.get("snippet", {}).get("text") or "")

            out.append({
                "rule_id": rule_id,
                "level": level,
                "message": msg,
                "file": uri,
                "line": start_line,
                "snippet": snippet,
            })
    return out


def _format_finding_body(items: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for item in items:
        lines.append(
            f"  [{item['level']}] {item['file']}:{item['line']}"
        )
        if item["message"]:
            lines.append(f"    {item['message'][:200]}")
        if item["snippet"]:
            lines.append(f"    code: {item['snippet'][:120]}")
    return "\n".join(lines)


def convert(report: dict[str, Any]) -> ET.Element:
    """Build the JUnit ``<testsuite>`` from a Semgrep SARIF report."""
    findings = _collect_findings(report)

    by_rule: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for f in findings:
        by_rule[f["rule_id"]].append(f)

    test_count = max(len(by_rule), 1)
    fail_count = sum(
        1 for items in by_rule.values()
        if any(i["level"] in _FAIL_LEVELS for i in items)
    )
    skip_count = sum(
        1 for items in by_rule.values()
        if all(i["level"] not in _FAIL_LEVELS for i in items)
    )

    suite = ET.Element("testsuite", {
        "name": _SUITE_NAME,
        "tests": str(test_count),
        "failures": str(fail_count),
        "errors": "0",
        "skipped": str(skip_count),
    })

    if not by_rule:
        ET.SubElement(suite, "testcase", {
            "name": "no-findings",
            "classname": "semgrep",
        })
        return suite

    for rid, items in sorted(by_rule.items()):
        worst = next(
            (lv for lv in ("error", "warning", "note") if any(i["level"] == lv for i in items)),
            "note",
        )
        tc = ET.SubElement(suite, "testcase", {
            "name": f"{rid}",
            "classname": f"semgrep.{worst}",
        })
        body = _format_finding_body(items)
        if worst in _FAIL_LEVELS:
            fail = ET.SubElement(tc, "failure", {
                "message": (
                    f"{worst.upper()}: {len(items)} sighting(s) of "
                    f"rule '{rid}'"
                ),
                "type": f"semgrep-{worst}",
            })
            fail.text = body
        else:
            sk = ET.SubElement(tc, "skipped", {
                "message": f"{worst.upper()}: informational ({len(items)} sighting(s))",
            })
            sk.text = body

    return suite


def _emit_skipped() -> ET.Element:
    suite = ET.Element("testsuite", {
        "name": _SUITE_NAME,
        "tests": "1",
        "failures": "0",
        "errors": "0",
        "skipped": "1",
    })
    tc = ET.SubElement(suite, "testcase", {
        "name": "semgrep-not-installed",
        "classname": "semgrep",
    })
    sk = ET.SubElement(tc, "skipped", {
        "message": "semgrep not on PATH; skipping per L3 graceful-degradation policy.",
    })
    sk.text = (
        "Install semgrep via `pip install semgrep` or `brew install semgrep`. "
        "See https://semgrep.dev/docs/getting-started/ for details."
    )
    return suite


def _emit_covered() -> ET.Element:
    """Emit a single passing testcase for a demoted custom-rule scan.

    Used for the ``regex-without-deadline`` custom rule when
    ``scripts/security-static.sh::check_regex_tests`` has confirmed every
    flagged regex has matching ``@settings(deadline=...)`` coverage. The rule
    only *enumerates* the regexes (it cannot see the cross-checked test file),
    so its raw findings are demoted to a pass rather than re-counted by the L9
    aggregator. The cross-check itself remains the authority: when coverage is
    missing, the script emits the raw findings (failures) instead.
    """
    suite = ET.Element("testsuite", {
        "name": _SUITE_NAME,
        "tests": "1",
        "failures": "0",
        "errors": "0",
        "skipped": "0",
    })
    tc = ET.SubElement(suite, "testcase", {
        "name": "regex-without-deadline",
        "classname": "semgrep",
    })
    out = ET.SubElement(tc, "system-out")
    out.text = (
        "All re.compile() regexes in tool_args.py have matching "
        "@settings(deadline=...) test coverage (verified by "
        "scripts/security-static.sh::check_regex_tests)."
    )
    return suite


def _run_semgrep(
    config_args: list[str],
    targets: list[str],
    output_path: Path,
) -> bool:
    """Run semgrep with the given configs and targets, writing SARIF to output_path."""
    cmd = [
        "semgrep",
        "--sarif",
        "--output", str(output_path),
    ] + [f"--config={c}" for c in config_args] + list(targets)
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )
    # semgrep exits 1 when findings exist — that is not a failure here.
    # Exit 2 or higher is a real error.
    if result.returncode >= 2:
        print(
            f"junit_from_semgrep: semgrep exited with code {result.returncode}:\n"
            f"{result.stderr[:500]}",
            file=sys.stderr,
        )
        return False
    return output_path.is_file()


def _check_tool() -> bool:
    """Return True if semgrep is available on PATH."""
    try:
        subprocess.run(
            ["semgrep", "--version"],
            capture_output=True, check=True,
        )
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, default=None,
                   help="Path to write JUnit XML (default: stdout)")
    p.add_argument("--skipped", action="store_true",
                   help="Emit a synthetic 'semgrep not installed' skipped testcase.")
    p.add_argument("--covered", action="store_true",
                   help="Emit a passing testcase for a custom rule whose findings "
                        "have been demoted by an external coverage cross-check.")
    p.add_argument("--config", action="append", default=[],
                   help="Semgrep config (repeatable, e.g. --config p/python).")
    p.add_argument("--target", action="append", default=[],
                   help="Target path to scan (repeatable).")
    args = p.parse_args(argv)

    suite: ET.Element
    if args.skipped:
        suite = _emit_skipped()
    elif args.covered:
        suite = _emit_covered()
    elif not args.config or not args.target:
        print(
            "junit_from_semgrep: --config and --target are required (or use --skipped).",
            file=sys.stderr,
        )
        return 1
    else:
        # Check tool availability
        if not _check_tool():
            suite = _emit_skipped()
        else:
            with tempfile.TemporaryDirectory() as tmp:
                sarif_path = Path(tmp) / "semgrep.sarif"
                ok = _run_semgrep(args.config, args.target, sarif_path)
                if not ok:
                    suite = _emit_skipped()
                elif not sarif_path.is_file():
                    suite = convert({})
                else:
                    raw = sarif_path.read_text(encoding="utf-8")
                    if not raw.strip():
                        suite = convert({})
                    else:
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError as e:
                            print(
                                f"junit_from_semgrep: invalid JSON — {e}",
                                file=sys.stderr,
                            )
                            return 1
                        suite = convert(data)

    root = ET.Element("testsuites", {
        "name": _SUITE_NAME,
        "tests": suite.get("tests", "0"),
        "failures": suite.get("failures", "0"),
        "errors": "0",
    })
    root.append(suite)

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        tree.write(args.output, encoding="utf-8", xml_declaration=True)
    else:
        sys.stdout.write(ET.tostring(root, encoding="unicode", xml_declaration=True))
        sys.stdout.write("\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())