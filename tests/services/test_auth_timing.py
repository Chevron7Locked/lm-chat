# SPDX-License-Identifier: Apache-2.0
"""Timing test: unknown-username path vs wrong-password path.

Timing-safe auth tests (constant-time comparison under load).

Purpose
--------
Demonstrate that an attacker cannot enumerate valid usernames by measuring
login response latency.  Both failure paths must take approximately the same
time because both run a full scrypt verification.

Methodology
-----------
We use a LOWER scrypt cost (n=2^14) for this test so it completes in a
reasonable time in CI (< 30 s total).  The dummy hash in auth_service uses
DEFAULT_N (2^17) in production, but n=2^14 still dominates the timing budget
for both paths and is sufficient to demonstrate the property.

Assertion bound
----------------
We assert:
    |mean_unknown - mean_known_wrong| < max(mean_unknown, mean_known_wrong) * 0.5

This is a generous 50% relative tolerance.  The goal is to confirm that the
unknown-username path is NOT fast (which would be the case if it skipped
scrypt entirely).  Tight millisecond-level bounds would flake in noisy CI
environments and are not the goal of this test.

The test does NOT assert absolute latency values because those are hardware-
dependent.  It only asserts relative order-of-magnitude equivalence.

Note: this test is slow by design (~several seconds per trial at n=2^14).
It is kept in the test suite as a regression guard against accidental removal
of the _dummy_verify() call in the unknown-username path.
"""
from __future__ import annotations

import time
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import metadata
from lmchat.services.auth_errors import BadCredentialsError
from lmchat.services.auth_service import login
from lmchat.session.sqlite_store import SQLiteSessionStore
from lmchat.utils.hashing import hash_password

# Use a lower cost for test speed while still exercising the real scrypt path.
_TEST_N: int = 2**14


@pytest.fixture(autouse=True)
def _set_secret(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Set LM_CHAT_SECRET for the duration of these tests."""
    monkeypatch.setenv("LM_CHAT_SECRET", "timing-test-secret-key")
    from lmchat.config import get_settings
    from lmchat.services.auth_service import _reset_dummy_hash_cache

    get_settings.cache_clear()
    _reset_dummy_hash_cache()
    yield
    get_settings.cache_clear()
    _reset_dummy_hash_cache()


@pytest.fixture()
async def timing_engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    """Yield a test engine with a known user inserted at low scrypt cost."""
    db_path = tmp_path / "timing.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}", pool_pre_ping=True)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)

    # Insert a real user with a lower-cost hash so test runs faster.
    from sqlalchemy import func, select, text

    from lmchat.db.schema import users as users_tbl

    pw_hash = hash_password("correct-password", n=_TEST_N, r=8, p=1)
    async with eng.begin() as conn:
        id_result = await conn.execute(
            select(func.coalesce(func.max(users_tbl.c.id), 0) + 1)
        )
        next_id = id_result.scalar()
        await conn.execute(
            text(
                "INSERT INTO users (id, username, password_hash)"
                " VALUES (:id, :u, :ph)"
            ),
            {"id": next_id, "u": "timing_user", "ph": pw_hash},
        )

    yield eng
    await eng.dispose()


async def test_unknown_username_takes_comparable_time_to_wrong_password(
    timing_engine: AsyncEngine,
) -> None:
    """The unknown-username path takes approximately the same time as wrong-password.

    Bound: |mean_unknown - mean_wrong| < max(mean_unknown, mean_wrong) * 0.5

    The 50% tolerance is intentionally loose — CI environments are noisy and
    tight bounds would produce flaky results.  The property we are testing is
    that the unknown-username path does NOT bypass scrypt (which would make it
    10-100x faster), not that it is exactly equal in milliseconds.

    Both paths use the same scrypt cost (_TEST_N = 2^14) via the dummy hash
    cache and the real password hash respectively.
    """
    store = SQLiteSessionStore(engine=timing_engine)
    trials: int = 5  # Enough to compute a stable mean without taking too long.

    # The _hash_n matches the hash stored for timing_user (_TEST_N = 2^14)
    # so the dummy verify uses the same cost as the real verify path.
    # Note: LOW_COST (n=2^10) is too fast; _TEST_N (n=2^14) is slow enough
    # to demonstrate timing equivalence while staying within OpenSSL limits.
    timing_cost = {"_hash_n": _TEST_N, "_hash_r": 8, "_hash_p": 1}

    # We need to prime the dummy hash cache first — the first call computes
    # the dummy hash and takes extra time.  Run one warmup trial outside the
    # measurement window.
    try:
        await login(
            username="unknown_warmup",
            password="warmup",
            totp_code=None,
            ip=None,
            user_agent=None,
            session_store=store,
            engine=timing_engine,
            **timing_cost,
        )
    except BadCredentialsError:
        pass

    # --- unknown username trials ---
    unknown_times: list[float] = []
    for _ in range(trials):
        start = time.perf_counter()
        try:
            await login(
                username="nobody_known",
                password="any-password",
                totp_code=None,
                ip=None,
                user_agent=None,
                session_store=store,
                engine=timing_engine,
                **timing_cost,
            )
        except BadCredentialsError:
            pass
        unknown_times.append(time.perf_counter() - start)

    # --- wrong password trials ---
    wrong_times: list[float] = []
    for _ in range(trials):
        start = time.perf_counter()
        try:
            await login(
                username="timing_user",
                password="wrong-password",
                totp_code=None,
                ip=None,
                user_agent=None,
                session_store=store,
                engine=timing_engine,
                **timing_cost,
            )
        except BadCredentialsError:
            pass
        wrong_times.append(time.perf_counter() - start)

    mean_unknown = sum(unknown_times) / len(unknown_times)
    mean_wrong = sum(wrong_times) / len(wrong_times)
    tolerance = max(mean_unknown, mean_wrong) * 0.5

    delta = abs(mean_unknown - mean_wrong)
    assert delta < tolerance, (
        f"Timing difference too large: |{mean_unknown:.3f}s - {mean_wrong:.3f}s|"
        f" = {delta:.3f}s exceeds tolerance {tolerance:.3f}s.\n"
        f"Unknown-username path appears to be significantly faster, suggesting "
        f"the dummy scrypt verify is not running."
    )
