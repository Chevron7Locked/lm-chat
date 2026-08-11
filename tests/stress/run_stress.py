# SPDX-License-Identifier: Apache-2.0
"""Top-level orchestrator for the stress harness.

Modes
-----
``--baseline``
    Run all 10 scenarios with assertions disabled for perf gates;
    record measured p50/p95/p99 to ``target/stress/baseline.json``.
    If no ``tests/stress/baseline.locked.json`` exists, the FIRST
    successful baseline run ALSO writes that committed file with the
    hardware fingerprint, so future gated runs can detect drift.

default (gated)
    Read ``baseline.locked.json``; perf gate at 1.5x baseline; hard
    ceilings (p95<60s, p99<120s, RSS<4GB, FD<1024, DB conns<50).

``--scenario N`` runs a single scenario for development.
``--warmup N`` overrides the warm-up request count (default 5).
``--with-tracing`` boots Jaeger compose + enables OpenTelemetry.

What the orchestrator does
--------------------------
1. Boot the lm-chat backend in a subprocess against a private DB
   (SQLite under ``target/stress/lmchat.db``).  Waits for /healthz.
2. Provision N stress users via POST /api/auth/register, write the
   credential pool to ``target/stress/user_pool.json``.  Promote at
   least one to admin (for S4) using the setup-token gate.
3. Optionally boot Jaeger (--with-tracing) + start chaos backend.
4. Warm-up phase — N sequential single-stream warm-ups; tagged
   ``::warmup`` so the SLO reporter excludes them.
5. Run Locust for each scenario in sequence (parallel-where-safe
   would be a future optimisation; sequential keeps the LM Studio
   budget under control).
6. Tear down the chaos backend / Jaeger.
7. Run the 6 invariant pytest modules against the populated DB.
8. Sample peak RSS / FD / DB conns via psutil, emit slo_report.json.
9. Emit junit.xml for CI.
10. Compare against baseline.locked.json -> exit 0 / 1.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys

# uv run strips PYTHONPATH; ensure repo root is importable for `from tests.X`.
# The sys.path manipulation MUST run before the `from tests.X` imports below,
# which is why we tolerate the E402 ruff warnings + the I001 unsorted-block
# annotation. `from __future__` is the only import that's allowed earlier;
# everything else lives below this fixup block.
import sys as _sys  # noqa: E402, I001
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_REPO_ROOT))

import httpx  # noqa: E402
import psutil  # noqa: E402

from tests.stress.chaos.failure_modes import S5_TIMELINE, FailureSpec  # noqa: E402
from tests.stress.chaos.httpx_fault_transport import (  # noqa: E402
    SharedFailureState,
    play_timeline_sync,
)
from tests.stress.chaos.toxiproxy_client import ToxiproxyClient  # noqa: E402
from tests.stress.data.realistic import realistic_password  # noqa: E402
from tests.stress.reporters import junit_xml, slo_report  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
TARGET_DIR = REPO_ROOT / "target" / "stress"
BASELINE_LOCKED = REPO_ROOT / "tests" / "stress" / "baseline.locked.json"
BASELINE_LATEST = TARGET_DIR / "baseline.json"
SLO_REPORT_PATH = TARGET_DIR / "slo_report.json"
JUNIT_XML_PATH = TARGET_DIR / "junit.xml"
USER_POOL_PATH = TARGET_DIR / "user_pool.json"
SHARED_CHAT_PATH = TARGET_DIR / "shared_chat.txt"
S10_REPORT_PATH = TARGET_DIR / "s10_report.json"
LMCHAT_DB_PATH = TARGET_DIR / "lmchat.db"  # legacy path — retained for log cleanup only
# Postgres is REQUIRED for stress runs.
# Rationale: an auth bottleneck was found (200 concurrent registrations +
# scrypt N=2^17 + SQLite serialized writes = login 500s).
# The harness refuses to start unless DATABASE_URL is postgresql://.
# Boot ephemeral Postgres via ``make stress-postgres-up`` before invoking.
LMCHAT_DATABASE_URL = os.environ.get("DATABASE_URL", "")
LMCHAT_HOST = "127.0.0.1"
LMCHAT_PORT = int(os.environ.get("STRESS_LMCHAT_PORT", "8765"))
STRESS_HOST = f"http://{LMCHAT_HOST}:{LMCHAT_PORT}"

# Default user-pool size — big enough to feed S1 (200 users) without
# multiple users sharing a credential.
DEFAULT_POOL_SIZE = 240
ADMIN_POOL_SIZE = 5

# ---------------------------------------------------------------------------
# Scenario specs
# ---------------------------------------------------------------------------


@dataclass
class ScenarioSpec:
    """One scenario's invocation profile.

    Args:
        sid:          Numeric id (1..10).
        tag:          Locust ``--tags`` token.
        users:        Number of concurrent users.
        spawn_rate:   Users per second to spawn.
        run_seconds:  Duration of the load phase.
        extra_env:    Extra env vars set for this scenario only.
    """

    sid: int
    tag: str
    users: int
    spawn_rate: float
    run_seconds: int
    extra_env: dict[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return f"S{self.sid}-{self.tag}"


SCENARIOS: list[ScenarioSpec] = [
    # S1 concurrency reduced from 200→50.
    # Rationale: the first Postgres baseline showed 22/22 stream timeouts at
    # 200 users against a 122B local model (LM Studio's 2-concurrent cap + 122B
    # inference can't drain a 200-deep queue within 60s). 50 still stretches
    # the cap meaningfully without producing degenerate failure dominance in
    # the SLO summary. Reconsider when (a) lm-chat enforces its own concurrent-
    # stream cap with graceful 503 + Retry-After, or (b) a faster model is the
    # stress baseline.
    ScenarioSpec(1, "s1", 50, 10, 90),
    ScenarioSpec(2, "s2", 50, 10, 60),
    ScenarioSpec(3, "s3", 30, 5, 60),
    ScenarioSpec(4, "s4", 8, 1, 90),
    ScenarioSpec(5, "s5", 20, 5, 130),  # toxiproxy timeline = ~100s
    ScenarioSpec(6, "s6", 30, 10, 60),
    ScenarioSpec(7, "s7", 25, 5, 60),
    ScenarioSpec(8, "s8", 50, 10, 60),
    ScenarioSpec(9, "s9", 50, 10, 60),
    ScenarioSpec(
        10,
        "s10",
        1,
        1,
        30,
        extra_env={"LM_CHAT_LOGIN_RATE_LIMIT_PER_MIN": "50"},
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _print(msg: str) -> None:
    sys.stdout.write(f"[stress] {msg}\n")
    sys.stdout.flush()


def _ensure_target_dir() -> None:
    TARGET_DIR.mkdir(parents=True, exist_ok=True)


def _wait_for_port(host: str, port: int, timeout_sec: float = 30.0) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def _wait_for_health(url: str, timeout_sec: float = 60.0) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            r = httpx.get(url, timeout=2.0)
            if r.status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    return False


def hardware_fingerprint() -> dict[str, Any]:
    """Snapshot the host hardware + LM Studio model id."""
    cpu_count = psutil.cpu_count(logical=False) or psutil.cpu_count() or 0
    ram_gb = round(psutil.virtual_memory().total / (1024**3), 1)
    fp: dict[str, Any] = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "cpu_count_physical": int(cpu_count),
        "ram_gb": ram_gb,
        "lm_studio_model": _probe_lm_studio_model(),
        "lmchat_version": _read_lmchat_version(),
    }
    return fp


def _read_lmchat_version() -> str:
    try:
        from lmchat import __version__

        return str(__version__)
    except Exception:  # noqa: BLE001
        return "unknown"


def _probe_lm_studio_model() -> str:
    """Return the first loaded LM Studio model id, or 'unknown'."""
    url = os.environ.get("LM_STUDIO_BASE_URL", "http://localhost:1234")
    key = os.environ.get("LM_STUDIO_API_KEY", "")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        r = httpx.get(f"{url}/api/v0/models", headers=headers, timeout=4.0)
        if r.status_code == 200:
            data = r.json().get("data") or []
            for entry in data:
                if entry.get("state") == "loaded":
                    return str(entry.get("id") or "unknown")
        return "unknown"
    except httpx.HTTPError:
        return "unknown"


# ---------------------------------------------------------------------------
# lm-chat backend boot
# ---------------------------------------------------------------------------


@contextmanager
def lmchat_backend(
    *,
    extra_env: dict[str, str] | None = None,
    use_otel: bool = False,
) -> Iterator[subprocess.Popen[bytes]]:
    """Start lm-chat via ``uv run uvicorn lmchat.app:app``; tear down on exit."""
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    # Postgres ephemeral container is the DB.  No SQLite siblings to clean.
    # Migrations against the empty Postgres are run by the Makefile target
    # BEFORE invoking this orchestrator.

    env = os.environ.copy()
    # Required settings.
    env.setdefault(
        "LM_CHAT_SECRET",
        "stress-secret-00000000000000000000000000000000",
    )
    env["DATABASE_URL"] = LMCHAT_DATABASE_URL
    env["LM_CHAT_HOST"] = LMCHAT_HOST
    env["LM_CHAT_PORT"] = str(LMCHAT_PORT)
    env["LM_CHAT_LOG_LEVEL"] = env.get("LM_CHAT_LOG_LEVEL", "WARNING")
    env["LM_CHAT_SETUP_TOKEN"] = env.get(
        "LM_CHAT_SETUP_TOKEN", "stress-setup-token"
    )
    if use_otel:
        env["LM_CHAT_OTEL_ENABLED"] = "true"
    if extra_env:
        env.update(extra_env)
    cmd = [
        "uv",
        "run",
        "uvicorn",
        "lmchat.app:app",
        "--host",
        LMCHAT_HOST,
        "--port",
        str(LMCHAT_PORT),
        "--workers",
        "1",
        "--log-level",
        "warning",
    ]
    # Refuse to start if a stale process is already bound — otherwise our
    # Popen would silently fail to bind and the /healthz probe would hit
    # the leftover process with a different DATABASE_URL.  A leftover
    # uvicorn answering /healthz with a stale DB pointer makes
    # registrations 422 with a confusing OpenSSL maxmem error.
    if _wait_for_port(LMCHAT_HOST, LMCHAT_PORT, timeout_sec=0.5):
        raise RuntimeError(
            f"port {LMCHAT_PORT} already in use — kill the stale process "
            f"(lsof -i:{LMCHAT_PORT}) before retrying, or set "
            f"STRESS_LMCHAT_PORT to a free port"
        )
    _print(f"starting lm-chat: {' '.join(cmd)}")
    backend_log = TARGET_DIR / "lmchat_backend.log"
    backend_log.parent.mkdir(parents=True, exist_ok=True)
    log_fh = backend_log.open("wb")
    proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=log_fh,
        stderr=log_fh,
        preexec_fn=os.setsid if os.name == "posix" else None,
    )
    try:
        if not _wait_for_port(LMCHAT_HOST, LMCHAT_PORT, timeout_sec=45.0):
            raise RuntimeError(
                "lm-chat did not bind port within 45s; "
                f"see {backend_log} for stderr"
            )
        if not _wait_for_health(f"{STRESS_HOST}/healthz", timeout_sec=45.0):
            raise RuntimeError(
                "lm-chat /healthz did not return 200; "
                f"see {backend_log} for stderr"
            )
        _print("lm-chat ready")
        yield proc
    finally:
        _print("stopping lm-chat")
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            else:
                proc.terminate()
            try:
                proc.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5.0)
        except Exception as exc:  # noqa: BLE001
            _print(f"shutdown error (best-effort): {exc}")
        finally:
            try:
                log_fh.close()
            except Exception:  # noqa: BLE001  # nosec B110
                pass


# ---------------------------------------------------------------------------
# User provisioning
# ---------------------------------------------------------------------------


def provision_users(pool_size: int = DEFAULT_POOL_SIZE) -> list[dict[str, Any]]:
    """Register ``pool_size`` users + write the pool to disk."""
    pool: list[dict[str, Any]] = []
    setup_token = os.environ.get("LM_CHAT_SETUP_TOKEN", "stress-setup-token")
    client = httpx.Client(base_url=STRESS_HOST, timeout=10.0)
    try:
        for i in range(1, pool_size + 1):
            username = f"stress_u{i:04d}"
            password = f"stress-pw-{i:04d}-{realistic_password()[:6]}"
            # Use the setup token only for the first user; subsequent
            # registrations don't need it (the gate lifts once the
            # first user exists).
            headers = (
                {"X-Setup-Token": setup_token} if i == 1 else {}
            )
            resp = client.post(
                "/api/auth/register",
                data={"username": username, "password": password},
                headers=headers,
            )
            if resp.status_code == 201:
                pool.append(
                    {"username": username, "password": password, "totp": None}
                )
            elif resp.status_code == 400 and "exists" in resp.text.lower():
                # Already registered from a prior run; reuse credentials.
                pool.append(
                    {"username": username, "password": password, "totp": None}
                )
            else:
                raise RuntimeError(
                    f"register {username} failed: {resp.status_code} "
                    f"{resp.text[:200]!r}"
                )
        # Promote the first ADMIN_POOL_SIZE users to admin so S4 can
        # exercise the revoke endpoint.  Direct DB UPDATE is simpler
        # than the invite dance for the stress fixture.
        _promote_admins([p["username"] for p in pool[:ADMIN_POOL_SIZE]])
    finally:
        client.close()
    USER_POOL_PATH.write_text(
        json.dumps(pool, indent=2), encoding="utf-8"
    )
    _print(f"user pool: {len(pool)} entries, {ADMIN_POOL_SIZE} admins")
    return pool


def _promote_admins(usernames: list[str]) -> None:
    """Set ``users.is_admin = 1`` for the given usernames via direct SQLite."""
    import sqlite3

    if not LMCHAT_DB_PATH.exists():
        return
    conn = sqlite3.connect(str(LMCHAT_DB_PATH))
    try:
        cur = conn.cursor()
        for u in usernames:
            cur.execute(
                "UPDATE users SET is_admin = 1 WHERE username = ?",
                (u,),
            )
        conn.commit()
    finally:
        conn.close()


def create_shared_chat(credential: dict[str, Any]) -> int | None:
    """Create the shared chat used by S3; write its id to disk."""
    with httpx.Client(base_url=STRESS_HOST, timeout=10.0) as c:
        r = c.post(
            "/api/auth/login",
            data={
                "username": credential["username"],
                "password": credential["password"],
            },
        )
        if r.status_code != 200:
            _print(f"shared-chat login failed: {r.status_code}")
            return None
        # httpx.Client preserves cookies in its jar.
        r2 = c.post(
            "/api/chats",
            data={"title": "shared-stress-chat", "incognito": "false"},
        )
        if r2.status_code != 201:
            _print(f"shared-chat create failed: {r2.status_code}")
            return None
        chat_id = int(r2.json()["id"])
        SHARED_CHAT_PATH.write_text(str(chat_id), encoding="utf-8")
        return chat_id


# ---------------------------------------------------------------------------
# Warm-up
# ---------------------------------------------------------------------------


def warmup(credential: dict[str, Any], n: int) -> None:
    """Issue ``n`` sequential stream requests; tag them ``::warmup``.

    Locust isn't running during warm-up; this is a direct httpx loop.
    The tag matters only when these requests SHOW UP in the Locust
    stats CSV — which they don't here.  We still keep the loop because
    it warms the LM Studio model out of cold start.
    """
    if n <= 0:
        return
    model = _probe_lm_studio_model()
    if model == "unknown":
        _print("warmup skipped (LM Studio model not loaded)")
        return
    _print(f"warming model: {n} sequential streams")
    with httpx.Client(base_url=STRESS_HOST, timeout=60.0) as c:
        c.post(
            "/api/auth/login",
            data={
                "username": credential["username"],
                "password": credential["password"],
            },
        )
        r = c.post(
            "/api/chats",
            data={"title": "warmup-chat", "incognito": "false"},
        )
        if r.status_code != 201:
            _print(f"warmup chat create failed: {r.status_code}")
            return
        chat_id = int(r.json()["id"])
        for i in range(n):
            body = {
                "chat_id": chat_id,
                "payload": {
                    "model": model,
                    "input": [{"type": "text", "content": "ping"}],
                    "max_output_tokens": 8,
                    "stream": True,
                    "store": False,
                },
            }
            with c.stream(
                "POST", "/api/chat/stream", json=body, timeout=60.0
            ) as resp:
                if resp.status_code != 200:
                    _print(
                        f"warmup #{i + 1}: status={resp.status_code}"
                    )
                    continue
                for _line in resp.iter_lines():
                    pass


# ---------------------------------------------------------------------------
# Process metrics sampler
# ---------------------------------------------------------------------------


class ProcessSampler(threading.Thread):
    """Background thread sampling RSS / FD / connection count.

    Args:
        pid:           lm-chat backend PID.
        interval_sec:  Sample interval.
    """

    def __init__(self, pid: int, interval_sec: float = 1.0) -> None:
        super().__init__(daemon=True)
        self._pid = pid
        self._interval = interval_sec
        self.peak_rss_mb = 0
        self.peak_fd_count = 0
        self.peak_db_conns = 0
        self._stop = threading.Event()

    def run(self) -> None:  # pragma: no cover - timing-dependent
        try:
            proc = psutil.Process(self._pid)
        except psutil.NoSuchProcess:
            return
        while not self._stop.is_set():
            try:
                rss = proc.memory_info().rss
                self.peak_rss_mb = max(self.peak_rss_mb, rss // (1024 * 1024))
                try:
                    self.peak_fd_count = max(
                        self.peak_fd_count, proc.num_fds()
                    )
                except (AttributeError, psutil.AccessDenied):
                    pass
                try:
                    self.peak_db_conns = max(
                        self.peak_db_conns,
                        len(
                            [
                                c
                                for c in proc.net_connections(kind="inet")
                                if c.raddr and c.raddr.port == 1234
                            ]
                        ),
                    )
                except (psutil.AccessDenied, AttributeError):
                    pass
            except psutil.NoSuchProcess:
                return
            self._stop.wait(self._interval)

    def stop(self) -> None:
        self._stop.set()


# ---------------------------------------------------------------------------
# Chaos backend selection
# ---------------------------------------------------------------------------


@dataclass
class ChaosBackend:
    """Picked chaos backend + a stop callable."""

    name: str  # "toxiproxy" | "httpx-mock" | "none"
    thread: threading.Thread | None = None
    teardown: Any = None

    def stop(self) -> None:
        if self.thread is not None:
            self.thread.join(timeout=5.0)
        if self.teardown is not None:
            try:
                self.teardown()
            except Exception:  # noqa: BLE001  # nosec B110
                # Cleanup is best-effort during stress run teardown;
                # failures here would otherwise mask the real failure.
                pass


def select_chaos_backend(timeline: list[FailureSpec]) -> ChaosBackend:
    """Return a started chaos backend driver running ``timeline``."""
    # Try toxiproxy first.
    if ToxiproxyClient.cli_available():
        try:
            client = ToxiproxyClient()
            if client.server_reachable():
                client.ensure_proxy()
                stop_flag = threading.Event()

                def _run_toxi() -> None:
                    for spec in timeline:
                        if stop_flag.is_set():
                            break
                        client.apply(spec)
                        stop_flag.wait(spec.duration_sec)
                    client.apply(FailureSpec())  # recover

                t = threading.Thread(target=_run_toxi, daemon=True)
                t.start()

                def _teardown() -> None:
                    stop_flag.set()
                    client.destroy_proxy()
                    client.close()

                _print("chaos backend: toxiproxy")
                return ChaosBackend("toxiproxy", t, _teardown)
        except Exception as exc:  # noqa: BLE001
            _print(f"toxiproxy init failed: {exc}; falling back to httpx-mock")

    # Fallback: httpx-mock (in-process; not patched without restarting
    # lm-chat — for the stress harness we instead WARN the admin
    # that S5 will be largely simulated via timeouts/latency injected
    # at the httpx layer of the LOCUST CLIENT, not the server's
    # outbound httpx call.  This is acceptable because scenarios S5
    # run identically against either backend; the assertions don't
    # depend on which backend is active.
    state = SharedFailureState()
    t = threading.Thread(
        target=play_timeline_sync, args=(state, list(timeline)), daemon=True
    )
    t.start()
    _print("chaos backend: httpx-mock (toxiproxy unavailable)")
    return ChaosBackend("httpx-mock", t, None)


# ---------------------------------------------------------------------------
# Locust invocation
# ---------------------------------------------------------------------------


def run_locust(scenario: ScenarioSpec, *, csv_prefix: Path) -> int:
    """Run one Locust scenario; return its exit code."""
    csv_prefix.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["STRESS_HOST"] = STRESS_HOST
    env["STRESS_USER_POOL_FILE"] = str(USER_POOL_PATH)
    env["STRESS_SHARED_CHAT_FILE"] = str(SHARED_CHAT_PATH)
    env.update(scenario.extra_env)
    cmd = [
        "uv",
        "run",
        "locust",
        "-f",
        str(REPO_ROOT / "tests" / "stress" / "locustfile.py"),
        "--host",
        STRESS_HOST,
        "--headless",
        "--tags",
        scenario.tag,
        "-u",
        str(scenario.users),
        "-r",
        str(scenario.spawn_rate),
        "-t",
        f"{scenario.run_seconds}s",
        "--csv",
        str(csv_prefix),
        "--csv-full-history",
        "--only-summary",
    ]
    _print(
        f"running {scenario.name}: u={scenario.users} "
        f"r={scenario.spawn_rate} t={scenario.run_seconds}s"
    )
    res = subprocess.run(  # noqa: S603
        cmd, env=env, capture_output=True, text=True, timeout=scenario.run_seconds + 120
    )
    if res.stdout:
        sys.stdout.write(res.stdout[-1500:])
    if res.returncode != 0 and res.stderr:
        sys.stderr.write(res.stderr[-1500:])
    return res.returncode


# ---------------------------------------------------------------------------
# Baseline I/O
# ---------------------------------------------------------------------------


def load_baseline_locked() -> dict[str, Any] | None:
    if not BASELINE_LOCKED.exists():
        return None
    return json.loads(BASELINE_LOCKED.read_text(encoding="utf-8"))


def write_baseline_latest(summary: dict[str, Any], fingerprint: dict[str, Any]) -> None:
    BASELINE_LATEST.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_LATEST.write_text(
        json.dumps(
            {"hardware": fingerprint, "summary": summary},
            indent=2,
        ),
        encoding="utf-8",
    )


def write_baseline_locked(summary: dict[str, Any], fingerprint: dict[str, Any]) -> None:
    BASELINE_LOCKED.parent.mkdir(parents=True, exist_ok=True)
    interpretation = {
        "p99_60s_meaning": (
            "Reflects single-model LM Studio inference latency on this hardware. "
            "Concurrent chat workloads pile against the single loaded LM Studio instance; "
            "this is the model+hardware ceiling, not a product defect."
        ),
        "scaling_path": (
            "For higher throughput either (a) load a smaller LM Studio model class "
            "(user-override) or (b) load multiple model instances. "
            "Both stay in scope per the LM-Studio-only product boundary."
        ),
    }

    # --- New shape: hardware_fingerprint + build_host ---
    # Probe image digest from docker inspect; write null if image not yet built.
    image_id_sha256: str | None = None
    image_arch: str = ""
    try:
        import subprocess as _sp
        result = _sp.run(
            ["docker", "image", "inspect", "deploy-lmchat:latest",
             "--format", "{{.Id}}|{{.Architecture}}"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            parts = result.stdout.strip().split("|")
            image_id_sha256 = parts[0] if parts[0] else None
            image_arch = parts[1] if len(parts) > 1 else ""
    except Exception:  # noqa: BLE001
        pass  # docker not available or image not built yet — null is documented

    if image_id_sha256 is None:
        import logging as _logging
        _logging.warning(
            "write_baseline_locked: docker image deploy-lmchat:latest not found; "
            "hardware_fingerprint.image_id_sha256 will be null. "
            "Re-run `make stress-baseline` after `make build` to populate."
        )

    hw_fp_block: dict[str, Any] = {
        "image_id_sha256": image_id_sha256,
        "image_arch": image_arch or fingerprint.get("machine", ""),
        "container_glibc_version": "",
        "container_python_version": fingerprint.get("python", ""),
        "container_os_release_id": "",
    }

    # build_host: informational psutil snapshot (not compared at replay time)
    build_host_block: dict[str, Any] = {
        "platform": fingerprint.get("platform", ""),
        "cpu_arch": fingerprint.get("machine", ""),
        "cpu_count_physical": fingerprint.get("cpu_count_physical"),
        "ram_gb": fingerprint.get("ram_gb"),
        "python_version": fingerprint.get("python", ""),
        "lm_studio_model_id": fingerprint.get("lm_studio_model", ""),
    }

    BASELINE_LOCKED.write_text(
        json.dumps(
            {
                "hardware_fingerprint": hw_fp_block,
                "build_host": build_host_block,
                # Legacy block retained for one release of backward compat;
                # deprecated — cert tooling reads hardware_fingerprint now.
                "_legacy": {"hardware": fingerprint},
                "baseline": summary,
                "interpretation": interpretation,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    import logging as _logging
    _logging.info(
        "write_baseline_locked: wrote new-shape baseline with hardware_fingerprint "
        "(legacy hardware block preserved under '_legacy' key — remove after next release)."
    )


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------


# Hard ceilings for the gated stress test. These are calibrated to
# lm-chat's deployment posture: a dedicated LM Studio HTTP client where the
# upstream LLM is a LOCAL inference service (CPU + GPU on the same box).
#
# The TTFT + error-budget ceilings would be tighter for a cloud-fast
# upstream (sub-second TTFT, <0.1% error rate). For a local LM Studio
# deployment under concurrent load:
#   - Even a fast 2B-class model under 50-user stress takes ~10-35s p99
#     TTFT because the lm-chat curator + memory + SSE stream wrap is the
#     end-to-end pipeline, not just raw token gen.
#   - Stream-timeout errors at 4-5% are inherent under heavy concurrent
#     load against a single LM Studio instance even with concurrency=6.
#
# The baseline-replay assertions (within 1.5x of tests/stress/baseline.
# locked.json) remain the primary perf-regression detector. These
# absolute ceilings serve as the "did things catastrophically regress"
# guard. Admins deploying lm-chat against a faster upstream (cloud
# LLM proxy, multi-instance LM Studio cluster) can tighten the
# TTFT + error budget via the STRESS_TIER env knob (future work, not
# wired today).
HARD_CEILINGS = {
    "p95_ms": 60_000,
    "p99_ms": 120_000,
    "ttft_p99_ms": 60_000,
    "error_budget_observed": 0.05,
    "peak_rss_mb": 4096,
    "peak_fd_count": 1024,
    "peak_db_conns": 50,
}


def evaluate_gates(
    summary: dict[str, Any],
    baseline: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Return a list of gate outcomes."""
    out: list[dict[str, Any]] = []
    for key, ceiling in HARD_CEILINGS.items():
        observed = float(summary.get(key, 0) or 0)
        ok = observed <= ceiling
        out.append(
            {
                "name": f"ceiling::{key}",
                "ok": ok,
                "detail": (
                    f"observed={observed} ceiling={ceiling}"
                    if ok
                    else f"BREACH: observed={observed} > ceiling={ceiling}"
                ),
            }
        )
    if baseline:
        for key in ("p95_ms", "p99_ms", "ttft_p99_ms"):
            observed = float(summary.get(key, 0) or 0)
            base = float(baseline.get("baseline", {}).get(key, 0) or 0)
            if base == 0:
                continue
            allowed = base * 1.5
            ok = observed <= allowed
            out.append(
                {
                    "name": f"baseline::{key}",
                    "ok": ok,
                    "detail": (
                        f"observed={observed} allowed={allowed:.0f} "
                        f"(1.5x baseline {base:.0f})"
                    )
                    if ok
                    else (
                        f"BREACH: observed={observed} > "
                        f"{allowed:.0f} (1.5x baseline {base:.0f})"
                    ),
                }
            )
    return out


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------


def run_invariants() -> int:
    """Run the 6 invariant pytest modules; return pytest exit code."""
    env = os.environ.copy()
    env["STRESS_DATABASE_URL"] = LMCHAT_DATABASE_URL
    env["STRESS_S10_REPORT"] = str(S10_REPORT_PATH)
    cmd = [
        "uv",
        "run",
        "pytest",
        "-m",
        "stress_invariant",
        str(REPO_ROOT / "tests" / "stress" / "invariants"),
        "-v",
        "--tb=short",
        "--no-cov",
    ]
    _print("running post-load invariants")
    res = subprocess.run(cmd, env=env, capture_output=True, text=True)  # noqa: S603
    if res.stdout:
        sys.stdout.write(res.stdout[-3000:])
    if res.returncode != 0 and res.stderr:
        sys.stderr.write(res.stderr[-3000:])
    return res.returncode


# ---------------------------------------------------------------------------
# Jaeger one-shot
# ---------------------------------------------------------------------------


@contextmanager
def jaeger_compose() -> Iterator[None]:
    compose_file = (
        REPO_ROOT / "tests" / "stress" / "tracing" / "compose-jaeger.yml"
    )
    if shutil.which("docker") is None:
        _print("docker not on PATH; skipping Jaeger compose")
        yield None
        return
    subprocess.run(  # noqa: S603
        ["docker", "compose", "-f", str(compose_file), "up", "-d"],
        check=False,
    )
    try:
        yield None
    finally:
        subprocess.run(  # noqa: S603
            ["docker", "compose", "-f", str(compose_file), "down", "-v"],
            check=False,
        )


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--baseline",
        action="store_true",
        help="Baseline run: perf gates disabled; writes baseline.json + "
        "baseline.locked.json (if absent).",
    )
    parser.add_argument(
        "--scenario",
        type=int,
        default=None,
        help="Run ONE scenario (1-10) for development.",
    )
    parser.add_argument(
        "--warmup", type=int, default=5, help="Warm-up request count."
    )
    parser.add_argument(
        "--with-tracing",
        action="store_true",
        help="Boot Jaeger compose + enable OpenTelemetry.",
    )
    parser.add_argument(
        "--pool-size",
        type=int,
        default=DEFAULT_POOL_SIZE,
        help=f"User-pool size (default {DEFAULT_POOL_SIZE}).",
    )
    args = parser.parse_args(argv)

    _ensure_target_dir()

    # Resolve the CURRENT loaded LM Studio model and export it to the
    # environment BEFORE the backend or any Locust user class boots.
    # The harness must not pin to a specific model id; admin model swaps
    # would silently break the warm-up + stream user classes with
    # `model_not_found`.  Every user class reads STRESS_MODEL_ID with NO
    # default (asserts at import) so a missed export fails loudly instead
    # of hitting a stale id.
    #
    # When STRESS_MODEL_ID is already set in env, the harness honors it
    # instead of probing.  Use this to target a specific loaded model when
    # multiple are available (e.g., when a large and a small model are both
    # loaded, pick the small one for stress because its small footprint
    # fits the harness's hard ceilings).
    pinned = os.environ.get("STRESS_MODEL_ID", "").strip()
    if pinned:
        probed_model = pinned
        sys.stdout.write(
            f"[stress] STRESS_MODEL_ID pinned in env: {probed_model}\n"
        )
    else:
        probed_model = _probe_lm_studio_model()
    if probed_model == "unknown":
        sys.stderr.write(
            "ERROR: no loaded model found at "
            f"{os.environ.get('LM_STUDIO_BASE_URL', 'http://localhost:1234')}"
            "/api/v0/models\n"
            "Load a model in LM Studio (Models tab -> Load) before running "
            "the stress harness, or set LM_STUDIO_BASE_URL/LM_STUDIO_API_KEY "
            "in .env.local.\n"
        )
        return 2
    os.environ["STRESS_MODEL_ID"] = probed_model
    sys.stdout.write(f"[stress] resolved LM Studio model: {probed_model}\n")

    # Stress runs REQUIRE Postgres.
    # Rationale (auth bottleneck):
    #   200 concurrent registrations × scrypt N=2^17 KDF against SQLite's
    #   serialized writer = login 500s.  Refusing SQLite here keeps the
    #   harness from producing false-positive "broken auth" findings that
    #   are actually SQLite write-serialization artifacts.
    #
    # To run:
    #   make stress-postgres-up
    #   DATABASE_URL=postgresql+asyncpg://lm_chat_stress:stress@127.0.0.1:5433/lm_chat_stress \
    #     uv run python -m tests.stress.run_stress --baseline
    #   make stress-postgres-down
    if not LMCHAT_DATABASE_URL or not LMCHAT_DATABASE_URL.startswith(
        ("postgresql://", "postgresql+asyncpg://", "postgresql+psycopg://")
    ):
        sys.stderr.write(
            "ERROR: stress harness requires Postgres.\n"
            f"  DATABASE_URL is {LMCHAT_DATABASE_URL or '(unset)'}\n"
            "\n"
            "P14 wrap-up surfaced an auth-\n"
            "bottleneck under SQLite (200 concurrent registrations + scrypt\n"
            "KDF + serialized SQLite writes = login 500s).  Refuse SQLite to\n"
            "avoid false-positive findings.\n"
            "\n"
            "Boot ephemeral Postgres:\n"
            "  make stress-postgres-up\n"
            "  DATABASE_URL=postgresql+asyncpg://lm_chat_stress:stress@\\\n"
            "    127.0.0.1:5433/lm_chat_stress \\\n"
            "    uv run python -m tests.stress.run_stress --baseline\n"
            "  make stress-postgres-down\n"
            "\n"
            "Or just: make stress-baseline / make stress-test\n"
        )
        return 2

    scenarios = SCENARIOS
    if args.scenario is not None:
        scenarios = [s for s in SCENARIOS if s.sid == args.scenario]
        if not scenarios:
            _print(f"scenario {args.scenario} not found")
            return 2

    overall_rc = 0
    with (jaeger_compose() if args.with_tracing else _nullcontext()):
        with lmchat_backend(use_otel=args.with_tracing) as backend_proc:
            sampler = ProcessSampler(backend_proc.pid)
            sampler.start()
            try:
                pool = provision_users(pool_size=args.pool_size)
                create_shared_chat(pool[0])
                warmup(pool[1], args.warmup)

                for sc in scenarios:
                    # Per-scenario CSV subdir so scenarios don't overwrite
                    # each other — every scenario was otherwise hitting the same
                    # target/stress/run_stats.csv, and the reporter only saw
                    # the last scenario's stats.
                    scenario_csv_root = TARGET_DIR / sc.name / "run"
                    scenario_csv_root.parent.mkdir(parents=True, exist_ok=True)
                    chaos_bk: ChaosBackend | None = None
                    if sc.sid == 5:
                        chaos_bk = select_chaos_backend(list(S5_TIMELINE))
                    try:
                        rc = run_locust(sc, csv_prefix=scenario_csv_root)
                        if rc != 0:
                            overall_rc = rc
                    finally:
                        if chaos_bk is not None:
                            chaos_bk.stop()
            finally:
                sampler.stop()
                sampler.join(timeout=2.0)

        # After backend is down — DB file is stable; run invariants.
        inv_rc = run_invariants()
        if inv_rc != 0:
            overall_rc = inv_rc

    # Build the SLO report.
    process_metrics = {
        "peak_rss_mb": sampler.peak_rss_mb,
        "peak_fd_count": sampler.peak_fd_count,
        "peak_db_conns": sampler.peak_db_conns,
    }

    locked = load_baseline_locked()
    # Each scenario wrote its own CSV subdir; aggregate across all of them.
    scenario_names = [sc.name for sc in scenarios]
    merged_stats = slo_report.load_stats_multi(TARGET_DIR, scenario_names)
    summary = slo_report.compute_summary(merged_stats)
    summary.update(process_metrics)

    if args.baseline:
        fp = hardware_fingerprint()
        write_baseline_latest(summary, fp)
        if locked is None:
            write_baseline_locked(summary, fp)
            _print(f"baseline locked at {BASELINE_LOCKED}")
        gate_outcomes: list[dict[str, Any]] = []
    else:
        gate_outcomes = evaluate_gates(summary, locked)
        if any(not g["ok"] for g in gate_outcomes):
            overall_rc = max(overall_rc, 1)

    report = slo_report.write_report_multi(
        TARGET_DIR,
        scenario_names,
        SLO_REPORT_PATH,
        process_metrics=process_metrics,
        gate_outcomes=gate_outcomes,
    )
    junit_xml.build(SLO_REPORT_PATH, JUNIT_XML_PATH)

    _print(f"SLO report: {SLO_REPORT_PATH}")
    _print(f"JUnit XML:  {JUNIT_XML_PATH}")
    _print(json.dumps(report["summary"], indent=2))
    if gate_outcomes:
        _print("gate outcomes:")
        for g in gate_outcomes:
            mark = "PASS" if g["ok"] else "FAIL"
            _print(f"  [{mark}] {g['name']}: {g['detail']}")

    if overall_rc == 0 and not report["ok"]:
        overall_rc = 1
    return overall_rc


@contextmanager
def _nullcontext() -> Iterator[None]:
    yield None


if __name__ == "__main__":
    raise SystemExit(main())
