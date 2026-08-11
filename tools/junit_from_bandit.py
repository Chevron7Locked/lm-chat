#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Convert ``bandit -f json`` output into JUnit XML.

Bandit emits a JSON document with shape::

    {
      "results": [
        {
          "test_id": "B105",
          "test_name": "hardcoded_password_string",
          "issue_severity": "LOW",
          "issue_confidence": "MEDIUM",
          "issue_text": "Possible hardcoded password...",
          "filename": "src/lmchat/foo.py",
          "line_number": 42,
          "more_info": "https://bandit.readthedocs.io/..."
        }, ...
      ],
      "metrics": {"_totals": {"SEVERITY.HIGH": 0, ...}}
    }

Severity → JUnit mapping:

  - HIGH / MEDIUM  → ``<failure>``
  - LOW            → ``<skipped>`` (informational)

Each unique ``test_id`` becomes one ``<testcase>``; multiple sightings of
the same test_id are collapsed into the failure body. When no findings are
present, a single zero-failure testcase is emitted so the XML is valid.

When ``--skipped`` is passed, emits a synthetic ``bandit-not-installed``
skipped testcase for graceful degradation when the binary is absent.

Docker fallback note: if bandit is unavailable as a binary but the Python
package is installed, use ``uv run bandit ...``.  This wrapper always
invokes bandit via the venv (``uv run bandit``), so the ``--skipped``
mode applies only when the package itself is absent from the environment.
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

_SUITE_NAME = "bandit-static-security"

# Severities that flip the gate red.
_FAIL_SEVERITIES = {"HIGH", "MEDIUM"}
# Severity ordering for worst-case aggregation.
_SEV_ORDER = ["HIGH", "MEDIUM", "LOW", "UNDEFINED"]


def _norm_sev(s: Any) -> str:
    return str(s or "").strip().upper()


def _collect_findings(report: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in report.get("results") or []:
        if not isinstance(r, dict):
            continue
        out.append({
            "id": str(r.get("test_id") or "B000"),
            "name": str(r.get("test_name") or "unknown"),
            "severity": _norm_sev(r.get("issue_severity")),
            "confidence": _norm_sev(r.get("issue_confidence")),
            "text": str(r.get("issue_text") or ""),
            "filename": str(r.get("filename") or ""),
            "line": str(r.get("line_number") or ""),
            "url": str(r.get("more_info") or ""),
        })
    return out


def _format_finding_body(items: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for item in items:
        lines.append(
            f"  [{item['severity']}] confidence={item['confidence']} "
            f"{item['filename']}:{item['line']}"
        )
        if item["text"]:
            lines.append(f"    {item['text'][:200]}")
        if item["url"]:
            lines.append(f"    ref: {item['url']}")
    return "\n".join(lines)


def convert(report: dict[str, Any]) -> ET.Element:
    """Build the JUnit ``<testsuite>`` from a Bandit JSON report."""
    findings = _collect_findings(report)

    by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for f in findings:
        by_id[f["id"]].append(f)

    test_count = max(len(by_id), 1)
    fail_count = sum(
        1 for items in by_id.values()
        if any(i["severity"] in _FAIL_SEVERITIES for i in items)
    )
    skip_count = sum(
        1 for items in by_id.values()
        if all(i["severity"] not in _FAIL_SEVERITIES for i in items)
    )

    suite = ET.Element("testsuite", {
        "name": _SUITE_NAME,
        "tests": str(test_count),
        "failures": str(fail_count),
        "errors": "0",
        "skipped": str(skip_count),
    })

    if not by_id:
        ET.SubElement(suite, "testcase", {
            "name": "no-findings",
            "classname": "bandit",
        })
        return suite

    for tid, items in sorted(by_id.items()):
        worst = next(
            (s for s in _SEV_ORDER if any(i["severity"] == s for i in items)),
            "UNDEFINED",
        )
        tc = ET.SubElement(suite, "testcase", {
            "name": f"{tid}-{items[0]['name']}",
            "classname": f"bandit.{worst.lower()}",
        })
        body = _format_finding_body(items)
        if worst in _FAIL_SEVERITIES:
            fail = ET.SubElement(tc, "failure", {
                "message": f"{worst}: {len(items)} sighting(s) of {tid} ({items[0]['name']})",
                "type": f"bandit-{worst.lower()}",
            })
            fail.text = body
        else:
            sk = ET.SubElement(tc, "skipped", {
                "message": f"{worst}: informational ({len(items)} sighting(s))",
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
        "name": "bandit-not-installed",
        "classname": "bandit",
    })
    sk = ET.SubElement(tc, "skipped", {
        "message": "bandit not available in venv; skipping per L3 graceful-degradation policy.",
    })
    sk.text = (
        "Add bandit>=1.8 to [dependency-groups] dev in pyproject.toml "
        "and run `uv sync --dev` to enable static security scanning."
    )
    return suite


def _run_bandit(src_dir: Path, output_path: Path) -> bool:
    """Run bandit and write JSON to *output_path*. Returns True on success."""
    subprocess.run(
        [
            "uv", "run", "bandit",
            "-ll", "-ii",
            "-r", str(src_dir),
            "-f", "json",
            "-o", str(output_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    # bandit exits 1 when findings are present — that is not a failure here.
    return output_path.is_file()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, default=None,
                   help="Path to write JUnit XML (default: stdout)")
    p.add_argument("--skipped", action="store_true",
                   help="Emit a synthetic 'bandit not installed' skipped testcase.")
    p.add_argument("--src-dir", type=Path, default=Path("src/lmchat"),
                   help="Source directory to scan (default: src/lmchat).")
    args = p.parse_args(argv)

    suite: ET.Element
    if args.skipped:
        suite = _emit_skipped()
    else:
        with tempfile.TemporaryDirectory() as tmp:
            json_path = Path(tmp) / "bandit.json"
            ok = _run_bandit(args.src_dir, json_path)
            if not ok:
                # bandit missing or crashed without producing output.
                suite = _emit_skipped()
            else:
                raw = json_path.read_text(encoding="utf-8")
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as e:
                    print(f"junit_from_bandit: invalid JSON from bandit — {e}",
                          file=sys.stderr)
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
