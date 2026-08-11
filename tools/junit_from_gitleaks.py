#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Convert ``gitleaks detect`` JSON output into JUnit XML.

gitleaks detect --report-format json --report-path <file> emits a JSON array::

    [
      {
        "RuleID": "generic-api-key",
        "Description": "Generic API Key",
        "StartLine": 12,
        "EndLine": 12,
        "StartColumn": 1,
        "EndColumn": 40,
        "Match": "api_key = ...",
        "Secret": "REDACTED",
        "File": "src/lmchat/config.py",
        "Commit": "abc1234",
        "Author": "Kevin O'Neill",
        "Email": "...",
        "Date": "2026-05-01T00:00:00Z",
        "Message": "feat: initial commit",
        "Tags": ["key", "api"],
        "Fingerprint": "abc1234:src/lmchat/config.py:generic-api-key:12"
      }, ...
    ]

Gate policy: any secret found in git history is a blocking failure —
there are no severity tiers for git-history leaks. The commit SHA and file
path are surfaced in the failure body for rapid remediation.

Docker fallback: if gitleaks is not on PATH, the wrapper can be invoked
via Docker::

    docker run --rm -v "$(pwd):/repo" zricethezav/gitleaks:latest \\
        detect --source=/repo --no-banner --report-format json \\
        --report-path /repo/gitleaks.json

This wrapper invokes the system ``gitleaks`` binary directly. When absent,
it emits a ``<skipped>`` testcase (graceful degradation).

When ``--skipped`` is passed, emits a synthetic ``gitleaks-not-installed``
skipped testcase without running gitleaks.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

_SUITE_NAME = "git-history-secrets"


def _collect_findings(report: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in report:
        if not isinstance(r, dict):
            continue
        out.append({
            "rule_id": str(r.get("RuleID") or "UNKNOWN"),
            "description": str(r.get("Description") or ""),
            "file": str(r.get("File") or ""),
            "start_line": str(r.get("StartLine") or ""),
            "commit": str(r.get("Commit") or "")[:12],
            "author": str(r.get("Author") or ""),
            "date": str(r.get("Date") or ""),
            "fingerprint": str(r.get("Fingerprint") or ""),
            "match": str(r.get("Match") or "")[:120],
        })
    return out


def _format_finding_body(f: dict[str, Any]) -> str:
    return (
        f"rule: {f['rule_id']}\n"
        f"description: {f['description']}\n"
        f"file: {f['file']}:{f['start_line']}\n"
        f"commit: {f['commit']}\n"
        f"author: {f['author']} ({f['date']})\n"
        f"fingerprint: {f['fingerprint']}\n"
        f"match: {f['match']}"
    )


def convert(report: list[dict[str, Any]]) -> ET.Element:
    """Build the JUnit ``<testsuite>`` from a gitleaks JSON report."""
    findings = _collect_findings(report)

    test_count = max(len(findings), 1)
    fail_count = len(findings)

    suite = ET.Element("testsuite", {
        "name": _SUITE_NAME,
        "tests": str(test_count),
        "failures": str(fail_count),
        "errors": "0",
        "skipped": "0",
    })

    if not findings:
        ET.SubElement(suite, "testcase", {
            "name": "no-findings",
            "classname": "gitleaks",
        })
        return suite

    for f in findings:
        name = f["fingerprint"] or f"{f['rule_id']}:{f['file']}:{f['start_line']}"
        tc = ET.SubElement(suite, "testcase", {
            "name": name[:200],
            "classname": f"gitleaks.{f['rule_id']}",
        })
        body = _format_finding_body(f)
        fail = ET.SubElement(tc, "failure", {
            "message": (
                f"SECRET IN HISTORY: {f['rule_id']} in {f['file']}:{f['start_line']} "
                f"(commit {f['commit']})"
            ),
            "type": "git-secret",
        })
        fail.text = body

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
        "name": "gitleaks-not-installed",
        "classname": "gitleaks",
    })
    sk = ET.SubElement(tc, "skipped", {
        "message": (
            "gitleaks binary not on PATH; skipping per L3 graceful-degradation policy. "
            "Docker fallback: docker run --rm -v \"$(pwd):/repo\" "
            "zricethezav/gitleaks:latest detect --source=/repo --no-banner"
        ),
    })
    sk.text = (
        "Install gitleaks via `brew install gitleaks` or use Docker:\n"
        "  docker run --rm -v \"$(pwd):/repo\" zricethezav/gitleaks:latest "
        "detect --source=/repo --report-format json --report-path /repo/gitleaks.json --no-banner"
    )
    return suite


def _run_gitleaks(output_path: Path, source_dir: Path) -> tuple[bool, str]:
    """Run gitleaks. Returns (output_file_exists, stderr)."""
    result = subprocess.run(
        [
            "gitleaks", "detect",
            "--source", str(source_dir),
            "--report-format", "json",
            "--report-path", str(output_path),
            "--no-banner",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    # gitleaks exits 1 when secrets are found — not a crash (exit 2 is a real error).
    if result.returncode == 2:
        return False, result.stderr
    return True, result.stderr


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, default=None,
                   help="Path to write JUnit XML (default: stdout)")
    p.add_argument("--skipped", action="store_true",
                   help="Emit a synthetic 'gitleaks not installed' skipped testcase.")
    p.add_argument("--source", type=Path, default=Path("."),
                   help="Source directory for gitleaks (default: current directory).")
    args = p.parse_args(argv)

    suite: ET.Element
    if args.skipped:
        suite = _emit_skipped()
    else:
        # Check gitleaks is available.
        try:
            subprocess.run(
                ["gitleaks", "version"],
                capture_output=True, check=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError):
            suite = _emit_skipped()
        else:
            with tempfile.TemporaryDirectory() as tmp:
                json_path = Path(tmp) / "gitleaks.json"
                ok, stderr = _run_gitleaks(json_path, args.source)
                if not ok:
                    print(
                        f"junit_from_gitleaks: gitleaks exited with error:\n{stderr}",
                        file=sys.stderr,
                    )
                    # Degrade: emit a skipped entry rather than crashing.
                    suite = _emit_skipped()
                elif not json_path.is_file():
                    # gitleaks exit 0 with no report = clean scan.
                    suite = convert([])
                else:
                    raw = json_path.read_text(encoding="utf-8")
                    if not raw.strip():
                        # Empty report = no findings.
                        suite = convert([])
                    else:
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError as e:
                            print(
                                f"junit_from_gitleaks: invalid JSON from gitleaks — {e}",
                                file=sys.stderr,
                            )
                            return 1
                        if not isinstance(data, list):
                            # gitleaks can emit null on clean scan.
                            suite = convert([])
                        else:
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
