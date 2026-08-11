#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Convert ``hadolint --format json`` output into JUnit XML.

Hadolint emits a JSON array; each entry has fields ``code``, ``message``,
``level``, ``file``, ``line``, ``column``. We bucket findings by code into
testcases, with one ``<failure>`` per finding under that code.

When ``--input`` is ``-`` or omitted, reads from stdin. Writes JUnit XML to
``--output`` (default stdout). The ``testsuite name="l0-hadolint"`` matches
the L9 aggregator's layer-attribution invariant.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

_ERROR_LEVELS = {"error", "warning"}  # fail-the-gate severities


def convert(findings: list[dict[str, Any]]) -> ET.Element:
    """Build the JUnit ``<testsuite>`` from a list of hadolint findings."""
    by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for f in findings:
        if not isinstance(f, dict):
            continue
        code = str(f.get("code") or "DLunknown")
        by_code[code].append(f)

    failing = sum(
        1 for code, items in by_code.items()
        if any(str(i.get("level", "")).lower() in _ERROR_LEVELS for i in items)
    )

    test_count = max(len(by_code), 1)

    suite = ET.Element("testsuite", {
        "name": "l0-hadolint",
        "tests": str(test_count),
        "failures": str(failing),
        "errors": "0",
        "skipped": "0",
    })

    if not by_code:
        ET.SubElement(suite, "testcase", {
            "name": "no-findings",
            "classname": "hadolint",
        })
        return suite

    for code, items in sorted(by_code.items()):
        tc = ET.SubElement(suite, "testcase", {
            "name": code,
            "classname": "hadolint",
        })
        errs = [i for i in items if str(i.get("level", "")).lower() in _ERROR_LEVELS]
        if errs:
            lines = [
                f"{i.get('file', 'Dockerfile')}:{i.get('line', 0)}:{i.get('column', 0)}: "
                f"[{i.get('level', '?')}] {code} {i.get('message', '')}"
                for i in errs
            ]
            failure = ET.SubElement(tc, "failure", {
                "message": f"{len(errs)} {code} finding(s)",
                "type": "hadolint",
            })
            failure.text = "\n".join(lines)
        # Info / style entries get attached as system-out (not failing).
        infos = [i for i in items if str(i.get("level", "")).lower() not in _ERROR_LEVELS]
        if infos:
            sysout = ET.SubElement(tc, "system-out")
            sysout.text = "\n".join(
                f"[{i.get('level', '?')}] {code} {i.get('message', '')}"
                for i in infos
            )

    return suite


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=str, default="-",
                   help="Path to hadolint JSON, or '-' for stdin (default: -)")
    p.add_argument("--output", type=Path, default=None,
                   help="Path to write JUnit XML (default: stdout)")
    p.add_argument("--skipped", action="store_true",
                   help="Emit a synthetic 'hadolint not installed' skipped testcase.")
    args = p.parse_args(argv)

    if args.skipped:
        suite = ET.Element("testsuite", {
            "name": "l0-hadolint",
            "tests": "1",
            "failures": "0",
            "errors": "0",
            "skipped": "1",
        })
        tc = ET.SubElement(suite, "testcase", {
            "name": "hadolint",
            "classname": "hadolint",
        })
        skipped_el = ET.SubElement(tc, "skipped", {
            "message": "hadolint binary not on PATH; skipping per L0 graceful-degradation policy.",
        })
        skipped_el.text = "Install hadolint to enable Dockerfile static analysis."
    else:
        if args.input == "-":
            raw = sys.stdin.read()
        else:
            raw = Path(args.input).read_text(encoding="utf-8")

        try:
            data = json.loads(raw) if raw.strip() else []
        except json.JSONDecodeError as e:
            print(f"junit_from_hadolint: input is not valid JSON — {e}", file=sys.stderr)
            return 1
        if not isinstance(data, list):
            print("junit_from_hadolint: expected JSON array of findings.", file=sys.stderr)
            return 1
        suite = convert(data)

    root = ET.Element("testsuites", {
        "name": "l0-hadolint",
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
