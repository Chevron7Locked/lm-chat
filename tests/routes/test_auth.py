# SPDX-License-Identifier: Apache-2.0
"""Integration tests for auth routes.

Tests for authentication routes.

Uses FastAPI TestClient against a real app instance backed by a per-test
tmp_path SQLite DB.  No mocks — real scrypt, real TOTP, real DB writes,
real middleware stack.

scrypt cost: all helper calls pass ``_hash_n=2**10`` (LOW_COST) so tests
run within the OpenSSL maxmem limit.  The app-level dependency overrides
inject a per-test engine so routes use the test DB, not the production DB.

Dependency override strategy:
- ``get_engine_dep`` → returns the per-test engine.
- ``get_default_session_store_dep`` → returns a SQLiteSessionStore backed
  by the per-test engine.
- ``_reset_dummy_hash_cache()`` is called before tests that exercise the
  low-cost scrypt path so the dummy hash is computed at n=2^10.

The LoginRateLimitMiddleware is part of the full app stack and IS exercised
for the rate-limit test; the 10-attempt cap is configured at the default
(10/min), so 11 rapid attempts trigger a 429.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator, Generator, Iterator
from pathlib import Path
from typing import Any

import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.app import create_app
from lmchat.db.schema import audit_log, metadata, sessions, users
from lmchat.middleware._bucket_store import InMemoryBucketStore
from lmchat.routes._dependencies import get_default_session_store_dep, get_engine_dep
from lmchat.services.auth_service import _reset_dummy_hash_cache
from lmchat.session.sqlite_store import SQLiteSessionStore
from lmchat.utils.hashing import hash_password

# ---------------------------------------------------------------------------
# Low-cost scrypt (keeps tests under OpenSSL maxmem)
# ---------------------------------------------------------------------------

_LOW_N: int = 2**10
_LOW_COST: dict[str, int] = {"_hash_n": _LOW_N, "_hash_r": 8, "_hash_p": 1}

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Set required env vars and clear caches before each test."""
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.config import get_settings

    get_settings.cache_clear()
    _reset_dummy_hash_cache()
    yield
    get_settings.cache_clear()
    _reset_dummy_hash_cache()


@pytest.fixture()
async def db_engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    """Yield a fresh per-test SQLite engine with the full schema."""
    db_path = tmp_path / "test_routes_auth.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}", pool_pre_ping=True)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture()
def test_client(
    db_engine: AsyncEngine,
) -> Generator[TestClient]:
    """Yield a TestClient wired to the per-test engine.

    Overrides ``get_engine_dep`` and ``get_default_session_store_dep`` so
    every route uses the tmp_path DB, not the production DB.

    The TestClient is synchronous (uses the sync test transport) which is
    correct for routes that use AsyncEngine internally — FastAPI's async
    handling is transparent to the test transport.
    """
    app = create_app()
    store = SQLiteSessionStore(engine=db_engine)

    app.dependency_overrides[get_engine_dep] = lambda: db_engine
    app.dependency_overrides[get_default_session_store_dep] = lambda: store

    # TestClient is used as a context manager so lifespan runs.
    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        yield client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _register(
    client: TestClient,
    username: str = "alice",
    password: str = "correct-horse-battery",
) -> Any:
    """POST /api/auth/register and return the response."""
    return client.post(
        "/api/auth/register",
        data={"username": username, "password": password},
    )


def _login(
    client: TestClient,
    username: str = "alice",
    password: str = "correct-horse-battery",
    totp_code: str | None = None,
) -> Any:
    """POST /api/auth/login and return the response."""
    data: dict[str, str] = {"username": username, "password": password}
    if totp_code is not None:
        data["totp_code"] = totp_code
    return client.post("/api/auth/login", data=data)


async def _insert_user_low_cost(
    engine: AsyncEngine,
    username: str = "alice",
    password: str = "correct-horse-battery",
) -> int:
    """Insert a user at low-cost scrypt and return the user_id."""
    pw_hash = hash_password(password, n=_LOW_N, r=8, p=1)
    async with engine.begin() as conn:
        id_result = await conn.execute(
            select(func.coalesce(func.max(users.c.id), 0) + 1)
        )
        next_id = id_result.scalar()
        if next_id is None:
            raise RuntimeError("coalesce returned None — unreachable")
        await conn.execute(
            text(
                "INSERT INTO users (id, username, password_hash)"
                " VALUES (:id, :u, :ph)"
            ),
            {"id": next_id, "u": username, "ph": pw_hash},
        )
    return int(next_id)


async def _count_audit_rows(engine: AsyncEngine, event: str | None = None) -> int:
    """Count audit_log rows, optionally filtering by event string."""
    async with engine.connect() as conn:
        q = select(audit_log)
        if event is not None:
            q = q.where(audit_log.c.event == event)
        result = await conn.execute(q)
        return len(result.fetchall())


# ---------------------------------------------------------------------------
# Register tests
# ---------------------------------------------------------------------------


def test_register_creates_user_returns_201(test_client: TestClient) -> None:
    """POST /register with valid creds → 201 + {id, username}."""
    resp = _register(test_client)
    assert resp.status_code == 201
    body = resp.json()
    assert body["username"] == "alice"
    assert isinstance(body["id"], int)


async def test_register_duplicate_returns_400(
    test_client: TestClient, db_engine: AsyncEngine
) -> None:
    """Registering the same username twice → 400 username already taken.

    The single-admin gate blocks plain registration when users exist.
    Bypass it by pre-inserting alice directly in the DB, then attempt to
    register alice again via an invite token (which bypasses the gate but
    still hits the duplicate-username check).
    """
    # Pre-insert alice directly to avoid going through the register endpoint.
    await _insert_user_low_cost(db_engine, "alice")

    # Create an admin+invite in DB so we can redeem a token.
    await _insert_user_low_cost(db_engine, "admin_dup", "admin-pw-123")
    # Make admin_dup an admin directly.
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET is_admin = 1 WHERE username = 'admin_dup'")
        )

    # Log in as admin and issue an invite.
    login_resp = test_client.post(
        "/api/auth/login",
        data={"username": "admin_dup", "password": "admin-pw-123"},
    )
    assert login_resp.status_code == 200
    invite_resp = test_client.post("/api/admin/invite")
    assert invite_resp.status_code == 200, invite_resp.text
    token = invite_resp.json()["token"]
    test_client.cookies.clear()

    # Try to register alice (duplicate) with the invite token → 400.
    resp = test_client.post(
        f"/api/auth/register?token={token}",
        data={"username": "alice", "password": "correct-horse-battery"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "username already taken"


def test_register_invalid_username_returns_422(test_client: TestClient) -> None:
    """Username with invalid characters → 422."""
    resp = test_client.post(
        "/api/auth/register",
        data={"username": "bad name!", "password": "correct-horse-battery"},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Single-admin registration gate tests
# ---------------------------------------------------------------------------


def test_register_first_user_succeeds(test_client: TestClient) -> None:
    """First registration (count==0) → 201 + granted admin automatically."""
    resp = _register(test_client)
    assert resp.status_code == 201
    body = resp.json()
    assert body["username"] == "alice"
    assert isinstance(body["id"], int)


async def test_register_second_user_plain_returns_403(
    test_client: TestClient, db_engine: AsyncEngine
) -> None:
    """Plain registration when a user already exists → 403 (gate closed)."""
    # Seed the DB with an existing user to trigger the gate.
    await _insert_user_low_cost(db_engine, "existing_user")
    resp = test_client.post(
        "/api/auth/register",
        data={"username": "newcomer", "password": "correct-horse-battery"},
    )
    assert resp.status_code == 403
    assert "Registration is closed" in resp.json()["detail"]


async def test_register_closed_does_not_reveal_username_existence(
    test_client: TestClient, db_engine: AsyncEngine
) -> None:
    """Anti-enumeration: a CLOSED registration must return
    403 identically whether or not the username already exists.

    Before the fix the duplicate-username check ran BEFORE the closed gate, so
    an existing name returned 400 "already taken" and a new name returned 403
    "registration is closed" — an anonymous caller diffed the two to enumerate
    valid usernames. Now both return an identical 403 (the gate runs first).
    """
    await _insert_user_low_cost(db_engine, "existing_user")

    # Existing username, plain (closed) registration → 403, NOT 400.
    resp_existing = test_client.post(
        "/api/auth/register",
        data={"username": "existing_user", "password": "correct-horse-battery"},
    )
    # Brand-new username, same closed state → also 403.
    resp_new = test_client.post(
        "/api/auth/register",
        data={"username": "does_not_exist_xyz", "password": "correct-horse-battery"},
    )

    assert resp_existing.status_code == 403
    assert resp_new.status_code == 403
    # Indistinguishable: same status AND same detail → no enumeration signal.
    assert resp_existing.json()["detail"] == resp_new.json()["detail"]
    assert "already taken" not in resp_existing.text


async def test_register_with_valid_invite_when_users_exist_succeeds(
    test_client: TestClient, db_engine: AsyncEngine
) -> None:
    """Valid invite token bypasses the single-admin gate and grants admin."""
    # Set up an existing admin in the DB.
    admin_id = await _insert_user_low_cost(db_engine, "admin_invite_test", "adminpw1234")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET is_admin = 1 WHERE id = :uid"),
            {"uid": admin_id},
        )

    # Log in as admin and issue an invite.
    login_resp = test_client.post(
        "/api/auth/login",
        data={"username": "admin_invite_test", "password": "adminpw1234"},
    )
    assert login_resp.status_code == 200
    invite_resp = test_client.post("/api/admin/invite")
    assert invite_resp.status_code == 200, invite_resp.text
    token = invite_resp.json()["token"]
    test_client.cookies.clear()

    # Register with the invite token → 201 + admin.
    reg_resp = test_client.post(
        f"/api/auth/register?token={token}",
        data={"username": "invited_user", "password": "correct-horse-battery"},
    )
    assert reg_resp.status_code == 201, reg_resp.text
    new_id = reg_resp.json()["id"]

    # Confirm is_admin was granted.
    async with db_engine.connect() as conn:
        from lmchat.db.schema import users as users_table
        row = (
            await conn.execute(
                select(users_table.c.is_admin).where(users_table.c.id == new_id)
            )
        ).fetchone()
    assert row is not None
    assert bool(row._mapping["is_admin"]) is True


# ---------------------------------------------------------------------------
# Password policy tests
# ---------------------------------------------------------------------------


def test_register_short_password_returns_422(test_client: TestClient) -> None:
    """Password under 8 chars → 422 with policy message.

    Client-side `minLength={8}` was bypassable via direct POST; the
    backend now enforces the same floor.
    """
    resp = test_client.post(
        "/api/auth/register",
        data={"username": "validuser", "password": "short1"},
    )
    assert resp.status_code == 422
    assert "at least 8" in resp.json()["detail"]


def test_register_empty_password_returns_422(test_client: TestClient) -> None:
    """Whitespace-only password → 422."""
    resp = test_client.post(
        "/api/auth/register",
        data={"username": "validuser", "password": "        "},
    )
    assert resp.status_code == 422
    assert "empty" in resp.json()["detail"].lower() or "whitespace" in resp.json()["detail"].lower()


def test_change_password_short_returns_422(test_client: TestClient) -> None:
    """change_password rejects sub-policy new_password."""
    _register(test_client)
    login_resp = test_client.post(
        "/api/auth/login",
        data={"username": "alice", "password": "correct-horse-battery"},
    )
    assert login_resp.status_code == 200

    resp = test_client.post(
        "/api/auth/password",
        data={"old_password": "correct-horse-battery", "new_password": "1"},
    )
    assert resp.status_code == 422
    assert "at least 8" in resp.json()["detail"]


def test_change_password_null_byte_returns_422(test_client: TestClient) -> None:
    """change_password rejects new_password containing NUL bytes."""
    _register(test_client)
    test_client.post(
        "/api/auth/login",
        data={"username": "alice", "password": "correct-horse-battery"},
    )
    resp = test_client.post(
        "/api/auth/password",
        data={
            "old_password": "correct-horse-battery",
            "new_password": "validlength\x00password",
        },
    )
    assert resp.status_code == 422
    assert "null" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Login tests
# ---------------------------------------------------------------------------


def test_login_success_sets_cookie(test_client: TestClient) -> None:
    """Successful login → 200 + lmchat_session cookie set."""
    _register(test_client)
    resp = _login(test_client)
    assert resp.status_code == 200
    body = resp.json()
    assert "user_id" in body
    assert "expires_at" in body
    assert "lmchat_session" in resp.cookies


def test_login_wrong_password_returns_401_with_uniform_body(
    test_client: TestClient,
) -> None:
    """Wrong password → 401 with uniform detail (no field disclosure)."""
    _register(test_client)
    resp = _login(test_client, password="wrong-password")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid credentials"


def test_login_unknown_user_returns_401_with_uniform_body(
    test_client: TestClient,
) -> None:
    """Unknown username → 401 with same detail as wrong password."""
    resp = _login(test_client, username="nobody")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid credentials"


def test_login_totp_required_returns_401_with_totp_marker(
    test_client: TestClient,
    db_engine: AsyncEngine,
) -> None:
    """User with TOTP configured but no code supplied → 401 detail=totp required."""
    # Register and login to get a session.
    _register(test_client)
    resp = _login(test_client)
    assert resp.status_code == 200

    # Set up TOTP.
    totp_setup_resp = test_client.post("/api/auth/totp/setup")
    assert totp_setup_resp.status_code == 200
    setup_data = totp_setup_resp.json()
    secret = setup_data["secret"]

    # Verify TOTP (persist the secret).
    code = pyotp.TOTP(secret).now()
    verify_resp = test_client.post(
        "/api/auth/totp/verify", data={"secret": secret, "code": code}
    )
    assert verify_resp.status_code == 200

    # Logout.
    test_client.post("/api/auth/logout")

    # Now login without totp_code.
    resp = _login(test_client)
    assert resp.status_code == 401
    assert resp.json()["detail"] == "totp required"


def test_login_with_totp_success(
    test_client: TestClient,
    db_engine: AsyncEngine,
) -> None:
    """Login with correct TOTP code → 200."""
    # Register + initial login.
    _register(test_client)
    _login(test_client)

    # Setup + verify TOTP.
    setup_resp = test_client.post("/api/auth/totp/setup")
    secret = setup_resp.json()["secret"]
    code = pyotp.TOTP(secret).now()
    test_client.post("/api/auth/totp/verify", data={"secret": secret, "code": code})

    # Logout.
    test_client.post("/api/auth/logout")

    # Login with TOTP code.
    code2 = pyotp.TOTP(secret).now()
    resp = _login(test_client, totp_code=code2)
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Logout tests
# ---------------------------------------------------------------------------


def test_logout_clears_cookie(test_client: TestClient) -> None:
    """Logout → 200 and cookie is cleared (Max-Age=0)."""
    _register(test_client)
    _login(test_client)
    resp = test_client.post("/api/auth/logout")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    # After logout the cookie should be gone (cleared by set_cookie with max_age=0)
    # TestClient reflects the cleared cookie as absent or empty.
    cookie_val = resp.cookies.get("lmchat_session", "")
    assert cookie_val == ""


def test_logout_missing_cookie_returns_200_idempotent(test_client: TestClient) -> None:
    """Logout with no session cookie → 200 (idempotent)."""
    # Ensure no cookie is set.
    test_client.cookies.clear()
    resp = test_client.post("/api/auth/logout")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# Change password tests
# ---------------------------------------------------------------------------


def test_change_password_requires_auth(test_client: TestClient) -> None:
    """POST /password without session cookie → 401."""
    test_client.cookies.clear()
    resp = test_client.post(
        "/api/auth/password",
        data={"old_password": "old", "new_password": "new"},
    )
    assert resp.status_code == 401


def test_change_password_revokes_other_sessions_keeps_current(
    test_client: TestClient,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Change password revokes other sessions; the current session stays valid.

    Single-session mode is disabled for this test so that two concurrent
    sessions can coexist (by design: we need one session to still be alive
    after a second login for the change-password call to use).  With single-
    session ON, the second login would revoke the first session before
    change_password even runs.
    """
    # Disable single-session so both sessions coexist until change_password.
    monkeypatch.setenv("LM_CHAT_SINGLE_SESSION", "false")
    from lmchat.config import get_settings

    get_settings.cache_clear()

    _register(test_client)

    # Login session A (the current test_client session).
    resp_a = _login(test_client)
    session_a_token = resp_a.cookies.get("lmchat_session") or test_client.cookies.get(
        "lmchat_session"
    )
    user_id = resp_a.json()["user_id"]
    assert session_a_token is not None

    # Create a second session by logging in with a separate client.
    app2 = create_app()
    store2 = SQLiteSessionStore(engine=db_engine)
    app2.dependency_overrides[get_engine_dep] = lambda: db_engine
    app2.dependency_overrides[get_default_session_store_dep] = lambda: store2
    with TestClient(app2, raise_server_exceptions=True) as client2:
        client2.app.state.session_store = store2  # type: ignore[attr-defined]
        client2.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client2.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        resp_b = _login(client2)
        assert resp_b.status_code == 200
        session_b_token = resp_b.cookies.get("lmchat_session") or client2.cookies.get(
            "lmchat_session"
        )
        assert session_b_token is not None

    # Both sessions should exist (be LIVE / usable) at this point. Counts
    # only non-rotated rows: login() now rotates its session immediately
    # after minting it (AUTH-SESSION defense-in-depth), so each login
    # leaves TWO rows behind (the discarded pre-rotation row, kept only
    # for the audit trail, plus the live rotated row) — raw row count is
    # no longer the same as live-session count.
    import asyncio

    async def _count_live_sessions() -> int:
        async with db_engine.connect() as conn:
            result = await conn.execute(
                select(sessions).where(
                    sessions.c.user_id == user_id, sessions.c.rotated_at.is_(None)
                )
            )
            return len(result.fetchall())

    before = asyncio.get_event_loop().run_until_complete(_count_live_sessions())
    assert before == 2, f"Expected 2 live sessions before change_password, got {before}"

    # Change password with session A.
    cp_resp = test_client.post(
        "/api/auth/password",
        data={"old_password": "correct-horse-battery", "new_password": "new-pass-xyz"},
    )
    assert cp_resp.status_code == 200

    # Session A should still work (current session preserved).
    totp_check = test_client.post("/api/auth/totp/setup")
    assert totp_check.status_code == 200

    # Session B should be revoked — only the current session should remain.
    remaining = asyncio.get_event_loop().run_until_complete(_count_live_sessions())
    assert remaining == 1


# ---------------------------------------------------------------------------
# TOTP tests
# ---------------------------------------------------------------------------


def test_totp_setup_returns_provisioning_uri(test_client: TestClient) -> None:
    """totp/setup returns provisioning_uri and secret."""
    _register(test_client)
    _login(test_client)
    resp = test_client.post("/api/auth/totp/setup")
    assert resp.status_code == 200
    data = resp.json()
    assert "provisioning_uri" in data
    assert "secret" in data
    assert data["provisioning_uri"].startswith("otpauth://totp/")


def test_totp_verify_persists_encrypted_secret(
    test_client: TestClient,
    db_engine: AsyncEngine,
) -> None:
    """After totp/verify, users.totp_secret starts with enc$v1$."""
    _register(test_client)
    resp = _login(test_client)
    user_id = resp.json()["user_id"]

    setup_resp = test_client.post("/api/auth/totp/setup")
    secret = setup_resp.json()["secret"]
    code = pyotp.TOTP(secret).now()

    verify_resp = test_client.post(
        "/api/auth/totp/verify", data={"secret": secret, "code": code}
    )
    assert verify_resp.status_code == 200

    # Check DB directly.
    import asyncio

    async def _get_totp_secret() -> str | None:
        async with db_engine.connect() as conn:
            result = await conn.execute(
                select(users.c.totp_secret).where(users.c.id == user_id)
            )
            row = result.fetchone()
            return row[0] if row else None

    stored = asyncio.get_event_loop().run_until_complete(_get_totp_secret())
    assert stored is not None
    assert stored.startswith("enc$v1$")


def test_totp_disable_requires_password(test_client: TestClient) -> None:
    """totp/disable with wrong password → 400."""
    _register(test_client)
    _login(test_client)

    # Setup + verify TOTP first.
    setup_resp = test_client.post("/api/auth/totp/setup")
    secret = setup_resp.json()["secret"]
    code = pyotp.TOTP(secret).now()
    test_client.post("/api/auth/totp/verify", data={"secret": secret, "code": code})

    # Wrong password → 400.
    resp = test_client.post(
        "/api/auth/totp/disable", data={"password": "wrong-password"}
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Rate limit test
# ---------------------------------------------------------------------------


def test_rate_limit_triggers_after_threshold(
    test_client: TestClient,
) -> None:
    """11th rapid login attempt → 429 with Retry-After header."""
    _register(test_client)
    limit = 10
    last_status = 0

    for _i in range(limit + 1):
        resp = test_client.post(
            "/api/auth/login",
            data={"username": "alice", "password": "wrong-password"},
        )
        last_status = resp.status_code
        if resp.status_code == 429:
            assert "retry-after" in resp.headers
            break
    assert last_status == 429


# ---------------------------------------------------------------------------
# Audit log test
# ---------------------------------------------------------------------------


def test_audit_log_rows_present_after_integration_flow(
    test_client: TestClient,
    db_engine: AsyncEngine,
) -> None:
    """Register + login + logout flow produces audit_log rows."""
    import asyncio

    _register(test_client)
    _login(test_client)
    test_client.post("/api/auth/logout")

    register_count = asyncio.get_event_loop().run_until_complete(
        _count_audit_rows(db_engine, "auth.register")
    )
    login_count = asyncio.get_event_loop().run_until_complete(
        _count_audit_rows(db_engine, "auth.login.success")
    )
    logout_count = asyncio.get_event_loop().run_until_complete(
        _count_audit_rows(db_engine, "auth.logout")
    )
    assert register_count >= 1
    assert login_count >= 1
    assert logout_count >= 1


# ---------------------------------------------------------------------------
# enc$v1$ prefix test
# ---------------------------------------------------------------------------


def test_enc_v1_prefix_on_persisted_totp_secret(
    test_client: TestClient,
    db_engine: AsyncEngine,
) -> None:
    """Persisted totp_secret must start with enc$v1$ (encryption envelope)."""
    _register(test_client)
    resp = _login(test_client)
    user_id = resp.json()["user_id"]

    setup_resp = test_client.post("/api/auth/totp/setup")
    secret = setup_resp.json()["secret"]
    code = pyotp.TOTP(secret).now()
    test_client.post("/api/auth/totp/verify", data={"secret": secret, "code": code})

    import asyncio

    async def _fetch_secret() -> str | None:
        async with db_engine.connect() as conn:
            result = await conn.execute(
                select(users.c.totp_secret).where(users.c.id == user_id)
            )
            row = result.fetchone()
            return row[0] if row else None

    stored = asyncio.get_event_loop().run_until_complete(_fetch_secret())
    assert stored is not None
    assert stored.startswith("enc$v1$"), f"Expected enc$v1$ prefix, got: {stored!r}"


# ---------------------------------------------------------------------------
# Full round-trip integration test
# ---------------------------------------------------------------------------


def test_register_login_totp_logout_round_trip(
    test_client: TestClient,
    db_engine: AsyncEngine,
) -> None:
    """Full round-trip: register → login → totp-setup → totp-login → logout.

    Exercises the entire auth flow end-to-end in a single sequential test.
    """
    import asyncio

    # 1. Register.
    reg_resp = _register(test_client, username="roundtrip", password="rt-password-123")
    assert reg_resp.status_code == 201
    user_id = reg_resp.json()["id"]

    # 2. Login (no TOTP yet).
    login_resp = _login(test_client, username="roundtrip", password="rt-password-123")
    assert login_resp.status_code == 200
    assert "lmchat_session" in test_client.cookies

    # 3. Setup TOTP.
    setup_resp = test_client.post("/api/auth/totp/setup")
    assert setup_resp.status_code == 200
    secret = setup_resp.json()["secret"]
    uri = setup_resp.json()["provisioning_uri"]
    assert uri.startswith("otpauth://totp/")

    # 4. Verify TOTP (persist the secret).
    code = pyotp.TOTP(secret).now()
    verify_resp = test_client.post(
        "/api/auth/totp/verify", data={"secret": secret, "code": code}
    )
    assert verify_resp.status_code == 200

    # Confirm enc$v1$ stored.
    async def _check_totp_secret() -> str | None:
        async with db_engine.connect() as conn:
            r = await conn.execute(
                select(users.c.totp_secret).where(users.c.id == user_id)
            )
            row = r.fetchone()
            return row[0] if row else None

    stored = asyncio.get_event_loop().run_until_complete(_check_totp_secret())
    assert stored is not None and stored.startswith("enc$v1$")

    # 5. Logout.
    logout_resp = test_client.post("/api/auth/logout")
    assert logout_resp.status_code == 200

    # 6. Login with TOTP code.
    code2 = pyotp.TOTP(secret).now()
    login_totp_resp = _login(
        test_client, username="roundtrip", password="rt-password-123", totp_code=code2
    )
    assert login_totp_resp.status_code == 200
    assert "lmchat_session" in test_client.cookies

    # 7. Final logout.
    final_logout = test_client.post("/api/auth/logout")
    assert final_logout.status_code == 200

    # 8. Verify audit log has all expected events.
    register_n = asyncio.get_event_loop().run_until_complete(
        _count_audit_rows(db_engine, "auth.register")
    )
    login_n = asyncio.get_event_loop().run_until_complete(
        _count_audit_rows(db_engine, "auth.login.success")
    )
    logout_n = asyncio.get_event_loop().run_until_complete(
        _count_audit_rows(db_engine, "auth.logout")
    )
    totp_verified_n = asyncio.get_event_loop().run_until_complete(
        _count_audit_rows(db_engine, "auth.totp.verified")
    )
    assert register_n >= 1
    assert login_n >= 2  # two successful logins
    assert logout_n >= 2  # two logouts
    assert totp_verified_n >= 1


# ---------------------------------------------------------------------------
# GET /api/auth/me tests
# ---------------------------------------------------------------------------


async def test_me_authenticated_returns_200_with_correct_shape(
    test_client: TestClient,
    db_engine: AsyncEngine,
) -> None:
    """GET /api/auth/me with a valid session returns 200 + {user_id, username,
    is_admin}."""
    # Insert meuser directly (bypassing the registration gate) so it is not
    # the first user (which would be auto-promoted to admin).  Two users are
    # needed: the placeholder seeded first, then meuser.
    await _insert_user_low_cost(db_engine, username="placeholder_me")
    await _insert_user_low_cost(db_engine, username="meuser", password="me-password-123")
    login_resp = _login(test_client, username="meuser", password="me-password-123")
    user_id = login_resp.json()["user_id"]

    resp = test_client.get("/api/auth/me")
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == user_id
    assert body["username"] == "meuser"
    assert isinstance(body["is_admin"], bool)
    assert body["is_admin"] is False
    # New user has no TOTP secret → totp_enabled=False (SA-gaps fix:
    # SecuritySettings reads this on mount to render the correct state).
    assert isinstance(body["totp_enabled"], bool)
    assert body["totp_enabled"] is False


def test_me_unauthenticated_returns_401(
    test_client: TestClient,
) -> None:
    """GET /api/auth/me without a session cookie returns 401."""
    test_client.cookies.clear()
    resp = test_client.get("/api/auth/me")
    assert resp.status_code == 401


def test_me_returns_is_admin_true_for_admin_user(
    test_client: TestClient,
    db_engine: AsyncEngine,
) -> None:
    """GET /api/auth/me returns is_admin=True when the DB row has is_admin=1."""
    import asyncio

    from sqlalchemy import text

    _register(test_client, username="adminme", password="admin-pass-123")
    login_resp = _login(test_client, username="adminme", password="admin-pass-123")
    user_id = login_resp.json()["user_id"]

    # Flip the is_admin flag directly in the DB.
    async def _set_admin() -> None:
        async with db_engine.begin() as conn:
            await conn.execute(
                text("UPDATE users SET is_admin = 1 WHERE id = :uid"),
                {"uid": user_id},
            )

    asyncio.get_event_loop().run_until_complete(_set_admin())

    resp = test_client.get("/api/auth/me")
    assert resp.status_code == 200
    assert resp.json()["is_admin"] is True


def test_me_returns_totp_enabled_true_after_totp_setup(
    test_client: TestClient,
) -> None:
    """GET /api/auth/me returns totp_enabled=True once the user has a TOTP
    secret stored.

    Closes SA-gaps: SecuritySettings was resetting to "not configured" on
    every reload because /me did not surface the TOTP state.  Now the SPA
    can hydrate the Settings TOTP surface from /me on mount.
    """
    _register(test_client, username="totpme", password="totp-pass-123")
    _login(test_client, username="totpme", password="totp-pass-123")

    # Bootstrap a TOTP secret + complete the verify step so users.totp_secret
    # is non-null in the DB.
    setup_resp = test_client.post("/api/auth/totp/setup")
    assert setup_resp.status_code == 200
    setup = setup_resp.json()
    secret = setup["secret"]
    code = pyotp.TOTP(secret).now()
    verify_resp = test_client.post(
        "/api/auth/totp/verify", data={"secret": secret, "code": code}
    )
    assert verify_resp.status_code == 200

    resp = test_client.get("/api/auth/me")
    assert resp.status_code == 200
    body = resp.json()
    assert body["totp_enabled"] is True
    assert body["username"] == "totpme"


def test_login_returns_totp_enabled_field(
    test_client: TestClient,
) -> None:
    """POST /api/auth/login response carries totp_enabled for zero-round-trip
    auth-store hydration.

    Pre-TOTP: totp_enabled=False.  After /totp/setup+verify, a subsequent
    login (with the required code) returns totp_enabled=True.
    """
    _register(test_client, username="logintotp", password="login-pass-123")
    login_resp = _login(test_client, username="logintotp", password="login-pass-123")
    assert login_resp.status_code == 200
    assert login_resp.json()["totp_enabled"] is False

    # Enrol TOTP.
    setup_resp = test_client.post("/api/auth/totp/setup")
    assert setup_resp.status_code == 200
    secret = setup_resp.json()["secret"]
    code = pyotp.TOTP(secret).now()
    verify_resp = test_client.post(
        "/api/auth/totp/verify", data={"secret": secret, "code": code}
    )
    assert verify_resp.status_code == 200

    # Logout + login again with the TOTP code; response now reports
    # totp_enabled=True so the SPA can render the Settings TOTP surface
    # in the "enabled" state without a follow-up /me round-trip.
    test_client.post("/api/auth/logout")
    code2 = pyotp.TOTP(secret).now()
    relogin = _login(
        test_client,
        username="logintotp",
        password="login-pass-123",
        totp_code=code2,
    )
    assert relogin.status_code == 200
    assert relogin.json()["totp_enabled"] is True


# ---------------------------------------------------------------------------
# Setup-status (anonymous) + setup-token gating
# ---------------------------------------------------------------------------


def test_setup_status_empty_db_returns_needs_setup_true(
    test_client: TestClient,
) -> None:
    """GET /api/auth/setup_status with no users returns {needs_setup: true}.

    The endpoint is anonymous (no session cookie required); it lives in
    the AuthMiddleware ``_SKIP_EXACT`` set so the Login page can probe
    it on mount.
    """
    test_client.cookies.clear()
    resp = test_client.get("/api/auth/setup_status")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"needs_setup": True}
    # Recon-leak shape — only the boolean flag is exposed.
    assert set(body.keys()) == {"needs_setup"}


def test_setup_status_after_first_user_returns_needs_setup_false(
    test_client: TestClient,
) -> None:
    """After at least one user exists, setup_status reports needs_setup=false."""
    _register(test_client, username="firstuser", password="bootstrap-pw-9")
    test_client.cookies.clear()
    resp = test_client.get("/api/auth/setup_status")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"needs_setup": False}


def test_register_without_setup_token_returns_403_when_configured(
    test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When LM_CHAT_SETUP_TOKEN is set and the DB is empty, POST /register
    without the token returns 403."""
    monkeypatch.setenv("LM_CHAT_SETUP_TOKEN", "s3cret-token")
    from lmchat.config import get_settings

    get_settings.cache_clear()

    resp = test_client.post(
        "/api/auth/register",
        data={"username": "alice", "password": "pw-with-enough-length"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "setup token required"

    get_settings.cache_clear()


def test_register_with_setup_token_query_returns_201_when_configured(
    test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The setup token may be supplied via the ?token=... query param."""
    monkeypatch.setenv("LM_CHAT_SETUP_TOKEN", "s3cret-token")
    from lmchat.config import get_settings

    get_settings.cache_clear()

    resp = test_client.post(
        "/api/auth/register?token=s3cret-token",
        data={"username": "alice", "password": "pw-with-enough-length"},
    )
    assert resp.status_code == 201

    get_settings.cache_clear()


def test_register_with_setup_token_header_returns_201_when_configured(
    test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The setup token may be supplied via the X-Setup-Token header."""
    monkeypatch.setenv("LM_CHAT_SETUP_TOKEN", "s3cret-token")
    from lmchat.config import get_settings

    get_settings.cache_clear()

    resp = test_client.post(
        "/api/auth/register",
        data={"username": "alice", "password": "pw-with-enough-length"},
        headers={"X-Setup-Token": "s3cret-token"},
    )
    assert resp.status_code == 201

    get_settings.cache_clear()


def test_register_with_wrong_setup_token_returns_403(
    test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mismatched token returns 403 (same as the missing-token path);
    the response body MUST NOT disclose the configured value."""
    monkeypatch.setenv("LM_CHAT_SETUP_TOKEN", "s3cret-token")
    from lmchat.config import get_settings

    get_settings.cache_clear()

    resp = test_client.post(
        "/api/auth/register?token=wrong",
        data={"username": "alice", "password": "pw-with-enough-length"},
    )
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail == "setup token required"
    # Defensive: the configured token must never appear in the response.
    assert "s3cret-token" not in resp.text

    get_settings.cache_clear()


def test_register_setup_token_gate_lifts_after_first_user(
    test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the first user registers, the setup-token requirement lifts.
    The single-admin gate then takes over: plain registration is closed (403),
    but registration via a valid admin invite still works.
    """
    monkeypatch.setenv("LM_CHAT_SETUP_TOKEN", "s3cret-token")
    from lmchat.config import get_settings

    get_settings.cache_clear()

    # Bootstrap admin uses the token.
    first = test_client.post(
        "/api/auth/register?token=s3cret-token",
        data={"username": "admin", "password": "pw-with-enough-length"},
    )
    assert first.status_code == 201

    # The setup-token gate has lifted, but the single-admin gate now fires.
    # Plain registration without an invite → 403.
    second = test_client.post(
        "/api/auth/register",
        data={"username": "regular", "password": "pw-with-enough-length"},
    )
    assert second.status_code == 403
    assert "Registration is closed" in second.json()["detail"]

    get_settings.cache_clear()


def test_register_without_setup_token_returns_201_when_unset(
    test_client: TestClient,
) -> None:
    """When LM_CHAT_SETUP_TOKEN is unset (default), /register works without
    a token even on a fresh DB."""
    # Note: _set_env fixture does not set LM_CHAT_SETUP_TOKEN, so the
    # default empty string is in effect.
    resp = test_client.post(
        "/api/auth/register",
        data={"username": "alice", "password": "pw-with-enough-length"},
    )
    assert resp.status_code == 201


def test_me_returns_needs_setup_false_for_authenticated_user(
    test_client: TestClient,
) -> None:
    """GET /api/auth/me includes needs_setup=false once a user exists."""
    _register(test_client, username="meneeds", password="pw-with-enough-length")
    _login(test_client, username="meneeds", password="pw-with-enough-length")

    resp = test_client.get("/api/auth/me")
    assert resp.status_code == 200
    body = resp.json()
    assert body["needs_setup"] is False


# ---------------------------------------------------------------------------
# Profile tests
# ---------------------------------------------------------------------------


def _login_default(client: TestClient) -> None:
    _register(client)
    _login(client, username="alice", password="correct-horse-battery")


def test_me_returns_null_profile_fields_for_fresh_account(
    test_client: TestClient,
) -> None:
    """Fresh accounts have email/display_name/avatar_url=null."""
    _login_default(test_client)
    body = test_client.get("/api/auth/me").json()
    assert body["email"] is None
    assert body["display_name"] is None
    assert body["avatar_url"] is None


def test_patch_profile_updates_all_fields(test_client: TestClient) -> None:
    """PATCH /api/auth/profile round-trips email + display_name + avatar_url."""
    _login_default(test_client)
    resp = test_client.patch(
        "/api/auth/profile",
        data={
            "email": "alice@example.com",
            "display_name": "Alice Liddell",
            "avatar_url": "https://cdn.example.com/a/alice.png",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["email"] == "alice@example.com"
    assert body["display_name"] == "Alice Liddell"
    assert body["avatar_url"] == "https://cdn.example.com/a/alice.png"
    # /me reflects the same values.
    me = test_client.get("/api/auth/me").json()
    assert me["email"] == "alice@example.com"


def test_patch_profile_partial_leaves_others_untouched(
    test_client: TestClient,
) -> None:
    """Omitting a field leaves the stored value alone."""
    _login_default(test_client)
    test_client.patch(
        "/api/auth/profile",
        data={"email": "first@example.com", "display_name": "First"},
    )
    test_client.patch(
        "/api/auth/profile",
        data={"display_name": "Second"},
    )
    me = test_client.get("/api/auth/me").json()
    assert me["email"] == "first@example.com"
    assert me["display_name"] == "Second"


def test_patch_profile_clear_param_nulls_field(test_client: TestClient) -> None:
    """Listing a field in `clear` NULLs it. Mirrors LM Studio override clearing."""
    _login_default(test_client)
    test_client.patch(
        "/api/auth/profile",
        data={"email": "before@example.com", "display_name": "Keep"},
    )
    test_client.patch(
        "/api/auth/profile",
        data={"clear": "email"},
    )
    me = test_client.get("/api/auth/me").json()
    assert me["email"] is None
    # display_name was NOT in the clear list → preserved.
    assert me["display_name"] == "Keep"


def test_patch_profile_clear_unknown_field_returns_422(
    test_client: TestClient,
) -> None:
    """Unknown clear field → 422 with the offending name in the detail."""
    _login_default(test_client)
    resp = test_client.patch(
        "/api/auth/profile",
        data={"clear": "password_hash"},
    )
    assert resp.status_code == 422
    assert "password_hash" in resp.json()["detail"]


def test_patch_profile_rejects_invalid_email(test_client: TestClient) -> None:
    """Malformed email → 422 with policy message."""
    _login_default(test_client)
    resp = test_client.patch(
        "/api/auth/profile",
        data={"email": "not-an-email"},
    )
    assert resp.status_code == 422
    assert "email" in resp.json()["detail"].lower()


def test_patch_profile_rejects_javascript_avatar_url(
    test_client: TestClient,
) -> None:
    """avatar_url=javascript:... → 422 (xss defense)."""
    _login_default(test_client)
    resp = test_client.patch(
        "/api/auth/profile",
        data={"avatar_url": "javascript:alert(1)"},
    )
    assert resp.status_code == 422
    assert "http" in resp.json()["detail"].lower()


def test_patch_profile_requires_auth(test_client: TestClient) -> None:
    """PATCH /api/auth/profile without a session → 401."""
    resp = test_client.patch(
        "/api/auth/profile",
        data={"email": "anon@example.com"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# /me/probe tests — must NOT 401
# ---------------------------------------------------------------------------


def test_me_probe_returns_200_with_null_user_when_unauthenticated(
    test_client: TestClient,
) -> None:
    """GET /api/auth/me/probe without a session → 200 + user_id: null.

    The mount-time AuthHydrator uses this endpoint to avoid the
    browser-DevTools 401 noise that /me generates on cold load.
    """
    resp = test_client.get("/api/auth/me/probe")
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] is None
    assert body["username"] is None
    assert body["is_admin"] is False


def test_me_probe_returns_user_when_authenticated(
    test_client: TestClient,
) -> None:
    """GET /api/auth/me/probe with a session → 200 + populated user."""
    _login_default(test_client)
    body = test_client.get("/api/auth/me/probe").json()
    assert body["user_id"] is not None
    assert body["username"] == "alice"
