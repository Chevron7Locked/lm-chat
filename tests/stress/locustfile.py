# SPDX-License-Identifier: Apache-2.0
"""Locust entrypoint for the P14 stress harness.

Imports all 10 user classes from ``tests.stress.users.*`` and exposes
them so they can be selected via ``--tags`` per scenario.

Usage (from the orchestrator):
    uv run locust -f tests/stress/locustfile.py \\
        --host=$STRESS_HOST \\
        --tags s1 streaming \\
        --headless -u 200 -r 20 -t 90s \\
        --csv target/stress/run

Available tags (one per scenario):

    s1 streaming           — 200 concurrent SSE streams
    s2 incognito r15       — R-15 incognito x share x memory
    s3 crud-race           — concurrent CRUD on shared chat
    s4 session-revoke      — session revocation mid-stream
    s5 lm-studio-failure   — toxiproxy chaos
    s6 adversarial         — Hypothesis adversarial inputs
    s7 quota-rollover      — quota window rollover
    s8 isolation           — 50 isolated users
    s9 db-contention       — DB integrity under contention
    s10 rate-limit-boundary — rate-limit boundary detection
"""
from __future__ import annotations

# Re-exports for Locust's @tag-based selection.  Each import registers
# the user class with Locust's runtime registry.
from tests.stress.users.adversarial_user import AdversarialUser
from tests.stress.users.crud_race_user import CrudRaceUser
from tests.stress.users.db_contention_user import DbContentionUser
from tests.stress.users.incognito_user import IncognitoUser
from tests.stress.users.isolated_users import IsolatedUser
from tests.stress.users.lm_studio_failure_user import LmStudioFailureUser
from tests.stress.users.quota_rollover_user import QuotaRolloverUser
from tests.stress.users.rate_limit_boundary_user import RateLimitBoundaryUser
from tests.stress.users.session_revoke_user import SessionRevokeUser
from tests.stress.users.streaming_user import StreamingUser

__all__ = [
    "AdversarialUser",
    "CrudRaceUser",
    "DbContentionUser",
    "IncognitoUser",
    "IsolatedUser",
    "LmStudioFailureUser",
    "QuotaRolloverUser",
    "RateLimitBoundaryUser",
    "SessionRevokeUser",
    "StreamingUser",
]
