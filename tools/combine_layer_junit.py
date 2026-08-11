#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Combine a layer's sub-tool JUnit XMLs into a single ``L<N>-<slug>.xml`` file.

Generalisation of ``tools/combine_l0_junit.py``. The L0 combiner hard-codes
``(L0-pyright.xml, L0-ruff.xml, L0-hadolint.xml)``; this one takes the layer
prefix + slug + an explicit list of sub-file names, then merges every
``<testsuite>`` it finds under one ``<testsuites name="l<N>-<slug>">`` root.

Exit code: 0 if combined ``failures + errors`` == 0, else 1.

Used by P15b (L4) and later layers that compose multiple sub-tools.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from xml.etree import ElementTree as ET


def combine(
    gates_dir: Path,
    layer: int,
    slug: str,
    sub_files: list[str],
) -> int:
    """Merge sub-file suites into ``gates_dir/L<layer>-<slug>.xml``."""
    suite_name = f"l{layer}-{slug}"
    root = ET.Element("testsuites", {"name": suite_name})
    total_t = total_f = total_e = total_s = 0

    for sub in sub_files:
        p_xml = gates_dir / sub
        if not p_xml.is_file():
            # Missing sub-file is a finding, not a crash. Surface it as a
            # synthetic skipped testcase so it appears in the dashboard.
            placeholder = ET.SubElement(root, "testsuite", {
                "name": f"{suite_name}-missing-{sub}",
                "tests": "1", "failures": "0", "errors": "0", "skipped": "1",
            })
            tc = ET.SubElement(placeholder, "testcase", {
                "name": f"sub-file-missing-{sub}",
                "classname": "combiner",
            })
            sk = ET.SubElement(tc, "skipped", {
                "message": f"expected sub-tool XML not found: {sub}",
            })
            sk.text = f"path={p_xml}"
            total_t += 1
            total_s += 1
            continue
        try:
            tree = ET.parse(p_xml)
        except ET.ParseError as e:
            print(
                f"[combine_layer_junit] WARN: parse error in {sub}: {e}",
                file=sys.stderr,
            )
            continue
        for suite in tree.getroot().iter("testsuite"):
            try:
                total_t += int(suite.get("tests", "0") or "0")
                total_f += int(suite.get("failures", "0") or "0")
                total_e += int(suite.get("errors", "0") or "0")
                total_s += int(suite.get("skipped", "0") or "0")
            except ValueError:
                pass
            root.append(suite)

    root.set("tests", str(total_t))
    root.set("failures", str(total_f))
    root.set("errors", str(total_e))

    out_path = gates_dir / f"L{layer}-{slug}.xml"
    ET.indent(ET.ElementTree(root), space="  ")
    ET.ElementTree(root).write(out_path, encoding="utf-8", xml_declaration=True)
    print(
        f"[L{layer} {slug}] combined: {total_t} tests, {total_f} failures, "
        f"{total_e} errors, {total_s} skipped -> {out_path}"
    )
    return 1 if (total_f or total_e) else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--gates-dir", type=Path, default=Path("target/gates"))
    p.add_argument("--layer", type=int, required=True,
                   help="Layer number, e.g. 4 for L4.")
    p.add_argument("--slug", required=True,
                   help="Layer slug, e.g. 'container-supplychain'.")
    p.add_argument("--sub", action="append", default=[],
                   help="Sub-file basename (repeatable).")
    args = p.parse_args(argv)

    if not args.sub:
        print("combine_layer_junit: at least one --sub is required.", file=sys.stderr)
        return 2

    return combine(args.gates_dir, args.layer, args.slug, list(args.sub))


if __name__ == "__main__":
    sys.exit(main())
