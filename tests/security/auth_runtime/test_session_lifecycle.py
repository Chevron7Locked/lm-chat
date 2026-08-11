# SPDX-License-Identifier: Apache-2.0
"""Session lifecycle tests.

Covers:
  - Session creation on login (cookie set, HttpOnly, SameSite=lax).
  - Session destruction on logout (cookie cleared, subsequent requests 401).
  - Cookie attribute assertions: HttpOnly, SameSite=Lax (Secure is omitted on
    plain-HTTP TestClient).

Uses FastAPI TestClient with per-test SQLite engine (no running server).
"""
from __future__ import annotations

from collections.abc import AsyncGenerator, Generator, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.app import create_app
from lmchat.db.schema import metadata, users
from lmchat.middleware._bucket_store import InMemoryBucketStore
from lmchat.routes._dependencies import get_default_session_store_dep, get_engine_dep
from lmchat.services.auth_service import _reset_dummy_hash_cache
from lmchat.session.sqlite_store import SQLiteSessionStore
from lmchat.utils.hashing import hash_password

_LOW_N = 2**10
COOKIE_NAME = "lmchat_session"
_USERNAME = "session_lifecycle_user"
_PASSWORD = "lifecycle-password-1!"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    from lmchat.config import get_settings
    get_settings.cache_clear()
    _reset_dummy_hash_cache()
    yield
    get_settings.cache_clear()
    _reset_dummy_hash_cache()


@pytest.fixture()
async def db_engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    db_path = tmp_path / "session_lifecycle.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}", pool_pre_ping=True)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture()
def test_client(db_engine: AsyncEngine) -> Generator[TestClient]:
    app = create_app()
    store = SQLiteSessionStore(engine=db_engine)
    app.dependency_overrides[get_engine_dep] = lambda: db_engine
    app.dependency_overrides[get_default_session_store_dep] = lambda: store
    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.session_store = store  # type: ignore[attr-defined]
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        yield client


@pytest.fixture()
async def registered_user(db_engine: AsyncEngine) -> int:
    """Insert a user and return their user_id."""
    pw_hash = hash_password(_PASSWORD, n=_LOW_N, r=8, p=1)
    async with db_engine.begin() as conn:
        id_result = await conn.execute(
            select(func.coalesce(func.max(users.c.id), 0) + 1)
        )
        next_id = id_result.scalar() or 1
        await conn.execute(
            text(
                "INSERT INTO users (id, username, password_hash)"
                " VALUES (:id, :u, :ph)"
            ),
            {"id": next_id, "u": _USERNAME, "ph": pw_hash},
        )
    return int(next_id)


# ---------------------------------------------------------------------------
# Tests: session creation
# ---------------------------------------------------------------------------


def test_login_creates_session_cookie(
    test_client: TestClient, registered_user: int
) -> None:
    """POST /api/auth/login must set a non-empty lmchat_session cookie."""
    resp = test_client.post(
        "/api/auth/login",
        data={"username": _USERNAME, "password": _PASSWORD},
    )
    assert resp.status_code == 200, f"Login failed: {resp.text}"
    cookie = resp.cookies.get(COOKIE_NAME)
    assert cookie is not None, "No session cookie returned on login"
    assert len(cookie) > 0, "Session cookie is empty"


def test_login_response_contains_user_id(
    test_client: TestClient, registered_user: int
) -> None:
    """Login response body must contain user_id matching the registered user."""
    resp = test_client.post(
        "/api/auth/login",
        data={"username": _USERNAME, "password": _PASSWORD},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "user_id" in body
    assert body["user_id"] == registered_user


def test_login_cookie_httponly(
    test_client: TestClient, registered_user: int
) -> None:
    """The Set-Cookie header must include HttpOnly."""
    resp = test_client.post(
        "/api/auth/login",
        data={"username": _USERNAME, "password": _PASSWORD},
    )
    assert resp.status_code == 200
    set_cookie = resp.headers.get("set-cookie", "")
    assert "httponly" in set_cookie.lower(), (
        f"Set-Cookie missing HttpOnly attribute. Header: {set_cookie!r}"
    )


def test_login_cookie_samesite_lax(
    test_client: TestClient, registered_user: int
) -> None:
    """The Set-Cookie header must include SameSite=Lax."""
    resp = test_client.post(
        "/api/auth/login",
        data={"username": _USERNAME, "password": _PASSWORD},
    )
    assert resp.status_code == 200
    set_cookie = resp.headers.get("set-cookie", "").lower()
    assert "samesite=lax" in set_cookie, (
        f"Set-Cookie missing SameSite=lax attribute. Header: {set_cookie!r}"
    )


def test_login_cookie_path_is_root(
    test_client: TestClient, registered_user: int
) -> None:
    """Cookie path must be / so all routes receive the cookie."""
    resp = test_client.post(
        "/api/auth/login",
        data={"username": _USERNAME, "password": _PASSWORD},
    )
    assert resp.status_code == 200
    set_cookie = resp.headers.get("set-cookie", "").lower()
    assert "path=/" in set_cookie, (
        f"Set-Cookie missing Path=/ attribute. Header: {set_cookie!r}"
    )


def test_session_allows_authenticated_request(
    test_client: TestClient, registered_user: int
) -> None:
    """A valid session cookie must allow access to authenticated endpoints."""
    login_resp = test_client.post(
        "/api/auth/login",
        data={"username": _USERNAME, "password": _PASSWORD},
    )
    assert login_resp.status_code == 200
    cookie = login_resp.cookies.get(COOKIE_NAME)
    assert cookie

    me_resp = test_client.get("/api/auth/me", cookies={COOKIE_NAME: cookie})
    assert me_resp.status_code == 200, (
        f"Authenticated request failed with {me_resp.status_code}: {me_resp.text}"
    )


# ---------------------------------------------------------------------------
# Tests: session destruction
# ---------------------------------------------------------------------------


def test_logout_clears_cookie(
    test_client: TestClient, registered_user: int
) -> None:
    """POST /api/auth/logout must clear the session cookie."""
    login_resp = test_client.post(
        "/api/auth/login",
        data={"username": _USERNAME, "password": _PASSWORD},
    )
    assert login_resp.status_code == 200
    cookie = login_resp.cookies.get(COOKIE_NAME)
    assert cookie

    logout_resp = test_client.post(
        "/api/auth/logout",
        cookies={COOKIE_NAME: cookie},
    )
    assert logout_resp.status_code == 200

    # After logout the cookie should be absent or have an empty/expired value.
    set_cookie_after = logout_resp.headers.get("set-cookie", "")
    cookie_after = logout_resp.cookies.get(COOKIE_NAME, "")
    assert cookie_after == "" or "max-age=0" in set_cookie_after.lower() or \
           "expires=" in set_cookie_after.lower(), (
        f"Cookie not properly cleared on logout. "
        f"set-cookie header: {set_cookie_after!r}, cookie value: {cookie_after!r}"
    )


def test_logout_invalidates_session(
    test_client: TestClient, registered_user: int
) -> None:
    """After logout, the old session token must yield 401 on authenticated routes."""
    login_resp = test_client.post(
        "/api/auth/login",
        data={"username": _USERNAME, "password": _PASSWORD},
    )
    assert login_resp.status_code == 200
    old_cookie = login_resp.cookies.get(COOKIE_NAME)
    assert old_cookie

    test_client.post("/api/auth/logout", cookies={COOKIE_NAME: old_cookie})

    # Re-use old token
    me_resp = test_client.get("/api/auth/me", cookies={COOKIE_NAME: old_cookie})
    assert me_resp.status_code == 401, (
        f"SECURITY BUG: revoked session token accepted with {me_resp.status_code}. "
        f"Session was not invalidated on logout."
    )


def test_bad_credentials_no_cookie(test_client: TestClient) -> None:
    """Failed login must not set a session cookie."""
    resp = test_client.post(
        "/api/auth/login",
        data={"username": "nonexistent_user", "password": "wrong"},
    )
    assert resp.status_code == 401
    cookie = resp.cookies.get(COOKIE_NAME)
    assert not cookie, (
        f"SECURITY BUG: failed login set a session cookie: {cookie!r}"
    )
