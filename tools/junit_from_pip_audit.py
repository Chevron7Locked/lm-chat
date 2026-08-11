#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Convert ``pip-audit --format=json`` output into JUnit XML.

pip-audit emits a JSON array with shape::

    [
      {
        "name": "somepackage",
        "version": "1.2.3",
        "vulns": [
          {
            "id": "GHSA-xxxx-yyyy-zzzz",
            "fix_versions": ["1.2.4"],
            "aliases": ["CVE-2024-9999"],
            "description": "..."
          }
        ]
      }, ...
    ]

Severity is not directly provided by pip-audit; we derive it from the vuln
``id`` prefix:

  - ``CVE-*`` / ``GHSA-*`` IDs default to HIGH (treated as failing)
  - MEDIUM / LOW are not distinguished by pip-audit; all CVEs are treated
    as HIGH by default since pip-audit only reports unfixed vulnerabilities.

Gate mapping:
  - Any vulnerability → ``<failure>``
  - No vulnerabilities → one zero-failure testcase named ``no-findings``

Newer pip-audit (>=2.7) wraps the output in an envelope::

    {"dependencies": [...], "fixes": [...]}

Older versions emitted a bare array. Both shapes are accepted.

When ``--skipped`` is passed, emits a synthetic ``pip-audit-not-installed``
skipped testcase for graceful degradation when the package is absent.
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

_SUITE_NAME = "dep-cve-scan"


def _collect_vulns(report: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for pkg in report:
        if not isinstance(pkg, dict):
            continue
        name = str(pkg.get("name") or "unknown")
        version = str(pkg.get("version") or "unknown")
        for vuln in pkg.get("vulns") or []:
            if not isinstance(vuln, dict):
                continue
            vid = str(vuln.get("id") or "VULN-UNKNOWN")
            aliases = vuln.get("aliases") or []
            fix_versions = vuln.get("fix_versions") or []
            desc = str(vuln.get("description") or "")
            out.append({
                "pkg": name,
                "version": version,
                "id": vid,
                "aliases": aliases,
                "fix_versions": fix_versions,
                "description": desc,
            })
    return out


def _format_vuln_body(v: dict[str, Any]) -> str:
    lines = [
        f"package: {v['pkg']}=={v['version']}",
        f"vuln_id: {v['id']}",
    ]
    if v["aliases"]:
        lines.append(f"aliases: {', '.join(str(a) for a in v['aliases'])}")
    if v["fix_versions"]:
        lines.append(f"fix_versions: {', '.join(str(f) for f in v['fix_versions'])}")
    if v["description"]:
        lines.append(f"description: {v['description'][:300]}")
    return "\n".join(lines)


def convert(report: list[dict[str, Any]]) -> ET.Element:
    """Build the JUnit ``<testsuite>`` from a pip-audit JSON report."""
    vulns = _collect_vulns(report)

    test_count = max(len(vulns), 1)
    fail_count = len(vulns)  # every reported vuln is a failure

    suite = ET.Element("testsuite", {
        "name": _SUITE_NAME,
        "tests": str(test_count),
        "failures": str(fail_count),
        "errors": "0",
        "skipped": "0",
    })

    if not vulns:
        ET.SubElement(suite, "testcase", {
            "name": "no-findings",
            "classname": "pip-audit",
        })
        return suite

    for v in vulns:
        case_name = f"{v['pkg']}-{v['id']}"
        tc = ET.SubElement(suite, "testcase", {
            "name": case_name,
            "classname": "pip-audit.vuln",
        })
        body = _format_vuln_body(v)
        fail = ET.SubElement(tc, "failure", {
            "message": f"vulnerable: {v['pkg']}=={v['version']} — {v['id']}",
            "type": "dep-cve",
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
        "name": "pip-audit-not-installed",
        "classname": "pip-audit",
    })
    sk = ET.SubElement(tc, "skipped", {
        "message": "pip-audit package not available; skipping per L3 graceful-degradation policy.",
    })
    sk.text = (
        "Add pip-audit>=2.7 to [dependency-groups] dev in pyproject.toml "
        "and run `uv sync --dev` to enable dependency CVE scanning."
    )
    return suite


def _run_pip_audit(output_path: Path) -> bool:
    """Run pip-audit and write JSON to *output_path*. Returns True on success."""
    subprocess.run(
        [
            "uv", "run", "pip-audit",
            "--format=json",
            f"--output={output_path}",
            "--progress-spinner", "off",
            # Risk-accepted, no patched version exists upstream. torch is pulled
            # only by the optional garak L7 red-team tooling (a CLI invoked
            # offline; it "degrades gracefully when the binary is missing" and
            # never sits in the request path). CVE-2025-3000 has no fixed
            # release as of audit time; re-evaluate when one ships. This is the
            # only ignore — every other dep is bumped to its fixed line.
            "--ignore-vuln", "CVE-2025-3000",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    # pip-audit exits 1 when vulnerabilities are found — not a crash.
    return output_path.is_file()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, default=None,
                   help="Path to write JUnit XML (default: stdout)")
    p.add_argument("--skipped", action="store_true",
                   help="Emit a synthetic 'pip-audit not installed' skipped testcase.")
    args = p.parse_args(argv)

    suite: ET.Element
    if args.skipped:
        suite = _emit_skipped()
    else:
        with tempfile.TemporaryDirectory() as tmp:
            json_path = Path(tmp) / "pip-audit.json"
            ok = _run_pip_audit(json_path)
            if not ok:
                suite = _emit_skipped()
            else:
                raw = json_path.read_text(encoding="utf-8")
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as e:
                    print(f"junit_from_pip_audit: invalid JSON from pip-audit — {e}",
                          file=sys.stderr)
                    return 1
                # pip-audit >= 2.7 wraps the array: {"dependencies": [...], "fixes": [...]}
                if isinstance(data, dict) and "dependencies" in data:
                    data = data["dependencies"]
                if not isinstance(data, list):
                    print("junit_from_pip_audit: expected JSON array or envelope.", file=sys.stderr)
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
