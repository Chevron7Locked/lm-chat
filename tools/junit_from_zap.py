#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Convert OWASP ZAP baseline JSON output into JUnit XML.

ZAP ``zap-baseline.py -J <file>`` emits a JSON document with shape::

    {
      "@version": "2.18.0",
      "@generated": "Sat, 23 May 2026 ...",
      "site": [
        {
          "@name": "http://localhost:18001",
          "@host": "localhost",
          "@port": "18001",
          "@ssl": "false",
          "alerts": [
            {
              "pluginid": "10038",
              "alertRef": "10038-1",
              "alert": "Content Security Policy (CSP) Header Not Set",
              "name":  "Content Security Policy (CSP) Header Not Set",
              "riskcode": "2",
              "confidence": "3",
              "riskdesc": "Medium (High)",
              "desc": "<p>...</p>",
              "instances": [
                {"uri": "http://localhost:18001/", "method": "GET",
                 "param": "", "attack": "", "evidence": ""}
              ],
              "count": "5",
              "solution": "<p>...</p>",
              "reference": "<p>https://...</p>",
              "cweid": "693",
              "wascid": "15",
              "sourceid": "1"
            }, ...
          ]
        }, ...
      ]
    }

Per P15d brief — risk → JUnit mapping:

  - ``High`` (riskcode=3)      → ``<failure>``
  - ``Medium`` (riskcode=2)    → ``<failure>``
  - ``Low`` (riskcode=1)       → ``<skipped>``
  - ``Informational`` (riskcode=0)  → ``<skipped>``

Each ``pluginid`` becomes one ``<testcase>``; duplicates across sites or
URIs are collapsed and listed inside the failure body.

A profile config at ``tests/security/zap-baseline.conf`` (PLAN R-28)
whitelists known-false-positive ZAP plugin IDs; those become ``<skipped>``
regardless of risk.

When ``--skipped`` is passed, emits a synthetic ``zap-not-installed``
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

_SUITE_NAME = "l6-zap"

# riskcode → severity label (ZAP convention).
_RISK_LABELS = {
    "3": "HIGH",
    "2": "MEDIUM",
    "1": "LOW",
    "0": "INFORMATIONAL",
}

# Severities that fail the gate.
_FAIL_SEVERITIES = {"HIGH", "MEDIUM"}


def _load_whitelist(path: Path | None) -> set[str]:
    """Read whitelist plugin IDs (one per line, ``#`` comments)."""
    if path is None or not path.is_file():
        return set()
    out: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        # Strip inline comments after value.
        token = s.split("#", 1)[0].strip()
        if token:
            out.add(token)
    return out


def _norm_severity(alert: dict[str, Any]) -> str:
    """Resolve an alert's severity from riskcode (preferred) or riskdesc."""
    code = str(alert.get("riskcode") or "").strip()
    if code in _RISK_LABELS:
        return _RISK_LABELS[code]
    # Fall back to parsing riskdesc like "Medium (High)".
    desc = str(alert.get("riskdesc") or "").strip()
    head = desc.split("(", 1)[0].strip().upper()
    if head in _FAIL_SEVERITIES or head in {"LOW", "INFORMATIONAL"}:
        return head
    return "INFORMATIONAL"


def _collect_alerts(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten ZAP ``site[*].alerts[*]`` into a list of finding dicts."""
    out: list[dict[str, Any]] = []
    sites = report.get("site") or []
    if not isinstance(sites, list):
        return out
    for site in sites:
        if not isinstance(site, dict):
            continue
        site_name = str(site.get("@name") or site.get("@host") or "<unknown>")
        alerts = site.get("alerts") or []
        if not isinstance(alerts, list):
            continue
        for a in alerts:
            if not isinstance(a, dict):
                continue
            pid = str(a.get("pluginid") or a.get("alertRef") or "ZAP-UNKNOWN")
            instances = a.get("instances") or []
            inst_lines: list[str] = []
            if isinstance(instances, list):
                for inst in instances[:10]:  # cap, ZAP can produce thousands
                    if not isinstance(inst, dict):
                        continue
                    uri = str(inst.get("uri") or "")
                    method = str(inst.get("method") or "")
                    evidence = str(inst.get("evidence") or "")[:120]
                    inst_lines.append(
                        f"  {method} {uri}" + (f" — {evidence}" if evidence else "")
                    )
            out.append({
                "id": pid,
                "severity": _norm_severity(a),
                "site": site_name,
                "title": str(a.get("alert") or a.get("name") or ""),
                "cweid": str(a.get("cweid") or ""),
                "wascid": str(a.get("wascid") or ""),
                "count": str(a.get("count") or len(instances) or 1),
                "solution": str(a.get("solution") or "")[:500],
                "instances": inst_lines,
            })
    return out


def _format_finding_body(items: list[dict[str, Any]]) -> str:
    """Multi-line body summarising one plugin's findings."""
    head = items[0]
    bits = [
        f"plugin={head['id']}  severity={head['severity']}  count={head['count']}",
        f"title: {head['title']}",
    ]
    if head["cweid"]:
        bits.append(f"cwe={head['cweid']}  wasc={head['wascid']}")
    if head["solution"]:
        bits.append("solution:")
        bits.append(f"  {head['solution']}")
    if head["instances"]:
        bits.append("instances:")
        bits.extend(head["instances"])
    if len(items) > 1:
        bits.append(f"(+{len(items) - 1} sighting(s) across other sites)")
    return "\n".join(bits)


def convert(report: dict[str, Any], whitelist: set[str] | None = None) -> ET.Element:
    """Build the JUnit ``<testsuite>`` from a ZAP JSON report."""
    whitelist = whitelist or set()
    findings = _collect_alerts(report)

    by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for f in findings:
        by_id[f["id"]].append(f)

    test_count = max(len(by_id), 1)
    fail_count = 0
    skip_count = 0

    for pid, items in by_id.items():
        worst = next(
            (s for s in ["HIGH", "MEDIUM", "LOW", "INFORMATIONAL"]
             if any(i["severity"] == s for i in items)),
            "INFORMATIONAL",
        )
        if pid in whitelist:
            skip_count += 1
        elif worst in _FAIL_SEVERITIES:
            fail_count += 1
        else:
            skip_count += 1

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
            "classname": "zap",
        })
        return suite

    for pid, items in sorted(by_id.items()):
        worst = next(
            (s for s in ["HIGH", "MEDIUM", "LOW", "INFORMATIONAL"]
             if any(i["severity"] == s for i in items)),
            "INFORMATIONAL",
        )
        tc = ET.SubElement(suite, "testcase", {
            "name": pid,
            "classname": f"zap.{worst.lower()}",
        })
        body = _format_finding_body(items)
        if pid in whitelist:
            sk = ET.SubElement(tc, "skipped", {
                "message": f"whitelisted in zap-baseline.conf (was {worst})",
            })
            sk.text = body
        elif worst in _FAIL_SEVERITIES:
            fail = ET.SubElement(tc, "failure", {
                "message": f"{worst}: {items[0]['title'][:140]}",
                "type": f"zap-{worst.lower()}",
            })
            fail.text = body
        else:
            sk = ET.SubElement(tc, "skipped", {
                "message": f"{worst}: informational",
            })
            sk.text = body

    return suite


def _emit_skipped(reason: str = "") -> ET.Element:
    """Synthetic 'zap not installed' suite for graceful degradation."""
    suite = ET.Element("testsuite", {
        "name": _SUITE_NAME,
        "tests": "1",
        "failures": "0",
        "errors": "0",
        "skipped": "1",
    })
    tc = ET.SubElement(suite, "testcase", {
        "name": "zap-not-installed",
        "classname": "zap",
    })
    msg = reason or (
        "zap baseline scan was not run; install via Docker "
        "ghcr.io/zaproxy/zaproxy:stable"
    )
    sk = ET.SubElement(tc, "skipped", {"message": msg})
    sk.text = msg
    return suite


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=str, default="-",
                   help="Path to ZAP JSON, or '-' for stdin (default: -)")
    p.add_argument("--output", type=Path, default=None,
                   help="Path to write JUnit XML (default: stdout)")
    p.add_argument("--skipped", action="store_true",
                   help="Emit a synthetic 'zap not installed' skipped testcase.")
    p.add_argument("--skipped-reason", type=str, default="",
                   help="Override skipped-mode reason string.")
    p.add_argument("--whitelist", type=Path, default=None,
                   help="Optional path to zap-baseline.conf (PLAN R-28).")
    args = p.parse_args(argv)

    suite: ET.Element
    if args.skipped:
        suite = _emit_skipped(args.skipped_reason)
    else:
        raw = ""
        if args.input == "-":
            raw = sys.stdin.read()
            suite = _emit_skipped("zap JSON stdin was empty")
        else:
            in_path = Path(args.input)
            if not in_path.is_file():
                # Missing input → degrade gracefully.
                suite = _emit_skipped(
                    f"zap JSON missing at {in_path} — scan did not produce output"
                )
            else:
                raw = in_path.read_text(encoding="utf-8")
                suite = _emit_skipped("zap JSON file is empty")

        if raw.strip():
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                print(f"junit_from_zap: input is not valid JSON — {e}",
                      file=sys.stderr)
                return 1
            if not isinstance(data, dict):
                print("junit_from_zap: expected JSON object (zap report).",
                      file=sys.stderr)
                return 1
            whitelist = _load_whitelist(args.whitelist)
            suite = convert(data, whitelist=whitelist)

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
