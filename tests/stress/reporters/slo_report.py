# SPDX-License-Identifier: Apache-2.0
"""SLO report — read Locust CSV output, compute p50/p95/p99/TTFT.

Events tagged ``warmup=true`` (anything whose Locust ``Name`` ends
with ``::warmup``) are excluded from the arithmetic.

Reads ``<csv_prefix>_stats.csv`` and ``<csv_prefix>_requests.csv``
(Locust's standard CSV emission via ``--csv``).  The first is the
aggregated stats per request name; the second is the per-request
raw event log.

Output: ``target/stress/slo_report.json`` with schema::

    {
      "ok": true|false,
      "summary": {
        "p50_ms": ..., "p95_ms": ..., "p99_ms": ...,
        "ttft_p99_ms": ...,
        "error_budget_observed": 0.0xx,
        "peak_rss_mb": ..., "fd_count": ..., "db_conn_peak": ...
      },
      "per_scenario": {"S1": {...}, ...},
      "gate_outcomes": [...]
    }
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    return float(statistics.quantiles(values, n=100, method="inclusive")[int(q * 100) - 1])


def _is_warmup(name: str) -> bool:
    return name.endswith("::warmup")


def _is_ttft(name: str) -> bool:
    return name.startswith("stream-ttft")


def _column_float(row: dict[str, Any], *keys: str) -> float:
    """Return the first ``keys`` value coercible to float, else 0.0.

    Lifted out of ``compute_summary``'s inner loop so the closure
    doesn't capture the loop variable (ruff B023).
    """
    for k in keys:
        v = row.get(k)
        if v in ("", None):
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return 0.0


def load_request_log(csv_prefix: Path) -> list[dict[str, Any]]:
    """Load the per-request CSV that Locust writes when --csv is set.

    Locust writes ``<prefix>_stats.csv`` (aggregated) AND, when
    ``--csv-full-history`` is passed, ``<prefix>_stats_history.csv``.
    The raw per-request log is at ``<prefix>_failures.csv`` for
    failures only; we use ``stats_history`` for percentiles.
    """
    stats_history = csv_prefix.parent / f"{csv_prefix.name}_stats_history.csv"
    if not stats_history.exists():
        return []
    rows: list[dict[str, Any]] = []
    with stats_history.open(encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            rows.append(row)
    return rows


def load_stats(csv_prefix: Path) -> list[dict[str, Any]]:
    stats_csv = csv_prefix.parent / f"{csv_prefix.name}_stats.csv"
    if not stats_csv.exists():
        return []
    rows: list[dict[str, Any]] = []
    with stats_csv.open(encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            rows.append(row)
    return rows


def load_stats_multi(
    target_dir: Path,
    scenario_names: Iterable[str],
) -> list[dict[str, Any]]:
    """Walk per-scenario CSV subdirs and merge stats rows.

    The orchestrator writes ``<target_dir>/<scenario.name>/run_stats.csv``
    per scenario so scenarios don't overwrite each other's CSV. This helper
    aggregates across all scenarios: it skips per-scenario "Aggregated"
    rows because they're misleading when merged, keeps the per-endpoint
    rows, and tags each row with its scenario name in a new
    ``_scenario`` field for downstream reporting.
    """
    all_rows: list[dict[str, Any]] = []
    for name in scenario_names:
        prefix = target_dir / name / "run"
        rows = load_stats(prefix)
        for row in rows:
            row_name = row.get("Name") or row.get("name") or ""
            if row_name == "Aggregated":
                continue
            row["_scenario"] = name
            all_rows.append(row)
    return all_rows


def _is_meta_row(row: dict[str, Any]) -> bool:
    """Drop synthetic non-HTTP rows like REPORT/WARN sentinels.

    The S10 boundary user emits ``REPORT s10-boundary`` and
    ``WARN s10-boundary-mismatch`` rows with response time 0 to carry
    invariant signal through Locust's stats CSV.  They are NOT real
    HTTP responses and must not contribute to the latency summary.
    """
    row_type = (row.get("Type") or row.get("type") or "").upper()
    return row_type in {"REPORT", "WARN"}


def compute_summary(
    stats: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Aggregate Locust stats into a single summary block.

    Excludes warmup rows, Aggregated rows, and synthetic REPORT/WARN
    meta rows.  Computes p50/p95/p99 as the MAX of the per-endpoint
    p* values (so a single slow endpoint dominates the summary, which
    is the conservative gate semantics).
    """
    p50s: list[float] = []
    p95s: list[float] = []
    p99s: list[float] = []
    ttft_p99s: list[float] = []
    total_requests = 0
    total_failures = 0
    for row in stats:
        name = row.get("Name") or row.get("name") or ""
        if name == "Aggregated":
            continue
        if _is_warmup(name):
            continue
        if _is_meta_row(row):
            continue
        try:
            count = int(row.get("Request Count", "0") or 0)
        except ValueError:
            count = 0
        try:
            failures = int(row.get("Failure Count", "0") or 0)
        except ValueError:
            failures = 0
        total_requests += count
        total_failures += failures
        # Locust stats CSV columns vary by version; tolerate both
        # "Median Response Time" and "50%".
        p50 = _column_float(row, "50%", "Median Response Time")
        p95 = _column_float(row, "95%")
        p99 = _column_float(row, "99%")
        if _is_ttft(name):
            ttft_p99s.append(p99)
        else:
            p50s.append(p50)
            p95s.append(p95)
            p99s.append(p99)
    summary: dict[str, Any] = {
        "p50_ms": int(max(p50s) if p50s else 0),
        "p95_ms": int(max(p95s) if p95s else 0),
        "p99_ms": int(max(p99s) if p99s else 0),
        "ttft_p99_ms": int(max(ttft_p99s) if ttft_p99s else 0),
        "total_requests": total_requests,
        "total_failures": total_failures,
        "error_budget_observed": (
            total_failures / total_requests if total_requests else 0.0
        ),
    }
    return summary


def write_report_multi(
    target_dir: Path,
    scenario_names: Iterable[str],
    out_path: Path,
    *,
    process_metrics: dict[str, Any] | None = None,
    gate_outcomes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Aggregate per-scenario CSVs and write the final report JSON.

    Like :func:`write_report` but reads from per-scenario subdirs
    populated by the orchestrator (one ``<target_dir>/<name>/run_*.csv``
    set per scenario) so scenarios don't overwrite each other's CSV.
    """
    return _write_report_from_stats(
        load_stats_multi(target_dir, scenario_names),
        out_path,
        process_metrics=process_metrics,
        gate_outcomes=gate_outcomes,
    )


def write_report(
    csv_prefix: Path,
    out_path: Path,
    *,
    process_metrics: dict[str, Any] | None = None,
    gate_outcomes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Write the final report JSON; return the parsed contents.

    Args:
        csv_prefix: Path prefix Locust used for its --csv output.
        out_path:   Destination JSON path.
        process_metrics: Optional ``{"peak_rss_mb", "fd_count", "db_conn_peak"}``
            from the orchestrator's psutil sampling.
        gate_outcomes: List of ``{"name", "ok", "detail"}`` dicts.
    """
    return _write_report_from_stats(
        load_stats(csv_prefix),
        out_path,
        process_metrics=process_metrics,
        gate_outcomes=gate_outcomes,
    )


def _write_report_from_stats(
    stats: list[dict[str, Any]],
    out_path: Path,
    *,
    process_metrics: dict[str, Any] | None,
    gate_outcomes: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Shared implementation for write_report and write_report_multi."""
    summary = compute_summary(stats)
    if process_metrics:
        summary.update(process_metrics)

    # Per-scenario block: pick the slowest REAL HTTP endpoint per scenario
    # (highest p99) so the admin sees the worst-case latency per load
    # profile.  Earlier behaviour keyed the dict by scenario name only and
    # let the last CSV row win, which surfaced the REPORT/WARN sentinel
    # rows (response time 0) and masked real perf data.
    per_scenario: dict[str, dict[str, Any]] = {}
    for row in stats:
        name = row.get("Name") or row.get("name") or ""
        if name == "Aggregated" or _is_warmup(name) or not name:
            continue
        if _is_meta_row(row):
            continue
        scenario_key = str(row.get("_scenario") or name)
        candidate = {
            "endpoint": name,
            "p50_ms": row.get("50%") or row.get("Median Response Time"),
            "p95_ms": row.get("95%"),
            "p99_ms": row.get("99%"),
            "requests": row.get("Request Count"),
            "failures": row.get("Failure Count"),
        }
        existing = per_scenario.get(scenario_key)
        if existing is None:
            per_scenario[scenario_key] = candidate
            continue
        try:
            existing_p99 = float(existing.get("p99_ms") or 0)
            candidate_p99 = float(candidate.get("p99_ms") or 0)
        except (TypeError, ValueError):
            existing_p99 = 0.0
            candidate_p99 = 0.0
        if candidate_p99 > existing_p99:
            per_scenario[scenario_key] = candidate

    ok = (
        summary["error_budget_observed"] < 0.001
        and (not gate_outcomes or all(g["ok"] for g in gate_outcomes))
    )

    report = {
        "ok": ok,
        "summary": summary,
        "per_scenario": per_scenario,
        "gate_outcomes": gate_outcomes or [],
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv-prefix", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    report = write_report(args.csv_prefix, args.out)
    print(json.dumps(report["summary"], indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
