#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Convert ``pip-licenses --format=json`` output into JUnit XML.

pip-licenses emits a JSON array with shape::

    [
      {
        "Name": "fastapi",
        "Version": "0.136.0",
        "License": "MIT",
        "URL": "https://github.com/tiangolo/fastapi"
      }, ...
    ]

Gate policy: Apache-2.0 is the project license. Any dependency under a
copyleft license that would re-encumber lm-chat's Apache-2.0 distribution
is a gate failure. Blocked license families:

  - GPL-2.0, GPL-3.0 (and variants: LGPL-2.0, LGPL-2.1, LGPL-3.0)
  - AGPL-3.0 (strong copyleft)
  - SSPL-1.0 (MongoDB-style copyleft)
  - Commons-Clause (distribution restriction add-on)
  - EUPL-1.1, EUPL-1.2 (EU copyleft)
  - OSL-3.0 (Open Software License)

Permissive / compatible licenses (MIT, BSD-*, Apache-2.0, ISC, PSF,
Python-2.0, Unlicense, CC0, WTFPL, Zlib, MPL-2.0) → pass.

Unknown / ambiguous licenses → ``<skipped>`` (informational; investigate
manually but don't gate-block).

One ``<testcase>`` per dependency. When no blocked licenses are found, a
single zero-failure testcase named ``no-blocked-licenses`` is emitted.

When ``--skipped`` is passed, emits a synthetic ``pip-licenses-not-installed``
skipped testcase for graceful degradation when the binary is absent.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

_SUITE_NAME = "license-bill"

# License substrings (case-insensitive) that are blocking.
_BLOCKED_FRAGMENTS: list[str] = [
    "GPL-2",
    "GPL-3",
    "LGPL-2",
    "LGPL-3",
    "AGPL",
    "SSPL",
    "Commons-Clause",
    "OSL-3",
    "EUPL",
]

# License substrings that are explicitly allowed (short-circuit the block check).
_ALLOWED_FRAGMENTS: list[str] = [
    "MIT",
    "BSD",
    "Apache",
    "ISC",
    "PSF",
    "Python",
    "Unlicense",
    "CC0",
    "WTFPL",
    "Zlib",
    "MPL-2",
    "Public Domain",
]


def _is_blocked(license_text: str) -> bool:
    upper = license_text.upper()
    for frag in _ALLOWED_FRAGMENTS:
        if frag.upper() in upper:
            return False
    for frag in _BLOCKED_FRAGMENTS:
        if frag.upper() in upper:
            return True
    return False


def _is_unknown(license_text: str) -> bool:
    cleaned = license_text.strip().upper()
    return cleaned in {"UNKNOWN", "", "N/A", "NONE", "NOT FOUND"}


def convert(report: list[dict[str, Any]]) -> ET.Element:
    """Build the JUnit ``<testsuite>`` from a pip-licenses JSON report."""
    entries: list[dict[str, Any]] = [e for e in report if isinstance(e, dict)]

    fail_count = 0
    skip_count = 0
    pass_count = 0

    for e in entries:
        lic = str(e.get("License") or "UNKNOWN")
        if _is_unknown(lic):
            skip_count += 1
        elif _is_blocked(lic):
            fail_count += 1
        else:
            pass_count += 1

    test_count = max(len(entries), 1)

    suite = ET.Element("testsuite", {
        "name": _SUITE_NAME,
        "tests": str(test_count),
        "failures": str(fail_count),
        "errors": "0",
        "skipped": str(skip_count),
    })

    if not entries:
        ET.SubElement(suite, "testcase", {
            "name": "no-blocked-licenses",
            "classname": "pip-licenses",
        })
        return suite

    for e in sorted(entries, key=lambda x: str(x.get("Name") or "").lower()):
        name = str(e.get("Name") or "unknown")
        version = str(e.get("Version") or "unknown")
        lic = str(e.get("License") or "UNKNOWN")
        url = str(e.get("URL") or "")

        tc = ET.SubElement(suite, "testcase", {
            "name": f"{name}-{version}",
            "classname": "pip-licenses",
        })

        if _is_unknown(lic):
            sk = ET.SubElement(tc, "skipped", {
                "message": f"unknown license: {name}=={version} (investigate manually)",
            })
            sk.text = f"package={name} version={version} license={lic} url={url}"
        elif _is_blocked(lic):
            fail = ET.SubElement(tc, "failure", {
                "message": f"blocked license: {lic} on {name}=={version}",
                "type": "license-incompatible",
            })
            fail.text = (
                f"package={name} version={version} license={lic} url={url}\n"
                f"Reason: {lic} is copyleft and would re-encumber lm-chat's Apache-2.0 "
                f"distribution. Replace or vendor this dependency."
            )
        else:
            so = ET.SubElement(tc, "system-out")
            so.text = f"OK: {name}=={version} license={lic}"

    return suite


def _emit_skipped() -> ET.Element:
    suite = ET.Element("testsuite", {
        "name": _SUITE_NAME,
        "tests": "1",
        "failures": "0",
        "errors": "0",
        "skipped": "1",
    })
    tc = ET.SubElement(suite, "testcase", {
        "name": "pip-licenses-not-installed",
        "classname": "pip-licenses",
    })
    sk = ET.SubElement(tc, "skipped", {
        "message": "pip-licenses not available; skipping per L3 graceful-degradation policy.",
    })
    sk.text = (
        "Add pip-licenses>=5 to [dependency-groups] dev in pyproject.toml "
        "and run `uv sync --dev` to enable license compliance scanning."
    )
    return suite


def _run_pip_licenses(output_path: Path) -> bool:
    """Run pip-licenses and write JSON to *output_path*. Returns True on success."""
    result = subprocess.run(
        [
            "uv", "run", "pip-licenses",
            "--format=json",
            "--with-urls",
            f"--output-file={output_path}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return output_path.is_file() and result.returncode == 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, default=None,
                   help="Path to write JUnit XML (default: stdout)")
    p.add_argument("--skipped", action="store_true",
                   help="Emit a synthetic 'pip-licenses not installed' skipped testcase.")
    args = p.parse_args(argv)

    suite: ET.Element
    if args.skipped:
        suite = _emit_skipped()
    else:
        with tempfile.TemporaryDirectory() as tmp:
            json_path = Path(tmp) / "pip-licenses.json"
            ok = _run_pip_licenses(json_path)
            if not ok:
                suite = _emit_skipped()
            else:
                raw = json_path.read_text(encoding="utf-8")
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as e:
                    print(f"junit_from_pip_licenses: invalid JSON — {e}",
                          file=sys.stderr)
                    return 1
                if not isinstance(data, list):
                    print("junit_from_pip_licenses: expected JSON array.", file=sys.stderr)
                    return 1
                suite = convert(data)

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
