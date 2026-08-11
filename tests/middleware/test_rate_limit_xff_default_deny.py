# SPDX-License-Identifier: Apache-2.0
"""Integration tests for X-Forwarded-For default-deny behaviour.

Per docs/audit/2026-06-13-qa-security-suite-PLAN-v3.md §1D-5:

The middleware (src/lmchat/middleware/rate_limit.py) honours XFF **only**
when ``settings.lm_chat_trusted_proxy`` is non-empty.  When it is empty
(the default), rotating X-Forwarded-For on each request MUST NOT bypass
the rate limiter — the real ASGI client IP is always used as the key.

Tests
-----
test_xff_rotation_does_not_bypass_when_no_trusted_proxy
    lm_chat_trusted_proxy="" (empty) → XFF rotation does NOT bypass.
    N+1 requests from the same real IP but different XFF values → 429.

test_xff_rotation_does_bypass_when_trusted_proxy_set
    lm_chat_trusted_proxy="127.0.0.1" → XFF rotation DOES bypass.
    Each different XFF value is treated as a distinct client.
"""
from __future__ import annotations

from urllib.parse import urlencode

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from httpx import ASGITransport

from lmchat import config as config_mod
from lmchat.config import Settings
from lmchat.middleware._bucket_store import InMemoryBucketStore
from lmchat.middleware.rate_limit import LoginRateLimitMiddleware

# ---------------------------------------------------------------------------
# Test helpers (mirror test_rate_limit.py)
# ---------------------------------------------------------------------------


def _make_app(
    store: InMemoryBucketStore | None = None,
    rate_per_minute: int = 10,
    burst: int | None = None,
) -> FastAPI:
    """Build a minimal FastAPI app with the rate-limit middleware and stub route."""
    app = FastAPI()

    @app.post("/api/auth/login")
    async def _login_stub() -> PlainTextResponse:
        return PlainTextResponse("ok", status_code=200)

    app.add_middleware(
        LoginRateLimitMiddleware,
        store=store,
        rate_per_minute=rate_per_minute,
        burst=burst,
    )
    return app


def _form_body(username: str = "alice") -> bytes:
    """Encode a login form body."""
    return urlencode({"username": username, "password": "pw"}).encode()


def _form_headers() -> dict[str, str]:
    """Headers for a form-encoded POST."""
    return {"content-type": "application/x-www-form-urlencoded"}


# ---------------------------------------------------------------------------
# Test 1 — default-deny: XFF rotation does NOT bypass when trust is empty
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_xff_rotation_does_not_bypass_when_no_trusted_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """XFF rotation does NOT bypass rate limit when lm_chat_trusted_proxy is empty.

    Sends two login requests from the same real (ASGI) IP with different
    X-Forwarded-For values.  Because trusted_proxy is empty, the middleware
    ignores XFF and uses the real IP for both — so the second request
    exhausts the burst and gets 429.
    """
    import lmchat.middleware.rate_limit as rl_mod

    # Patch get_settings so lm_chat_trusted_proxy is empty (default)
    fake_settings = Settings(
        lm_chat_trusted_proxy="",
        lm_chat_login_rate_limit_per_min=10,
    )
    monkeypatch.setattr(config_mod, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(rl_mod, "get_settings", lambda: fake_settings)

    store = InMemoryBucketStore()
    burst = 1  # Allow only 1 request before rate-limit fires
    app = _make_app(store=store, rate_per_minute=burst, burst=burst)

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Request 1 — X-Forwarded-For: 1.1.1.1 → should succeed (burst=1)
        r1 = await client.post(
            "/api/auth/login",
            content=_form_body(),
            headers={
                **_form_headers(),
                "x-forwarded-for": "1.1.1.1",
            },
        )
        assert r1.status_code == 200, (
            f"First request (XFF=1.1.1.1) should succeed, got {r1.status_code}"
        )

        # Request 2 — X-Forwarded-For: 2.2.2.2 (rotated) → should be rate-limited
        # because trusted_proxy is empty so the middleware uses the real IP
        r2 = await client.post(
            "/api/auth/login",
            content=_form_body(),
            headers={
                **_form_headers(),
                "x-forwarded-for": "2.2.2.2",
            },
        )
        assert r2.status_code == 429, (
            f"Second request (XFF=2.2.2.2) should be 429, got {r2.status_code} "
            f"— XFF rotation bypassed the rate limit when trusted_proxy is empty"
        )

    # Verify the bucket key contains the ASGI client IP (127.0.0.1), not the XFF IPs
    keys = list(store._buckets.keys())
    assert any("127.0.0.1" in k for k in keys), (
        f"Expected a key containing the real IP (127.0.0.1), got: {keys}"
    )
    assert not any("1.1.1.1" in k for k in keys), (
        f"Key must NOT contain XFF IP (1.1.1.1) when trusted_proxy is empty: {keys}"
    )


# ---------------------------------------------------------------------------
# Test 2 — trusted-proxy: XFF rotation DOES bypass when trust is configured
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_xff_rotation_does_bypass_when_trusted_proxy_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """XFF rotation DOES bypass rate limit when lm_chat_trusted_proxy is set.

    Complements the existing test at test_rate_limit.py:492 by proving that
    with a trusted proxy configured, different XFF values are treated as
    different clients, so each request gets its own bucket and the second
    does NOT get rate-limited.

    This confirms the trusted-proxy path works end-to-end and is documented
    alongside the default-deny test for symmetry.
    """
    import lmchat.middleware.rate_limit as rl_mod

    # Patch get_settings so lm_chat_trusted_proxy is "127.0.0.1"
    fake_settings = Settings(
        lm_chat_trusted_proxy="127.0.0.1",
        lm_chat_login_rate_limit_per_min=10,
    )
    monkeypatch.setattr(config_mod, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(rl_mod, "get_settings", lambda: fake_settings)

    store = InMemoryBucketStore()
    burst = 1  # Allow only 1 request per bucket before rate-limit fires
    app = _make_app(store=store, rate_per_minute=burst, burst=burst)

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Request 1 — X-Forwarded-For: 3.3.3.3 → should succeed
        r1 = await client.post(
            "/api/auth/login",
            content=_form_body(),
            headers={
                **_form_headers(),
                "x-forwarded-for": "3.3.3.3",
            },
        )
        assert r1.status_code == 200, (
            f"First request (XFF=3.3.3.3) should succeed, got {r1.status_code}"
        )

        # Request 2 — X-Forwarded-For: 4.4.4.4 (rotated) → should ALSO succeed
        # because trusted_proxy is set, so the middleware extracts the XFF IP
        # and treats 4.4.4.4 as a different client with its own bucket
        r2 = await client.post(
            "/api/auth/login",
            content=_form_body(),
            headers={
                **_form_headers(),
                "x-forwarded-for": "4.4.4.4",
            },
        )
        assert r2.status_code == 200, (
            f"Second request (XFF=4.4.4.4) should succeed (different XFF IP), "
            f"got {r2.status_code} — XFF rotation did NOT bypass when "
            f"trusted_proxy is set"
        )

    # Verify keys include both XFF IPs, proving they were used as part of the key
    keys = list(store._buckets.keys())
    assert any("3.3.3.3" in k for k in keys), (
        f"Expected a key containing XFF IP 3.3.3.3, got: {keys}"
    )
    assert any("4.4.4.4" in k for k in keys), (
        f"Expected a key containing XFF IP 4.4.4.4, got: {keys}"
    )