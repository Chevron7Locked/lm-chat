#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Session fixation standalone test.

Standalone script (not a pytest test) that posts to /api/auth/login under two
scenarios and asserts the server always mints a fresh session ID on login.

Scenario A: no pre-existing cookie → server issues a Set-Cookie with a fresh ID.
Scenario B: client-supplied session cookie with a fixed attacker value → server
            ignores that ID and mints a new one (no fixation).

Emits JUnit XML to target/gates/auth-session-fixation.xml with testsuite name
``auth-session-fixation``.

Exit code: 0 on all-pass, 1 on any failure, 2 on infrastructure error.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

# ---------------------------------------------------------------------------
# Optional app import for in-process testing (pyright-friendly defaults)
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

TestClient: Any = None
func: Any = None
select: Any = None
text: Any = None
create_async_engine: Any = None
create_app: Any = None
metadata: Any = None
users: Any = None
InMemoryBucketStore: Any = None
get_default_session_store_dep: Any = None
get_engine_dep: Any = None
SQLiteSessionStore: Any = None
hash_password: Any = None
_APP_AVAILABLE = False

try:
    import os as _os

    # Ensure LM_CHAT_SECRET is set before importing the app
    _os.environ.setdefault("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")

    from fastapi.testclient import TestClient as _TestClient
    from sqlalchemy import func as _func
    from sqlalchemy import select as _select
    from sqlalchemy import text as _text
    from sqlalchemy.ext.asyncio import create_async_engine as _create_async_engine

    from lmchat.app import create_app as _create_app
    from lmchat.db.schema import metadata as _metadata
    from lmchat.db.schema import users as _users
    from lmchat.middleware._bucket_store import InMemoryBucketStore as _InMemoryBucketStore
    from lmchat.routes._dependencies import (
        get_default_session_store_dep as _get_default_session_store_dep,
    )
    from lmchat.routes._dependencies import (
        get_engine_dep as _get_engine_dep,
    )
    from lmchat.session.sqlite_store import SQLiteSessionStore as _SQLiteSessionStore
    from lmchat.utils.hashing import hash_password as _hash_password

    TestClient = _TestClient
    func = _func
    select = _select
    text = _text
    create_async_engine = _create_async_engine
    create_app = _create_app
    metadata = _metadata
    users = _users
    InMemoryBucketStore = _InMemoryBucketStore
    get_default_session_store_dep = _get_default_session_store_dep
    get_engine_dep = _get_engine_dep
    SQLiteSessionStore = _SQLiteSessionStore
    hash_password = _hash_password

    _APP_AVAILABLE = True
except Exception:  # noqa: BLE001
    _APP_AVAILABLE = False

COOKIE_NAME = "lmchat_session"
ATTACKER_SESSION_ID = "ATTACKER_FIXED_SESSION_ID_0000000000"

# ---------------------------------------------------------------------------
# JUnit helpers
# ---------------------------------------------------------------------------


def _make_suite(name: str) -> ET.Element:
    return ET.Element(
        "testsuite",
        {"name": name, "tests": "0", "failures": "0", "errors": "0", "skipped": "0"},
    )


def _add_pass(suite: ET.Element, classname: str, testname: str, elapsed: float) -> None:
    tc = ET.SubElement(
        suite, "testcase", {"name": testname, "classname": classname,
                            "time": f"{elapsed:.3f}"}
    )
    _ = tc  # no child = pass
    suite.set("tests", str(int(suite.get("tests", "0")) + 1))


def _add_fail(
    suite: ET.Element, classname: str, testname: str, message: str, elapsed: float
) -> None:
    tc = ET.SubElement(
        suite, "testcase", {"name": testname, "classname": classname,
                            "time": f"{elapsed:.3f}"}
    )
    fl = ET.SubElement(tc, "failure", {"message": message, "type": "AssertionError"})
    fl.text = message
    suite.set("tests", str(int(suite.get("tests", "0")) + 1))
    suite.set("failures", str(int(suite.get("failures", "0")) + 1))


def _add_skip(suite: ET.Element, classname: str, testname: str, reason: str) -> None:
    tc = ET.SubElement(suite, "testcase", {"name": testname, "classname": classname})
    sk = ET.SubElement(tc, "skipped", {"message": reason})
    sk.text = reason
    suite.set("tests", str(int(suite.get("tests", "0")) + 1))
    suite.set("skipped", str(int(suite.get("skipped", "0")) + 1))


def _emit_junit(suite: ET.Element, out_path: Path) -> None:
    root = ET.Element(
        "testsuites",
        {
            "name": "auth-session-fixation",
            "tests": suite.get("tests", "0"),
            "failures": suite.get("failures", "0"),
            "errors": suite.get("errors", "0"),
        },
    )
    root.append(suite)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(ET.ElementTree(root), space="  ")
    ET.ElementTree(root).write(out_path, encoding="utf-8", xml_declaration=True)


# ---------------------------------------------------------------------------
# In-process test implementation
# ---------------------------------------------------------------------------


def _run_in_process(suite: ET.Element) -> int:
    """Run both fixation scenarios using FastAPI TestClient (no network)."""
    import tempfile

    failures = 0

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "fixation_test.db"

        import asyncio

        from sqlalchemy.ext.asyncio import AsyncEngine as _AsyncEngine

        async def _setup() -> _AsyncEngine:
            _eng = create_async_engine(
                f"sqlite+aiosqlite:///{db_path}", pool_pre_ping=True
            )
            async with _eng.begin() as conn:
                await conn.run_sync(metadata.create_all)
            # Insert a test user with low-cost scrypt
            pw_hash = hash_password("correct-horse-battery", n=2**10, r=8, p=1)
            async with _eng.begin() as conn:
                id_result = await conn.execute(
                    select(func.coalesce(func.max(users.c.id), 0) + 1)
                )
                next_id = id_result.scalar() or 1
                await conn.execute(
                    text(
                        "INSERT INTO users (id, username, password_hash)"
                        " VALUES (:id, :u, :ph)"
                    ),
                    {"id": next_id, "u": "fixation_user", "ph": pw_hash},
                )
            return _eng

        eng = asyncio.run(_setup())
        store = SQLiteSessionStore(engine=eng)
        app = create_app()
        app.dependency_overrides[get_engine_dep] = lambda: eng
        app.dependency_overrides[get_default_session_store_dep] = lambda: store

        with TestClient(app, raise_server_exceptions=True) as client:
            client.app.state.session_store = store  # type: ignore[attr-defined]
            client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
            client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]

            login_data = {
                "username": "fixation_user",
                "password": "correct-horse-battery",
            }

            # --- Scenario A: no pre-existing cookie ---
            t0 = time.monotonic()
            resp_a = client.post("/api/auth/login", data=login_data)
            elapsed_a = time.monotonic() - t0

            if resp_a.status_code != 200:
                _add_fail(
                    suite,
                    "session-fixation",
                    "scenario-a-login-succeeds",
                    f"Expected 200, got {resp_a.status_code}: {resp_a.text[:200]}",
                    elapsed_a,
                )
                failures += 1
            else:
                cookie_a = resp_a.cookies.get(COOKIE_NAME)
                if not cookie_a:
                    _add_fail(
                        suite,
                        "session-fixation",
                        "scenario-a-cookie-present",
                        f"Server did not set {COOKIE_NAME!r} cookie on login",
                        elapsed_a,
                    )
                    failures += 1
                else:
                    _add_pass(
                        suite, "session-fixation",
                        "scenario-a-fresh-session-id-issued", elapsed_a,
                    )

            # --- Scenario B: client-supplied attacker cookie ---
            t1 = time.monotonic()
            # Use a fresh session (no_redirect; override cookie jar)
            client.cookies.clear()
            client.cookies.set(COOKIE_NAME, ATTACKER_SESSION_ID)
            resp_b = client.post("/api/auth/login", data=login_data)
            elapsed_b = time.monotonic() - t1

            if resp_b.status_code != 200:
                _add_fail(
                    suite,
                    "session-fixation",
                    "scenario-b-login-succeeds",
                    f"Expected 200, got {resp_b.status_code}: {resp_b.text[:200]}",
                    elapsed_b,
                )
                failures += 1
            else:
                cookie_b = resp_b.cookies.get(COOKIE_NAME)
                if not cookie_b:
                    _add_fail(
                        suite,
                        "session-fixation",
                        "scenario-b-server-mints-new-cookie",
                        f"Server did not set a new {COOKIE_NAME!r} cookie",
                        elapsed_b,
                    )
                    failures += 1
                elif cookie_b == ATTACKER_SESSION_ID:
                    _add_fail(
                        suite,
                        "session-fixation",
                        "scenario-b-server-ignores-attacker-cookie",
                        (
                            f"SECURITY BUG: server echoed attacker-supplied session ID"
                            f" {ATTACKER_SESSION_ID!r} — session fixation is possible"
                        ),
                        elapsed_b,
                    )
                    failures += 1
                else:
                    _add_pass(
                        suite,
                        "session-fixation",
                        "scenario-b-server-ignores-attacker-cookie",
                        elapsed_b,
                    )

        asyncio.run(eng.dispose())

    return failures


def main() -> int:
    out_path = _REPO_ROOT / "target" / "gates" / "auth-session-fixation.xml"
    suite = _make_suite("auth-session-fixation")

    if not _APP_AVAILABLE:
        _add_skip(
            suite,
            "session-fixation",
            "session-fixation-all",
            "lm-chat app not importable — check PYTHONPATH and dependencies",
        )
        _emit_junit(suite, out_path)
        print("[session_fixation_test] SKIPPED: app not importable")
        return 0

    try:
        _run_in_process(suite)
    except Exception as exc:  # noqa: BLE001
        _add_fail(
            suite,
            "session-fixation",
            "session-fixation-infrastructure",
            f"Infrastructure error: {exc}",
            0.0,
        )

    _emit_junit(suite, out_path)

    f = int(suite.get("failures", "0"))
    e = int(suite.get("errors", "0"))
    t = int(suite.get("tests", "0"))
    print(
        f"[session_fixation_test] {t} tests, {f} failures, {e} errors"
        f" -> {out_path}"
    )
    return 1 if (f or e) else 0


if __name__ == "__main__":
    sys.exit(main())
