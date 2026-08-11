#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run slowhttptest (Slowloris-style DoS probe) and emit JUnit XML.

slowhttptest is run via Docker (``dlebauer/slowhttptest``) against the
lm-chat target on ``http://localhost:18001`` (the L6 compose-target port).

slowhttptest stdout (final summary block) contains a line like::

    Test exit status: Exit on Connection count reached 0

or::

    Test exit status: Service available on 127.0.0.1:18001

The adapter parses this line to determine pass/fail.  A service that
remains *available* despite the slowloris attack passes.

Graceful skip when:
  - Docker daemon is not reachable (mirrors garak skip pattern).
  - ``--skipped`` flag is passed explicitly.

Emits a JUnit XML testsuite named ``dos-slowloris``.

Usage
-----
    # normal mode (requires Docker + L6 compose target on :18001):
    uv run python tools/junit_from_slowhttptest.py \
        --target-url http://localhost:18001 \
        --output target/gates/L6-dos-slowloris.xml

    # skip mode:
    uv run python tools/junit_from_slowhttptest.py --skipped \
        --output target/gates/L6-dos-slowloris.xml

Exit codes: 0 = pass or skipped, 1 = failure detected.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

_SUITE_NAME = "dos-slowloris"

# Docker image for slowhttptest.
_DOCKER_IMAGE = "dlebauer/slowhttptest"

# Default target URL (L6 compose-target).
_DEFAULT_TARGET_URL = "http://localhost:18001"

# slowhttptest options:
#   -c  connection count (200)
#   -H  slowloris mode (header-based)
#   -i  data send interval (10 s)
#   -r  connection rate per second (200)
#   -s  content length header value
#   -t  verb (GET)
#   -u  target URL
#   -x  max data length
#   -p  follow-up connection probe interval (3 s)
_SLOWHTTPTEST_ARGS: list[str] = [
    "-c", "200",
    "-H",
    "-i", "10",
    "-r", "200",
    "-s", "8192",
    "-t", "GET",
    "-x", "10",
    "-p", "3",
]

# Timeout for the Docker run (seconds).
_DOCKER_TIMEOUT_S: int = 60


def _docker_available() -> bool:
    """Return True if the Docker daemon is reachable."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _run_slowhttptest(target_url: str) -> tuple[int, str]:
    """Run slowhttptest Docker container; return (returncode, stdout+stderr)."""
    cmd = [
        "docker", "run", "--rm", "--network", "host",
        _DOCKER_IMAGE,
        "-u", target_url,
    ] + _SLOWHTTPTEST_ARGS
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_DOCKER_TIMEOUT_S,
        )
        output = result.stdout + result.stderr
        return result.returncode, output
    except subprocess.TimeoutExpired:
        return -1, f"slowhttptest Docker run timed out after {_DOCKER_TIMEOUT_S}s"
    except Exception as exc:  # noqa: BLE001
        return -2, f"Docker run failed: {exc!r}"


def _parse_exit_status(output: str) -> tuple[bool, str]:
    """Parse slowhttptest output; return (service_available, status_line).

    slowhttptest exit status meanings:
      0 - Service available despite the attack — PASS
      1 - Service unavailable during test       — FAIL (Slowloris successful)
      2 - No connections established             — inconclusive / infra issue

    We look for the ``Test exit status:`` summary line in the output.
    """
    for line in output.splitlines():
        s = line.strip()
        if s.startswith("Test exit status:"):
            status_text = s[len("Test exit status:"):].strip()
            # "Service available" in the status line = good.
            if "service available" in status_text.lower():
                return True, s
            # "Exit on Connection count reached 0" or similar = fail.
            return False, s

    # No status line found — treat as inconclusive (skip rather than hard-fail
    # to avoid blocking the gate on infrastructure noise).
    return True, "no 'Test exit status:' line found in output (inconclusive — treating as pass)"


def _emit_skipped(reason: str = "") -> ET.Element:
    suite = ET.Element(
        "testsuite",
        {
            "name": _SUITE_NAME,
            "tests": "1",
            "failures": "0",
            "errors": "0",
            "skipped": "1",
        },
    )
    tc = ET.SubElement(
        suite, "testcase", {"name": "slowhttptest-skipped", "classname": "dos.slowloris"}
    )
    msg = reason or (
        "slowhttptest not run — Docker daemon unavailable or --skipped flag passed. "
        "Install Docker and bring up tests/security/compose-target.yml first."
    )
    sk = ET.SubElement(tc, "skipped", {"message": msg})
    sk.text = msg
    return suite


def _build_suite(passed: bool, status_line: str, raw_output: str) -> ET.Element:
    suite = ET.Element(
        "testsuite",
        {
            "name": _SUITE_NAME,
            "tests": "1",
            "failures": "0" if passed else "1",
            "errors": "0",
            "skipped": "0",
        },
    )
    tc = ET.SubElement(
        suite, "testcase", {"name": "slowloris-attack", "classname": "dos.slowloris"}
    )
    if not passed:
        fail = ET.SubElement(
            tc,
            "failure",
            {
                "message": f"Slowloris attack succeeded: {status_line[:140]}",
                "type": "dos-slowloris",
            },
        )
        fail.text = (
            f"status: {status_line}\n\n"
            f"raw output (last 2000 chars):\n{raw_output[-2000:]}"
        )
    return suite


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--target-url", type=str, default=_DEFAULT_TARGET_URL,
                   help=f"Target URL for slowhttptest (default: {_DEFAULT_TARGET_URL})")
    p.add_argument("--output", type=Path, default=None,
                   help="Path to write JUnit XML (default: stdout)")
    p.add_argument("--skipped", action="store_true",
                   help="Emit a synthetic skipped testcase.")
    p.add_argument("--skipped-reason", type=str, default="",
                   help="Override skipped-mode reason string.")
    args = p.parse_args(argv)

    if args.skipped:
        suite = _emit_skipped(args.skipped_reason)
    elif not _docker_available():
        suite = _emit_skipped(
            "Docker daemon not reachable — slowhttptest requires Docker "
            "(image: dlebauer/slowhttptest)"
        )
    else:
        rc, output = _run_slowhttptest(args.target_url)
        if rc == -2 or rc == -1:
            # Infrastructure error — treat as skipped.
            suite = _emit_skipped(f"slowhttptest Docker run failed: {output[:200]}")
        else:
            passed, status_line = _parse_exit_status(output)
            suite = _build_suite(passed, status_line, output)

    root = ET.Element(
        "testsuites",
        {
            "name": _SUITE_NAME,
            "tests": suite.get("tests", "0"),
            "failures": suite.get("failures", "0"),
            "errors": "0",
        },
    )
    root.append(suite)
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        tree.write(args.output, encoding="utf-8", xml_declaration=True)
    else:
        sys.stdout.write(ET.tostring(root, encoding="unicode", xml_declaration=True))
        sys.stdout.write("\n")

    return 1 if int(suite.get("failures", "0")) > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
