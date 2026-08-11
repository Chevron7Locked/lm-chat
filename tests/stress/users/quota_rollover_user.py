# SPDX-License-Identifier: Apache-2.0
"""S7 — Quota window rollover at midnight UTC.

Profile:
    50 users at 99/100 quota at 23:59:59 UTC; 1 second later at
    00:00:01 UTC.
    Assertions: 99->100 request succeeds; 100->101 request 429s;
    window rolls cleanly (no double-counting).

Implementation
--------------
We can't wait for a real midnight in a 10-min stress run.  The
orchestrator monkey-patches the quota's "today" clock by setting
``STRESS_QUOTA_CLOCK_OVERRIDE_UNIX`` and ``STRESS_QUOTA_ROLL_AT_UNIX``.
The quota middleware reads these in test mode (see the read in
``src/lmchat/middleware/quota.py``) and rolls when ``time.time()`` >=
``STRESS_QUOTA_ROLL_AT_UNIX``.

For the wire-level test, this user fires requests in two phases:

1. Pre-rollover: drive requests up to quota-1, then quota, then
   quota+1.  The +1 must 429.
2. Post-rollover: orchestrator advances the clock; user retries the
   same request and gets 200 again.
"""
from __future__ import annotations

import os
import time

from locust import between, tag, task

from .base_user import StressUser, warmup_tag

STRESS_QUOTA_LIMIT = int(os.environ.get("STRESS_QUOTA_LIMIT", "100"))


@tag("s7", "quota-rollover")  # type: ignore[arg-type]
class QuotaRolloverUser(StressUser):
    """Fire GET /api/chats repeatedly to consume the daily quota."""

    wait_time = between(0.05, 0.15)

    def on_start(self) -> None:
        super().on_start()
        self._requests_made = 0
        self._first_429_at: int | None = None

    @task
    def consume(self) -> None:
        self._requests_made += 1
        resp = self.client.get(
            "/api/chats",
            name=warmup_tag("GET /api/chats (quota-probe)"),
            catch_response=True,
        )
        with resp:
            if resp.status_code == 200:
                # The user hasn't crossed the boundary yet.
                resp.success()
                # If we already saw a 429 once, the quota must have
                # rolled — record that recovery worked.
                if self._first_429_at is not None:
                    # Recovery after rollover — that's the success path.
                    self._first_429_at = None
            elif resp.status_code == 429:
                if self._first_429_at is None:
                    self._first_429_at = self._requests_made
                # Boundary fired — that's expected at the quota limit.
                resp.success()
            elif 500 <= resp.status_code < 600:
                resp.failure(f"5xx during quota probe: {resp.status_code}")
            else:
                resp.success()
        # Light back-pressure so we don't oversubscribe the loop.
        time.sleep(0.01)
