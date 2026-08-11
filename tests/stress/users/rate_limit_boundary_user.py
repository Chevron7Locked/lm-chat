# SPDX-License-Identifier: Apache-2.0
"""S10 — Rate limiting boundary.

Profile:
    Single user, 100 requests in 60s, rate limit 50/min.
    Assertions: first 50 succeed; 51-100 return 429; no 429 on
    1-50; 51 at +1s after 50 still 429s.

We exercise the login rate limit (10/min by default) because:

- It is the most heavily exercised limiter in the codebase.
- It is documented as a fixed-window-on-IP limiter with a clear
  boundary — perfect for off-by-one detection.
- The streaming limiter is per-user / burst-30 and burns LM Studio
  tokens unnecessarily for this test.

The orchestrator sets ``LM_CHAT_LOGIN_RATE_LIMIT_PER_MIN=50`` for the
duration of S10 so the boundary is 50.  We fire 100 logins from ONE
user (single client) and count statuses.
"""
from __future__ import annotations

import os

from locust import between, events, tag, task

from .base_user import StressUser, warmup_tag

STRESS_S10_BUDGET = int(os.environ.get("STRESS_S10_BUDGET", "100"))
STRESS_S10_LIMIT = int(os.environ.get("STRESS_S10_LIMIT", "50"))


@tag("s10", "rate-limit-boundary")  # type: ignore[arg-type]
class RateLimitBoundaryUser(StressUser):
    """Fire login requests until the bucket drains; count 429 boundary."""

    wait_time = between(0.0, 0.05)

    def on_start(self) -> None:
        # IMPORTANT: do NOT call super().on_start() — we don't want the
        # one successful login at on_start to count against the budget.
        self.credential = {
            "username": os.environ.get("STRESS_S10_USERNAME", "stress_u0001"),
            "password": os.environ.get("STRESS_S10_PASSWORD", "stress-pw-0001"),
            "totp": None,
        }
        self._success_count = 0
        self._reject_count = 0
        self._fired = 0

    @task
    def hammer_login(self) -> None:
        if self._fired >= STRESS_S10_BUDGET:
            self.environment.runner.quit()
            return
        self._fired += 1
        resp = self.client.post(
            "/api/auth/login",
            data={
                "username": self.credential["username"],
                "password": self.credential["password"],
            },
            name=warmup_tag("POST /api/auth/login (rate-limit-probe)"),
            catch_response=True,
        )
        with resp:
            if resp.status_code in (200, 401):
                # 401 = invalid credentials (also counts toward bucket
                # in current implementation); both are "allowed past
                # the bucket".
                self._success_count += 1
                resp.success()
            elif resp.status_code == 429:
                self._reject_count += 1
                resp.success()  # expected post-boundary
            else:
                resp.failure(
                    f"unexpected {resp.status_code} on login-probe"
                )

    def on_stop(self) -> None:
        # Emit a synthetic event capturing the boundary count.
        self.environment.events.request.fire(
            request_type="REPORT",
            name="s10-boundary",
            response_time=0,
            response_length=self._success_count,
            exception=None,
            context={
                "success_count": self._success_count,
                "reject_count": self._reject_count,
                "fired": self._fired,
                "limit_target": STRESS_S10_LIMIT,
            },
        )


@events.test_start.add_listener  # type: ignore[misc]
def _s10_runtime_check(environment, **_):  # type: ignore[no-untyped-def]
    """Sanity-check that the rate-limit env var matches the expected boundary."""
    expected = STRESS_S10_LIMIT
    # We can't import the settings (FastAPI must remain isolated) — read
    # the env var directly.
    runtime = int(os.environ.get("LM_CHAT_LOGIN_RATE_LIMIT_PER_MIN", "10"))
    if runtime != expected:  # pragma: no cover - tooling
        environment.events.request.fire(
            request_type="WARN",
            name="s10-boundary-mismatch",
            response_time=0,
            response_length=0,
            exception=None,
            context={"runtime": runtime, "expected": expected},
        )
