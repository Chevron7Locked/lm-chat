#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Convert ``trivy image --format json`` output into JUnit XML.

Trivy emits a JSON document with shape::

    {
      "ArtifactName": "deploy-lmchat:latest",
      "ArtifactType": "container_image",
      "Results": [
        {
          "Target": "deploy-lmchat:latest (debian 12.x)",
          "Class": "os-pkgs",
          "Type": "debian",
          "Vulnerabilities": [
            {
              "VulnerabilityID": "CVE-2024-...",
              "PkgName": "libfoo",
              "Severity": "HIGH",
              "Title": "...",
              "Description": "...",
              "InstalledVersion": "1.2.3",
              "FixedVersion": "1.2.4"
            }, ...
          ],
          "Secrets": [...],
          "Misconfigurations": [...]
        }, ...
      ]
    }

We bucket findings by severity. ``CRITICAL`` and ``HIGH`` become
``<failure>``; ``MEDIUM`` / ``LOW`` / ``UNKNOWN`` become ``<skipped>``
(informational only — they don't flip the gate red).

Each ``VulnerabilityID``/``RuleID``/``ID`` becomes one ``<testcase>``;
duplicates across packages are collapsed and listed inside the failure
body. The ``testsuite name="l4-trivy"`` matches the L9 aggregator's
layer-attribution invariant.

When ``--skipped`` is passed, emits a synthetic ``trivy-not-installed``
skipped testcase for graceful degradation when the binary is absent.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

# Severities that fail the gate (mapped to <failure>).
_FAIL_SEVERITIES = {"CRITICAL", "HIGH"}
# Severities that are informational (mapped to <skipped>).
_INFO_SEVERITIES = {"MEDIUM", "LOW", "UNKNOWN", "NEGLIGIBLE", ""}


def _norm_sev(s: Any) -> str:
    """Normalise a trivy severity string."""
    return str(s or "").strip().upper()


def _collect_findings(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten Trivy ``Results[*]`` into a list of finding dicts.

    Each finding has at minimum: ``id``, ``severity``, ``target``,
    ``kind`` (``vulnerability`` | ``secret`` | ``misconfig``), and ``title``.
    """
    out: list[dict[str, Any]] = []
    results = report.get("Results") or []
    if not isinstance(results, list):
        return out
    for r in results:
        if not isinstance(r, dict):
            continue
        target = str(r.get("Target") or r.get("Class") or "<unknown>")

        for v in r.get("Vulnerabilities") or []:
            if not isinstance(v, dict):
                continue
            out.append({
                "id": str(v.get("VulnerabilityID") or "CVE-UNKNOWN"),
                "severity": _norm_sev(v.get("Severity")),
                "target": target,
                "kind": "vulnerability",
                "title": str(v.get("Title") or v.get("Description") or ""),
                "pkg": str(v.get("PkgName") or ""),
                "installed": str(v.get("InstalledVersion") or ""),
                "fixed": str(v.get("FixedVersion") or ""),
            })

        for s in r.get("Secrets") or []:
            if not isinstance(s, dict):
                continue
            out.append({
                "id": str(s.get("RuleID") or "SECRET-UNKNOWN"),
                "severity": _norm_sev(s.get("Severity")),
                "target": target,
                "kind": "secret",
                "title": str(s.get("Title") or s.get("Category") or "secret"),
                "match": str(s.get("Match") or "")[:200],
            })

        for m in r.get("Misconfigurations") or []:
            if not isinstance(m, dict):
                continue
            out.append({
                "id": str(m.get("ID") or m.get("AVDID") or "MISCONF-UNKNOWN"),
                "severity": _norm_sev(m.get("Severity")),
                "target": target,
                "kind": "misconfig",
                "title": str(m.get("Title") or m.get("Description") or ""),
                "resolution": str(m.get("Resolution") or ""),
            })
    return out


def _format_finding_line(f: dict[str, Any]) -> str:
    """One-line summary of a finding for the failure body."""
    bits = [f"[{f['kind']}]", f"{f['severity']}", f["id"]]
    if f.get("pkg"):
        v = f.get("installed") or "?"
        fv = f.get("fixed") or "no-fix"
        bits.append(f"pkg={f['pkg']} {v}→{fv}")
    if f.get("title"):
        bits.append(f"— {f['title'][:140]}")
    bits.append(f"@ {f['target']}")
    return " ".join(bits)


def convert(report: dict[str, Any]) -> ET.Element:
    """Build the JUnit ``<testsuite>`` from a Trivy JSON report."""
    findings = _collect_findings(report)

    # Bucket by id so each CVE / rule = one <testcase> aggregating sightings.
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
        "name": "l4-trivy",
        "tests": str(test_count),
        "failures": str(fail_count),
        "errors": "0",
        "skipped": str(skip_count),
    })

    if not by_id:
        ET.SubElement(suite, "testcase", {
            "name": "no-findings",
            "classname": "trivy",
        })
        return suite

    for fid, items in sorted(by_id.items()):
        # Use the highest severity in the group for triage.
        sev_order = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN"]
        worst = next(
            (s for s in sev_order if any(i["severity"] == s for i in items)),
            "UNKNOWN",
        )
        tc = ET.SubElement(suite, "testcase", {
            "name": fid,
            "classname": f"trivy.{items[0]['kind']}",
        })
        body = "\n".join(_format_finding_line(i) for i in items)

        if worst in _FAIL_SEVERITIES:
            fail = ET.SubElement(tc, "failure", {
                "message": f"{worst}: {len(items)} sighting(s) of {fid}",
                "type": f"trivy-{worst.lower()}",
            })
            fail.text = body
        else:
            sk = ET.SubElement(tc, "skipped", {
                "message": f"{worst or 'UNKNOWN'}: informational ({len(items)} sighting(s))",
            })
            sk.text = body

    return suite


def _emit_skipped() -> ET.Element:
    """Synthetic 'trivy not installed' suite for graceful degradation."""
    suite = ET.Element("testsuite", {
        "name": "l4-trivy",
        "tests": "1",
        "failures": "0",
        "errors": "0",
        "skipped": "1",
    })
    tc = ET.SubElement(suite, "testcase", {
        "name": "trivy-not-installed",
        "classname": "trivy",
    })
    sk = ET.SubElement(tc, "skipped", {
        "message": "trivy binary not on PATH; skipping per L4 graceful-degradation policy.",
    })
    sk.text = (
        "Install trivy (https://aquasecurity.github.io/trivy/) "
        "to enable container vulnerability scanning."
    )
    return suite


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=str, default="-",
                   help="Path to trivy JSON, or '-' for stdin (default: -)")
    p.add_argument("--output", type=Path, default=None,
                   help="Path to write JUnit XML (default: stdout)")
    p.add_argument("--skipped", action="store_true",
                   help="Emit a synthetic 'trivy not installed' skipped testcase.")
    args = p.parse_args(argv)

    if args.skipped:
        suite = _emit_skipped()
    else:
        if args.input == "-":
            raw = sys.stdin.read()
        else:
            raw = Path(args.input).read_text(encoding="utf-8")

        if not raw.strip():
            # Empty / missing file → degrade gracefully rather than crashing.
            suite = _emit_skipped()
        else:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                print(f"junit_from_trivy: input is not valid JSON — {e}", file=sys.stderr)
                return 1
            if not isinstance(data, dict):
                print("junit_from_trivy: expected JSON object (trivy report).", file=sys.stderr)
                return 1
            suite = convert(data)

    root = ET.Element("testsuites", {
        "name": "l4-trivy",
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
