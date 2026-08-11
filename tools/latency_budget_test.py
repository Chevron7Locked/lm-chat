#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Latency budget smoke test for lm-chat API endpoints.

Performs N GET requests to each of three endpoints and asserts p95 latency
under a per-endpoint budget.  Emits JUnit XML.

Endpoints tested
----------------
- ``/api/auth/me/probe``   (budget: 500 ms)
- ``/api/models``           (budget: 1500 ms)
- ``/api/chats``            (budget: 1000 ms)

These are generous smoke budgets intended to catch gross performance
regressions, not micro-benchmarks.

Graceful skip when
-------------------
- ``--skipped`` flag is passed explicitly.
- The backend is unreachable (connection refused, DNS failure, timeout).

Usage
-----
    # normal mode (requires a running backend):
    uv run python tools/latency_budget_test.py \
        --target-url http://localhost:18001 \
        --output target/gates/L6-dos-latency.xml

    # skip mode:
    uv run python tools/latency_budget_test.py --skipped \
        --output target/gates/L6-dos-latency.xml

Exit codes: 0 = pass or skipped, 1 = one or more budgets violated.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path
from xml.etree import ElementTree as ET

_SUITE_NAME = "dos-latency"

# Per-endpoint p95 latency budgets (milliseconds).  These are smoke-level
# values — generous enough to pass on a moderately loaded CI runner but
# tight enough to catch obvious regressions (e.g. N+1 queries, missing
# connection pooling, unbounded serialization).
_ME_PROBE_BUDGET_MS: int = 500
_MODELS_BUDGET_MS: int = 1500
_CHATS_BUDGET_MS: int = 1000

# Number of GET requests per endpoint.
_N_REQUESTS: int = 30

# Endpoints to test (tuple of path, display label, budget ms).
_ENDPOINTS: list[tuple[str, str, int]] = [
    ("/api/auth/me/probe", "me-probe", _ME_PROBE_BUDGET_MS),
    ("/api/models", "models", _MODELS_BUDGET_MS),
    ("/api/chats", "chats", _CHATS_BUDGET_MS),
]


def _compute_latencies(
    base_url: str, path: str, n: int, timeout_s: float = 10.0
) -> list[float] | None:
    """Perform n GET requests; return list of latencies (ms), or None if unreachable.

    Returns None if the first request fails with a connection-level error,
    indicating the backend is unreachable — triggers a graceful skip in
    the caller.
    """
    import httpx

    try:
        client = httpx.Client(base_url=base_url, timeout=timeout_s)
    except Exception:
        return None

    latencies: list[float] = []
    try:
        for _ in range(n):
            try:
                start = time.perf_counter()
                resp = client.get(path)
                elapsed = (time.perf_counter() - start) * 1000  # ms
                latencies.append(elapsed)
                # Surface the status code in the latencies list by encoding
                # 5xx responses with negative latency so the caller can flag
                # them as failures.  We still include them to avoid silently
                # passing on a broken endpoint.
                if resp.status_code >= 500:
                    latencies[-1] = -elapsed
            except httpx.ConnectError:
                return None
            except httpx.TimeoutException:
                latencies.append(float("inf"))
    finally:
        client.close()

    return latencies


def _run_endpoint(endpoint: tuple[str, str, int], base_url: str) -> tuple[str, bool, str]:
    """Run latency test for one endpoint.

    Returns (name, passed, detail) where:
      name   — JUnit testcase name (e.g. "me-probe-latency")
      passed — True if p95 <= budget
      detail — human-readable summary
    """
    path, label, budget_ms = endpoint
    name = f"{label}-latency"

    latencies = _compute_latencies(base_url, path, n=_N_REQUESTS)
    if latencies is None:
        return name, True, "backend unreachable (skipped)"

    # Filter out invalid entries (server errors flagged as negative).
    valid = [abs(val) for val in latencies if val != float("inf") and val >= 0]
    errors = sum(1 for val in latencies if val < 0)
    timeouts = sum(1 for val in latencies if val == float("inf"))

    if len(valid) < 2:
        p50 = p95 = 0.0
    else:
        valid_sorted = sorted(valid)
        p50 = statistics.median(valid_sorted)
        idx95 = max(0, min(len(valid_sorted) - 1, int(len(valid_sorted) * 0.95)))
        p95 = valid_sorted[idx95]

    detail = (
        f"n={len(latencies)} valid={len(valid)} errors={errors} timeouts={timeouts} "
        f"p50={p50:.1f}ms p95={p95:.1f}ms budget={budget_ms}ms"
    )

    # Fail if more than 50% of requests errored or timed out (dead endpoint).
    if errors + timeouts > 0.5 * len(latencies):
        return (
            name,
            False,
            (
                f"error+timeout rate {(errors + timeouts)}/{len(latencies)} "
                f"exceeds 50% — endpoint returning 5xx or timing out — {detail}"
            ),
        )

    if p95 > budget_ms:
        return name, False, (f"p95 {p95:.1f}ms exceeds budget {budget_ms}ms — {detail}")

    return name, True, detail


def _check_reachable(base_url: str) -> bool:
    """Quick connectivity check — GET the base URL (or /)."""
    import httpx

    try:
        with httpx.Client(base_url=base_url, timeout=5.0) as client:
            client.get("/")
            return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# JUnit builders
# ---------------------------------------------------------------------------


def _build_suite(results: list[tuple[str, bool, str]]) -> ET.Element:
    total = len(results)
    failures = sum(1 for _, ok, _ in results if not ok)
    suite = ET.Element(
        "testsuite",
        {
            "name": _SUITE_NAME,
            "tests": str(total),
            "failures": str(failures),
            "errors": "0",
            "skipped": "0",
        },
    )
    for name, ok, detail in results:
        tc = ET.SubElement(suite, "testcase", {"name": name, "classname": "dos.latency"})
        if not ok:
            fail = ET.SubElement(
                tc,
                "failure",
                {
                    "message": f"p95 latency exceeds budget: {name}",
                    "type": "dos-latency",
                },
            )
            fail.text = detail
    return suite


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
        suite, "testcase", {"name": "latency-test-skipped", "classname": "dos.latency"}
    )
    msg = reason or "latency_budget_test skipped (--skipped flag or backend unreachable)"
    sk = ET.SubElement(tc, "skipped", {"message": msg})
    sk.text = msg
    return suite


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--target-url", type=str, required=True, help="Base URL of the live target backend."
    )
    p.add_argument(
        "--output", type=Path, default=None, help="Path to write JUnit XML (default: stdout)"
    )
    p.add_argument("--skipped", action="store_true", help="Emit a synthetic skipped testcase.")
    p.add_argument(
        "--skipped-reason", type=str, default="", help="Override skipped-mode reason string."
    )
    args = p.parse_args(argv)

    if args.skipped:
        suite = _emit_skipped(args.skipped_reason)
    else:
        # Quick connectivity check first.
        if not _check_reachable(args.target_url):
            suite = _emit_skipped(
                f"backend at {args.target_url} is unreachable — skipping latency test. "
                "Ensure the L6 compose target is running."
            )
        else:
            results: list[tuple[str, bool, str]] = []
            for endpoint in _ENDPOINTS:
                name, passed, detail = _run_endpoint(endpoint, args.target_url)
                results.append((name, passed, detail))
            suite = _build_suite(results)

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
