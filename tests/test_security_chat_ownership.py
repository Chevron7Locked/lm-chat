"""Chat ownership enforcement (auth-on and auth-off modes).

Pre-0.5.0, ``_verify_chat_owner`` short-circuited on ``AUTH_ENABLED=false``
and ``_user_filter`` returned an empty WHERE clause — so a database
populated under auth-enabled multi-user mode would leak every user's chats
to every visitor the moment an operator flipped ``LM_CHAT_AUTH=false``.

The fix is in two places:
  * ``_verify_chat_owner`` and ``_user_filter`` now always require a
    user_id match.  In auth-disabled mode, every request comes from
    ``user_id == "default"`` so they correctly see only the default user's
    rows.
  * The startup gate in ``__main__`` refuses to launch ``AUTH=false`` if
    the DB already contains non-default users — operators must explicitly
    migrate to a fresh DB.

Tests:
  * Multi-user data is invisible across users in auth-on mode (regression).
  * Multi-user data is **not** leaked when auth is toggled off (the bug).
  * Startup refuses to bring up an auth-disabled server on a populated DB.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
import uuid
from pathlib import Path

from conftest import ADMIN_PASS, ADMIN_USER, CSRF_HEADER, _free_port


# ---------------------------------------------------------------------------
# Auth-on cross-user isolation (regression — already worked)
# ---------------------------------------------------------------------------

def test_user_cannot_read_another_users_chat(make_inproc_server):
    """alice creates a chat — bob with a separate session must not see it."""
    srv = make_inproc_server()

    # Admin invites a regular user "alice" via the standard admin flow.
    admin_cookie = _login(srv.url, ADMIN_USER, ADMIN_PASS)
    _invite(srv.url, admin_cookie, "alice", "alicepassword12")
    _invite(srv.url, admin_cookie, "bob", "bobpassword1234")

    alice_cookie = _login(srv.url, "alice", "alicepassword12")
    bob_cookie = _login(srv.url, "bob", "bobpassword1234")

    chat_id = _create_chat(srv.url, alice_cookie, "alice's secret")

    # alice's own list contains the chat.
    alice_list = _list_chats(srv.url, alice_cookie)
    assert any(c["id"] == chat_id for c in alice_list)

    # bob's list does NOT contain it.
    bob_list = _list_chats(srv.url, bob_cookie)
    assert not any(c["id"] == chat_id for c in bob_list), (
        f"bob saw alice's chat in his list: {bob_list}"
    )

    # bob's direct GET on /api/chats/<id>/messages must 404.
    status = _get_messages_status(srv.url, bob_cookie, chat_id)
    assert status == 404, f"bob can read alice's messages: status={status}"


# ---------------------------------------------------------------------------
# Auth-off mode never leaks per-user data
#
# Setup a DB that *was* multi-user (rows with user_id != "default") and then
# point an auth-disabled server at it.  The startup gate refuses — that's
# the primary defence.  We assert the gate fires by running server.py as a
# subprocess and checking exit code + stderr.
# ---------------------------------------------------------------------------

def test_startup_refuses_auth_disabled_on_populated_db(tmp_path):
    """Auth-off mode + DB with non-default users → server exits with code 2."""
    db_path = tmp_path / "chats.db"
    logs_dir = tmp_path / "logs"

    # Seed the DB with a real user row.  Reproduces the operator scenario
    # where auth was enabled at some point and real users wrote data.
    _seed_multiuser_db(db_path, logs_dir)

    proc = subprocess.run(
        [sys.executable, str(Path(__file__).parent.parent / "server.py")],
        env={
            **os.environ,
            "PORT":         str(_free_port()),
            "LM_CHAT_DB":   str(db_path),
            "LM_CHAT_AUTH": "false",
            "LM_CHAT_LOGS": str(logs_dir),
            "LMSTUDIO_URL": "http://127.0.0.1:1",  # won't be reached
        },
        capture_output=True,
        timeout=10,
    )

    assert proc.returncode == 2, (
        f"expected exit 2 (refusal), got {proc.returncode}.\n"
        f"stdout: {proc.stdout!r}\n"
        f"stderr: {proc.stderr!r}"
    )
    output = (proc.stdout + proc.stderr).decode("utf-8", "replace")
    assert "REFUSING TO START" in output
    assert "multi-user" in output.lower()


def test_startup_allows_auth_disabled_on_empty_db(tmp_path, mock_lmstudio):
    """Fresh DB (no non-default users) → auth-disabled server starts fine."""
    # Use the existing inproc fixture machinery, but with auth=False.
    from conftest import _start_inproc_server

    handle = _start_inproc_server(
        mock_lmstudio_url=mock_lmstudio.url,
        db_path=str(tmp_path / "chats.db"),
        log_dir=str(tmp_path / "logs"),
        auth=False,
    )
    try:
        # Server is up and serving — the gate didn't fire.
        req = urllib.request.Request(handle.url + "/api/health")
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status in (200, 503)
    finally:
        handle.shutdown()


def test_auth_disabled_request_filter_scopes_to_default_user(tmp_path, mock_lmstudio):
    """Auth-off requests only see chats with user_id='default'.

    Even if some stray row exists under a different user_id (e.g. from a
    schema-level seeding), the filter must not surface it.
    """
    from conftest import _start_inproc_server

    db_path = tmp_path / "chats.db"
    handle = _start_inproc_server(
        mock_lmstudio_url=mock_lmstudio.url,
        db_path=str(db_path),
        log_dir=str(tmp_path / "logs"),
        auth=False,
    )
    try:
        # Insert a non-default chat row directly via the module's get_db.
        # This simulates a leftover row from a previous auth-enabled run.
        s = handle.module
        db = s.get_db()
        s.init_db()  # idempotent — ensures users + chats tables exist
        # Make sure a foreign-keyable user exists for the rogue chat.
        rogue_id = uuid.uuid4().hex
        db.execute(
            "INSERT OR IGNORE INTO users (id,username,password_hash,salt,display_name,is_admin,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (rogue_id, "alice-orphan", "x" * 128, "00" * 16, "Alice", 0, time.time()),
        )
        db.execute(
            "INSERT INTO chats (id,title,model,updated_at,user_id) VALUES (?,?,?,?,?)",
            ("c_rogue_99", "alice's chat", "test", time.time(), rogue_id),
        )
        # The default user's own chat — should be visible.
        db.execute(
            "INSERT INTO chats (id,title,model,updated_at,user_id) VALUES (?,?,?,?,?)",
            ("c_default_1", "default chat", "test", time.time(), "default"),
        )
        db.commit()

        # List chats as the anonymous (default) user.
        req = urllib.request.Request(handle.url + "/api/chats")
        with urllib.request.urlopen(req, timeout=5) as r:
            chats = json.loads(r.read())

        ids = [c["id"] for c in chats]
        assert "c_default_1" in ids, "default user's own chat missing from list"
        assert "c_rogue_99" not in ids, (
            f"auth-disabled filter leaked another user's chat: {ids}"
        )

        # Direct fetch on the rogue chat's messages must 404.
        try:
            urllib.request.urlopen(
                handle.url + "/api/chats/c_rogue_99/messages", timeout=5,
            )
            raise AssertionError("rogue chat is reachable by id under auth-disabled mode")
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        handle.shutdown()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _login(base_url: str, username: str, password: str) -> str:
    data = json.dumps({"username": username, "password": password}).encode()
    req = urllib.request.Request(
        base_url + "/api/auth/login",
        data=data,
        headers={"Content-Type": "application/json", **CSRF_HEADER},
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=10)
    return resp.headers.get("Set-Cookie", "").split(";")[0].strip()


def _invite(base_url: str, admin_cookie: str, username: str, password: str):
    body = json.dumps({"username": username, "password": password}).encode()
    req = urllib.request.Request(
        base_url + "/api/auth/invite",
        data=body,
        headers={"Content-Type": "application/json", "Cookie": admin_cookie, **CSRF_HEADER},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=5)


def _create_chat(base_url: str, cookie: str, title: str) -> str:
    body = json.dumps({"title": title}).encode()
    req = urllib.request.Request(
        base_url + "/api/chats",
        data=body,
        headers={"Content-Type": "application/json", "Cookie": cookie, **CSRF_HEADER},
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=5)
    return json.loads(resp.read())["id"]


def _list_chats(base_url: str, cookie: str):
    req = urllib.request.Request(
        base_url + "/api/chats", headers={"Cookie": cookie},
    )
    resp = urllib.request.urlopen(req, timeout=5)
    return json.loads(resp.read())


def _get_messages_status(base_url: str, cookie: str, chat_id: str) -> int:
    req = urllib.request.Request(
        base_url + f"/api/chats/{chat_id}/messages",
        headers={"Cookie": cookie},
    )
    try:
        urllib.request.urlopen(req, timeout=5)
        return 200
    except urllib.error.HTTPError as e:
        return e.code


def _seed_multiuser_db(db_path: Path, logs_dir: Path):
    """Create a DB with a real non-default user — for the startup-gate test."""
    # We import server.py with AUTH=true once just to call init_db so the
    # schema matches whatever the latest migrations need.  Then we insert
    # a user row by hand.
    import importlib
    saved = dict(os.environ)
    try:
        os.environ.update({
            "LM_CHAT_DB":   str(db_path),
            "LM_CHAT_AUTH": "true",
            "LM_CHAT_LOGS": str(logs_dir),
            "LM_CHAT_SECRET": "test-secret",
        })
        for m in list(sys.modules):
            if m == "server" or m.startswith("server."):
                del sys.modules[m]
        sys.path.insert(0, str(Path(__file__).parent.parent))
        import server  # noqa: F401
        srv = importlib.import_module("server")
        srv.init_db()
        db = srv.get_db()
        db.execute(
            "INSERT INTO users (id,username,password_hash,salt,display_name,is_admin,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                uuid.uuid4().hex,
                "alice",
                "scrypt$n=131072$r=8$p=1$" + "a" * 128,
                "00" * 16,
                "Alice",
                1,
                time.time(),
            ),
        )
        db.commit()
    finally:
        # Restore env so subsequent tests aren't poisoned.
        for k in ("LM_CHAT_DB", "LM_CHAT_AUTH", "LM_CHAT_LOGS", "LM_CHAT_SECRET"):
            if k in saved:
                os.environ[k] = saved[k]
            else:
                os.environ.pop(k, None)
        for m in list(sys.modules):
            if m == "server" or m.startswith("server."):
                del sys.modules[m]
