#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Auth + session + crypto runtime aggregator (P15g).

Runs the three P15g tool scripts sequentially:
  1. tools/session_fixation_test.py
  2. tools/totp_race_test.py
  3. tools/enc_envelope_tamper_test.py

Combines their JUnit XML outputs into a single top-level
<testsuites> wrapper at target/gates/L8-auth-session.xml.

Mirrors the combine_layer_junit.py pattern used by L4/L5/L6/L7.

Exit code: 0 if all three pass, 1 if any failure/error.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from xml.etree import ElementTree as ET

_REPO_ROOT = Path(__file__).resolve().parent.parent
_GATES_DIR = _REPO_ROOT / "target" / "gates"

_TOOLS: list[tuple[str, str]] = [
    ("tools/session_fixation_test.py", "auth-session-fixation.xml"),
    ("tools/totp_race_test.py", "auth-totp-race.xml"),
    ("tools/enc_envelope_tamper_test.py", "crypto-aesgcm-tamper.xml"),
    ("tools/xff_default_deny_test.py", "L8-xff-default-deny.xml"),
]

_OUT_XML = _GATES_DIR / "L8-auth-session.xml"


def _run_tool(script: str) -> int:
    """Run a tool script via the same Python interpreter and return its exit code."""
    result = subprocess.run(
        [sys.executable, str(_REPO_ROOT / script)],
        cwd=str(_REPO_ROOT),
    )
    return result.returncode


def _combine(sub_xmls: list[Path]) -> ET.Element:
    """Merge all sub-tool JUnit XMLs under one <testsuites> root."""
    root = ET.Element("testsuites", {"name": "l8-auth-session"})
    total_t = total_f = total_e = total_s = 0

    for p_xml in sub_xmls:
        if not p_xml.is_file():
            placeholder = ET.SubElement(root, "testsuite", {
                "name": f"missing-{p_xml.name}",
                "tests": "1", "failures": "0", "errors": "0", "skipped": "1",
            })
            tc = ET.SubElement(placeholder, "testcase", {
                "name": f"sub-file-missing-{p_xml.name}",
                "classname": "combiner",
            })
            sk = ET.SubElement(tc, "skipped",
                               {"message": f"expected XML not found: {p_xml}"})
            sk.text = str(p_xml)
            total_t += 1
            total_s += 1
            continue

        try:
            tree = ET.parse(p_xml)
        except ET.ParseError as e:
            print(f"[junit_from_auth_session] WARN: parse error in {p_xml.name}: {e}",
                  file=sys.stderr)
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
    return root


def main(argv: list[str] | None = None) -> int:
    _GATES_DIR.mkdir(parents=True, exist_ok=True)

    t_start = time.monotonic()
    any_failure = False

    for script, _out_name in _TOOLS:
        print(f"[junit_from_auth_session] running {script} ...")
        rc = _run_tool(script)
        if rc != 0:
            print(f"[junit_from_auth_session]   {script} exited {rc}")
            any_failure = True
        else:
            print(f"[junit_from_auth_session]   {script} OK")

    sub_xmls = [_GATES_DIR / xml_name for _, xml_name in _TOOLS]
    combined = _combine(sub_xmls)

    ET.indent(ET.ElementTree(combined), space="  ")
    ET.ElementTree(combined).write(_OUT_XML, encoding="utf-8", xml_declaration=True)

    f = int(combined.get("failures", "0"))
    e = int(combined.get("errors", "0"))
    t = int(combined.get("tests", "0"))
    elapsed = time.monotonic() - t_start

    print(
        f"[junit_from_auth_session] combined: {t} tests, {f} failures, "
        f"{e} errors in {elapsed:.1f}s -> {_OUT_XML}"
    )

    return 1 if (any_failure or f or e) else 0


if __name__ == "__main__":
    sys.exit(main())
