#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Combine the three L0 sub-tool JUnit XMLs into ``L0-static-fast.xml``.

L0 runs three independent tools (pyright via adapter, ruff native, hadolint
via adapter). The Makefile produces three XML files; this script merges
their suites under a single ``<testsuites name="l0-static-fast">`` root so
the L9 aggregator can find one file per layer.

Exit code: 0 if combined failures + errors == 0, else 1.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

SUB_FILES = (
    "L0-pyright.xml",
    "L0-ruff.xml",
    "L0-hadolint.xml",
    "L0-semgrep-jsts.xml",
    "L0-semgrep-custom.xml",
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--gates-dir", type=Path, default=Path("target/gates"))
    args = p.parse_args(argv)
    gates: Path = args.gates_dir

    root = ET.Element("testsuites", {"name": "l0-static-fast"})
    total_t = total_f = total_e = total_s = 0
    for sub in SUB_FILES:
        p_xml = gates / sub
        if not p_xml.is_file():
            continue
        try:
            tree = ET.parse(p_xml)
        except ET.ParseError as e:
            print(f"[combine_l0_junit] WARN: parse error in {sub}: {e}", file=sys.stderr)
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

    out_path = gates / "L0-static-fast.xml"
    ET.indent(ET.ElementTree(root), space="  ")
    ET.ElementTree(root).write(out_path, encoding="utf-8", xml_declaration=True)
    print(
        f"[L0] combined: {total_t} tests, {total_f} failures, "
        f"{total_e} errors, {total_s} skipped -> {out_path}"
    )
    return 1 if (total_f or total_e) else 0


if __name__ == "__main__":
    sys.exit(main())
