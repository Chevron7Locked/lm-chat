# SPDX-License-Identifier: Apache-2.0
"""Unit tests for lmchat.utils.clock.

The bug these helpers fix only manifests when the host's local
timezone differs from UTC. To make that deterministic (independent of
what time-of-day CI happens to run) without a freeze-time dependency,
these tests flip the host TZ to two opposite fixed offsets
(``Etc/GMT-12`` / ``Etc/GMT+12``) via ``time.tzset()``. Those two
offsets' "local calendar date == UTC calendar date" windows are
complementary — together they cover every hour of the day, so at
least one of each pair of parametrized runs always exercises the
divergence, regardless of when the suite executes.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest

from lmchat.utils.clock import ensure_utc, utc_now, utc_today

pytestmark = pytest.mark.skipif(
    not hasattr(time, "tzset"), reason="time.tzset() is POSIX-only"
)


@pytest.fixture()
def _tz(monkeypatch: pytest.MonkeyPatch):
    """Temporarily set the process TZ, restoring it on teardown."""

    def _set(tz_name: str) -> None:
        monkeypatch.setenv("TZ", tz_name)
        time.tzset()

    yield _set
    monkeypatch.delenv("TZ", raising=False)
    time.tzset()


@pytest.mark.parametrize("tz_name", ["Etc/GMT-12", "Etc/GMT+12"])
def test_utc_now_is_tz_independent(_tz, tz_name: str) -> None:
    _tz(tz_name)
    now = utc_now()
    assert now.tzinfo is not None
    offset = now.utcoffset()
    assert offset is not None
    assert offset.total_seconds() == 0
    # Must track the real UTC instant, not a host-local reading of it.
    assert abs((now - datetime.now(UTC)).total_seconds()) < 5


@pytest.mark.parametrize("tz_name", ["Etc/GMT-12", "Etc/GMT+12"])
def test_utc_today_matches_real_utc_date_under_shifted_tz(_tz, tz_name: str) -> None:
    """utc_today() must equal datetime.now(UTC).date() under ANY host TZ.

    A naive re-implementation using date.today() (host-local) would
    diverge from this for at least one of the two parametrized offsets
    on any given real clock time — that is exactly the bug fixed in
    routes/quotas.py and services/analytics_service.py.
    """
    _tz(tz_name)
    assert utc_today() == datetime.now(UTC).date()


def test_ensure_utc_none_passthrough() -> None:
    assert ensure_utc(None) is None


def test_ensure_utc_attaches_utc_to_naive_datetime() -> None:
    naive = datetime(2026, 1, 1, 12, 30, 0)
    result = ensure_utc(naive)
    assert result is not None
    assert result.tzinfo is UTC
    assert result.replace(tzinfo=None) == naive


def test_ensure_utc_leaves_aware_datetime_unchanged() -> None:
    aware = datetime(2026, 1, 1, 12, 30, 0, tzinfo=UTC)
    assert ensure_utc(aware) == aware


@pytest.mark.parametrize("tz_name", ["Etc/GMT-12", "Etc/GMT+12"])
def test_ensure_utc_naive_timestamp_is_tz_independent(_tz, tz_name: str) -> None:
    """A naive value must convert to the SAME epoch regardless of host TZ.

    This is the exact bug in projects_service._row_to_project: calling
    ``.timestamp()`` directly on a naive datetime interprets it as
    host-local, skewing the epoch by the host's UTC offset. ensure_utc()
    must make the conversion TZ-independent.
    """
    _tz(tz_name)
    naive_utc_wallclock = datetime(2026, 1, 1, 12, 0, 0)
    expected_epoch = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC).timestamp()
    result = ensure_utc(naive_utc_wallclock)
    assert result is not None
    assert result.timestamp() == pytest.approx(expected_epoch)
