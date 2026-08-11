#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Convert ``pyright --outputjson`` output into JUnit XML.

Pyright doesn't emit JUnit natively, so the L0 layer pipes pyright's JSON
through this adapter. Each analysed file becomes a ``<testcase>``; each
diagnostic of severity ``error`` becomes a ``<failure>`` on that testcase
(warnings/information are reported as ``<system-out>`` to preserve them
without flipping the gate red).

Reads pyright JSON from stdin OR from ``--input PATH``. Writes JUnit XML to
``--output PATH`` (default stdout).

The XML uses ``testsuite name="l0-pyright"`` so the L9 aggregator can
attribute findings to the right layer.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


def _safe_get(obj: Any, *keys: str, default: Any = "") -> Any:
    """Walk a nested dict; return default at first missing key."""
    cur = obj
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def convert(pyright_json: dict[str, Any]) -> ET.Element:
    """Build a JUnit ``<testsuite>`` element from pyright JSON."""
    diagnostics = pyright_json.get("generalDiagnostics") or []
    summary = pyright_json.get("summary") or {}

    # Group diagnostics by file. Severity ``error`` is a JUnit ``<failure>``;
    # warning/information get attached to system-out so they're preserved but
    # don't fail the gate (matches our existing ruff/bandit convention).
    by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for d in diagnostics:
        if not isinstance(d, dict):
            continue
        file_path = str(d.get("file") or "<unknown>")
        by_file[file_path].append(d)

    # If pyright reports nothing, emit a single OK testcase so the suite is
    # non-empty (some JUnit consumers reject empty suites).
    analysed = int(summary.get("filesAnalyzed", 0) or 0)
    error_count = int(summary.get("errorCount", 0) or 0)
    warning_count = int(summary.get("warningCount", 0) or 0)

    # Files-with-errors count drives `failures`; testcase count = filesAnalyzed
    # when known, otherwise = files-with-diagnostics + 1 (synthetic OK case).
    files_with_errors = sum(
        1 for diags in by_file.values()
        if any(d.get("severity") == "error" for d in diags)
    )

    testcase_count = max(analysed, len(by_file), 1)

    suite = ET.Element("testsuite", {
        "name": "l0-pyright",
        "tests": str(testcase_count),
        "failures": str(files_with_errors),
        "errors": "0",
        "skipped": "0",
        "time": str(summary.get("timeInSec", 0) or 0),
    })

    if not by_file:
        # All clean — emit a single synthetic OK testcase.
        ET.SubElement(suite, "testcase", {
            "name": "no-diagnostics",
            "classname": "pyright",
        })
        return suite

    for file_path, diags in sorted(by_file.items()):
        rel = file_path
        # Try to make path repo-relative for readability.
        try:
            rel = str(Path(file_path).relative_to(Path.cwd()))
        except (ValueError, OSError):
            pass

        tc = ET.SubElement(suite, "testcase", {
            "name": rel,
            "classname": "pyright",
        })

        errors = [d for d in diags if d.get("severity") == "error"]
        warnings = [d for d in diags if d.get("severity") == "warning"]

        if errors:
            msg_lines: list[str] = []
            for d in errors:
                line = _safe_get(d, "range", "start", "line", default=0)
                col = _safe_get(d, "range", "start", "character", default=0)
                # pyright lines/columns are 0-based; bump to 1-based for users.
                msg_lines.append(
                    f"{rel}:{int(line) + 1}:{int(col) + 1}: {d.get('message', '')}"
                )
            failure = ET.SubElement(tc, "failure", {
                "message": f"{len(errors)} error(s)",
                "type": "pyright-error",
            })
            failure.text = "\n".join(msg_lines)

        if warnings:
            warn_lines = [
                f"{rel}:{int(_safe_get(d, 'range', 'start', 'line', default=0)) + 1}: "
                f"{d.get('message', '')}"
                for d in warnings
            ]
            sysout = ET.SubElement(tc, "system-out")
            sysout.text = "\n".join(warn_lines)

    # Sanity: error_count from summary should match what we counted.
    if error_count and error_count != sum(
        len([d for d in v if d.get("severity") == "error"]) for v in by_file.values()
    ):
        # Don't crash — just note the discrepancy.
        note = ET.SubElement(suite, "system-out")
        note.text = (
            f"NOTE: pyright summary reported {error_count} errors but the "
            f"diagnostics list contained a different count."
        )

    if warning_count:
        pass  # warnings already attached per-testcase

    return suite


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=Path, default=None,
                   help="Path to pyright JSON (default: stdin)")
    p.add_argument("--output", type=Path, default=None,
                   help="Path to write JUnit XML (default: stdout)")
    args = p.parse_args(argv)

    if args.input is not None:
        raw = args.input.read_text(encoding="utf-8")
    else:
        raw = sys.stdin.read()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"junit_from_pyright: input is not valid JSON — {e}", file=sys.stderr)
        return 1

    suite = convert(data)

    # Wrap in <testsuites> so the file is a valid JUnit aggregate.
    root = ET.Element("testsuites", {
        "name": "l0-pyright",
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
