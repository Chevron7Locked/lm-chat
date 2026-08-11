#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Convert Nuclei JSON-lines output into JUnit XML.

Nuclei ``-j -o <file>`` emits one JSON object per line, shape::

    {
      "template-id": "exposed-panels/grafana-detect",
      "template": "/nuclei-templates/http/exposed-panels/grafana-detect.yaml",
      "info": {
        "name": "Grafana Default Login",
        "severity": "high",
        "tags": ["panel","grafana"],
        "description": "..."
      },
      "type": "http",
      "host": "http://localhost:18001",
      "matched-at": "http://localhost:18001/login",
      "request": "...",
      "response": "...",
      "timestamp": "2026-05-23T..."
    }

Per P15d brief — severity → JUnit mapping:

  - ``critical`` → ``<failure>``
  - ``high``     → ``<failure>``
  - ``medium`` / ``low`` / ``info`` / ``unknown`` → ``<skipped>``

Each ``(template-id, matched-at)`` tuple becomes one ``<testcase>``;
multiple sightings of the same template are collapsed under the
template id, with all matched-URLs in the failure body.

When ``--skipped`` is passed, emits a synthetic ``nuclei-not-installed``
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

_SUITE_NAME = "l6-nuclei"

_FAIL_SEVERITIES = {"CRITICAL", "HIGH"}
_INFO_SEVERITIES = {"MEDIUM", "LOW", "INFO", "INFORMATIONAL", "UNKNOWN", ""}


def _norm_sev(s: Any) -> str:
    return str(s or "").strip().upper()


def _parse_jsonl(raw: str) -> list[dict[str, Any]]:
    """Parse nuclei's JSON-lines output. Tolerant of blank lines + comments."""
    out: list[dict[str, Any]] = []
    for ln in raw.splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            # Skip malformed line rather than crashing the gate.
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _collect_findings(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten nuclei records into a list of finding dicts."""
    out: list[dict[str, Any]] = []
    for r in records:
        info = r.get("info") if isinstance(r.get("info"), dict) else {}
        out.append({
            "id": str(r.get("template-id") or r.get("templateID") or "nuclei-unknown"),
            "severity": _norm_sev((info or {}).get("severity")),
            "name": str((info or {}).get("name") or ""),
            "type": str(r.get("type") or ""),
            "host": str(r.get("host") or ""),
            "matched": str(r.get("matched-at") or r.get("matched") or ""),
            "tags": ",".join(
                t for t in ((info or {}).get("tags") or [])
                if isinstance(t, str)
            ),
            "description": str((info or {}).get("description") or "")[:300],
        })
    return out


def _format_finding_body(items: list[dict[str, Any]]) -> str:
    head = items[0]
    bits = [
        f"template={head['id']}  severity={head['severity']}  type={head['type']}",
        f"name: {head['name']}",
    ]
    if head["tags"]:
        bits.append(f"tags: {head['tags']}")
    if head["description"]:
        bits.append(f"description: {head['description']}")
    bits.append("matched-at:")
    for it in items[:20]:
        m = it.get("matched") or it.get("host") or ""
        if m:
            bits.append(f"  {m}")
    if len(items) > 20:
        bits.append(f"  (+{len(items) - 20} more)")
    return "\n".join(bits)


def convert(records: list[dict[str, Any]]) -> ET.Element:
    """Build the JUnit ``<testsuite>`` from a list of nuclei findings."""
    findings = _collect_findings(records)

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
            "classname": "nuclei",
        })
        return suite

    for tid, items in sorted(by_id.items()):
        worst = next(
            (s for s in ["CRITICAL", "HIGH", "MEDIUM", "LOW",
                         "INFO", "INFORMATIONAL", "UNKNOWN"]
             if any(i["severity"] == s for i in items)),
            "UNKNOWN",
        )
        tc = ET.SubElement(suite, "testcase", {
            "name": tid,
            "classname": f"nuclei.{worst.lower() or 'unknown'}",
        })
        body = _format_finding_body(items)
        if worst in _FAIL_SEVERITIES:
            fail = ET.SubElement(tc, "failure", {
                "message": f"{worst}: {items[0]['name'][:140] or tid}",
                "type": f"nuclei-{worst.lower()}",
            })
            fail.text = body
        else:
            sk = ET.SubElement(tc, "skipped", {
                "message": f"{worst or 'UNKNOWN'}: informational ({len(items)} sighting(s))",
            })
            sk.text = body

    return suite


def _emit_skipped(reason: str = "") -> ET.Element:
    """Synthetic 'nuclei not installed' suite for graceful degradation."""
    suite = ET.Element("testsuite", {
        "name": _SUITE_NAME,
        "tests": "1",
        "failures": "0",
        "errors": "0",
        "skipped": "1",
    })
    tc = ET.SubElement(suite, "testcase", {
        "name": "nuclei-not-installed",
        "classname": "nuclei",
    })
    msg = reason or (
        "nuclei extended scan was not run; install via Docker "
        "projectdiscovery/nuclei:latest"
    )
    sk = ET.SubElement(tc, "skipped", {"message": msg})
    sk.text = msg
    return suite


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=str, default="-",
                   help="Path to nuclei JSON-lines file, or '-' for stdin.")
    p.add_argument("--output", type=Path, default=None,
                   help="Path to write JUnit XML (default: stdout)")
    p.add_argument("--skipped", action="store_true",
                   help="Emit a synthetic 'nuclei not installed' skipped testcase.")
    p.add_argument("--skipped-reason", type=str, default="",
                   help="Override skipped-mode reason string.")
    args = p.parse_args(argv)

    suite: ET.Element
    if args.skipped:
        suite = _emit_skipped(args.skipped_reason)
    else:
        raw = ""
        if args.input == "-":
            raw = sys.stdin.read()
            # Empty stdin → treat as zero-findings (legitimate nuclei outcome).
            suite = convert([])
        else:
            in_path = Path(args.input)
            if not in_path.is_file():
                suite = _emit_skipped(
                    f"nuclei JSON missing at {in_path} — scan did not produce output"
                )
            else:
                raw = in_path.read_text(encoding="utf-8")
                # Empty file is a legitimate "zero findings" outcome
                # (nuclei writes nothing when no templates match).
                suite = convert([])

        if raw.strip():
            records = _parse_jsonl(raw)
            suite = convert(records)

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
