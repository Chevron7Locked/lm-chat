# SPDX-License-Identifier: Apache-2.0
"""Live-backend integration tests for auth routes.

P10a.1 — covers all 8 endpoints in routes/auth.py against a live
uvicorn instance (not the ASGI TestClient).

Endpoints covered
-----------------
1.  POST /api/auth/register
2.  POST /api/auth/login
3.  POST /api/auth/logout
4.  GET  /api/auth/me
5.  POST /api/auth/password
6.  POST /api/auth/totp/setup
7.  POST /api/auth/totp/verify
8.  POST /api/auth/totp/disable

Each endpoint has at least one happy-path and one error-path test.
All POST bodies are form-encoded (``data=`` in httpx, not ``json=``).
"""
from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Generator

import httpx
import pyotp
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration.conftest import (
    login_user,
    make_admin,
    register_and_login,
    register_user,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Gate-aware clean-DB fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def _clean_users_db() -> Generator[None, None, None]:
    """Delete all rows from ``users`` before the test; restore the bootstrap
    placeholder afterwards.

    The integration conftest seeds ``__bootstrap_placeholder__`` at session
    start so that the very first test registration doesn't accidentally
    receive the auto-admin grant.  That placeholder causes ``count_users``
    to return > 0, which trips the single-admin registration gate and returns
    403 instead of 201 for any test that exercises ``POST /register`` as the
    first registration.

    This fixture removes the placeholder (and any other users left by prior
    tests), lets the test run against a genuinely empty ``users`` table, then
    re-inserts the placeholder so subsequent tests see the expected seeded
    state.
    """
    import tests.integration.conftest as _iconf

    db_path = _iconf._live_db_path
    if db_path is None:
        yield
        return

    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("DELETE FROM users")
        conn.commit()

    try:
        yield
    finally:
        # Re-insert the bootstrap placeholder so the shared session DB is in
        # the expected seeded state for tests that follow.
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                "DELETE FROM users WHERE username = '__bootstrap_placeholder__'"
            )
            conn.execute(
                "INSERT OR IGNORE INTO users (username, password_hash, is_admin)"
                " VALUES ('__bootstrap_placeholder__', 'scrypt$0$0$0$0$0', 0)"
            )
            conn.commit()


# ---------------------------------------------------------------------------
# POST /api/auth/register
# ---------------------------------------------------------------------------


async def test_register_happy_path(
    client: httpx.AsyncClient,
    _clean_users_db: None,
) -> None:
    """POST /register with valid form body → 201 + {id, username}.

    Requires a clean DB (no pre-existing users) so the registration is the
    first user and the single-admin gate does not block it.
    """
    uname = f"reg_{secrets.token_hex(6)}"
    resp = await client.post(
        "/api/auth/register",
        data={"username": uname, "password": "valid-password-ok"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["username"] == uname
    assert isinstance(body["id"], int)


async def test_register_duplicate_username_plain_returns_closed_403(
    client: httpx.AsyncClient,
    _clean_users_db: None,
) -> None:
    """A second PLAIN registration returns the uniform 403 (closed), even for a
    duplicate username.

    Anti-enumeration (audit 2026-07-11): the single-admin gate now runs BEFORE
    the duplicate-username check, so once the first user exists a plain
    registration returns 403 "Registration is closed" identically whether or not
    the username is a duplicate — an anonymous caller can't diff 400-vs-403 to
    enumerate usernames. The duplicate-username 400 is still returned on OPEN
    registrations (via an admin invite — see tests/routes/test_auth.py).
    """
    uname = f"dup_{secrets.token_hex(6)}"
    pw = "dup-pass-ok"
    r1 = await client.post(
        "/api/auth/register", data={"username": uname, "password": pw}
    )
    assert r1.status_code == 201
    r2 = await client.post(
        "/api/auth/register", data={"username": uname, "password": pw}
    )
    assert r2.status_code == 403, r2.text
    assert "Registration is closed" in r2.json().get("detail", "")


async def test_register_invalid_username_422(
    client: httpx.AsyncClient,
) -> None:
    """Username with spaces/special chars → 422 from route-level validation.

    The ``username`` Form field now carries a regex pattern that FastAPI
    validates before the handler body runs, so 422 is returned regardless
    of how many users already exist in the DB.  No clean-DB fixture needed.
    """
    resp = await client.post(
        "/api/auth/register",
        data={"username": "bad name!", "password": "correct-horse-battery"},
    )
    assert resp.status_code == 422, resp.text


async def test_register_missing_field_422(
    client: httpx.AsyncClient,
) -> None:
    """Missing password field → 422."""
    resp = await client.post(
        "/api/auth/register",
        data={"username": f"x_{secrets.token_hex(4)}"},
    )
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# POST /api/auth/login
# ---------------------------------------------------------------------------


async def test_login_happy_path(
    client: httpx.AsyncClient,
) -> None:
    """Successful login → 200, lmchat_session cookie set, correct JSON shape."""
    uname = f"log_{secrets.token_hex(6)}"
    pw = "login-ok-pw"
    await register_user(client, uname, pw)
    resp = await login_user(client, uname, pw)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "user_id" in body
    assert "expires_at" in body
    assert "username" in body
    assert "is_admin" in body
    assert "lmchat_session" in resp.cookies


async def test_login_wrong_password(
    client: httpx.AsyncClient,
) -> None:
    """Wrong password → 401 detail 'invalid credentials' (uniform, no field leak)."""
    uname = f"lbw_{secrets.token_hex(6)}"
    await register_user(client, uname)
    resp = await login_user(client, uname, "WRONG-PASSWORD")
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "invalid credentials"


async def test_login_unknown_user(
    client: httpx.AsyncClient,
) -> None:
    """Unknown username → 401 same detail as wrong password."""
    resp = await login_user(client, f"nobody_{secrets.token_hex(8)}")
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "invalid credentials"


async def test_login_totp_required_no_code(
    client: httpx.AsyncClient,
) -> None:
    """User with TOTP enabled but no code supplied → 401 detail 'totp required'."""
    uname = f"totp_{secrets.token_hex(6)}"
    pw = "totp-test-pw"
    await register_user(client, uname, pw)
    resp_login = await login_user(client, uname, pw)
    assert resp_login.status_code == 200
    session_cookie: str = resp_login.cookies.get("lmchat_session") or ""

    # Setup TOTP.
    setup_resp = await client.post(
        "/api/auth/totp/setup",
        cookies={"lmchat_session": session_cookie},
    )
    assert setup_resp.status_code == 200, setup_resp.text
    secret = setup_resp.json()["secret"]

    # Verify (persist) TOTP.
    code = pyotp.TOTP(secret).now()
    verify_resp = await client.post(
        "/api/auth/totp/verify",
        data={"secret": secret, "code": code},
        cookies={"lmchat_session": session_cookie},
    )
    assert verify_resp.status_code == 200, verify_resp.text

    # Logout.
    await client.post(
        "/api/auth/logout",
        cookies={"lmchat_session": session_cookie},
    )

    # Login without totp_code.
    resp_no_code = await login_user(client, uname, pw)
    assert resp_no_code.status_code == 401, resp_no_code.text
    assert resp_no_code.json()["detail"] == "totp required"


async def test_login_rate_limited(
    live_servers: dict,
) -> None:
    """Rapid repeated failed logins → eventual 429 with Retry-After.

    Uses a dedicated client (no persisted cookies, fresh username) so
    the rate-limit bucket is isolated to this test.  The default rate
    limit is 10/minute; 11 rapid attempts to the same user exhaust it.
    """
    base_url = live_servers["base_url"]
    uname = f"rl_{secrets.token_hex(8)}"
    last_status = 0
    async with httpx.AsyncClient(
        base_url=base_url,
        follow_redirects=True,
        timeout=15.0,
    ) as raw:
        # Register the user first.
        await raw.post(
            "/api/auth/register",
            data={"username": uname, "password": "rl-register-pw"},
        )
        # Exhaust the bucket with wrong passwords.
        for _ in range(15):
            r = await raw.post(
                "/api/auth/login",
                data={"username": uname, "password": "wrong-pw-rl"},
            )
            last_status = r.status_code
            if r.status_code == 429:
                assert "retry-after" in r.headers
                break
    assert last_status == 429, (
        f"Expected 429 after exhausting rate-limit bucket, last_status={last_status}"
    )


# ---------------------------------------------------------------------------
# POST /api/auth/logout
# ---------------------------------------------------------------------------


async def test_logout_happy_path(
    client: httpx.AsyncClient,
) -> None:
    """Authenticated logout → 200, cookie cleared."""
    uname = f"lo_{secrets.token_hex(6)}"
    _, cookie = await register_and_login(client, uname)
    resp = await client.post(
        "/api/auth/logout",
        cookies={"lmchat_session": cookie},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"


async def test_logout_unauthenticated_returns_200(
    client: httpx.AsyncClient,
) -> None:
    """Logout with no cookie → 200 (idempotent — no session to revoke)."""
    resp = await client.post("/api/auth/logout")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"


async def test_logout_stale_session_returns_401(
    client: httpx.AsyncClient,
) -> None:
    """Logout with an invalid cookie → 401.

    The AuthMiddleware validates the cookie before the route handler runs.
    A syntactically valid but unknown session token is rejected by the
    middleware with 401.  The route handler's idempotent path (returning 200
    with no session revocation) is only reached when NO cookie is present at
    all, not when a bad cookie is present.
    """
    resp = await client.post(
        "/api/auth/logout",
        cookies={"lmchat_session": "nonexistent-stale-token-xyz"},
    )
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# GET /api/auth/me
# ---------------------------------------------------------------------------


async def test_me_happy_path(
    client: httpx.AsyncClient,
) -> None:
    """GET /me with valid session → 200 + {user_id, username, is_admin}."""
    uname = f"me_{secrets.token_hex(6)}"
    user, cookie = await register_and_login(client, uname)
    resp = await client.get(
        "/api/auth/me",
        cookies={"lmchat_session": cookie},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user_id"] == user["id"]
    assert body["username"] == uname
    assert body["is_admin"] is False
    # SA-gaps: /me carries totp_enabled so SecuritySettings can render
    # the correct TOTP state on mount.  New user → no secret → False.
    assert body["totp_enabled"] is False


async def test_me_no_cookie_401(
    client: httpx.AsyncClient,
) -> None:
    """GET /me without session cookie → 401."""
    resp = await client.get("/api/auth/me")
    assert resp.status_code == 401, resp.text


async def test_me_reflects_admin_flag(
    client: httpx.AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """GET /me after admin promotion → is_admin=True."""
    uname = f"mea_{secrets.token_hex(6)}"
    user, cookie = await register_and_login(client, uname)
    await make_admin(db_engine, user["id"])
    resp = await client.get(
        "/api/auth/me",
        cookies={"lmchat_session": cookie},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_admin"] is True


# ---------------------------------------------------------------------------
# POST /api/auth/password
# ---------------------------------------------------------------------------


async def test_change_password_happy_path(
    client: httpx.AsyncClient,
) -> None:
    """Change password with correct old_password → 200."""
    uname = f"cp_{secrets.token_hex(6)}"
    pw = "old-pass-ok"
    _, cookie = await register_and_login(client, uname, pw)
    resp = await client.post(
        "/api/auth/password",
        data={"old_password": pw, "new_password": "new-pass-ok-2"},
        cookies={"lmchat_session": cookie},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"


async def test_change_password_unauthenticated_401(
    client: httpx.AsyncClient,
) -> None:
    """POST /password without session → 401."""
    resp = await client.post(
        "/api/auth/password",
        data={"old_password": "x", "new_password": "y"},
    )
    assert resp.status_code == 401, resp.text


async def test_change_password_wrong_old_password(
    client: httpx.AsyncClient,
) -> None:
    """Wrong old_password → 400 'old password is incorrect'."""
    uname = f"cpw_{secrets.token_hex(6)}"
    _, cookie = await register_and_login(client, uname)
    resp = await client.post(
        "/api/auth/password",
        data={"old_password": "WRONG-OLD", "new_password": "new-pass-ok"},
        cookies={"lmchat_session": cookie},
    )
    assert resp.status_code == 400, resp.text
    assert "incorrect" in resp.json().get("detail", "").lower()


# ---------------------------------------------------------------------------
# POST /api/auth/totp/setup
# ---------------------------------------------------------------------------


async def test_totp_setup_happy_path(
    client: httpx.AsyncClient,
) -> None:
    """totp/setup → 200, provisioning_uri + secret in body."""
    uname = f"ts_{secrets.token_hex(6)}"
    _, cookie = await register_and_login(client, uname)
    resp = await client.post(
        "/api/auth/totp/setup",
        cookies={"lmchat_session": cookie},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "provisioning_uri" in body
    assert "secret" in body
    assert body["provisioning_uri"].startswith("otpauth://totp/")


async def test_totp_setup_unauthenticated_401(
    client: httpx.AsyncClient,
) -> None:
    """totp/setup without session → 401."""
    resp = await client.post("/api/auth/totp/setup")
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# POST /api/auth/totp/verify
# ---------------------------------------------------------------------------


async def test_totp_verify_happy_path(
    client: httpx.AsyncClient,
) -> None:
    """totp/verify with a valid TOTP code → 200, secret persisted."""
    uname = f"tv_{secrets.token_hex(6)}"
    _, cookie = await register_and_login(client, uname)

    setup_resp = await client.post(
        "/api/auth/totp/setup",
        cookies={"lmchat_session": cookie},
    )
    assert setup_resp.status_code == 200
    secret = setup_resp.json()["secret"]
    code = pyotp.TOTP(secret).now()

    resp = await client.post(
        "/api/auth/totp/verify",
        data={"secret": secret, "code": code},
        cookies={"lmchat_session": cookie},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"


async def test_totp_verify_unauthenticated_401(
    client: httpx.AsyncClient,
) -> None:
    """totp/verify without session → 401."""
    resp = await client.post(
        "/api/auth/totp/verify",
        data={"secret": "FAKESECRET", "code": "123456"},
    )
    assert resp.status_code == 401, resp.text


async def test_totp_verify_invalid_code(
    client: httpx.AsyncClient,
) -> None:
    """totp/verify with wrong code → 400 'invalid TOTP code'."""
    uname = f"tvi_{secrets.token_hex(6)}"
    _, cookie = await register_and_login(client, uname)

    setup_resp = await client.post(
        "/api/auth/totp/setup",
        cookies={"lmchat_session": cookie},
    )
    secret = setup_resp.json()["secret"]

    resp = await client.post(
        "/api/auth/totp/verify",
        data={"secret": secret, "code": "000000"},
        cookies={"lmchat_session": cookie},
    )
    assert resp.status_code == 400, resp.text
    assert "invalid" in resp.json().get("detail", "").lower()


# ---------------------------------------------------------------------------
# POST /api/auth/totp/disable
# ---------------------------------------------------------------------------


async def _enable_totp(
    client: httpx.AsyncClient, cookie: str
) -> str:
    """Set up and verify TOTP for the session; return the raw secret."""
    setup_resp = await client.post(
        "/api/auth/totp/setup",
        cookies={"lmchat_session": cookie},
    )
    assert setup_resp.status_code == 200
    secret = setup_resp.json()["secret"]
    code = pyotp.TOTP(secret).now()
    verify_resp = await client.post(
        "/api/auth/totp/verify",
        data={"secret": secret, "code": code},
        cookies={"lmchat_session": cookie},
    )
    assert verify_resp.status_code == 200
    return secret


async def test_totp_disable_happy_path(
    client: httpx.AsyncClient,
) -> None:
    """totp/disable with correct password → 200."""
    uname = f"td_{secrets.token_hex(6)}"
    pw = "disable-totp-pw"
    _, cookie = await register_and_login(client, uname, pw)
    await _enable_totp(client, cookie)

    resp = await client.post(
        "/api/auth/totp/disable",
        data={"password": pw},
        cookies={"lmchat_session": cookie},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"


async def test_totp_disable_unauthenticated_401(
    client: httpx.AsyncClient,
) -> None:
    """totp/disable without session → 401."""
    resp = await client.post(
        "/api/auth/totp/disable",
        data={"password": "pw"},
    )
    assert resp.status_code == 401, resp.text


async def test_totp_disable_wrong_password(
    client: httpx.AsyncClient,
) -> None:
    """totp/disable with wrong password → 400."""
    uname = f"tdw_{secrets.token_hex(6)}"
    pw = "correct-disable-pw"
    _, cookie = await register_and_login(client, uname, pw)
    await _enable_totp(client, cookie)

    resp = await client.post(
        "/api/auth/totp/disable",
        data={"password": "WRONG-PASSWORD"},
        cookies={"lmchat_session": cookie},
    )
    assert resp.status_code == 400, resp.text


# ---------------------------------------------------------------------------
# Cross-user auth boundary
# ---------------------------------------------------------------------------


async def test_cross_user_gets_401_not_data(
    live_servers: dict,
) -> None:
    """Without a valid session cookie /me returns 401, not another user's data.

    Uses a fresh client (no persisted cookies) to verify the unauthenticated
    path and a cookie-explicit request for the authenticated path.

    User B is created via direct DB insert (bypasses the single-admin
    registration gate) so this test is not sensitive to how many users
    already exist in the shared session DB.
    """
    base_url = live_servers["base_url"]
    uname_b = f"xub_{secrets.token_hex(6)}"
    pw_b = "cross-user-pw"

    # Create user B via the gate-aware helper (direct DB insert).
    async with httpx.AsyncClient(
        base_url=base_url, follow_redirects=True, timeout=15.0
    ) as fresh:
        await register_user(fresh, uname_b, pw_b)
        login_resp = await fresh.post(
            "/api/auth/login",
            data={"username": uname_b, "password": pw_b},
        )
        assert login_resp.status_code == 200
        cookie_b: str = login_resp.cookies.get("lmchat_session") or ""

    # Without any cookie → 401 (use a second independent client).
    async with httpx.AsyncClient(
        base_url=base_url, follow_redirects=True, timeout=15.0
    ) as no_auth:
        resp_no_cookie = await no_auth.get("/api/auth/me")
        assert resp_no_cookie.status_code == 401, resp_no_cookie.text

    # With B's valid cookie → B's own profile (auth layer accepts it).
    async with httpx.AsyncClient(
        base_url=base_url, follow_redirects=True, timeout=15.0
    ) as with_b:
        with_b.cookies.set("lmchat_session", cookie_b)
        resp_with_b = await with_b.get("/api/auth/me")
        assert resp_with_b.status_code == 200, resp_with_b.text
        assert resp_with_b.json()["username"] == uname_b
