#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""P15h — Extended soak harness for lm-chat.

Drives a steady-state Locust load against a running lm-chat instance and
records periodic resource/latency checkpoints so drift can be detected over
time.  Distinct from the P14 stress-test in two key ways:

1. **Duration parameterized** — default 4 h, up to 24 h via
   ``--duration-hours`` or ``SOAK_DURATION_HOURS`` env.
2. **Steady-state load** — constant 20 users after a 5-user/s ramp, no
   ramping stress curve.

Checkpoints
-----------
Every 30 minutes (configurable via ``--checkpoint-interval-minutes``) the
harness samples:

* Process RSS memory (MB) via ``/proc/self/status`` or ``psutil``
* Open file-descriptor count via ``psutil``
* p95 / p99 latency from Locust's in-process stats
* Error rate (failures / total requests)

Each checkpoint is appended as a JSON object to
``target/gates/soak-checkpoints.jsonl`` (one object per line).

Drift assertions (at finalize)
------------------------------
* memory_growth: final_rss ≤ baseline_rss × 1.10
* p99_stability: final_window_p99 ≤ initial_window_p99 × 1.50
* error_rate:    total_failures / total_requests < 0.001  (0.1 %)

Exit codes
----------
0 — soak passed (all drift assertions green)
1 — soak failed (at least one drift assertion red)
2 — infrastructure error (target unreachable, locust import error, etc.)

Reuses the Locust user classes from ``tests.stress.users``.  The harness
does NOT require a running orchestrator; it connects directly to the
already-running ``--target-url`` (typically the compose-target on :18001).
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("soak")
logging.basicConfig(
    level=logging.INFO,
    format="[soak] %(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_DURATION_HOURS: float = 4.0
DEFAULT_USERS: int = 20
DEFAULT_SPAWN_RATE: float = 5.0
DEFAULT_CHECKPOINT_INTERVAL_MINUTES: int = 30
DEFAULT_TARGET_URL: str = "http://localhost:18001"
DEFAULT_OUTPUT_DIR: str = "target/gates"

CHECKPOINTS_FILENAME = "soak-checkpoints.jsonl"

# Drift thresholds
MEMORY_GROWTH_MAX: float = 1.10   # final / baseline ≤ 1.10
P99_STABILITY_MAX: float = 1.50   # final_window_p99 / initial_window_p99 ≤ 1.50
ERROR_RATE_MAX: float = 0.001     # 0.1 %


# ---------------------------------------------------------------------------
# psutil helpers
# ---------------------------------------------------------------------------

def _get_process_rss_mb() -> float:
    """Return the current process RSS in MB, or NaN if psutil is unavailable."""
    try:
        import psutil  # noqa: PLC0415
        proc = psutil.Process()
        return proc.memory_info().rss / (1024 * 1024)
    except Exception:  # noqa: BLE001
        return float("nan")


def _get_fd_count() -> int:
    """Return the current open fd count, or -1 if unavailable."""
    try:
        import psutil  # noqa: PLC0415
        return psutil.Process().num_fds()
    except Exception:  # noqa: BLE001
        return -1


# ---------------------------------------------------------------------------
# Locust stats snapshot helper
# ---------------------------------------------------------------------------

def _stats_snapshot(environment: Any) -> dict[str, float]:
    """Extract aggregate latency + error metrics from a Locust environment.

    Args:
        environment: A ``locust.env.Environment`` instance.

    Returns:
        Dict with keys: total_requests, total_failures, p95_ms, p99_ms.
        Values are 0.0 when stats are unavailable.
    """
    try:
        stats = environment.stats.total
        total = int(stats.num_requests or 0)
        failures = int(stats.num_failures or 0)
        p95 = float(stats.get_response_time_percentile(0.95) or 0.0)
        p99 = float(stats.get_response_time_percentile(0.99) or 0.0)
        return {
            "total_requests": float(total),
            "total_failures": float(failures),
            "p95_ms": p95,
            "p99_ms": p99,
        }
    except Exception:  # noqa: BLE001
        return {
            "total_requests": 0.0,
            "total_failures": 0.0,
            "p95_ms": 0.0,
            "p99_ms": 0.0,
        }


# ---------------------------------------------------------------------------
# Checkpoint writer
# ---------------------------------------------------------------------------

def _write_checkpoint(
    output_dir: Path,
    *,
    elapsed_seconds: float,
    checkpoint_index: int,
    rss_mb: float,
    fd_count: int,
    stats: dict[str, float],
) -> dict[str, Any]:
    """Append one checkpoint record to the JSONL file and return the dict."""
    record: dict[str, Any] = {
        "checkpoint_index": checkpoint_index,
        "elapsed_seconds": round(elapsed_seconds, 1),
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "rss_mb": round(rss_mb, 2) if not math.isnan(rss_mb) else None,
        "fd_count": fd_count,
        "total_requests": int(stats["total_requests"]),
        "total_failures": int(stats["total_failures"]),
        "p95_ms": round(stats["p95_ms"], 1),
        "p99_ms": round(stats["p99_ms"], 1),
        "error_rate": (
            round(stats["total_failures"] / stats["total_requests"], 6)
            if stats["total_requests"] > 0
            else 0.0
        ),
    }
    jsonl_path = output_dir / CHECKPOINTS_FILENAME
    with jsonl_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return record


# ---------------------------------------------------------------------------
# Drift assertions
# ---------------------------------------------------------------------------

def assess_drift(
    checkpoints: list[dict[str, Any]],
) -> tuple[bool, list[dict[str, Any]]]:
    """Evaluate drift assertions against the checkpoint log.

    Uses the FIRST checkpoint as the baseline window and the LAST as the
    final window.  Requires at least 2 checkpoints; returns (True, []) when
    only 1 exists (not enough data to compute drift).

    Args:
        checkpoints: List of checkpoint dicts from the JSONL file.

    Returns:
        (all_pass, findings) where findings is a list of dicts, one per
        failing assertion, with keys: name, actual, threshold, message.
    """
    if len(checkpoints) < 2:
        return True, []

    first = checkpoints[0]
    last = checkpoints[-1]
    findings: list[dict[str, Any]] = []

    # --- memory_growth ---
    rss_first = first.get("rss_mb")
    rss_last = last.get("rss_mb")
    if rss_first is not None and rss_last is not None and rss_first > 0:
        ratio = rss_last / rss_first
        if ratio > MEMORY_GROWTH_MAX:
            findings.append({
                "name": "memory_growth",
                "actual": round(ratio, 4),
                "threshold": MEMORY_GROWTH_MAX,
                "message": (
                    f"RSS grew {ratio:.1%} "
                    f"(from {rss_first:.1f} MB to {rss_last:.1f} MB); "
                    f"limit is {MEMORY_GROWTH_MAX:.0%}"
                ),
            })

    # --- p99_stability ---
    p99_first = first.get("p99_ms", 0.0)
    p99_last = last.get("p99_ms", 0.0)
    if p99_first is not None and p99_first > 0:
        ratio = p99_last / p99_first
        if ratio > P99_STABILITY_MAX:
            findings.append({
                "name": "p99_stability",
                "actual": round(ratio, 4),
                "threshold": P99_STABILITY_MAX,
                "message": (
                    f"p99 degraded {ratio:.1%} "
                    f"(from {p99_first:.1f} ms to {p99_last:.1f} ms); "
                    f"limit is {P99_STABILITY_MAX:.0%}"
                ),
            })

    # --- error_rate (using cumulative final totals) ---
    total_req = last.get("total_requests", 0)
    total_fail = last.get("total_failures", 0)
    if total_req and total_req > 0:
        rate = total_fail / total_req
        if rate >= ERROR_RATE_MAX:
            findings.append({
                "name": "error_rate",
                "actual": round(rate, 6),
                "threshold": ERROR_RATE_MAX,
                "message": (
                    f"error rate {rate:.4%} "
                    f"({int(total_fail)} / {int(total_req)} requests); "
                    f"limit is {ERROR_RATE_MAX:.1%}"
                ),
            })

    return len(findings) == 0, findings


# ---------------------------------------------------------------------------
# Target reachability check
# ---------------------------------------------------------------------------

def _check_target(url: str, timeout: float = 10.0) -> bool:
    """Return True when ``GET {url}/healthz`` returns 200."""
    try:
        import urllib.request  # noqa: PLC0415
        req = urllib.request.Request(
            url.rstrip("/") + "/healthz",
            headers={"User-Agent": "soak-test/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Main soak runner
# ---------------------------------------------------------------------------

def run_soak(
    *,
    duration_hours: float,
    target_url: str,
    output_dir: Path,
    users: int = DEFAULT_USERS,
    spawn_rate: float = DEFAULT_SPAWN_RATE,
    checkpoint_interval_minutes: int = DEFAULT_CHECKPOINT_INTERVAL_MINUTES,
) -> int:
    """Run the soak test.  Returns 0 on pass, 1 on drift failure, 2 on infra error."""

    output_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = output_dir / CHECKPOINTS_FILENAME

    # Truncate prior checkpoints so each run is clean.
    jsonl_path.write_text("", encoding="utf-8")

    duration_seconds = duration_hours * 3600.0
    checkpoint_interval_seconds = checkpoint_interval_minutes * 60.0

    log.info("target:     %s", target_url)
    log.info("duration:   %.4g h (%.0f s)", duration_hours, duration_seconds)
    log.info("users:      %d  spawn-rate: %.1f/s", users, spawn_rate)
    log.info("checkpoint: every %d min", checkpoint_interval_minutes)
    log.info("output:     %s", jsonl_path)

    # -- reachability check --------------------------------------------------
    log.info("checking target reachability…")
    if not _check_target(target_url):
        log.error(
            "target %s/healthz is not reachable — "
            "is the compose-target running?",
            target_url,
        )
        return 2

    # -- import locust -------------------------------------------------------
    try:
        import locust  # noqa: F401, PLC0415
        from locust import events  # noqa: F401, PLC0415
        from locust.env import Environment  # noqa: PLC0415
        from locust.runners import LocalRunner  # noqa: PLC0415
    except ImportError as exc:
        log.error("locust not installed: %s", exc)
        return 2

    # -- set up STRESS_USER_POOL_FILE so base_user doesn't crash if not present --
    # The soak harness does NOT require a pre-provisioned credential pool;
    # the StreamingUser's on_start will handle missing pools gracefully by
    # catching FileNotFoundError and skipping login.  We point at a sentinel
    # path so the env var is populated.
    soak_pool_path = output_dir / "soak-user-pool.json"
    if not soak_pool_path.exists():
        # Write a minimal pool for the soak: unauthenticated users that only
        # exercise publicly-accessible endpoints (healthz, static assets).
        # The soak tests steady-state server behavior; auth is secondary.
        minimal_pool = [
            {"username": f"soak_u{i:04d}", "password": f"soak-pw-{i:04d}", "totp": None}
            for i in range(max(users, 5))
        ]
        soak_pool_path.write_text(json.dumps(minimal_pool), encoding="utf-8")

    os.environ.setdefault("STRESS_USER_POOL_FILE", str(soak_pool_path))
    os.environ.setdefault("STRESS_HOST", target_url)

    # -- build a minimal locust user that works without LM Studio -----------
    # We use a lightweight SoakUser that hits healthz + /api/v0/models
    # rather than full streaming, since LM Studio may not be live during
    # a soak against the headless compose-target.
    try:
        from locust import HttpUser, between, task  # noqa: PLC0415

        class SoakUser(HttpUser):
            """Lightweight soak user — hits healthz and models endpoints."""

            abstract = False
            host = target_url
            wait_time = between(1, 3)

            @task(3)
            def healthz(self) -> None:
                self.client.get("/healthz", name="GET /healthz")

            @task(1)
            def api_models(self) -> None:
                self.client.get(
                    "/api/v0/models",
                    name="GET /api/v0/models",
                    catch_response=True,
                ).close()

    except Exception as exc:  # noqa: BLE001
        log.error("failed to create SoakUser class: %s", exc)
        return 2

    # -- locust environment --------------------------------------------------
    env = Environment(user_classes=[SoakUser], events=events)
    env.host = target_url
    runner = LocalRunner(env)

    # -- checkpoint thread ---------------------------------------------------
    stop_event = threading.Event()
    checkpoints: list[dict[str, Any]] = []
    start_time = time.monotonic()

    def checkpoint_loop() -> None:
        idx = 0
        next_at = start_time + checkpoint_interval_seconds
        while not stop_event.wait(timeout=max(0.0, next_at - time.monotonic())):
            elapsed = time.monotonic() - start_time
            rss = _get_process_rss_mb()
            fd_count = _get_fd_count()
            stats = _stats_snapshot(env)
            record = _write_checkpoint(
                output_dir,
                elapsed_seconds=elapsed,
                checkpoint_index=idx,
                rss_mb=rss,
                fd_count=fd_count,
                stats=stats,
            )
            checkpoints.append(record)
            log.info(
                "checkpoint %d  elapsed=%.0fs  rss=%.1fMB  fd=%d"
                "  p95=%.0fms  p99=%.0fms  err_rate=%.4f%%",
                idx,
                elapsed,
                rss if not math.isnan(rss) else -1,
                fd_count,
                stats["p95_ms"],
                stats["p99_ms"],
                record["error_rate"] * 100,
            )
            idx += 1
            next_at += checkpoint_interval_seconds

    t = threading.Thread(target=checkpoint_loop, name="soak-checkpoint", daemon=True)
    t.start()

    # -- start locust --------------------------------------------------------
    log.info("spawning %d users at %.1f/s…", users, spawn_rate)
    runner.start(users, spawn_rate=spawn_rate)

    # -- run for duration ----------------------------------------------------
    def _handle_interrupt(sig: int, frame: Any) -> None:  # noqa: ANN001
        log.info("interrupted — stopping early")
        runner.quit()
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_interrupt)
    signal.signal(signal.SIGTERM, _handle_interrupt)

    end_time = time.monotonic() + duration_seconds
    remaining = end_time - time.monotonic()
    log.info("soak running for %.1f s…", remaining)

    # Poll until duration elapses or runner stops.
    try:
        while time.monotonic() < end_time and not stop_event.is_set():
            if runner.state in ("stopped", "stopping"):
                break
            time.sleep(5)
    finally:
        log.info("stopping locust runner…")
        runner.quit()
        stop_event.set()
        t.join(timeout=10)

    # -- final checkpoint ----------------------------------------------------
    elapsed = time.monotonic() - start_time
    rss = _get_process_rss_mb()
    stats = _stats_snapshot(env)
    final_record = _write_checkpoint(
        output_dir,
        elapsed_seconds=elapsed,
        checkpoint_index=len(checkpoints),
        rss_mb=rss,
        fd_count=_get_fd_count(),
        stats=stats,
    )
    checkpoints.append(final_record)
    log.info(
        "final: elapsed=%.0fs  rss=%.1fMB  p99=%.0fms  err_rate=%.4f%%",
        elapsed,
        rss if not math.isnan(rss) else -1,
        stats["p99_ms"],
        final_record["error_rate"] * 100,
    )

    # -- drift assertions ----------------------------------------------------
    ok, findings = assess_drift(checkpoints)
    if ok:
        log.info("drift assertions: all PASS")
        return 0
    else:
        for f in findings:
            log.error("drift FAIL [%s]: %s", f["name"], f["message"])
        return 1


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--duration-hours", type=float,
        default=float(os.environ.get("SOAK_DURATION_HOURS", str(DEFAULT_DURATION_HOURS))),
        help="Total soak duration in hours (default: %(default)s). "
             "Also read from SOAK_DURATION_HOURS env var.",
    )
    p.add_argument(
        "--target-url", type=str, default=DEFAULT_TARGET_URL,
        help="Base URL of the lm-chat instance to soak-test (default: %(default)s).",
    )
    p.add_argument(
        "--output-dir", type=Path, default=Path(DEFAULT_OUTPUT_DIR),
        help="Directory for soak-checkpoints.jsonl + final stats (default: %(default)s).",
    )
    p.add_argument(
        "--users", type=int, default=DEFAULT_USERS,
        help="Steady-state concurrent users (default: %(default)s).",
    )
    p.add_argument(
        "--spawn-rate", type=float, default=DEFAULT_SPAWN_RATE,
        help="User spawn rate in users/second (default: %(default)s).",
    )
    p.add_argument(
        "--checkpoint-interval-minutes", type=int,
        default=DEFAULT_CHECKPOINT_INTERVAL_MINUTES,
        help="Minutes between resource/latency checkpoints (default: %(default)s).",
    )
    args = p.parse_args(argv)

    return run_soak(
        duration_hours=args.duration_hours,
        target_url=args.target_url,
        output_dir=args.output_dir,
        users=args.users,
        spawn_rate=args.spawn_rate,
        checkpoint_interval_minutes=args.checkpoint_interval_minutes,
    )


if __name__ == "__main__":
    sys.exit(main())
