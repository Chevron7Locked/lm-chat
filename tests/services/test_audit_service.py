# SPDX-License-Identifier: Apache-2.0
"""Contract tests for audit_service.write_audit_event.

Tests for the audit service.

Uses a per-test tmp_path SQLite engine with the schema bootstrapped via
``metadata.create_all`` so tests are fully isolated from the application DB.
"""
from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import audit_log, metadata
from lmchat.services.audit_service import write_audit_event

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    """Yield a fresh per-test SQLite engine with the schema created."""
    db_path = tmp_path / "test_audit.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}", pool_pre_ping=True)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


async def _insert_user(engine, user_id: int = 1, username: str = "alice") -> None:  # type: ignore[no-untyped-def]
    """Insert a minimal user row for FK constraint satisfaction."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO users (id, username, password_hash)"
                " VALUES (:id, :u, :ph)"
            ),
            {"id": user_id, "u": username, "ph": "scrypt$dummy"},
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_write_audit_event_full_args(engine) -> None:  # type: ignore[no-untyped-def]
    """write_audit_event writes a row with every column populated."""
    await _insert_user(engine)

    detail = {"session_id_prefix": "abc12345", "reason": "ok"}
    await write_audit_event(
        user_id=1,
        event="auth.login.success",
        ip="127.0.0.1",
        user_agent="TestAgent/1.0",
        detail=detail,
        engine=engine,
    )

    async with engine.connect() as conn:
        result = await conn.execute(select(audit_log))
        row = result.fetchone()

    assert row is not None
    assert row.user_id == 1
    assert row.event == "auth.login.success"
    assert row.ip == "127.0.0.1"
    assert row.user_agent == "TestAgent/1.0"
    # detail round-trips as dict (or JSON string in aiosqlite — normalise)
    stored_detail = row.detail
    if isinstance(stored_detail, str):
        stored_detail = json.loads(stored_detail)
    assert stored_detail == detail
    assert row.created_at is not None


async def test_write_audit_event_null_user_id(engine) -> None:  # type: ignore[no-untyped-def]
    """write_audit_event accepts user_id=None for anonymous events."""
    await write_audit_event(
        user_id=None,
        event="auth.login.failure",
        ip="192.168.1.1",
        user_agent=None,
        detail={"reason": "unknown_user"},
        engine=engine,
    )

    async with engine.connect() as conn:
        result = await conn.execute(select(audit_log))
        row = result.fetchone()

    assert row is not None
    assert row.user_id is None
    assert row.event == "auth.login.failure"


async def test_write_audit_event_null_detail(engine) -> None:  # type: ignore[no-untyped-def]
    """write_audit_event accepts detail=None (no extra context)."""
    await _insert_user(engine)
    await write_audit_event(
        user_id=1,
        event="auth.logout",
        ip=None,
        user_agent=None,
        detail=None,
        engine=engine,
    )

    async with engine.connect() as conn:
        result = await conn.execute(select(audit_log))
        row = result.fetchone()

    assert row is not None
    assert row.detail is None


async def test_detail_json_round_trips(engine) -> None:  # type: ignore[no-untyped-def]
    """The detail column stores and retrieves complex nested dicts faithfully."""
    await _insert_user(engine)
    detail: dict = {"nested": {"key": "value", "count": 42}, "list": [1, 2, 3]}
    await write_audit_event(
        user_id=1,
        event="auth.totp.verified",
        ip=None,
        user_agent=None,
        detail=detail,
        engine=engine,
    )

    async with engine.connect() as conn:
        result = await conn.execute(select(audit_log))
        row = result.fetchone()

    assert row is not None
    stored = row.detail
    if isinstance(stored, str):
        stored = json.loads(stored)
    assert stored == detail


async def test_multiple_events_written_in_order(engine) -> None:  # type: ignore[no-untyped-def]
    """Multiple audit events appear as separate rows in insertion order."""
    await _insert_user(engine)

    events = [
        "auth.register",
        "auth.login.success",
        "auth.logout",
    ]
    for event in events:
        await write_audit_event(
            user_id=1,
            event=event,  # type: ignore[arg-type]
            ip=None,
            user_agent=None,
            detail=None,
            engine=engine,
        )

    async with engine.connect() as conn:
        result = await conn.execute(select(audit_log).order_by(audit_log.c.id))
        rows = result.fetchall()

    assert len(rows) == 3
    assert [r.event for r in rows] == events


# ---------------------------------------------------------------------------
# write_audit_event_or_alert — loud-on-failure escalation.
# Security-critical events (login success/failure, logout,
# role change, user deletion) must escalate a failed audit write to an
# ERROR log + metric increment instead of the WARNING used elsewhere —
# proceed-but-loud, never blocking the caller's action.
# ---------------------------------------------------------------------------


def test_critical_audit_events_matches_operator_locked_subset() -> None:
    """CRITICAL_AUDIT_EVENTS is exactly the operator-locked security-critical subset."""
    from lmchat.services.audit_service import CRITICAL_AUDIT_EVENTS

    assert CRITICAL_AUDIT_EVENTS == frozenset(
        {
            "auth.login.success",
            "auth.login.failure",
            "auth.logout",
            "admin.users.role_changed",
            "admin.users.deleted",
        }
    )


async def test_write_audit_event_or_alert_success_is_transparent(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    """On success, write_audit_event_or_alert writes the row and stays silent.

    No ERROR is logged and the critical-failure metric is untouched — the
    happy path must be indistinguishable from a plain ``write_audit_event``
    call.
    """
    from lmchat.services import audit_service

    await _insert_user(engine)

    before = audit_service.AUDIT_WRITE_FAILURES_CRITICAL.labels(
        event="auth.login.success"
    )._value.get()

    error_events: list[str] = []
    original_error = audit_service.log.error

    def _spy_error(event: str, **kwargs: object) -> None:
        error_events.append(event)
        original_error(event, **kwargs)

    monkeypatch.setattr(audit_service.log, "error", _spy_error)

    await audit_service.write_audit_event_or_alert(
        user_id=1,
        event="auth.login.success",
        ip="127.0.0.1",
        user_agent="TestAgent/1.0",
        detail={"session_id_prefix": "abc12345"},
        engine=engine,
    )

    after = audit_service.AUDIT_WRITE_FAILURES_CRITICAL.labels(
        event="auth.login.success"
    )._value.get()

    assert error_events == []
    assert after == before

    async with engine.connect() as conn:
        result = await conn.execute(select(audit_log))
        rows = result.fetchall()
    assert len(rows) == 1
    assert rows[0].event == "auth.login.success"


async def test_write_audit_event_or_alert_logs_error_and_increments_metric_on_failure(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    """A failed critical-event audit write is escalated: ERROR + metric, never raised.

    Patches ``audit_service.write_audit_event`` itself to raise, simulating a
    DB write failure. The helper must swallow the exception (proceed-but-loud
    — it must never propagate and block/500 the caller) while logging at
    ERROR (not WARNING) and bumping :data:`AUDIT_WRITE_FAILURES_CRITICAL`.
    """
    from lmchat.services import audit_service

    async def _boom(**_kwargs: object) -> None:
        raise RuntimeError("simulated DB write failure")

    monkeypatch.setattr(audit_service, "write_audit_event", _boom)

    error_events: list[str] = []
    warning_events: list[str] = []
    original_error = audit_service.log.error
    original_warning = audit_service.log.warning

    def _spy_error(event: str, **kwargs: object) -> None:
        error_events.append(event)
        original_error(event, **kwargs)

    def _spy_warning(event: str, **kwargs: object) -> None:
        warning_events.append(event)
        original_warning(event, **kwargs)

    monkeypatch.setattr(audit_service.log, "error", _spy_error)
    monkeypatch.setattr(audit_service.log, "warning", _spy_warning)

    before = audit_service.AUDIT_WRITE_FAILURES_CRITICAL.labels(
        event="auth.login.success"
    )._value.get()

    # Must complete without raising — proceed-but-loud, not blocking.
    await audit_service.write_audit_event_or_alert(
        user_id=1,
        event="auth.login.success",
        ip="127.0.0.1",
        user_agent="TestAgent/1.0",
        detail={"session_id_prefix": "abc12345"},
        engine=engine,
    )

    after = audit_service.AUDIT_WRITE_FAILURES_CRITICAL.labels(
        event="auth.login.success"
    )._value.get()

    assert after == before + 1
    assert error_events == [
        "critical audit write failed — compliance record incomplete"
    ]
    assert warning_events == []


async def test_write_audit_event_or_alert_rejects_non_critical_event(engine) -> None:  # type: ignore[no-untyped-def]
    """Calling the helper with an event outside CRITICAL_AUDIT_EVENTS fails fast.

    This is a call-site programming error, not an audit-write failure, so it
    must raise immediately rather than silently alerting on the wrong thing.
    """
    from lmchat.services import audit_service

    with pytest.raises(ValueError, match="non-critical event"):
        await audit_service.write_audit_event_or_alert(
            user_id=1,
            event="auth.register",
            ip=None,
            user_agent=None,
            detail=None,
            engine=engine,
        )
