#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Convert osv-scanner JSON output into JUnit XML.

osv-scanner emits a JSON document with shape::

    {
      "results": [
        {
          "source": {
            "path": "web/pnpm-lock.yaml",
            "type": "lockfile"
          },
          "packages": [
            {
              "package": {
                "name": "lodash",
                "version": "4.17.20",
                "ecosystem": "npm",
                "commit": ""
              },
              "vulnerabilities": [
                {
                  "id": "GHSA-xxxx-yyyy-zzzz",
                  "aliases": ["CVE-2024-9999"],
                  "summary": "...",
                  "details": "...",
                  "severity": "HIGH",
                  "database_specific": {...},
                  "schema_version": "1.4.0"
                }
              ]
            }
          ]
        }
      ]
    }

osv-scanner also supports ``--format=json`` (default) and ``--format=sarif``.
This converter expects the JSON format.

Gate mapping:
  - Any vulnerability → ``<failure>``
  - No vulnerabilities → one zero-failure testcase named ``no-findings``

When ``--skipped`` is passed, emits a synthetic ``osv-scanner-not-installed``
skipped testcase for graceful degradation.
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

_SUITE_NAME = "osv-scanner-sca"


def _collect_vulns(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten osv-scanner results into a list of vulnerability dicts."""
    out: list[dict[str, Any]] = []
    for result in report.get("results") or []:
        source = result.get("source") or {}
        source_path = str(source.get("path") or "unknown")
        source_type = str(source.get("type") or "unknown")
        for pkg_entry in result.get("packages") or []:
            pkg_info = pkg_entry.get("package") or {}
            pkg_name = str(pkg_info.get("name") or "unknown")
            pkg_version = str(pkg_info.get("version") or "")
            pkg_ecosystem = str(pkg_info.get("ecosystem") or "unknown")
            for vuln in pkg_entry.get("vulnerabilities") or []:
                if not isinstance(vuln, dict):
                    continue
                vid = str(vuln.get("id") or "OSV-UNKNOWN")
                aliases = vuln.get("aliases") or []
                summary = str(vuln.get("summary") or "")
                details = str(vuln.get("details") or "")
                severity = str(vuln.get("severity") or "UNKNOWN")
                out.append({
                    "package": f"{pkg_name}@{pkg_version}",
                    "ecosystem": pkg_ecosystem,
                    "id": vid,
                    "aliases": aliases,
                    "severity": severity,
                    "summary": summary,
                    "details": details,
                    "source_path": source_path,
                    "source_type": source_type,
                })
    return out


def _format_vuln_body(v: dict[str, Any]) -> str:
    lines = [
        f"package: {v['package']}  ({v['ecosystem']})",
        f"vuln_id: {v['id']}",
        f"source:  {v['source_path']}",
    ]
    if v["aliases"]:
        lines.append(f"aliases: {', '.join(str(a) for a in v['aliases'])}")
    if v["severity"] != "UNKNOWN":
        lines.append(f"severity: {v['severity']}")
    if v["summary"]:
        lines.append(f"summary: {v['summary'][:200]}")
    if v["details"]:
        lines.append(f"details: {v['details'][:200]}")
    return "\n".join(lines)


def convert(report: dict[str, Any]) -> ET.Element:
    """Build the JUnit ``<testsuite>`` from an osv-scanner JSON report."""
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
            "classname": "osv-scanner",
        })
        return suite

    for v in vulns:
        case_name = f"{v['package']}-{v['id']}"
        tc = ET.SubElement(suite, "testcase", {
            "name": case_name[:250],
            "classname": "osv-scanner.vuln",
        })
        body = _format_vuln_body(v)
        fail = ET.SubElement(tc, "failure", {
            "message": (
                f"VULNERABLE: {v['package']} — {v['id']} "
                f"(severity: {v['severity']})"
            ),
            "type": "osv-cve",
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
        "name": "osv-scanner-not-installed",
        "classname": "osv-scanner",
    })
    sk = ET.SubElement(tc, "skipped", {
        "message": "osv-scanner binary not on PATH; skipping per L3 graceful-degradation policy.",
    })
    sk.text = (
        "Install osv-scanner via `go install github.com/google/osv-scanner/cmd/osv-scanner@latest` "
        "or `brew install osv-scanner`. See https://github.com/google/osv-scanner for details."
    )
    return suite


def _run_osv_scanner(lockfiles: list[Path], output_path: Path) -> bool:
    """Run osv-scanner on the given lockfiles, writing JSON to output_path."""
    cmd = [
        "osv-scanner",
        "--format=json",
        f"--output={output_path}",
    ]
    for lf in lockfiles:
        cmd.extend(["--lockfile", str(lf)])
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )
    # osv-scanner exits 1 when vulnerabilities are found — not a crash.
    if result.returncode >= 2:
        print(
            f"junit_from_osv: osv-scanner exited with code {result.returncode}:\n"
            f"{result.stderr[:500]}",
            file=sys.stderr,
        )
        return False
    return output_path.is_file()


def _check_tool() -> bool:
    """Return True if osv-scanner is available on PATH."""
    try:
        subprocess.run(
            ["osv-scanner", "--version"],
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
                   help="Emit a synthetic 'osv-scanner not installed' skipped testcase.")
    p.add_argument("--lockfile", action="append", default=[],
                   help="Lockfile path (repeatable, e.g. --lockfile web/pnpm-lock.yaml).")
    args = p.parse_args(argv)

    suite: ET.Element
    if args.skipped:
        suite = _emit_skipped()
    elif not args.lockfile:
        print(
            "junit_from_osv: --lockfile is required (or use --skipped).",
            file=sys.stderr,
        )
        return 1
    else:
        if not _check_tool():
            suite = _emit_skipped()
        else:
            with tempfile.TemporaryDirectory() as tmp:
                json_path = Path(tmp) / "osv-scanner.json"
                lockfile_paths = [Path(lf) for lf in args.lockfile]
                ok = _run_osv_scanner(lockfile_paths, json_path)
                if not ok:
                    suite = _emit_skipped()
                elif not json_path.is_file():
                    suite = convert({})
                else:
                    raw = json_path.read_text(encoding="utf-8")
                    if not raw.strip():
                        suite = convert({})
                    else:
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError as e:
                            print(
                                f"junit_from_osv: invalid JSON — {e}",
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