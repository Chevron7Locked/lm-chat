#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Convert ``dockle --format json`` output into JUnit XML.

Dockle emits a JSON document with shape::

    {
      "summary": {"fatal": 0, "warn": 2, "info": 1, "skip": 0, "pass": 31},
      "details": [
        {
          "code": "CIS-DI-0001",
          "title": "Create a user for the container",
          "level": "FATAL",   # FATAL | WARN | INFO | SKIP | PASS
          "alerts": ["Last user should not be root"]
        }, ...
      ]
    }

Each ``code`` becomes one ``<testcase>``. ``FATAL`` and ``WARN`` are
``<failure>`` (image hardening regressions); ``INFO`` and ``SKIP`` become
``<skipped>``; ``PASS`` is recorded as ``<system-out>`` so the testcase
counts but doesn't show as failed.

When ``--skipped`` is passed, emits a synthetic ``dockle-not-installed``
skipped testcase for graceful degradation.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

# Levels that fail the gate.
_FAIL_LEVELS = {"FATAL", "WARN"}
# Levels that are informational only.
_INFO_LEVELS = {"INFO", "SKIP"}


def _norm_level(s: Any) -> str:
    return str(s or "").strip().upper()


def convert(report: dict[str, Any]) -> ET.Element:
    """Build the JUnit ``<testsuite>`` from a Dockle JSON report."""
    details = report.get("details") or []
    if not isinstance(details, list):
        details = []

    test_count = max(len(details), 1)
    failures = 0
    skipped = 0

    suite = ET.Element("testsuite", {
        "name": "l4-dockle",
        "tests": str(test_count),
        "failures": "0",
        "errors": "0",
        "skipped": "0",
    })

    if not details:
        ET.SubElement(suite, "testcase", {
            "name": "no-findings",
            "classname": "dockle",
        })
        return suite

    for d in details:
        if not isinstance(d, dict):
            continue
        code = str(d.get("code") or "DKL-UNKNOWN")
        title = str(d.get("title") or "")
        level = _norm_level(d.get("level"))
        alerts = d.get("alerts") or []
        if not isinstance(alerts, list):
            alerts = [str(alerts)]
        alert_text = "\n".join(str(a) for a in alerts) or title

        tc = ET.SubElement(suite, "testcase", {
            "name": code,
            "classname": f"dockle.{level.lower() or 'unknown'}",
        })

        if level in _FAIL_LEVELS:
            failures += 1
            fail = ET.SubElement(tc, "failure", {
                "message": f"{level}: {title}" if title else level,
                "type": f"dockle-{level.lower()}",
            })
            fail.text = alert_text
        elif level in _INFO_LEVELS:
            skipped += 1
            sk = ET.SubElement(tc, "skipped", {
                "message": f"{level}: {title}" if title else level,
            })
            sk.text = alert_text
        else:
            # PASS or anything unexpected — record but don't fail.
            sysout = ET.SubElement(tc, "system-out")
            sysout.text = f"[{level}] {title}\n{alert_text}"

    suite.set("failures", str(failures))
    suite.set("skipped", str(skipped))
    return suite


def _emit_skipped() -> ET.Element:
    """Synthetic 'dockle not installed' suite for graceful degradation."""
    suite = ET.Element("testsuite", {
        "name": "l4-dockle",
        "tests": "1",
        "failures": "0",
        "errors": "0",
        "skipped": "1",
    })
    tc = ET.SubElement(suite, "testcase", {
        "name": "dockle-not-installed",
        "classname": "dockle",
    })
    sk = ET.SubElement(tc, "skipped", {
        "message": "dockle binary not on PATH; skipping per L4 graceful-degradation policy.",
    })
    sk.text = (
        "Install dockle (https://github.com/goodwithtech/dockle) to enable "
        "container hardening checks."
    )
    return suite


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=str, default="-",
                   help="Path to dockle JSON, or '-' for stdin (default: -)")
    p.add_argument("--output", type=Path, default=None,
                   help="Path to write JUnit XML (default: stdout)")
    p.add_argument("--skipped", action="store_true",
                   help="Emit a synthetic 'dockle not installed' skipped testcase.")
    args = p.parse_args(argv)

    if args.skipped:
        suite = _emit_skipped()
    else:
        if args.input == "-":
            raw = sys.stdin.read()
        elif not Path(args.input).is_file():
            # dockle produced no output (binary errored, e.g. could not read the
            # image on this host). Degrade gracefully like an absent tool rather
            # than crashing the L4 gate with FileNotFoundError.
            print(
                f"junit_from_dockle: input {args.input} missing — emitting skipped",
                file=sys.stderr,
            )
            raw = ""
        else:
            raw = Path(args.input).read_text(encoding="utf-8")

        if not raw.strip():
            suite = _emit_skipped()
        else:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                print(f"junit_from_dockle: input is not valid JSON — {e}", file=sys.stderr)
                return 1
            if not isinstance(data, dict):
                print("junit_from_dockle: expected JSON object (dockle report).", file=sys.stderr)
                return 1
            suite = convert(data)

    root = ET.Element("testsuites", {
        "name": "l4-dockle",
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
