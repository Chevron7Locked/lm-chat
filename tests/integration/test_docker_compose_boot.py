# SPDX-License-Identifier: Apache-2.0
"""P12g — Docker-compose boot smoke test.

Runs ``make validate-deploy`` as a subprocess and asserts exit 0. The make
target performs:

  1. Docker image build (deploy/Dockerfile).
  2. Image size assertion (< 200 MB).
  3. ``.env.validate`` render with placeholder values.
  4. Compose stack up.
  5. Healthz poll (60 s timeout).
  6. Source-map URL leakage check on the built JS bundle.
  7. Stack teardown (trap-driven; always runs).

The test is Docker-dependent. On CI runners or developer machines without a
running Docker daemon it skips cleanly. The corresponding CI job is tagged
``[docker]`` marker so this does not gate every PR.

See docs/deployment.md "Admin-equivalent validation".
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
# Generous ceiling: image build + boot + healthz poll can legitimately take
# several minutes on a cold cache. The make recipe itself enforces the
# 60 s healthz timeout; this is just an outer hard stop.
VALIDATE_DEPLOY_TIMEOUT_SECONDS = 600
# Empirically the compose build needs ~3 GiB of layer churn; 5 GiB leaves
# headroom for the healthz boot + teardown. Below this threshold the build
# step trips "no space left on device" mid-layer — an environmental flake,
# not a code regression. See FU-4.
MIN_FREE_DISK_BYTES = 5 * 1024 * 1024 * 1024


def free_disk_bytes() -> int:
    """Return free bytes on the root volume Docker Desktop draws from.

    macOS Docker Desktop's VM-backed data dir lives under the user home, but
    on a single-volume Mac that's the same filesystem as ``/``. Linux
    daemons store layers under ``/var/lib/docker``, also on ``/`` for
    default installs. Checking ``/`` is a faithful proxy in both cases.
    """
    return shutil.disk_usage("/").free


def docker_available() -> bool:
    """Return True iff ``docker info`` exits 0.

    Used as a skip predicate. We probe the daemon (not just the binary)
    because the binary can be installed without a running daemon (common
    on CI runners and on macOS when Docker Desktop is stopped).
    """
    try:
        result = subprocess.run(  # noqa: S603,S607 — fixed argv, no shell
            ["docker", "info"],
            capture_output=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


@pytest.mark.skipif(
    not docker_available(),
    reason="docker daemon not running",
)
@pytest.mark.skipif(
    free_disk_bytes() < MIN_FREE_DISK_BYTES,
    reason="Docker validate-deploy requires ~5 GiB free; current free disk too low for honest build",
)
# Slow subprocess build under full-suite resource contention (CPU/disk I/O
# shared with the rest of the suite) occasionally times out or hits a
# transient Docker daemon hiccup even though it passes reliably in isolation.
# Rerun rather than skip — a release gate should still run, just tolerate
# one contention-induced flake.
@pytest.mark.flaky(reruns=2, reruns_delay=10)
def test_make_validate_deploy_succeeds() -> None:
    """``make validate-deploy`` must exit 0 on a clean checkout.

    This is the P12g pre-tag gate. Failure here blocks a release tag.
    """
    result = subprocess.run(  # noqa: S603,S607 — fixed argv, no shell
        ["make", "validate-deploy"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=VALIDATE_DEPLOY_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        pytest.fail(
            "make validate-deploy failed "
            f"(exit {result.returncode})\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}",
        )
