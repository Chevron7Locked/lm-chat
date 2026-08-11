#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Normalise schemathesis JUnit XML so the L9 aggregator can read it.

schemathesis 4.19 emits JUnit XML when ``--report junit`` is passed, but
its ``<testsuites name="...">`` attribute uses the tool name
("schemathesis") rather than the layer slug the aggregator expects
("l5-dast-offline"); it also emits without an XML declaration, which the
aggregator's strict parser dislikes.

This wrapper reads the schemathesis XML (or a "skipped" sentinel if
schemathesis did not run, e.g. because the backend never came up) and
writes a normalised file with:

  - ``<testsuites name="l5-schemathesis">`` root
  - XML declaration
  - Pre-existing ``<testsuite>`` / ``<testcase>`` elements preserved

If the input file is missing or unparseable, this script emits a single
``<testcase>`` with ``<error>`` so the aggregator surfaces the failure
(rather than silently treating it as "no findings").

Usage::

    python tools/junit_from_schemathesis.py \\
        --input  target/gates/L5-schemathesis-raw.xml \\
        --output target/gates/L5-schemathesis.xml
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

_SUITE_NAME = "l5-schemathesis"


def _emit_error(output: Path, message: str) -> None:
    """Write a single-error JUnit XML to *output*."""
    root = ET.Element(
        "testsuites",
        {
            "name": _SUITE_NAME,
            "tests": "1", "failures": "0", "errors": "1", "skipped": "0",
        },
    )
    suite = ET.SubElement(
        root,
        "testsuite",
        {
            "name": _SUITE_NAME,
            "tests": "1", "failures": "0", "errors": "1", "skipped": "0",
        },
    )
    tc = ET.SubElement(
        suite,
        "testcase",
        {"classname": "schemathesis", "name": "schemathesis-run"},
    )
    err = ET.SubElement(tc, "error", {"message": message, "type": "SchemathesisError"})
    err.text = message
    output.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(ET.ElementTree(root), space="  ")
    ET.ElementTree(root).write(output, encoding="utf-8", xml_declaration=True)


def _emit_skipped(output: Path, reason: str) -> None:
    """Write a single-skipped JUnit XML to *output*."""
    root = ET.Element(
        "testsuites",
        {
            "name": _SUITE_NAME,
            "tests": "1", "failures": "0", "errors": "0", "skipped": "1",
        },
    )
    suite = ET.SubElement(
        root,
        "testsuite",
        {
            "name": _SUITE_NAME,
            "tests": "1", "failures": "0", "errors": "0", "skipped": "1",
        },
    )
    tc = ET.SubElement(
        suite,
        "testcase",
        {"classname": "schemathesis", "name": "schemathesis-run"},
    )
    sk = ET.SubElement(tc, "skipped", {"message": reason})
    sk.text = reason
    output.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(ET.ElementTree(root), space="  ")
    ET.ElementTree(root).write(output, encoding="utf-8", xml_declaration=True)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument(
        "--skipped-reason",
        default="",
        help="If set, emit a skipped sentinel instead of parsing --input.",
    )
    args = p.parse_args(argv)

    if args.skipped_reason:
        _emit_skipped(args.output, args.skipped_reason)
        print(f"[schemathesis] emitted skipped sentinel -> {args.output}")
        return 0

    if not args.input.is_file():
        _emit_error(args.output, f"schemathesis input file missing: {args.input}")
        print(
            f"[schemathesis] WARN: input {args.input} missing; emitted error sentinel",
            file=sys.stderr,
        )
        return 1

    try:
        tree = ET.parse(args.input)
    except ET.ParseError as exc:
        _emit_error(args.output, f"schemathesis XML parse error: {exc}")
        print(f"[schemathesis] WARN: {exc}", file=sys.stderr)
        return 1

    src_root = tree.getroot()
    # Re-tag the root to the canonical suite name expected by the aggregator.
    new_root = ET.Element("testsuites", dict(src_root.attrib))
    new_root.set("name", _SUITE_NAME)
    for child in src_root:
        new_root.append(child)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(ET.ElementTree(new_root), space="  ")
    ET.ElementTree(new_root).write(
        args.output, encoding="utf-8", xml_declaration=True
    )

    # Surface failure count to caller exit code so the Makefile can decide.
    fails = sum(int(s.get("failures", "0")) for s in new_root.iter("testsuite"))
    errs = sum(int(s.get("errors", "0")) for s in new_root.iter("testsuite"))
    print(
        f"[schemathesis] normalised -> {args.output} "
        f"(failures={fails}, errors={errs})"
    )
    return 0 if (fails == 0 and errs == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
