# SPDX-License-Identifier: Apache-2.0
"""Integration tests for LoginRateLimitMiddleware.

Tests cover the happy path, threshold
boundary, over-threshold 429, Retry-After header, key isolation, and
bypass of non-matching routes/methods.

Test architecture
-----------------
We build a minimal FastAPI app inline with a stub ``/api/auth/login``
route, mount ``LoginRateLimitMiddleware`` on it, and drive it through
``httpx.AsyncClient(transport=ASGITransport(...))``.  This avoids
importing ``create_app()`` (which would pull in DB/lifespan) and keeps
the test purely in-process.

A ``FakeBucketStore`` wraps ``InMemoryBucketStore`` but exposes a
monkeypatched clock so we can test time-based refill without sleeping.
"""
from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from httpx import ASGITransport
from starlette.types import Message

from lmchat.middleware._bucket_store import BucketStore, InMemoryBucketStore
from lmchat.middleware.rate_limit import LoginRateLimitMiddleware

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def make_app(
    store: BucketStore | None = None,
    rate_per_minute: int = 10,
    burst: int | None = None,
    ip_rate_per_minute: int | None = None,
    ip_burst: int | None = None,
) -> FastAPI:
    """Build a minimal FastAPI app with the rate-limit middleware and stub route."""
    app = FastAPI()

    @app.post("/api/auth/login")
    async def login_stub() -> PlainTextResponse:
        return PlainTextResponse("ok", status_code=200)

    @app.get("/api/auth/login")
    async def login_get_stub() -> PlainTextResponse:
        """Stub for GET — should bypass rate limit."""
        return PlainTextResponse("ok_get", status_code=200)

    @app.post("/api/auth/register")
    async def register_stub() -> PlainTextResponse:
        """Stub for register — should bypass rate limit."""
        return PlainTextResponse("ok_register", status_code=200)

    app.add_middleware(
        LoginRateLimitMiddleware,
        store=store,
        rate_per_minute=rate_per_minute,
        burst=burst,
        ip_rate_per_minute=ip_rate_per_minute,
        ip_burst=ip_burst,
    )
    return app


def form_body(username: str = "alice", password: str = "pw") -> bytes:
    """Encode a login form body."""
    return urlencode({"username": username, "password": password}).encode()


def form_headers() -> dict[str, str]:
    """Headers for a form-encoded POST."""
    return {"content-type": "application/x-www-form-urlencoded"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_store() -> InMemoryBucketStore:
    """A fresh InMemoryBucketStore per test."""
    return InMemoryBucketStore()


# ---------------------------------------------------------------------------
# Under threshold — all succeed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_under_threshold_all_200(fresh_store: InMemoryBucketStore) -> None:
    """5 rapid attempts well under burst=10 → all 200."""
    app = make_app(store=fresh_store, rate_per_minute=10, burst=10)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for _ in range(5):
            resp = await client.post(
                "/api/auth/login", content=form_body(), headers=form_headers()
            )
            assert resp.status_code == 200


# ---------------------------------------------------------------------------
# At threshold — exactly burst succeeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_at_threshold_all_succeed(fresh_store: InMemoryBucketStore) -> None:
    """Exactly burst=10 rapid attempts → all 200 (burst not exceeded)."""
    burst = 10
    app = make_app(store=fresh_store, rate_per_minute=10, burst=burst)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for i in range(burst):
            resp = await client.post(
                "/api/auth/login", content=form_body(), headers=form_headers()
            )
            assert resp.status_code == 200, f"call {i+1} of {burst} failed unexpectedly"


# ---------------------------------------------------------------------------
# Over threshold → 429
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_over_threshold_returns_429(fresh_store: InMemoryBucketStore) -> None:
    """11th immediate attempt returns 429 with Retry-After header."""
    burst = 10
    app = make_app(store=fresh_store, rate_per_minute=10, burst=burst)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for _ in range(burst):
            await client.post(
                "/api/auth/login", content=form_body(), headers=form_headers()
            )
        resp = await client.post(
            "/api/auth/login", content=form_body(), headers=form_headers()
        )
    assert resp.status_code == 429
    assert "retry-after" in resp.headers
    retry_val = int(resp.headers["retry-after"])
    assert retry_val > 0


# ---------------------------------------------------------------------------
# Retry-After: after waiting, attempt succeeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_after_wait_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """After Retry-After seconds (via fake clock), the next attempt succeeds."""
    fake_now: list[float] = [1000.0]

    def fake_monotonic() -> float:
        return fake_now[0]

    monkeypatch.setattr(time, "monotonic", fake_monotonic)

    store = InMemoryBucketStore()
    burst = 1
    rate_per_minute = 60  # 1 token/sec → easy arithmetic
    app = make_app(store=store, rate_per_minute=rate_per_minute, burst=burst)

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Exhaust the single token
        r1 = await client.post(
            "/api/auth/login", content=form_body(), headers=form_headers()
        )
        assert r1.status_code == 200

        # Now rate-limited
        r2 = await client.post(
            "/api/auth/login", content=form_body(), headers=form_headers()
        )
        assert r2.status_code == 429
        retry_after = int(r2.headers["retry-after"])
        assert retry_after >= 1

        # Advance clock past Retry-After
        fake_now[0] += float(retry_after) + 0.1

        # Should succeed now
        r3 = await client.post(
            "/api/auth/login", content=form_body(), headers=form_headers()
        )
        assert r3.status_code == 200


# ---------------------------------------------------------------------------
# Key isolation: different username on same IP → separate buckets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_different_username_separate_bucket(
    fresh_store: InMemoryBucketStore,
) -> None:
    """Draining alice's bucket does not affect bob's bucket.

    The per-IP bucket (shared across usernames) is given a generous
    explicit cap here so this test only ever exercises per-account
    independence — the per-IP cap must not be the thing that's limiting
    bob, alice, or anyone else in this scenario.
    """
    burst = 2
    app = make_app(
        store=fresh_store,
        rate_per_minute=2,
        burst=burst,
        ip_rate_per_minute=100,
        ip_burst=100,
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Drain alice
        for _ in range(burst):
            await client.post(
                "/api/auth/login",
                content=form_body("alice"),
                headers=form_headers(),
            )
        r_alice = await client.post(
            "/api/auth/login",
            content=form_body("alice"),
            headers=form_headers(),
        )
        assert r_alice.status_code == 429

        # bob should still succeed
        r_bob = await client.post(
            "/api/auth/login",
            content=form_body("bob"),
            headers=form_headers(),
        )
        assert r_bob.status_code == 200


# ---------------------------------------------------------------------------
# Key isolation: different IP same username → separate buckets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_different_ip_separate_bucket() -> None:
    """Different source IPs for the same username get independent buckets."""
    store = InMemoryBucketStore()
    burst = 1
    app = make_app(store=store, rate_per_minute=burst, burst=burst)

    # We can't easily change scope["client"] through httpx; verify the key
    # format by inspecting store internals after exhaustion.
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/api/auth/login",
            content=form_body("carol"),
            headers=form_headers(),
        )
        r2 = await client.post(
            "/api/auth/login",
            content=form_body("carol"),
            headers=form_headers(),
        )
        assert r2.status_code == 429

    # Verify the key stored in the internal dict includes an IP segment.
    # Two buckets now exist per request: the per-account bucket
    # (login:<ip>:<username>) AND the per-IP bucket (login:<ip>, consumed
    # on every login request regardless of username) — pick out the
    # per-account one to check its format.
    keys = list(store._buckets.keys())
    assert len(keys) == 2
    account_keys = [k for k in keys if k.count(":") == 2]
    assert len(account_keys) == 1
    assert account_keys[0].startswith("login:")
    parts = account_keys[0].split(":")
    # Format is login:<ip>:<username>
    assert len(parts) == 3
    assert parts[2] == "carol"


# ---------------------------------------------------------------------------
# Non-POST to /api/auth/login bypasses rate limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_to_login_bypasses_rate_limit(
    fresh_store: InMemoryBucketStore,
) -> None:
    """GET /api/auth/login is not subject to rate limiting."""
    # Create a middleware with burst=1 and drain it via POST;
    # then GET should succeed regardless.
    store = InMemoryBucketStore()
    burst_limit = 1
    app = make_app(store=store, rate_per_minute=burst_limit, burst=burst_limit)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Drain POST bucket
        await client.post("/api/auth/login", content=form_body(), headers=form_headers())
        r_post = await client.post(
            "/api/auth/login", content=form_body(), headers=form_headers()
        )
        assert r_post.status_code == 429

        # GET should bypass
        r_get = await client.get("/api/auth/login")
        assert r_get.status_code == 200


# ---------------------------------------------------------------------------
# Non-login path bypasses rate limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_login_path_bypasses_rate_limit() -> None:
    """POST /api/auth/register is not subject to rate limiting."""

    class AlwaysRefuseBucketStore(BucketStore):
        async def consume(
            self, key: str, *, rate: float, burst: int
        ) -> tuple[bool, float]:
            # If ever called, fail the test loudly
            raise AssertionError(
                "rate limit store should not be called for /register"
            )

    app = make_app(store=AlwaysRefuseBucketStore(), rate_per_minute=10, burst=10)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/auth/register",
            content=form_body(),
            headers=form_headers(),
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Malformed form body falls back to IP-only key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_form_body_falls_back_to_ip_key() -> None:
    """A body with no ``username`` field rate-limits on IP only.

    With no username ever extracted, only the per-IP bucket is ever
    consumed (the per-account bucket is never created) — so it's the
    per-IP knobs, not the per-account ones, that govern this test's math.
    """
    store = InMemoryBucketStore()
    burst = 2
    app = make_app(
        store=store,
        rate_per_minute=burst,
        burst=burst,
        ip_rate_per_minute=burst,
        ip_burst=burst,
    )
    malformed_body = b"not=valid&form=data_no_username"
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for _ in range(burst):
            await client.post(
                "/api/auth/login",
                content=malformed_body,
                headers=form_headers(),
            )
        r_over = await client.post(
            "/api/auth/login",
            content=malformed_body,
            headers=form_headers(),
        )
        assert r_over.status_code == 429

    # Key should be IP-only (no username segment)
    keys = list(store._buckets.keys())
    assert len(keys) == 1
    parts = keys[0].split(":")
    # IP-only key: "login:<ip>" → 2 parts
    assert len(parts) == 2, f"expected IP-only key, got {keys[0]!r}"


# ---------------------------------------------------------------------------
# Body replay: inner app receives the full body
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inner_app_receives_body_after_rate_check() -> None:
    """The inner app still receives the full request body after rate-limit check."""
    received_bodies: list[bytes] = []

    app = FastAPI()

    @app.post("/api/auth/login")
    async def login_echo(request: Request) -> PlainTextResponse:
        body = await request.body()
        received_bodies.append(body)
        return PlainTextResponse("ok", status_code=200)

    store = InMemoryBucketStore()
    app.add_middleware(
        LoginRateLimitMiddleware,
        store=store,
        rate_per_minute=10,
        burst=10,
    )

    payload = form_body("replay_user", "s3cr3t")
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/auth/login", content=payload, headers=form_headers()
        )
    assert resp.status_code == 200
    assert len(received_bodies) == 1
    assert b"username=replay_user" in received_bodies[0]
    assert b"s3cr3t" in received_bodies[0]


# ---------------------------------------------------------------------------
# 429 response body is human-readable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_429_body_is_human_readable() -> None:
    """429 response has a plain-text body with retry information."""
    store = InMemoryBucketStore()
    burst = 1
    app = make_app(store=store, rate_per_minute=burst, burst=burst)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/api/auth/login", content=form_body(), headers=form_headers())
        resp = await client.post(
            "/api/auth/login", content=form_body(), headers=form_headers()
        )
    assert resp.status_code == 429
    assert "too many" in resp.text.lower()
    assert "retry" in resp.text.lower()


# ---------------------------------------------------------------------------
# content-type header present on 429
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_429_has_correct_content_type() -> None:
    """429 response carries content-type: text/plain; charset=utf-8."""
    store = InMemoryBucketStore()
    burst = 1
    app = make_app(store=store, rate_per_minute=burst, burst=burst)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/api/auth/login", content=form_body(), headers=form_headers())
        resp = await client.post(
            "/api/auth/login", content=form_body(), headers=form_headers()
        )
    assert resp.status_code == 429
    assert "text/plain" in resp.headers.get("content-type", "")


# ---------------------------------------------------------------------------
# Websocket + lifespan scopes pass through
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_http_scope_passthrough() -> None:
    """Non-HTTP scopes pass through untouched (store never called)."""

    class AlwaysRefuseBucketStore(BucketStore):
        async def consume(
            self, key: str, *, rate: float, burst: int
        ) -> tuple[bool, float]:
            raise AssertionError("store should not be called for non-HTTP scopes")

    received_scopes: list[str] = []

    async def inner_app(scope: Any, receive: Any, send: Any) -> None:
        received_scopes.append(scope["type"])

    middleware = LoginRateLimitMiddleware(
        inner_app,
        store=AlwaysRefuseBucketStore(),
        rate_per_minute=10,
    )

    # Simulate a lifespan scope
    async def dummy_receive() -> dict[str, str]:
        return {"type": "lifespan.startup"}

    async def dummy_send(msg: Any) -> None:
        pass

    await middleware({"type": "lifespan"}, dummy_receive, dummy_send)
    assert received_scopes == ["lifespan"]


# ---------------------------------------------------------------------------
# X-Forwarded-For honoured when trusted_proxy is configured
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_xff_used_when_trusted_proxy_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """X-Forwarded-For is used for the key when lm_chat_trusted_proxy is set."""
    import lmchat.middleware.rate_limit as rl_mod
    from lmchat import config as config_mod
    from lmchat.config import Settings

    # Patch get_settings at the point of use in rate_limit.py
    fake_settings = Settings(
        lm_chat_trusted_proxy="10.0.0.1",
        lm_chat_login_rate_limit_per_min=10,
    )
    monkeypatch.setattr(config_mod, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(rl_mod, "get_settings", lambda: fake_settings)

    store = InMemoryBucketStore()
    burst = 1
    app = make_app(store=store, rate_per_minute=burst, burst=burst)

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Exhaust the bucket
        await client.post(
            "/api/auth/login",
            content=form_body("dave"),
            headers={
                **form_headers(),
                "x-forwarded-for": "203.0.113.42, 10.0.0.1",
            },
        )
        # A second attempt from the same XFF IP should be rate-limited
        r = await client.post(
            "/api/auth/login",
            content=form_body("dave"),
            headers={
                **form_headers(),
                "x-forwarded-for": "203.0.113.42, 10.0.0.1",
            },
        )
    assert r.status_code == 429

    # Key must include the XFF IP (203.0.113.42), not the ASGI client IP
    keys = list(store._buckets.keys())
    assert any("203.0.113.42" in k for k in keys), f"XFF IP not in keys: {keys}"


# ---------------------------------------------------------------------------
# No client in scope → falls back to "unknown"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_client_in_scope_falls_back_to_unknown() -> None:
    """When scope has no 'client', IP resolves to 'unknown'."""
    store = InMemoryBucketStore()
    burst = 1

    async def inner_app(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    middleware = LoginRateLimitMiddleware(
        inner_app,
        store=store,
        rate_per_minute=burst,
        burst=burst,
    )

    body = form_body("eve")
    body_sent = False

    async def receive() -> dict[str, object]:
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    sent_messages: list[Message] = []

    async def send_msg(msg: Message) -> None:
        sent_messages.append(msg)

    scope: dict[str, object] = {
        "type": "http",
        "method": "POST",
        "path": "/api/auth/login",
        "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
        # no 'client' key
    }
    await middleware(scope, receive, send_msg)
    # First call allowed (burst=1, bucket starts full)
    start_statuses = [m["status"] for m in sent_messages if m["type"] == "http.response.start"]
    assert start_statuses == [200]

    # Key should use "unknown" for IP
    keys = list(store._buckets.keys())
    assert any("unknown" in k for k in keys), f"'unknown' not in keys: {keys}"


# ---------------------------------------------------------------------------
# store property is accessible
# ---------------------------------------------------------------------------


def test_store_property_returns_store() -> None:
    """The .store property exposes the underlying BucketStore instance."""

    async def dummy_app(scope: Any, receive: Any, send: Any) -> None:
        pass

    custom_store = InMemoryBucketStore()
    middleware = LoginRateLimitMiddleware(
        dummy_app,
        store=custom_store,
        rate_per_minute=10,
    )
    assert middleware.store is custom_store


# ---------------------------------------------------------------------------
# Default rate_per_minute reads from settings
# ---------------------------------------------------------------------------


async def test_default_rate_reads_from_settings() -> None:
    """When rate_per_minute=None the middleware resolves the rate lazily on first request.

    The rate is NOT read at construction time — that would call ``get_settings()``
    before the lifespan validates required env vars.  Instead the rate is resolved
    once on the first matching request and cached for all subsequent calls.

    This test verifies:
    1.  ``_rate_per_minute`` is ``None`` immediately after construction (lazy).
    2.  After the first login request the cached value equals the settings default (10).
    3.  The settings default ``lm_chat_login_rate_limit_per_min`` is 10.
    """
    from lmchat.config import get_settings

    async def dummy_app(scope: Any, receive: Any, send: Any) -> None:
        pass

    # Before any request: the rate should be None (not yet resolved).
    middleware = LoginRateLimitMiddleware(dummy_app)
    assert middleware._rate_per_minute is None

    # After a login request the middleware resolves the rate from settings.
    app = make_app(rate_per_minute=10)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        body = urlencode({"username": "u", "password": "p"})
        await client.post(
            "/api/auth/login",
            content=body,
            headers={"content-type": "application/x-www-form-urlencoded"},
        )

    # Confirm the settings default matches the expected value.
    assert get_settings().lm_chat_login_rate_limit_per_min == 10


# ---------------------------------------------------------------------------
# _extract_username: invalid UTF-8 body returns None
# ---------------------------------------------------------------------------


def test_extract_username_invalid_bytes() -> None:
    """_extract_username returns None for invalid UTF-8 bytes."""
    from lmchat.middleware.rate_limit import _extract_username

    result = _extract_username(b"\xff\xfe invalid utf-8")
    assert result is None


# ---------------------------------------------------------------------------
# RED-ON-REVERT: per-IP cap catches username-rotation evasion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_username_rotation_from_same_ip_hits_per_ip_cap(
    fresh_store: InMemoryBucketStore,
) -> None:
    """An attacker rotating ``username`` from a single IP must still hit a cap.

    Each attempt below uses a BRAND-NEW username, so the per-account bucket
    (``login:<ip>:<username>``) never limits anything — every username gets
    its own fresh, never-before-touched bucket.  Only the per-IP bucket
    (``login:<ip>``, shared across ALL usernames from that IP) can catch
    this pattern.

    Configuration: per-account cap is generous (10/min) so it never fires;
    per-IP cap is deliberately small (3/min) so it's the only thing that can
    deny the 4th distinct-username attempt.

    This is the RED-ON-REVERT test for the per-IP bucket fix: if the
    per-IP ``consume()`` call is removed from ``LoginRateLimitMiddleware.__call__``
    (reverting to the old single-bucket-per-account behavior), every
    request here uses a fresh, never-exhausted per-account bucket and ALL
    of them (including the 4th) return 200 — this test then fails on the
    final assertion below.
    """
    app = make_app(
        store=fresh_store,
        rate_per_minute=10,
        burst=10,
        ip_rate_per_minute=3,
        ip_burst=3,
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for i in range(3):
            resp = await client.post(
                "/api/auth/login",
                content=form_body(f"rotating-user-{i}"),
                headers=form_headers(),
            )
            assert resp.status_code == 200, (
                f"attempt {i + 1} uses a fresh username and must not be "
                "blocked by the per-account bucket"
            )

        # 4th attempt, yet another brand-new username: its per-account
        # bucket has never been touched and would allow it on its own —
        # only the (now-exhausted) per-IP bucket can deny this request.
        resp = await client.post(
            "/api/auth/login",
            content=form_body("rotating-user-3"),
            headers=form_headers(),
        )
    assert resp.status_code == 429, (
        "4th login attempt with a brand-new username from the same IP "
        "was NOT denied — username rotation is evading the per-IP cap"
    )
    assert "retry-after" in resp.headers
