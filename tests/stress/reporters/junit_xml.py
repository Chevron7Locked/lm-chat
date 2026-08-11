# SPDX-License-Identifier: Apache-2.0
"""Emit a JUnit-XML pass/fail per scenario for CI consumption.

GitHub Actions, Buildkite, Jenkins all accept JUnit XML.  The schema
is minimal: one ``<testsuite>`` with one ``<testcase>`` per scenario.
Failures embed the SLO summary as the failure message so a CI dashboard
can render it without parsing JSON.
"""
from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path


def build(report_path: Path, out_path: Path) -> None:
    """Read ``slo_report.json`` and emit JUnit XML."""
    data = json.loads(report_path.read_text(encoding="utf-8"))
    suite = ET.Element(
        "testsuite",
        attrib={
            "name": "p14-stress",
            "tests": str(
                len(data.get("per_scenario") or {})
                + len(data.get("gate_outcomes") or [])
            ),
            "failures": str(
                0 if data.get("ok") else 1
            ),
        },
    )
    # One testcase per scenario.
    for name, metrics in (data.get("per_scenario") or {}).items():
        case = ET.SubElement(
            suite,
            "testcase",
            attrib={
                "classname": "stress.scenarios",
                "name": name,
                "time": "0.0",
            },
        )
        failures = int(metrics.get("failures") or 0)
        if failures > 0:
            fail = ET.SubElement(
                case,
                "failure",
                attrib={"message": f"{failures} failures recorded"},
            )
            fail.text = json.dumps(metrics, indent=2)
    # One testcase per gate outcome.
    for gate in (data.get("gate_outcomes") or []):
        case = ET.SubElement(
            suite,
            "testcase",
            attrib={
                "classname": "stress.gates",
                "name": gate.get("name", "unknown"),
                "time": "0.0",
            },
        )
        if not gate.get("ok"):
            fail = ET.SubElement(
                case,
                "failure",
                attrib={"message": gate.get("detail", "gate failed")},
            )
            fail.text = json.dumps(gate, indent=2)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(suite).write(out_path, encoding="utf-8", xml_declaration=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    build(args.report, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
