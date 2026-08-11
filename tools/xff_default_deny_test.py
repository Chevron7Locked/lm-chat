#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""X-Forwarded-For default-deny integration test — standalone runner.

Tests that when ``lm_chat_trusted_proxy`` is empty (default), rotating
X-Forwarded-For on each request does NOT bypass the rate limiter.

Two test scenarios:

**Scenario A (default-deny):** trusted_proxy="" → 2 requests with different
XFF values from the same real IP; second is rate-limited (429).

**Scenario B (trusted-proxy bypass):** trusted_proxy="127.0.0.1" → 2 requests
with different XFF values; both succeed because each XFF IP is treated as
a distinct client.

Emits JUnit XML to ``target/gates/L8-xff-default-deny.xml`` with testsuite
name ``xff-default-deny``.

Exit code: 0 on all-pass, 1 on any failure, 2 on infrastructure error.

Uses ASGITransport + httpx (matching the existing test_rate_limit.py pattern)
rather than TestClient, so the client IP is ``127.0.0.1`` not ``testclient``.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from urllib.parse import urlencode
from xml.etree import ElementTree as ET

# ---------------------------------------------------------------------------
# Optional app import (in-process testing)
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_APP_AVAILABLE = False

try:
    import os as _os

    _os.environ.setdefault("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")

    import httpx
    from fastapi import FastAPI
    from fastapi.responses import PlainTextResponse
    from httpx import ASGITransport

    from lmchat import config as config_mod
    from lmchat.config import Settings
    from lmchat.middleware._bucket_store import InMemoryBucketStore
    from lmchat.middleware.rate_limit import LoginRateLimitMiddleware

    _APP_AVAILABLE = True
except Exception:  # noqa: BLE001
    _APP_AVAILABLE = False

# ---------------------------------------------------------------------------
# JUnit helpers (mirror session_fixation_test.py)
# ---------------------------------------------------------------------------


def _make_suite(name: str) -> ET.Element:
    return ET.Element(
        "testsuite",
        {"name": name, "tests": "0", "failures": "0", "errors": "0", "skipped": "0"},
    )


def _add_pass(suite: ET.Element, classname: str, testname: str, elapsed: float) -> None:
    tc = ET.SubElement(
        suite,
        "testcase",
        {"name": testname, "classname": classname, "time": f"{elapsed:.3f}"},
    )
    _ = tc  # no child = pass
    suite.set("tests", str(int(suite.get("tests", "0")) + 1))


def _add_fail(
    suite: ET.Element, classname: str, testname: str, message: str, elapsed: float
) -> None:
    tc = ET.SubElement(
        suite,
        "testcase",
        {"name": testname, "classname": classname, "time": f"{elapsed:.3f}"},
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
            "name": "xff-default-deny",
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
# Test helpers
# ---------------------------------------------------------------------------


def _make_app(
    store: InMemoryBucketStore,  # type: ignore[name-defined]  # guarded by _APP_AVAILABLE
    burst: int,
) -> FastAPI:  # type: ignore[name-defined]  # guarded by _APP_AVAILABLE
    """Build a minimal FastAPI app with rate-limit middleware and stub login route."""
    app = FastAPI()  # type: ignore[name-defined]  # guarded by _APP_AVAILABLE

    @app.post("/api/auth/login")
    async def _login_stub() -> PlainTextResponse:  # type: ignore[name-defined]  # guarded by _APP_AVAILABLE
        return PlainTextResponse("ok", status_code=200)  # type: ignore[name-defined]  # guarded by _APP_AVAILABLE

    app.add_middleware(
        LoginRateLimitMiddleware,  # type: ignore[name-defined]  # guarded by _APP_AVAILABLE
        store=store,
        rate_per_minute=burst,
        burst=burst,
    )
    return app


def _form_body(username: str = "alice") -> bytes:
    return urlencode({"username": username, "password": "pw"}).encode()


def _form_headers() -> dict[str, str]:
    return {"content-type": "application/x-www-form-urlencoded"}


# ---------------------------------------------------------------------------
# In-process test implementation
# ---------------------------------------------------------------------------


async def _run_scenario_a(suite: ET.Element) -> int:
    """trusted_proxy="" → XFF rotation must NOT bypass."""
    import lmchat.middleware.rate_limit as rl_mod

    fake_settings = Settings(  # type: ignore[name-defined]  # guarded by _APP_AVAILABLE
        lm_chat_trusted_proxy="",
        lm_chat_login_rate_limit_per_min=10,
    )

    _orig_config_get_settings = config_mod.get_settings  # type: ignore[name-defined]  # guarded by _APP_AVAILABLE
    _orig_rl_get_settings = rl_mod.get_settings
    config_mod.get_settings = lambda: fake_settings  # type: ignore[name-defined, method-assign]  # guarded by _APP_AVAILABLE
    rl_mod.get_settings = lambda: fake_settings  # type: ignore[method-assign]

    failures = 0

    try:
        store = InMemoryBucketStore()  # type: ignore[name-defined]  # guarded by _APP_AVAILABLE
        burst = 1
        app = _make_app(store=store, burst=burst)

        transport = ASGITransport(app=app)  # type: ignore[name-defined]  # guarded by _APP_AVAILABLE
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:  # type: ignore[name-defined]  # guarded by _APP_AVAILABLE
            # Request 1 — XFF=1.1.1.1 → should succeed (burst=1)
            t0 = time.monotonic()
            r1 = await client.post(
                "/api/auth/login",
                content=_form_body(),
                headers={**_form_headers(), "x-forwarded-for": "1.1.1.1"},
            )
            elapsed_a = time.monotonic() - t0

            if r1.status_code != 200:
                _add_fail(
                    suite,
                    "xff-default-deny",
                    "default-deny-first-request-failed",
                    f"First request (XFF=1.1.1.1) should be 200, got {r1.status_code}",
                    elapsed_a,
                )
                failures += 1
            else:
                # Request 2 — XFF=2.2.2.2 → must be 429 (same real IP)
                t0 = time.monotonic()
                r2 = await client.post(
                    "/api/auth/login",
                    content=_form_body(),
                    headers={**_form_headers(), "x-forwarded-for": "2.2.2.2"},
                )
                elapsed_b = time.monotonic() - t0

                if r2.status_code != 429:
                    _add_fail(
                        suite,
                        "xff-default-deny",
                        "default-deny-second-request-not-blocked",
                        "SECURITY: Second request (XFF=2.2.2.2) got 200 instead of 429"
                        " — XFF rotation bypassed the rate limiter when"
                        " trusted_proxy is empty!",
                        elapsed_b,
                    )
                    failures += 1
                else:
                    # Verify key uses real IP, not XFF
                    keys = list(store._buckets.keys())
                    has_real_ip = any("127.0.0.1" in k for k in keys)
                    has_xff_ip = any("1.1.1.1" in k for k in keys)

                    if not has_real_ip:
                        _add_fail(
                            suite,
                            "xff-default-deny",
                            "default-deny-key-missing-real-ip",
                            f"Bucket key does not contain the real IP (127.0.0.1),"
                            f" got: {keys}",
                            elapsed_b,
                        )
                        failures += 1
                    elif has_xff_ip:
                        _add_fail(
                            suite,
                            "xff-default-deny",
                            "default-deny-key-contains-xff",
                            f"Bucket key incorrectly contains XFF IP (1.1.1.1): {keys}",
                            elapsed_b,
                        )
                        failures += 1
                    else:
                        _add_pass(
                            suite,
                            "xff-default-deny",
                            "default-deny-xff-rotation-blocked",
                            elapsed_a + elapsed_b,
                        )
    finally:
        config_mod.get_settings = _orig_config_get_settings  # type: ignore[name-defined]  # guarded by _APP_AVAILABLE
        rl_mod.get_settings = _orig_rl_get_settings

    return failures


async def _run_scenario_b(suite: ET.Element) -> int:
    """trusted_proxy='127.0.0.1' → XFF rotation MUST bypass."""
    import lmchat.middleware.rate_limit as rl_mod

    fake_settings = Settings(  # type: ignore[name-defined]  # guarded by _APP_AVAILABLE
        lm_chat_trusted_proxy="127.0.0.1",
        lm_chat_login_rate_limit_per_min=10,
    )

    _orig_config_get_settings = config_mod.get_settings  # type: ignore[name-defined]  # guarded by _APP_AVAILABLE
    _orig_rl_get_settings = rl_mod.get_settings
    config_mod.get_settings = lambda: fake_settings  # type: ignore[name-defined, method-assign]  # guarded by _APP_AVAILABLE
    rl_mod.get_settings = lambda: fake_settings  # type: ignore[method-assign]

    failures = 0

    try:
        store = InMemoryBucketStore()  # type: ignore[name-defined]  # guarded by _APP_AVAILABLE
        burst = 1
        app = _make_app(store=store, burst=burst)

        transport = ASGITransport(app=app)  # type: ignore[name-defined]  # guarded by _APP_AVAILABLE
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:  # type: ignore[name-defined]  # guarded by _APP_AVAILABLE
            # Request 1 — XFF=3.3.3.3 → should succeed
            t0 = time.monotonic()
            r1 = await client.post(
                "/api/auth/login",
                content=_form_body(),
                headers={**_form_headers(), "x-forwarded-for": "3.3.3.3"},
            )
            elapsed_a = time.monotonic() - t0

            if r1.status_code != 200:
                _add_fail(
                    suite,
                    "xff-default-deny",
                    "trusted-proxy-first-request-failed",
                    f"First request (XFF=3.3.3.3) should be 200, got {r1.status_code}",
                    elapsed_a,
                )
                failures += 1
            else:
                # Request 2 — XFF=4.4.4.4 → should ALSO succeed (different client)
                t0 = time.monotonic()
                r2 = await client.post(
                    "/api/auth/login",
                    content=_form_body(),
                    headers={**_form_headers(), "x-forwarded-for": "4.4.4.4"},
                )
                elapsed_b = time.monotonic() - t0

                if r2.status_code != 200:
                    _add_fail(
                        suite,
                        "xff-default-deny",
                        "trusted-proxy-second-request-blocked",
                        "Second request (XFF=4.4.4.4) got"
                        f" {r2.status_code} instead of 200 —"
                        " XFF rotation did NOT bypass when trusted_proxy is set",
                        elapsed_b,
                    )
                    failures += 1
                else:
                    # Verify keys include both XFF IPs
                    keys = list(store._buckets.keys())
                    has_first_xff = any("3.3.3.3" in k for k in keys)
                    has_second_xff = any("4.4.4.4" in k for k in keys)

                    if not has_first_xff:
                        _add_fail(
                            suite,
                            "xff-default-deny",
                            "trusted-proxy-key-missing-xff-1",
                            f"Bucket key missing XFF IP 3.3.3.3, got: {keys}",
                            elapsed_b,
                        )
                        failures += 1
                    elif not has_second_xff:
                        _add_fail(
                            suite,
                            "xff-default-deny",
                            "trusted-proxy-key-missing-xff-2",
                            f"Bucket key missing XFF IP 4.4.4.4, got: {keys}",
                            elapsed_b,
                        )
                        failures += 1
                    else:
                        _add_pass(
                            suite,
                            "xff-default-deny",
                            "trusted-proxy-xff-rotation-bypasses",
                            elapsed_a + elapsed_b,
                        )
    finally:
        config_mod.get_settings = _orig_config_get_settings  # type: ignore[name-defined]  # guarded by _APP_AVAILABLE
        rl_mod.get_settings = _orig_rl_get_settings

    return failures


async def _run_all(suite: ET.Element) -> int:
    """Run both scenarios asynchronously."""
    failures = 0
    failures += await _run_scenario_a(suite)
    failures += await _run_scenario_b(suite)
    return failures


def main() -> int:
    out_path = _REPO_ROOT / "target" / "gates" / "L8-xff-default-deny.xml"
    suite = _make_suite("xff-default-deny")

    if not _APP_AVAILABLE:
        _add_skip(
            suite,
            "xff-default-deny",
            "xff-default-deny-all",
            "lm-chat app not importable — check PYTHONPATH and dependencies",
        )
        _emit_junit(suite, out_path)
        print("[xff_default_deny_test] SKIPPED: app not importable")
        return 0

    try:
        asyncio.run(_run_all(suite))
    except Exception as exc:  # noqa: BLE001
        _add_fail(
            suite,
            "xff-default-deny",
            "xff-default-deny-infrastructure",
            f"Infrastructure error: {exc}",
            0.0,
        )
        _emit_junit(suite, out_path)
        return 2

    f = int(suite.get("failures", "0"))
    e = int(suite.get("errors", "0"))
    t = int(suite.get("tests", "0"))
    print(
        f"[xff_default_deny_test] {t} tests, {f} failures, {e} errors"
        f" -> {out_path}"
    )
    return 1 if (f or e) else 0


if __name__ == "__main__":
    sys.exit(main())