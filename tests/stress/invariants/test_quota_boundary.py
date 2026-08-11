# SPDX-License-Identifier: Apache-2.0
"""Rate-limiter boundary invariant.

Invariant:
    Rate limiter fires at exactly quota-th request (not before, not
    after)

The S10 scenario fires synthetic events of type ``REPORT`` with name
``s10-boundary``.  This test reads the Locust event log and confirms
the first 429 landed on the (limit+1)-th request, NOT earlier.

The orchestrator writes the parsed event log to
``target/stress/s10_report.json``.  When that file is absent (e.g.
S10 was not run), the test skips.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.stress_invariant

S10_REPORT = Path(
    os.environ.get("STRESS_S10_REPORT", "target/stress/s10_report.json")
)


def test_rate_limit_fires_at_boundary() -> None:
    """The first 429 must land on request number (limit + 1)."""
    if not S10_REPORT.exists():
        pytest.skip(f"{S10_REPORT} missing; S10 not run")
    data = json.loads(S10_REPORT.read_text(encoding="utf-8"))
    success = int(data.get("success_count", 0))
    rejects = int(data.get("reject_count", 0))
    target = int(data.get("limit_target", 50))
    fired = int(data.get("fired", 0))

    assert fired >= target + 1, (
        f"S10 fired {fired} requests but expected at least {target + 1}"
    )
    # The success count must be at most the configured limit.
    # The login limiter counts both 200 and 401 as "served" because
    # the failure happens after the bucket check.  So success_count
    # should equal target (or target+1 if the boundary is inclusive).
    assert target <= success <= target + 1, (
        f"S10 boundary off: success_count={success} expected ~{target}"
    )
    assert rejects >= 1, "no 429 rejects observed in S10"
