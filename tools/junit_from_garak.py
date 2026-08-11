#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Convert a garak report (JSONL) into JUnit XML for the L7 LLM-security layer.

garak emits one JSON object per line, each describing the outcome of a
probe + attempt against a generator.  The relevant fields (per garak's
``garak.runs.run.Run.report`` reporter) are::

    {
      "entry_type":  "attempt" | "eval" | "config" | ...,
      "probe":       "promptinject.HijackHateHumans",
      "detector":    "promptinject.AttackRogueString",
      "prompt":      "...",
      "outputs":     ["..."],
      "passed":      true | false,
      "score":       0.0 .. 1.0,
      "severity":    "info" | "low" | "medium" | "high" | "critical" (when present)
    }

Mapping:

    HIGH / CRITICAL probe hit -> ``<failure>``
    LOW / MEDIUM probe hit    -> ``<skipped>``
    no hits / passed          -> ``<testcase/>`` (clean)
    tied / INFO               -> ``<skipped>``

Each ``probe`` becomes one ``<testcase classname="garak.<probe>">`` with the
attempt count + hit count summarised in the message body.  The testsuite
name is ``l7-garak-<path>`` (e.g. ``l7-garak-sse``) so the L9 aggregator can
attribute findings to the right sub-channel.

Graceful degradation: when ``--skipped`` is passed (no report file, garak
not installed, backend unreachable), emit a single ``garak-skipped``
testcase so the layer does not crash the gate.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

# Severities that fail the gate.
_FAIL_SEVERITIES = {"CRITICAL", "HIGH"}
# Severities that are informational only.
_INFO_SEVERITIES = {"MEDIUM", "LOW", "INFO", "UNKNOWN", ""}


def _norm_sev(s: Any) -> str:
    """Normalise a garak severity string to UPPER ASCII."""
    return str(s or "").strip().upper()


def _iter_report_records(text: str) -> list[dict[str, Any]]:
    """Parse a garak report file (JSONL).

    Tolerates blank lines and any non-JSON lines (garak occasionally inserts
    comments).  Returns the list of decoded dicts; non-dict top-level values
    are dropped.
    """
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _bucket_by_probe(
    records: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Aggregate per-attempt records into per-probe summary buckets.

    Each bucket carries::

        {
          "probe":      str,
          "attempts":   int,
          "hits":       int,         # attempts where the detector fired
          "severity":   "HIGH" | ...,  # max severity observed
          "sample_prompts": list[str], # up to 3 prompts that produced a hit
        }
    """
    buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "probe": "",
            "attempts": 0,
            "hits": 0,
            "severity": "",
            "sample_prompts": [],
        }
    )
    for rec in records:
        entry_type = rec.get("entry_type", "")
        if entry_type not in {"attempt", "eval", ""}:
            continue
        probe = str(rec.get("probe") or rec.get("plugin") or "").strip()
        if not probe:
            continue
        bucket = buckets[probe]
        bucket["probe"] = probe
        bucket["attempts"] += 1
        # ``passed`` is True when the detector did NOT fire (model resisted
        # the attack).  ``passed`` False means the probe scored a hit.
        passed = rec.get("passed")
        score = rec.get("score")
        is_hit = (passed is False) or (
            isinstance(score, (int, float)) and float(score) >= 0.5
        )
        if is_hit:
            bucket["hits"] += 1
            sev = _norm_sev(rec.get("severity") or "HIGH")
            current = bucket["severity"] or ""
            # Promote to higher severity if we see one.
            if _severity_rank(sev) > _severity_rank(current):
                bucket["severity"] = sev
            prompt = str(rec.get("prompt") or "")
            samples = bucket["sample_prompts"]
            if prompt and len(samples) < 3 and prompt not in samples:
                samples.append(prompt[:200])
    return dict(buckets)


_SEV_ORDER = ["", "INFO", "UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"]


def _severity_rank(s: str) -> int:
    try:
        return _SEV_ORDER.index(_norm_sev(s))
    except ValueError:
        return 0


def convert(records: list[dict[str, Any]], suite_name: str) -> ET.Element:
    """Convert garak records into a JUnit ``<testsuite>`` element.

    Args:
        records: List of decoded garak JSONL records.
        suite_name: ``l7-garak-sse``, ``l7-garak-json``, etc.
    """
    buckets = _bucket_by_probe(records)
    # Sort for stable XML output.
    probes = sorted(buckets.keys())

    n_tests = len(probes) if probes else 1
    n_fail = 0
    n_skip = 0

    suite = ET.Element("testsuite", {
        "name": suite_name,
        "tests": "0", "failures": "0", "errors": "0", "skipped": "0",
    })

    if not probes:
        # No probe records at all — emit one synthetic skipped testcase
        # so the aggregator surfaces the "garak produced no records" state.
        tc = ET.SubElement(suite, "testcase", {
            "name": "no-records",
            "classname": "garak",
        })
        sk = ET.SubElement(tc, "skipped", {"message": "garak report had 0 records"})
        sk.text = "Garak did not emit any per-attempt records for this run."
        suite.set("tests", "1")
        suite.set("skipped", "1")
        return suite

    for probe in probes:
        b = buckets[probe]
        attempts = int(b["attempts"])
        hits = int(b["hits"])
        sev = _norm_sev(b["severity"])
        tc = ET.SubElement(suite, "testcase", {
            "name": probe,
            "classname": f"garak.{probe.split('.')[0]}",
        })
        # Always annotate attempt count for transparency.
        tc.set("attempts", str(attempts))
        tc.set("hits", str(hits))
        if hits == 0:
            # Clean — no children means PASS in JUnit semantics.
            continue
        body_lines = [
            f"probe={probe}",
            f"attempts={attempts}",
            f"hits={hits}",
            f"severity={sev or 'UNKNOWN'}",
        ]
        for p in b["sample_prompts"]:
            body_lines.append(f"sample_prompt={p!r}")
        body = "\n".join(body_lines)
        if sev in _FAIL_SEVERITIES:
            f = ET.SubElement(tc, "failure", {
                "message": f"{probe}: {hits}/{attempts} attempts succeeded",
                "type": f"garak.{probe}",
            })
            f.text = body
            n_fail += 1
        else:
            # MEDIUM/LOW/INFO/UNKNOWN -> <skipped>.
            sev_label = sev or "UNKNOWN"
            sk_msg = f"{probe}: {hits}/{attempts} attempts succeeded (sev={sev_label})"
            sk = ET.SubElement(tc, "skipped", {"message": sk_msg})
            sk.text = body
            n_skip += 1

    suite.set("tests", str(n_tests))
    suite.set("failures", str(n_fail))
    suite.set("skipped", str(n_skip))
    return suite


def _wrap_in_testsuites(suite: ET.Element, name: str) -> ET.Element:
    """Wrap a ``<testsuite>`` in a ``<testsuites>`` root, copying counters."""
    root = ET.Element("testsuites", {
        "name": name,
        "tests": suite.get("tests", "0"),
        "failures": suite.get("failures", "0"),
        "errors": suite.get("errors", "0"),
        "skipped": suite.get("skipped", "0"),
    })
    root.append(suite)
    return root


def _emit_skipped(reason: str, suite_name: str) -> ET.Element:
    """Emit a synthetic skipped suite for graceful degradation."""
    suite = ET.Element("testsuite", {
        "name": suite_name,
        "tests": "1", "failures": "0", "errors": "0", "skipped": "1",
    })
    tc = ET.SubElement(suite, "testcase", {
        "name": "garak-skipped",
        "classname": "garak",
    })
    sk = ET.SubElement(tc, "skipped", {"message": reason})
    sk.text = reason
    return suite


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=Path, help="garak JSONL report")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument(
        "--suite-name",
        default="l7-garak",
        help="testsuite name (default: l7-garak)",
    )
    p.add_argument(
        "--skipped",
        action="store_true",
        help="emit a graceful-skipped XML (no input needed)",
    )
    p.add_argument(
        "--skipped-reason",
        default="garak not installed or backend unreachable",
    )
    args = p.parse_args(argv)

    if args.skipped or args.input is None or not args.input.is_file():
        reason = (
            args.skipped_reason
            if args.skipped
            else f"garak report not found: {args.input}"
        )
        suite = _emit_skipped(reason, args.suite_name)
    else:
        try:
            text = args.input.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            suite = _emit_skipped(f"could not read {args.input}: {exc}", args.suite_name)
        else:
            records = _iter_report_records(text)
            suite = convert(records, args.suite_name)

    root = _wrap_in_testsuites(suite, args.suite_name)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(ET.ElementTree(root), space="  ")
    ET.ElementTree(root).write(args.output, encoding="utf-8", xml_declaration=True)
    print(
        f"[junit_from_garak] {args.suite_name}: "
        f"tests={suite.get('tests')} failures={suite.get('failures')} "
        f"skipped={suite.get('skipped')} -> {args.output}"
    )
    n_fail = int(suite.get("failures", "0") or "0")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":  # pragma: no cover - CLI dispatch
    sys.exit(main())
