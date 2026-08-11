# SPDX-License-Identifier: Apache-2.0
"""Integration tests for admin endpoints + invite consumption.

Covers
------
- ``GET /api/admin/users`` returns ``last_login`` derived from audit_log.
- ``DELETE /api/admin/users/{user_id}``
    - 200 + cascading delete + audit row written.
    - 400 when admin tries to delete themselves.
    - 404 for unknown user.
- ``POST /api/admin/invite``
    - issues a token + expires_at, persists to admin_invites, audit row.
    - 403 for non-admin.
- ``POST /api/auth/register`` with an invite token
    - registrant becomes admin even when other users exist.
    - token is consumed (used_at + used_by populated); second registration
      with the same token is rejected if a setup-token gate is configured
      (or registered as a non-admin if no gate is configured).
    - expired invite token does NOT grant admin.
"""
from __future__ import annotations

from collections.abc import Generator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import (
    admin_invites,
    audit_log,
    metadata,
    users,
)
from lmchat.middleware._bucket_store import InMemoryBucketStore
from lmchat.routes._dependencies import (
    admin_rate_limit,
    get_models_service_dep,
)
from lmchat.services.auth_service import _reset_dummy_hash_cache
from lmchat.utils.hashing import hash_password

_LOW_N: int = 2**10


# ---------------------------------------------------------------------------
# App factory (same pattern as test_admin.py)
# ---------------------------------------------------------------------------


def _make_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:  # noqa: ANN401
    from lmchat.app import create_app
    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod

    db_url = f"sqlite+aiosqlite:///{tmp_path}/admin_p13k_test.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    monkeypatch.setenv("LM_CHAT_ADMIN_RATE_LIMIT_PER_MIN", "60")
    # The invite-token tests rely on the bootstrap setup-token gate being
    # disabled so a non-token registration can still create a regular user.
    monkeypatch.delenv("LM_CHAT_SETUP_TOKEN", raising=False)

    get_settings.cache_clear()
    engine_mod.dispose_engine()
    _reset_dummy_hash_cache()

    app = create_app()

    stub_models = AsyncMock()
    stub_models.list_loaded = AsyncMock(return_value=[MagicMock(), MagicMock()])
    stub_models.refresh = AsyncMock(return_value=None)
    app.dependency_overrides[get_models_service_dep] = lambda: stub_models

    async def _noop_rl() -> None:
        return None

    app.dependency_overrides[admin_rate_limit] = _noop_rl
    return app


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod

    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    get_settings.cache_clear()
    _reset_dummy_hash_cache()
    yield
    get_settings.cache_clear()
    _reset_dummy_hash_cache()
    engine_mod.dispose_engine()


@pytest.fixture()
def test_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[TestClient]:
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        yield client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _engine_for(tmp_path: Path) -> AsyncEngine:
    db_url = f"sqlite+aiosqlite:///{tmp_path}/admin_p13k_test.db"
    eng = create_async_engine(db_url, pool_pre_ping=True)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return eng


async def _insert_user(
    tmp_path: Path,
    username: str,
    password: str = "test-pw",
    is_admin: bool = False,
) -> int:
    pw_hash = hash_password(password, n=_LOW_N, r=8, p=1)
    eng = await _engine_for(tmp_path)
    try:
        async with eng.begin() as conn:
            id_result = await conn.execute(
                select(func.coalesce(func.max(users.c.id), 0) + 1)
            )
            next_id = int(id_result.scalar() or 1)
            await conn.execute(
                text(
                    "INSERT INTO users (id, username, password_hash, is_admin)"
                    " VALUES (:id, :u, :ph, :ia)"
                ),
                {"id": next_id, "u": username, "ph": pw_hash, "ia": int(is_admin)},
            )
    finally:
        await eng.dispose()
    return next_id


async def _count_audit(tmp_path: Path, event: str) -> int:
    eng = await _engine_for(tmp_path)
    try:
        async with eng.connect() as conn:
            result = await conn.execute(
                select(func.count())
                .select_from(audit_log)
                .where(audit_log.c.event == event)
            )
            return int(result.scalar() or 0)
    finally:
        await eng.dispose()


async def _insert_audit_login(tmp_path: Path, user_id: int, when: datetime) -> None:
    """Force-insert an auth.login.success audit row for last_login testing."""
    eng = await _engine_for(tmp_path)
    try:
        async with eng.begin() as conn:
            await conn.execute(
                insert(audit_log).values(
                    user_id=user_id,
                    event="auth.login.success",
                    ip="127.0.0.1",
                    user_agent="pytest",
                    detail={},
                    created_at=when,
                )
            )
    finally:
        await eng.dispose()


def _login(client: TestClient, username: str, password: str = "test-pw") -> None:
    resp = client.post(
        "/api/auth/login", data={"username": username, "password": password}
    )
    assert resp.status_code == 200, f"Login failed: {resp.text}"


async def _setup_admin(tmp_path: Path, client: TestClient) -> tuple[int, int]:
    admin_id = await _insert_user(tmp_path, "p13k_admin", "admin_pw", is_admin=True)
    user_id = await _insert_user(tmp_path, "p13k_reg", "reg_pw", is_admin=False)
    _login(client, "p13k_admin", "admin_pw")
    return admin_id, user_id


# ---------------------------------------------------------------------------
# GET /api/admin/users — last_login derivation
# ---------------------------------------------------------------------------


async def test_list_users_includes_last_login_from_audit_log(
    tmp_path: Path, test_client: TestClient
) -> None:
    """``last_login`` is the max(created_at) of auth.login.success rows per user."""
    admin_id, user_id = await _setup_admin(tmp_path, test_client)
    when = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
    await _insert_audit_login(tmp_path, user_id, when)
    # Older row for the same user should be ignored by max().
    await _insert_audit_login(tmp_path, user_id, when - timedelta(days=5))

    resp = test_client.get("/api/admin/users")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    rows = {r["id"]: r for r in body}

    assert user_id in rows
    last_login_str = rows[user_id]["last_login"]
    assert last_login_str is not None
    last_login = datetime.fromisoformat(last_login_str.replace("Z", "+00:00"))
    # SQLite returns naive datetimes from TIMESTAMP columns; the wire-format
    # serialises with no tz suffix in that case.  Normalise both sides to
    # naive UTC + drop subsecond precision for stability.
    last_login_norm = last_login.astimezone(UTC).replace(
        tzinfo=None, microsecond=0
    ) if last_login.tzinfo is not None else last_login.replace(microsecond=0)
    when_norm = when.astimezone(UTC).replace(tzinfo=None, microsecond=0)
    assert last_login_norm == when_norm


async def test_list_users_last_login_none_when_no_login_audit(
    tmp_path: Path, test_client: TestClient
) -> None:
    """``last_login`` is null for a user with no auth.login.success rows."""
    _, user_id = await _setup_admin(tmp_path, test_client)
    resp = test_client.get("/api/admin/users")
    assert resp.status_code == 200
    rows = {r["id"]: r for r in resp.json()}
    assert rows[user_id]["last_login"] is None


# ---------------------------------------------------------------------------
# DELETE /api/admin/users/{user_id}
# ---------------------------------------------------------------------------


async def test_delete_user_succeeds_and_audits(
    tmp_path: Path, test_client: TestClient
) -> None:
    _admin_id, user_id = await _setup_admin(tmp_path, test_client)
    before = await _count_audit(tmp_path, "admin.users.deleted")

    resp = test_client.delete(f"/api/admin/users/{user_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"

    eng = await _engine_for(tmp_path)
    try:
        async with eng.connect() as conn:
            remaining = int(
                (
                    await conn.execute(
                        select(func.count())
                        .select_from(users)
                        .where(users.c.id == user_id)
                    )
                ).scalar()
                or 0
            )
    finally:
        await eng.dispose()
    assert remaining == 0

    after = await _count_audit(tmp_path, "admin.users.deleted")
    assert after == before + 1


async def test_delete_user_survives_audit_write_failure_and_logs_error(
    tmp_path: Path, test_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELETE still succeeds (200) even when the audit write fails.

    A failed ``admin.users.deleted`` audit write is escalated to an ERROR
    log + metric increment instead of the old best-effort WARNING — but
    the deletion itself (already committed) is never blocked or rolled
    back (proceed-but-loud).
    """
    from lmchat.services import audit_service

    _admin_id, user_id = await _setup_admin(tmp_path, test_client)

    async def _boom(**_kwargs: object) -> None:
        raise RuntimeError("simulated DB write failure")

    monkeypatch.setattr(audit_service, "write_audit_event", _boom)

    error_events: list[str] = []
    original_error = audit_service.log.error

    def _spy_error(event: str, **kwargs: object) -> None:
        error_events.append(event)
        original_error(event, **kwargs)

    monkeypatch.setattr(audit_service.log, "error", _spy_error)

    before = audit_service.AUDIT_WRITE_FAILURES_CRITICAL.labels(
        event="admin.users.deleted"
    )._value.get()

    resp = test_client.delete(f"/api/admin/users/{user_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"

    eng = await _engine_for(tmp_path)
    try:
        async with eng.connect() as conn:
            remaining = int(
                (
                    await conn.execute(
                        select(func.count())
                        .select_from(users)
                        .where(users.c.id == user_id)
                    )
                ).scalar()
                or 0
            )
    finally:
        await eng.dispose()
    assert remaining == 0

    after = audit_service.AUDIT_WRITE_FAILURES_CRITICAL.labels(
        event="admin.users.deleted"
    )._value.get()
    assert after == before + 1
    assert error_events == [
        "critical audit write failed — compliance record incomplete"
    ]


async def test_delete_user_refuses_self_deletion(
    tmp_path: Path, test_client: TestClient
) -> None:
    admin_id, _ = await _setup_admin(tmp_path, test_client)
    resp = test_client.delete(f"/api/admin/users/{admin_id}")
    assert resp.status_code == 400, resp.text


async def test_delete_user_404_unknown(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _setup_admin(tmp_path, test_client)
    resp = test_client.delete("/api/admin/users/9999")
    assert resp.status_code == 404, resp.text


async def test_delete_user_requires_admin(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "nonadmin", is_admin=False)
    target_id = await _insert_user(tmp_path, "target", is_admin=False)
    _login(test_client, "nonadmin")
    resp = test_client.delete(f"/api/admin/users/{target_id}")
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# POST /api/admin/invite
# ---------------------------------------------------------------------------


async def test_issue_invite_returns_token_and_persists(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _setup_admin(tmp_path, test_client)
    resp = test_client.post("/api/admin/invite")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "token" in body
    assert "expires_at" in body
    assert isinstance(body["token"], str)
    assert len(body["token"]) >= 20  # secrets.token_urlsafe(24) ≈ 32 chars

    eng = await _engine_for(tmp_path)
    try:
        async with eng.connect() as conn:
            row = (
                await conn.execute(
                    select(admin_invites).where(
                        admin_invites.c.token == body["token"]
                    )
                )
            ).fetchone()
    finally:
        await eng.dispose()
    assert row is not None
    assert row._mapping["used_at"] is None


async def test_issue_invite_requires_admin(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _insert_user(tmp_path, "nonadmin", is_admin=False)
    _login(test_client, "nonadmin")
    resp = test_client.post("/api/admin/invite")
    assert resp.status_code == 403, resp.text


async def test_issue_invite_audited(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _setup_admin(tmp_path, test_client)
    before = await _count_audit(tmp_path, "admin.invite.issued")
    resp = test_client.post("/api/admin/invite")
    assert resp.status_code == 200
    after = await _count_audit(tmp_path, "admin.invite.issued")
    assert after == before + 1


# ---------------------------------------------------------------------------
# /api/auth/register — invite-token consumption
# ---------------------------------------------------------------------------


async def test_register_with_invite_token_promotes_to_admin(
    tmp_path: Path, test_client: TestClient
) -> None:
    await _setup_admin(tmp_path, test_client)
    invite_resp = test_client.post("/api/admin/invite")
    assert invite_resp.status_code == 200
    token = invite_resp.json()["token"]

    # Sign out so the new registration doesn't reuse the admin's cookies.
    test_client.cookies.clear()

    reg_resp = test_client.post(
        f"/api/auth/register?token={token}",
        data={"username": "invited_admin", "password": "correct-horse-battery"},
    )
    assert reg_resp.status_code == 201, reg_resp.text
    new_id = reg_resp.json()["id"]

    eng = await _engine_for(tmp_path)
    try:
        async with eng.connect() as conn:
            row = (
                await conn.execute(
                    select(users.c.is_admin).where(users.c.id == new_id)
                )
            ).fetchone()
            invite_row = (
                await conn.execute(
                    select(admin_invites).where(admin_invites.c.token == token)
                )
            ).fetchone()
    finally:
        await eng.dispose()
    assert row is not None and bool(row._mapping["is_admin"]) is True
    assert invite_row is not None
    assert invite_row._mapping["used_at"] is not None
    assert int(invite_row._mapping["used_by"]) == new_id


async def test_register_with_invite_token_via_header(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Same flow as the query-param test but using the X-Setup-Token header."""
    await _setup_admin(tmp_path, test_client)
    invite_resp = test_client.post("/api/admin/invite")
    token = invite_resp.json()["token"]
    test_client.cookies.clear()

    reg_resp = test_client.post(
        "/api/auth/register",
        data={"username": "header_admin", "password": "correct-horse-battery"},
        headers={"X-Setup-Token": token},
    )
    assert reg_resp.status_code == 201, reg_resp.text
    new_id = reg_resp.json()["id"]

    eng = await _engine_for(tmp_path)
    try:
        async with eng.connect() as conn:
            row = (
                await conn.execute(
                    select(users.c.is_admin).where(users.c.id == new_id)
                )
            ).fetchone()
    finally:
        await eng.dispose()
    assert row is not None and bool(row._mapping["is_admin"]) is True


async def test_register_with_used_invite_token_is_rejected(
    tmp_path: Path, test_client: TestClient
) -> None:
    """A used invite token is not a valid invite: the single-admin gate
    closes registration to anyone without a valid (unused, unexpired) token
    once users already exist.

    The first consumption succeeds and promotes. The second attempt with the
    already-used token is rejected with 403 — the token is not valid so
    grant_admin_via_invite is False, and the gate fires.
    """
    await _setup_admin(tmp_path, test_client)
    invite_resp = test_client.post("/api/admin/invite")
    token = invite_resp.json()["token"]
    test_client.cookies.clear()

    # First consumption — succeeds and promotes.
    resp1 = test_client.post(
        f"/api/auth/register?token={token}",
        data={"username": "first_invitee", "password": "correct-horse-battery"},
    )
    assert resp1.status_code == 201, resp1.text

    test_client.cookies.clear()

    # Second attempt with the already-used token — rejected by the single-admin gate.
    resp2 = test_client.post(
        f"/api/auth/register?token={token}",
        data={"username": "second_invitee", "password": "correct-horse-battery"},
    )
    assert resp2.status_code == 403, resp2.text
    assert "Registration is closed" in resp2.json()["detail"]


async def test_register_with_expired_invite_token_is_rejected(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Expired invites are not valid invites — the single-admin gate blocks
    registration when users already exist and the token does not grant admin.
    """
    await _setup_admin(tmp_path, test_client)
    # Directly insert an expired invite (past expires_at).
    expired_token = "expired-token-fixed-value-for-test"
    past = datetime.now(UTC) - timedelta(hours=2)
    eng = await _engine_for(tmp_path)
    try:
        async with eng.begin() as conn:
            await conn.execute(
                insert(admin_invites).values(
                    token=expired_token,
                    created_by=None,
                    expires_at=past,
                )
            )
    finally:
        await eng.dispose()

    test_client.cookies.clear()
    reg_resp = test_client.post(
        f"/api/auth/register?token={expired_token}",
        data={"username": "late_invitee", "password": "correct-horse-battery"},
    )
    assert reg_resp.status_code == 403, reg_resp.text
    assert "Registration is closed" in reg_resp.json()["detail"]
