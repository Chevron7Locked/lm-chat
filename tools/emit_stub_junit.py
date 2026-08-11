#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Emit a 'not yet implemented' JUnit XML for a stub layer.

Used by Makefile stub targets for L1..L8 until those layers get real
implementations. Produces a single testsuite containing one skipped
testcase named ``not-yet-implemented`` so the L9 aggregator recognises
the layer as a stub (see aggregate_junit.py:_is_stub).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from xml.etree import ElementTree as ET


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--layer", type=int, required=True)
    p.add_argument("--slug", required=True, help="Layer slug, e.g. 'license'")
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args(argv)

    suite_name = f"l{args.layer}-{args.slug}"
    root = ET.Element("testsuites", {
        "name": suite_name,
        "tests": "1", "failures": "0", "errors": "0", "skipped": "1",
    })
    suite = ET.SubElement(root, "testsuite", {
        "name": suite_name,
        "tests": "1", "failures": "0", "errors": "0", "skipped": "1",
    })
    tc = ET.SubElement(suite, "testcase", {
        "name": "not-yet-implemented",
        "classname": "stub",
    })
    sk = ET.SubElement(tc, "skipped", {"message": "P15a stub"})
    sk.text = (
        f"Layer {args.layer} ({args.slug}) is a P15a placeholder; "
        "replaced by a later sub-cluster."
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(ET.ElementTree(root), space="  ")
    ET.ElementTree(root).write(args.output, encoding="utf-8", xml_declaration=True)
    print(f"[L{args.layer} {args.slug}] stub JUnit written -> {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
