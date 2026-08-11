#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Post-process a schemathesis JUnit XML to downgrade known-false-positive
failures to ``<skipped>`` elements.

Each entry in the allowlist YAML matches by two criteria:

* ``testcase`` — an ``fnmatch`` glob matched against the ``<testcase name>``
  attribute.
* ``failure_substring`` — a plain substring matched (case-sensitive) against
  the ``message`` attribute of any child ``<failure>`` element.

Both criteria must match for a ``<failure>`` to be downgraded.  When all
``<failure>`` children of a ``<testcase>`` are downgraded the testcase no
longer counts as failed.  The parent ``<testsuite>`` and ``<testsuites>``
counters (``failures``, ``skipped``) are updated accordingly.

The operation is **idempotent**: a ``<skipped>`` element never has a
``message`` attribute starting with the failure sentinel text, so re-running
never double-filters.

Usage::

    python tools/schemathesis_flake_filter.py \\
        --input  target/gates/L5-schemathesis.xml \\
        --allowlist tests/security/schemathesis-known-flakes.yaml \\
        --output target/gates/L5-schemathesis.xml
"""
from __future__ import annotations

import argparse
import logging
import sys
from fnmatch import fnmatch
from pathlib import Path
from xml.etree import ElementTree as ET

import yaml  # PyYAML — already in the project's dev-dep set

logging.basicConfig(format="[flake-filter] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

_SKIPPED_MESSAGE_PREFIX = "Known false positive"
_SKIPPED_TYPE = "schemathesis-known-flake"


def _load_allowlist(path: Path) -> list[dict[str, str]]:
    """Return the list of known-flake entries from *path*."""
    raw = yaml.safe_load(path.read_text())
    entries: list[dict[str, str]] = raw.get("known_flakes", [])
    return entries


def _matches(
    entry: dict[str, str],
    testcase_name: str,
    failure_message: str,
    element_text: str | None = None,
) -> bool:
    etxt = element_text or ""
    return fnmatch(testcase_name, entry["testcase"]) and (
        entry["failure_substring"] in failure_message
        or entry["failure_substring"] in etxt
    )


def _adjust_counter(element: ET.Element, attr: str, delta: int) -> None:
    """Add *delta* to the integer attribute *attr* on *element* (clamped ≥ 0)."""
    current = int(element.get(attr, "0"))
    element.set(attr, str(max(0, current + delta)))


def filter_xml(
    tree: ET.ElementTree[ET.Element],
    entries: list[dict[str, str]],
) -> int:
    """Downgrade matching failures/errors to skipped in-place.

    Returns the number of ``<failure>`` or ``<error>`` elements converted.
    """
    root = tree.getroot()
    assert root is not None, "ElementTree has no root element"
    converted_total = 0

    for testsuite in root.iter("testsuite"):
        suite_converted_failures = 0
        suite_converted_errors = 0

        for testcase in testsuite.iter("testcase"):
            name = testcase.get("name", "")
            children_to_replace: list[tuple[ET.Element, dict[str, str]]] = []

            for child in list(testcase):
                if child.tag not in ("failure", "error"):
                    continue
                msg = child.get("message", "")
                txt = child.text or ""
                for entry in entries:
                    if _matches(entry, name, msg, txt):
                        children_to_replace.append((child, entry))
                        break  # one entry match per child element is enough

            for child_el, entry in children_to_replace:
                idx = list(testcase).index(child_el)
                original_tag = child_el.tag  # "failure" or "error"
                testcase.remove(child_el)

                rationale_text = (
                    f"rationale: {entry['rationale'].strip()}\n"
                    f"lmc_id: {entry['lmc_id']}\n"
                    f"approved_by: {entry['approved_by']}\n"
                    f"approved_at: {entry['approved_at']}"
                )
                skipped = ET.Element(
                    "skipped",
                    {
                        "message": (
                            f"{_SKIPPED_MESSAGE_PREFIX} "
                            f"({entry['lmc_id']}); "
                            f"see schemathesis-known-flakes.yaml"
                        ),
                        "type": _SKIPPED_TYPE,
                    },
                )
                skipped.text = rationale_text
                testcase.insert(idx, skipped)

                if original_tag == "failure":
                    suite_converted_failures += 1
                else:
                    suite_converted_errors += 1

                log.info(
                    "downgraded %s → skipped: testcase=%r  entry=%s",
                    original_tag,
                    name,
                    entry["lmc_id"],
                )

        if suite_converted_failures:
            _adjust_counter(testsuite, "failures", -suite_converted_failures)
        if suite_converted_errors:
            _adjust_counter(testsuite, "errors", -suite_converted_errors)
        suite_total = suite_converted_failures + suite_converted_errors
        if suite_total:
            _adjust_counter(testsuite, "skipped", suite_total)
            converted_total += suite_total

    if converted_total:
        # Update testsuites-level counters by re-summing from suites.
        total_failures = sum(
            int(s.get("failures", "0")) for s in root.iter("testsuite")
        )
        total_errors = sum(
            int(s.get("errors", "0")) for s in root.iter("testsuite")
        )
        total_skipped = sum(
            int(s.get("skipped", "0")) for s in root.iter("testsuite")
        )
        root.set("failures", str(total_failures))
        root.set("errors", str(total_errors))
        root.set("skipped", str(total_skipped))

    return converted_total


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=Path, required=True, help="Input JUnit XML")
    p.add_argument(
        "--allowlist",
        type=Path,
        required=True,
        help="Path to schemathesis-known-flakes.yaml",
    )
    p.add_argument(
        "--output",
        type=Path,
        required=False,
        help="Output path (defaults to --input for in-place rewrite)",
    )
    args = p.parse_args(argv)

    output: Path = args.output if args.output is not None else args.input

    # --- allowlist missing → no-op, exit 0 -----------------------------------
    if not args.allowlist.is_file():
        log.info(
            "allowlist not found at %s — no-op (all schemathesis findings kept)",
            args.allowlist,
        )
        return 0

    entries = _load_allowlist(args.allowlist)
    if not entries:
        log.info("allowlist is empty — no-op")
        return 0

    # --- parse input ---------------------------------------------------------
    if not args.input.is_file():
        log.error("input file not found: %s", args.input)
        return 1

    try:
        tree = ET.parse(args.input)
    except ET.ParseError as exc:
        log.error("XML parse error in %s: %s", args.input, exc)
        return 1

    # --- filter --------------------------------------------------------------
    converted = filter_xml(tree, entries)

    # --- write output --------------------------------------------------------
    output.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(tree, space="  ")
    tree.write(output, encoding="utf-8", xml_declaration=True)

    root = tree.getroot()
    remaining_failures = int(root.get("failures", "0"))
    log.info(
        "done — converted=%d  remaining_failures=%d  output=%s",
        converted,
        remaining_failures,
        output,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
