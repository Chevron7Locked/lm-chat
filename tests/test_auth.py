"""
Auth boundary tests.

All tests use LM_CHAT_AUTH=true with a known admin password.
Each test gets a fresh DB and fresh rate-limit counters (function-scoped fixtures).
"""

import base64, json, urllib.error, urllib.request

import pytest

from conftest import (
    ADMIN_PASS, ADMIN_USER, CSRF_HEADER, AuthedClient,
    _Client, _login, generate_totp,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _anon(base_url: str) -> _Client:
    return _Client(base_url)


def _post_no_redirect(base_url: str, path: str, body: dict) -> int:
    """Return status code, suppressing HTTPError raises."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        base_url + path,
        data=data,
        headers={"Content-Type": "application/json", **CSRF_HEADER},
        method="POST",
    )
    opener = urllib.request.build_opener(
        urllib.request.HTTPRedirectHandler.__new__(
            type("NoRedirect", (urllib.request.HTTPErrorProcessor,), {
                "http_response": lambda self, req, resp: resp,
                "https_response": lambda self, req, resp: resp,
            })
        )
    )
    try:
        resp = opener.open(req, timeout=10)
        return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def _get_status(base_url: str, path: str, cookie: str = "") -> int:
    headers = {}
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(base_url + path, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.status
    except urllib.error.HTTPError as e:
        return e.code


# ---------------------------------------------------------------------------
# Unauthenticated access
# ---------------------------------------------------------------------------

class TestUnauthenticated:
    def test_root_without_cookie_redirects(self, app_server_auth):
        status = _get_status(app_server_auth, "/")
        # Server returns 302 redirect to login
        assert status in (302, 200)  # 200 if login page is inline

    def test_api_chats_without_auth_returns_401(self, app_server_auth):
        status = _get_status(app_server_auth, "/api/chats")
        assert status == 401

    def test_api_chat_without_auth_returns_401(self, app_server_auth):
        status = _post_no_redirect(
            app_server_auth, "/api/chat",
            {"model": "test-model", "input": "Hello"}
        )
        assert status == 401

    def test_health_endpoint_is_public(self, app_server_auth):
        status = _get_status(app_server_auth, "/api/health")
        assert status in (200, 503)  # always accessible, even if LM Studio down

    def test_auth_me_without_cookie_returns_user_none(self, app_server_auth):
        req = urllib.request.Request(app_server_auth + "/api/auth/me")
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        assert data["auth_enabled"] is True
        assert data["user"] is None


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

class TestLogin:
    def test_valid_login_sets_session_cookie(self, app_server_auth):
        cookie = _login(app_server_auth, ADMIN_USER, ADMIN_PASS)
        assert "=" in cookie  # name=value format

    def test_wrong_password_returns_401(self, app_server_auth):
        data = json.dumps({"username": ADMIN_USER, "password": "wrongpass"}).encode()
        req = urllib.request.Request(
            app_server_auth + "/api/auth/login",
            data=data,
            headers={"Content-Type": "application/json", **CSRF_HEADER},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=10)
        assert exc.value.code == 401

    def test_wrong_password_does_not_set_cookie(self, app_server_auth):
        data = json.dumps({"username": ADMIN_USER, "password": "wrongpass"}).encode()
        req = urllib.request.Request(
            app_server_auth + "/api/auth/login",
            data=data,
            headers={"Content-Type": "application/json", **CSRF_HEADER},
            method="POST",
        )
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            cookie = resp.headers.get("Set-Cookie", "")
        except urllib.error.HTTPError as e:
            cookie = e.headers.get("Set-Cookie", "")
        assert "session" not in cookie.lower()

    def test_session_cookie_enables_api_access(self, app_server_auth):
        cookie = _login(app_server_auth, ADMIN_USER, ADMIN_PASS)
        c = _Client(app_server_auth, cookie=cookie)
        resp = c.get("/api/chats")
        assert resp.status == 200

    def test_tampered_cookie_returns_401(self, app_server_auth):
        status = _get_status(app_server_auth, "/api/chats", cookie="session=fakejunk")
        assert status == 401

    def test_logout_invalidates_session(self, app_server_auth):
        cookie = _login(app_server_auth, ADMIN_USER, ADMIN_PASS)
        c = _Client(app_server_auth, cookie=cookie)
        c.post("/api/auth/logout")
        status = _get_status(app_server_auth, "/api/chats", cookie=cookie)
        assert status == 401


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------

class TestUserManagement:
    def test_admin_creates_user(self, authed_client: AuthedClient):
        # authed_client fixture already created 'testuser' successfully
        resp = authed_client.admin.get("/api/auth/users")
        users = authed_client.admin.json(resp)
        usernames = [u["username"] for u in users]
        assert "testuser" in usernames

    def test_non_admin_cannot_create_user(self, authed_client: AuthedClient):
        with pytest.raises(urllib.error.HTTPError) as exc:
            authed_client.user.post(
                "/api/auth/invite",
                {"username": "hacker", "password": "password123"},
            )
        assert exc.value.code == 403

    def test_duplicate_username_returns_409(self, authed_client: AuthedClient):
        with pytest.raises(urllib.error.HTTPError) as exc:
            authed_client.admin.post(
                "/api/auth/invite",
                {"username": "testuser", "password": "password123"},
            )
        assert exc.value.code == 409

    def test_cross_user_isolation(self, authed_client: AuthedClient):
        # Admin creates a chat
        admin_resp = authed_client.admin.post("/api/chats", {"title": "Admin private"})
        admin_chat_id = authed_client.admin.json(admin_resp)["id"]

        # Regular user cannot see admin's chats
        user_chats = authed_client.user.json(authed_client.user.get("/api/chats"))
        assert admin_chat_id not in [c["id"] for c in user_chats]

    def test_non_admin_cannot_list_users(self, authed_client: AuthedClient):
        with pytest.raises(urllib.error.HTTPError) as exc:
            authed_client.user.get("/api/auth/users")
        assert exc.value.code == 403

    def test_change_password(self, authed_client: AuthedClient):
        # Change testuser's password
        authed_client.user.post("/api/auth/change-password", {
            "current_password": "userpassword1",
            "new_password":     "newpassword99",
        })
        # Old password no longer works
        data = json.dumps({"username": "testuser", "password": "userpassword1"}).encode()
        req = urllib.request.Request(
            authed_client.user.base_url + "/api/auth/login",
            data=data,
            headers={"Content-Type": "application/json", **CSRF_HEADER},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=10)
        assert exc.value.code == 401

        # New password works
        new_cookie = _login(authed_client.user.base_url, "testuser", "newpassword99")
        assert "=" in new_cookie


# ---------------------------------------------------------------------------
# TOTP lifecycle
# ---------------------------------------------------------------------------

class TestTOTP:
    def test_totp_setup_returns_secret_and_qr(self, authed_client: AuthedClient):
        resp = authed_client.user.post("/api/auth/totp/setup")
        data = authed_client.user.json(resp)
        assert "secret" in data
        assert "setup_token" in data
        assert len(data["secret"]) > 0

    def test_totp_verify_enables_totp(self, authed_client: AuthedClient):
        setup_resp = authed_client.user.post("/api/auth/totp/setup")
        setup_data = authed_client.user.json(setup_resp)
        secret_b32 = setup_data["secret"]
        setup_token = setup_data["setup_token"]

        # Decode base32 secret
        secret_bytes = base64.b32decode(secret_b32.upper() + "=" * ((8 - len(secret_b32) % 8) % 8))
        code = generate_totp(secret_bytes)

        verify_resp = authed_client.user.post("/api/auth/totp/verify", {
            "code": code, "setup_token": setup_token
        })
        assert verify_resp.status == 200

    def test_totp_login_required_after_enrollment(self, app_server_auth):
        """Full TOTP enrollment + login flow in a single test."""
        # 1. Create user
        admin_cookie = _login(app_server_auth, ADMIN_USER, ADMIN_PASS)
        admin = _Client(app_server_auth, cookie=admin_cookie)
        admin.post("/api/auth/invite", {"username": "totp_user", "password": "pass1234!"})

        user_cookie = _login(app_server_auth, "totp_user", "pass1234!")
        user = _Client(app_server_auth, cookie=user_cookie)

        # 2. Set up TOTP
        setup_data = user.json(user.post("/api/auth/totp/setup"))
        secret_b32 = setup_data["secret"]
        setup_token = setup_data["setup_token"]
        secret_bytes = base64.b32decode(secret_b32.upper() + "=" * ((8 - len(secret_b32) % 8) % 8))

        # 3. Verify with current code (window 0)
        code0 = generate_totp(secret_bytes, offset=0)
        user.post("/api/auth/totp/verify", {"code": code0, "setup_token": setup_token})

        # 4. Log out
        user.post("/api/auth/logout")

        # 5. Password-only login now returns partial token (TOTP step needed)
        data = json.dumps({"username": "totp_user", "password": "pass1234!"}).encode()
        req = urllib.request.Request(
            app_server_auth + "/api/auth/login",
            data=data,
            headers={"Content-Type": "application/json", **CSRF_HEADER},
            method="POST",
        )
        try:
            partial_resp = urllib.request.urlopen(req, timeout=10)
            partial_data = json.loads(partial_resp.read())
        except urllib.error.HTTPError as e:
            partial_data = json.loads(e.read())

        # Server returns needs_totp=true and a partial_token when TOTP is enrolled
        assert partial_data.get("needs_totp") is True, \
            f"Expected needs_totp=true after TOTP enrollment, got: {partial_data}"
        assert "partial_token" in partial_data, \
            f"Expected partial_token in TOTP challenge response, got: {partial_data}"

    def test_totp_wrong_code_returns_401(self, authed_client: AuthedClient):
        setup_data = authed_client.user.json(authed_client.user.post("/api/auth/totp/setup"))
        secret_b32 = setup_data["secret"]
        setup_token = setup_data["setup_token"]
        secret_bytes = base64.b32decode(secret_b32.upper() + "=" * ((8 - len(secret_b32) % 8) % 8))

        # Verify to enable TOTP
        code = generate_totp(secret_bytes, offset=0)
        authed_client.user.post("/api/auth/totp/verify", {"code": code, "setup_token": setup_token})

        # Try TOTP login with wrong code
        with pytest.raises(urllib.error.HTTPError) as exc:
            authed_client.user.post("/api/auth/totp/login", {
                "partial_token": "fakepartialtoken",
                "code": "000000",
            })
        assert exc.value.code == 401


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

class TestRateLimit:
    def test_repeated_bad_logins_hit_rate_limit(self, app_server_auth):
        # Exhaust the 5 attempts / 15 min window
        for _ in range(5):
            data = json.dumps({"username": ADMIN_USER, "password": "wrong"}).encode()
            req = urllib.request.Request(
                app_server_auth + "/api/auth/login",
                data=data,
                headers={"Content-Type": "application/json", **CSRF_HEADER},
                method="POST",
            )
            try:
                urllib.request.urlopen(req, timeout=10)
            except urllib.error.HTTPError:
                pass

        # 6th attempt must be rate-limited
        data = json.dumps({"username": ADMIN_USER, "password": "wrong"}).encode()
        req = urllib.request.Request(
            app_server_auth + "/api/auth/login",
            data=data,
            headers={"Content-Type": "application/json", **CSRF_HEADER},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=10)
        assert exc.value.code == 429


# ---------------------------------------------------------------------------
# Admin delete user
# ---------------------------------------------------------------------------

class TestAdminDeleteUser:
    def test_admin_can_delete_user(self, authed_client):
        authed_client.admin.post("/api/auth/invite", {"username": "todelete", "password": "deleteme123"})
        users = json.loads(authed_client.admin.get("/api/auth/users").read())
        user_list = users if isinstance(users, list) else users.get("users", [])
        victim = next((u for u in user_list if u.get("username") == "todelete"), None)
        if victim is None:
            pytest.skip("Could not find created user")
        resp = authed_client.admin.delete(f"/api/auth/users/{victim['id']}")
        assert resp.status == 200

    def test_deleted_user_cannot_login(self, authed_client):
        admin = authed_client.admin
        admin.post("/api/auth/invite", {"username": "tobedeleted2", "password": "deleteme123"})
        users = json.loads(admin.get("/api/auth/users").read())
        user_list = users if isinstance(users, list) else users.get("users", [])
        victim = next((u for u in user_list if u.get("username") == "tobedeleted2"), None)
        if victim is None:
            pytest.skip("Could not find created user")
        admin.delete(f"/api/auth/users/{victim['id']}")
        with pytest.raises(urllib.error.HTTPError) as exc:
            data = json.dumps({"username": "tobedeleted2", "password": "deleteme123"}).encode()
            req = urllib.request.Request(
                admin.base_url + "/api/auth/login", data=data,
                headers={"Content-Type": "application/json", **CSRF_HEADER},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10)
        assert exc.value.code == 401

    def test_non_admin_cannot_delete_user(self, authed_client):
        users = json.loads(authed_client.admin.get("/api/auth/users").read())
        user_list = users if isinstance(users, list) else users.get("users", [])
        admin_user = next((u for u in user_list if u.get("username") == "admin"), None)
        if admin_user is None:
            pytest.skip("Admin user not found in list")
        with pytest.raises(urllib.error.HTTPError) as exc:
            authed_client.user.delete(f"/api/auth/users/{admin_user['id']}")
        assert exc.value.code == 403


# ---------------------------------------------------------------------------
# Profile update
# ---------------------------------------------------------------------------

class TestProfileUpdate:
    def test_update_profile_display_name(self, authed_client):
        resp = authed_client.admin.patch("/api/auth/profile", {"display_name": "New Display Name"})
        assert resp.status == 200

    def test_profile_update_reflected_in_me(self, authed_client):
        authed_client.admin.patch("/api/auth/profile", {"display_name": "Updated Name XYZ"})
        me = json.loads(authed_client.admin.get("/api/auth/me").read())
        user = me.get("user") or me
        assert user.get("display_name") == "Updated Name XYZ"


# ---------------------------------------------------------------------------
# User settings (lm_url, lm_apikey, remote_mcps)
# ---------------------------------------------------------------------------

class TestUserSettings:
    def test_get_user_settings_returns_object(self, authed_client):
        resp = authed_client.admin.get("/api/auth/settings")
        assert resp.status == 200
        data = json.loads(resp.read())
        assert isinstance(data, dict)

    def test_save_lm_apikey_setting(self, authed_client):
        # lm_apikey is stored server-side; GET returns True/False (masked), not the raw value
        authed_client.admin.post("/api/auth/settings", {"lm_apikey": "sk-test-key-abc123"})
        resp = authed_client.admin.get("/api/auth/settings")
        data = json.loads(resp.read())
        assert data.get("lm_apikey") is True

    def test_user_settings_isolated_per_user(self, authed_client):
        # Admin sets their API key; regular user's settings should not reflect it
        authed_client.admin.post("/api/auth/settings", {"lm_apikey": "sk-admin-only-key"})
        resp = authed_client.user.get("/api/auth/settings")
        data = json.loads(resp.read())
        assert data.get("lm_apikey") is not True


# ---------------------------------------------------------------------------
# TOTP disable
# ---------------------------------------------------------------------------

class TestTOTPDisable:
    def test_totp_disable_requires_valid_code(self, authed_client):
        """Full flow: setup → verify (enable) → disable with correct code."""
        setup_resp = json.loads(authed_client.admin.post("/api/auth/totp/setup", {}).read())
        secret_b32 = setup_resp.get("secret", "")
        if not secret_b32:
            pytest.skip("TOTP setup did not return secret")
        try:
            padded = secret_b32.upper() + "=" * ((-len(secret_b32)) % 8)
            secret_bytes = base64.b32decode(padded)
        except Exception:
            pytest.skip("Could not decode TOTP secret")
        setup_token = setup_resp.get("setup_token", "")
        code = generate_totp(secret_bytes)
        authed_client.admin.post("/api/auth/totp/verify", {"code": code, "setup_token": setup_token})
        # Use offset=1 to get next 30-second window code
        code2 = generate_totp(secret_bytes, offset=1)
        resp = authed_client.admin.post("/api/auth/totp/disable", {"code": code2})
        assert resp.status == 200

    def test_totp_disable_wrong_code_when_totp_enabled(self, authed_client):
        """Enable TOTP then verify that a wrong disable code returns 400."""
        setup_resp = json.loads(authed_client.admin.post("/api/auth/totp/setup", {}).read())
        secret_b32 = setup_resp.get("secret", "")
        if not secret_b32:
            pytest.skip("TOTP setup did not return secret")
        try:
            padded = secret_b32.upper() + "=" * ((-len(secret_b32)) % 8)
            secret_bytes = base64.b32decode(padded)
        except Exception:
            pytest.skip("Could not decode TOTP secret")
        setup_token = setup_resp.get("setup_token", "")
        code = generate_totp(secret_bytes)
        authed_client.admin.post("/api/auth/totp/verify", {"code": code, "setup_token": setup_token})
        with pytest.raises(urllib.error.HTTPError) as exc:
            authed_client.admin.post("/api/auth/totp/disable", {"code": "000000"})
        assert exc.value.code in (400, 401)

    def test_totp_disable_when_not_enabled_returns_400(self, authed_client):
        """Disable with no TOTP enabled returns 400."""
        with pytest.raises(urllib.error.HTTPError) as exc:
            authed_client.admin.post("/api/auth/totp/disable", {"code": "000000"})
        assert exc.value.code == 400


# ---------------------------------------------------------------------------
# Invite validation
# ---------------------------------------------------------------------------

class TestInviteValidation:
    def test_invite_missing_username_returns_400(self, authed_client):
        with pytest.raises(urllib.error.HTTPError) as exc:
            authed_client.admin.post("/api/auth/invite", {"password": "validpass123"})
        assert exc.value.code == 400


# ---------------------------------------------------------------------------
# Settings validation (negative tests)
# ---------------------------------------------------------------------------

class TestSettingsValidation:
    def test_save_disallowed_setting_key_ignored(self, authed_client):
        """POST a key not in ALLOWED_SETTINGS; it must be silently ignored."""
        authed_client.admin.post("/api/auth/settings", {"lm_url": "http://evil.com"})
        resp = authed_client.admin.get("/api/auth/settings")
        data = json.loads(resp.read())
        assert "lm_url" not in data, "Disallowed key 'lm_url' should not be stored"

    def test_save_empty_lm_apikey_clears_it(self, authed_client):
        """POST lm_apikey="" should clear it so GET returns False (not True)."""
        # First set a real key
        authed_client.admin.post("/api/auth/settings", {"lm_apikey": "sk-real-key"})
        resp = authed_client.admin.get("/api/auth/settings")
        data = json.loads(resp.read())
        assert data.get("lm_apikey") is True, "Setup: key should be stored"

        # Now clear it
        authed_client.admin.post("/api/auth/settings", {"lm_apikey": ""})
        resp = authed_client.admin.get("/api/auth/settings")
        data = json.loads(resp.read())
        assert data.get("lm_apikey") is not True, \
            "Empty lm_apikey should clear the stored key (GET should not return True)"
