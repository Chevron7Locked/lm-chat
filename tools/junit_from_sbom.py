#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Validate SBOM JSON output from ``anchore/syft`` and emit JUnit XML.

Accepts either SPDX-JSON or CycloneDX-JSON. One ``<testcase>`` per input
SBOM file, with the file's sha256 + package count attached as metadata via
``<system-out>``. A malformed / empty SBOM becomes a ``<failure>``.

This is a soft validator — it confirms the SBOM parses as JSON and that
the recognised package-list key is present and non-empty. Schema-level
validation of SPDX-2.3 / CycloneDX-1.5 is out of scope; trivy + syft run
in lockstep and any genuine schema break shows up upstream.

When ``--skipped`` is passed, emits a synthetic
``syft-not-installed`` skipped testcase for graceful degradation.

CLI shape mirrors ``junit_from_trivy.py`` so the Makefile recipes look
identical.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


def _detect_format(doc: dict[str, Any]) -> str:
    """Return ``"spdx"`` / ``"cyclonedx"`` / ``"unknown"``."""
    if "spdxVersion" in doc or "SPDXID" in doc:
        return "spdx"
    if str(doc.get("bomFormat", "")).lower() == "cyclonedx":
        return "cyclonedx"
    if "components" in doc and "specVersion" in doc:
        # Some CycloneDX exports omit bomFormat but always have these.
        return "cyclonedx"
    return "unknown"


def _count_packages(doc: dict[str, Any], fmt: str) -> int:
    if fmt == "spdx":
        pkgs = doc.get("packages")
        return len(pkgs) if isinstance(pkgs, list) else 0
    if fmt == "cyclonedx":
        comps = doc.get("components")
        return len(comps) if isinstance(comps, list) else 0
    return 0


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _validate_one(path: Path) -> tuple[bool, str, dict[str, Any]]:
    """Return (ok, message, meta)."""
    meta: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "format": "unknown",
        "packages": 0,
        "sha256": "",
        "size_bytes": 0,
    }
    if not path.is_file():
        return False, f"SBOM not found: {path}", meta
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        return False, f"read error: {e}", meta
    meta["size_bytes"] = len(raw.encode("utf-8"))
    meta["sha256"] = _file_sha256(path)
    if not raw.strip():
        return False, "SBOM file is empty.", meta
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as e:
        return False, f"invalid JSON: {e}", meta
    if not isinstance(doc, dict):
        return False, "SBOM root is not a JSON object.", meta

    fmt = _detect_format(doc)
    meta["format"] = fmt
    if fmt == "unknown":
        return False, "Unrecognised SBOM format (expected SPDX or CycloneDX).", meta

    count = _count_packages(doc, fmt)
    meta["packages"] = count
    if count == 0:
        return False, f"{fmt} SBOM contains zero packages/components.", meta

    return True, f"{fmt} SBOM with {count} package(s)", meta


def convert(paths: list[Path]) -> ET.Element:
    """Build the JUnit suite from a list of SBOM file paths."""
    test_count = max(len(paths), 1)
    failures = 0

    suite = ET.Element("testsuite", {
        "name": "l4-syft",
        "tests": str(test_count),
        "failures": "0",
        "errors": "0",
        "skipped": "0",
    })

    if not paths:
        ET.SubElement(suite, "testcase", {
            "name": "no-sboms-provided",
            "classname": "syft",
        })
        return suite

    for path in paths:
        ok, msg, meta = _validate_one(path)
        tc = ET.SubElement(suite, "testcase", {
            "name": path.name or str(path),
            "classname": f"syft.{meta['format']}",
        })
        sysout = ET.SubElement(tc, "system-out")
        sysout.text = "\n".join([
            f"path={meta['path']}",
            f"format={meta['format']}",
            f"packages={meta['packages']}",
            f"sha256={meta['sha256']}",
            f"size_bytes={meta['size_bytes']}",
        ])
        if not ok:
            failures += 1
            fail = ET.SubElement(tc, "failure", {
                "message": msg,
                "type": "sbom-invalid",
            })
            fail.text = msg

    suite.set("failures", str(failures))
    return suite


def _emit_skipped() -> ET.Element:
    """Synthetic 'syft not installed' suite for graceful degradation."""
    suite = ET.Element("testsuite", {
        "name": "l4-syft",
        "tests": "1",
        "failures": "0",
        "errors": "0",
        "skipped": "1",
    })
    tc = ET.SubElement(suite, "testcase", {
        "name": "syft-not-installed",
        "classname": "syft",
    })
    sk = ET.SubElement(tc, "skipped", {
        "message": "anchore/syft binary not on PATH; skipping per L4 graceful-degradation policy.",
    })
    sk.text = (
        "Install anchore/syft (https://github.com/anchore/syft) to enable "
        "SBOM generation. Note: this is the Go binary, NOT the PyPI 'syft' "
        "package (which is OpenMined/PySyft, unrelated)."
    )
    return suite


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--sbom", action="append", default=[], type=Path,
                   help="Path to an SBOM JSON file (repeatable).")
    p.add_argument("--output", type=Path, default=None,
                   help="Path to write JUnit XML (default: stdout)")
    p.add_argument("--skipped", action="store_true",
                   help="Emit a synthetic 'syft not installed' skipped testcase.")
    args = p.parse_args(argv)

    if args.skipped:
        suite = _emit_skipped()
    else:
        suite = convert(list(args.sbom))

    root = ET.Element("testsuites", {
        "name": "l4-syft",
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
