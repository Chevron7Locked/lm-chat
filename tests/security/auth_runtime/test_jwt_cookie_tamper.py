# SPDX-License-Identifier: Apache-2.0
"""JWT/session cookie tamper tests.

Flips bits in session cookies and asserts the middleware rejects with 401.
Uses FastAPI TestClient + the real auth middleware stack via dependency
injection (no running server required).

The session cookie (lmchat_session) carries an opaque session token validated
server-side by the SessionStore. Flipping bits in the token value should cause
the store lookup to return None → middleware returns 401.

Tests 5-10 well-chosen bit-flip positions, including the signature segment.
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
    db_path = tmp_path / "cookie_tamper.db"
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
async def valid_session_cookie(
    db_engine: AsyncEngine, test_client: TestClient
) -> str:
    """Register and log in a user; return the valid session cookie value."""
    pw_hash = hash_password("correct-horse-battery", n=_LOW_N, r=8, p=1)
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
            {"id": next_id, "u": "cookie_test_user", "ph": pw_hash},
        )
    resp = test_client.post(
        "/api/auth/login",
        data={"username": "cookie_test_user", "password": "correct-horse-battery"},
    )
    assert resp.status_code == 200, f"Login failed: {resp.text}"
    cookie = resp.cookies.get(COOKIE_NAME)
    assert cookie is not None, "No session cookie on login"
    return cookie


def _flip_char(token: str, pos: int) -> str:
    """Flip one character at position *pos* (wraps) to a different character."""
    pos = pos % len(token)
    chars = list(token)
    # Rotate through a-z to ensure a valid but different cookie string
    original = chars[pos]
    if original.isalpha():
        chars[pos] = "Z" if original != "Z" else "A"
    elif original.isdigit():
        chars[pos] = "9" if original != "9" else "0"
    elif original == "-":
        chars[pos] = "_"
    elif original == "_":
        chars[pos] = "-"
    else:
        chars[pos] = "X"
    return "".join(chars)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flip_pos", [0, 4, 8, 16, -1, -4, -8, -12, -16])
def test_tampered_cookie_rejected_401(
    test_client: TestClient,
    valid_session_cookie: str,
    flip_pos: int,
) -> None:
    """A cookie with one flipped character must be rejected with 401."""
    tampered = _flip_char(valid_session_cookie, flip_pos)
    assert tampered != valid_session_cookie, (
        f"flip_pos={flip_pos} produced identical cookie — test logic error"
    )
    # Hit an authenticated endpoint (GET /api/auth/me)
    resp = test_client.get(
        "/api/auth/me",
        cookies={COOKIE_NAME: tampered},
    )
    assert resp.status_code == 401, (
        f"SECURITY BUG: tampered cookie (flip_pos={flip_pos}) was accepted "
        f"with status {resp.status_code}. "
        f"Original cookie[:16]={valid_session_cookie[:16]!r}, "
        f"tampered[:16]={tampered[:16]!r}"
    )


def test_completely_random_cookie_rejected_401(
    test_client: TestClient,
    valid_session_cookie: str,
) -> None:
    """A completely fabricated session token must be rejected with 401."""
    fake_token = "X" * len(valid_session_cookie)
    resp = test_client.get(
        "/api/auth/me",
        cookies={COOKIE_NAME: fake_token},
    )
    assert resp.status_code == 401, (
        f"SECURITY BUG: fabricated cookie accepted with status {resp.status_code}"
    )


def test_empty_cookie_rejected_401(test_client: TestClient) -> None:
    """An empty session cookie must be rejected with 401."""
    resp = test_client.get(
        "/api/auth/me",
        cookies={COOKIE_NAME: ""},
    )
    assert resp.status_code in (401, 422), (
        f"Empty cookie should be rejected, got {resp.status_code}"
    )


def test_valid_cookie_accepted(
    test_client: TestClient,
    valid_session_cookie: str,
) -> None:
    """Control test: a valid, unmodified cookie must be accepted."""
    resp = test_client.get(
        "/api/auth/me",
        cookies={COOKIE_NAME: valid_session_cookie},
    )
    assert resp.status_code == 200, (
        f"Valid cookie rejected with {resp.status_code}: {resp.text}"
    )


def test_missing_cookie_rejected_401(test_client: TestClient) -> None:
    """No cookie at all must yield 401 on an authenticated endpoint."""
    resp = test_client.get("/api/auth/me")
    assert resp.status_code == 401, (
        f"Missing cookie should yield 401, got {resp.status_code}"
    )
