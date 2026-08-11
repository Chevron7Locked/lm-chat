# SPDX-License-Identifier: Apache-2.0
"""Tests for AuthMiddleware.

Auth middleware tests.

Test architecture
-----------------
We build a minimal FastAPI app with a ``/probe`` route that echoes the
user attached to ``request.state`` (or ``anonymous`` if none), then drive
it through ``httpx.AsyncClient(transport=ASGITransport(...))``.  The
:class:`~lmchat.middleware.auth.AuthMiddleware` is mounted directly on the
minimal app — no DB lifespan, no create_app().

A real :class:`~lmchat.session.sqlite_store.SQLiteSessionStore` is used
together with a real aiosqlite database so session-cookie resolution is
fully exercised without mocking.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from starlette.types import ASGIApp, Receive, Scope, Send

from lmchat.db.schema import metadata
from lmchat.db.schema import users as users_tbl
from lmchat.middleware.auth import AUTH_SKIP_PATHS, AUTH_SKIP_PREFIXES, AuthMiddleware
from lmchat.middleware.quota import QuotaMiddleware
from lmchat.services.quota_service import OverQuotaError
from lmchat.session.base import Session
from lmchat.session.sqlite_store import SQLiteSessionStore
from lmchat.utils.hashing import hash_password

# Low-cost scrypt — keeps tests fast without real cost.
_PW_HASH: str = hash_password("password", n=2**10, r=8, p=1)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def eng(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    """Yield a real async engine backed by a temp SQLite DB with schema."""
    db_path = tmp_path / "auth_test.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}", pool_pre_ping=True
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture()
async def user_id(eng: AsyncEngine) -> int:
    """Insert a test user and return their id."""
    from sqlalchemy import insert

    async with eng.begin() as conn:
        result = await conn.execute(
            insert(users_tbl).values(
                username="testuser",
                password_hash=_PW_HASH,
            )
        )
        pk = result.inserted_primary_key
        assert pk is not None
        return int(pk[0])


@pytest.fixture()
async def store(eng: AsyncEngine) -> SQLiteSessionStore:
    """Return a SQLiteSessionStore backed by the test engine."""
    return SQLiteSessionStore(engine=eng)


@pytest.fixture()
async def valid_session(store: SQLiteSessionStore, user_id: int) -> Session:
    """Create and return a real session for the test user."""
    return await store.create(user_id=user_id, ttl_seconds=3600)


def make_app(
    store: SQLiteSessionStore | None = None,
    engine: AsyncEngine | None = None,
) -> FastAPI:
    """Build a minimal FastAPI app with AuthMiddleware mounted."""
    app = FastAPI()
    app.add_middleware(AuthMiddleware, session_store=store, engine=engine)

    @app.get("/probe")
    async def probe(request: Request) -> JSONResponse:
        user = getattr(request.state, "user", None)
        return JSONResponse(
            {
                "user_id": user.id if user else None,
                "authenticated": user is not None,
            }
        )

    # /api/* path — invalid cookie here must still produce a JSON 401
    # (XHR/fetch clients expect it). Non-API paths (like /probe) are SPA
    # navigations and must pass through instead of dumping raw JSON.
    @app.get("/api/probe")
    async def api_probe(request: Request) -> JSONResponse:
        user = getattr(request.state, "user", None)
        return JSONResponse({"authenticated": user is not None})

    return app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", sorted(AUTH_SKIP_PATHS))
@pytest.mark.asyncio
async def test_auth_middleware_skip_healthz_unauthenticated_passes_through(
    path: str,
    store: SQLiteSessionStore,
    eng: AsyncEngine,
) -> None:
    """Skip-listed exact paths pass through without any auth check."""
    app = FastAPI()
    app.add_middleware(AuthMiddleware, session_store=store, engine=eng)

    @app.api_route(path, methods=["GET"])
    async def stub() -> JSONResponse:
        return JSONResponse({"ok": True})

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(path)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_auth_middleware_skip_metrics_unauthenticated_passes_through(
    store: SQLiteSessionStore,
    eng: AsyncEngine,
) -> None:
    """The /api/metrics path passes through without auth checking."""
    app = FastAPI()
    app.add_middleware(AuthMiddleware, session_store=store, engine=eng)

    @app.get("/api/metrics")
    async def metrics_stub() -> JSONResponse:
        return JSONResponse({"metrics": "ok"})

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/metrics")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_auth_middleware_absent_cookie_passes_through(
    store: SQLiteSessionStore,
    eng: AsyncEngine,
) -> None:
    """An absent cookie does NOT produce a 401; request passes through as anonymous."""
    app = make_app(store=store, engine=eng)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/probe")
    assert resp.status_code == 200
    data = resp.json()
    assert data["authenticated"] is False
    assert data["user_id"] is None


@pytest.mark.asyncio
async def test_auth_middleware_valid_cookie_attaches_user_to_state(
    store: SQLiteSessionStore,
    eng: AsyncEngine,
    user_id: int,
    valid_session: Session,
) -> None:
    """A valid session cookie results in the user being attached to request.state."""
    app = make_app(store=store, engine=eng)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/probe", headers={"Cookie": f"lmchat_session={valid_session.id}"}
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["authenticated"] is True
    assert data["user_id"] == user_id


@pytest.mark.asyncio
async def test_auth_middleware_invalid_cookie_on_api_path_returns_401(
    store: SQLiteSessionStore,
    eng: AsyncEngine,
) -> None:
    """An invalid cookie on an /api/ path returns a JSON 401 (XHR contract)."""
    app = make_app(store=store, engine=eng)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/probe", headers={"Cookie": "lmchat_session=totally-invalid-token"}
        )
    assert resp.status_code == 401
    body = resp.json()
    assert "detail" in body


@pytest.mark.asyncio
async def test_auth_middleware_invalid_cookie_on_spa_path_passes_through(
    store: SQLiteSessionStore,
    eng: AsyncEngine,
) -> None:
    """An invalid cookie on a non-API (SPA shell) path must NOT return raw
    401 JSON — it passes through anonymously (so the shell renders + the
    client redirects to login) AND expires the stale cookie."""
    app = make_app(store=store, engine=eng)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/probe", headers={"Cookie": "lmchat_session=totally-invalid-token"}
        )
    # Passed through to the route as anonymous — NOT a 401 JSON dump.
    assert resp.status_code == 200
    assert resp.json()["authenticated"] is False
    # The stale cookie is expired so the browser stops re-sending it.
    set_cookie = resp.headers.get("set-cookie", "")
    assert "lmchat_session=" in set_cookie
    assert "Max-Age=0" in set_cookie


@pytest.mark.asyncio
async def test_auth_middleware_expired_cookie_returns_401(
    eng: AsyncEngine,
    user_id: int,
) -> None:
    """A cookie whose session has expired returns HTTP 401."""
    # Create a session with a 1-second TTL, then expire it by using the
    # expired store: we insert a session row with expires_at in the past.
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import insert

    from lmchat.db.schema import sessions as sessions_tbl

    expired_token = "expired-test-token-abc123"
    past = datetime.now(UTC) - timedelta(seconds=10)

    async with eng.begin() as conn:
        await conn.execute(
            insert(sessions_tbl).values(
                id=expired_token,
                user_id=user_id,
                expires_at=past,
                created_at=past,
                rotated_at=None,
            )
        )

    store = SQLiteSessionStore(engine=eng)
    app = make_app(store=store, engine=eng)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/probe", headers={"Cookie": f"lmchat_session={expired_token}"}
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_auth_middleware_static_prefix_passes_through(
    store: SQLiteSessionStore,
    eng: AsyncEngine,
) -> None:
    """Paths under /static/ and /assets/ pass through without auth."""
    app = FastAPI()
    app.add_middleware(AuthMiddleware, session_store=store, engine=eng)

    @app.get("/static/main.css")
    async def static_stub() -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.get("/assets/bundle.js")
    async def assets_stub() -> JSONResponse:
        return JSONResponse({"ok": True})

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r1 = await client.get("/static/main.css")
        r2 = await client.get("/assets/bundle.js")

    assert r1.status_code == 200
    assert r2.status_code == 200


def test_auth_skip_paths_canonical() -> None:
    """AUTH_SKIP_PATHS contains the expected exact skip list.

    ``/api/auth/setup_status`` is anonymous-callable so the
    React Login page can probe it on mount before any session exists.
    The endpoint returns only ``{"needs_setup": <bool>}`` — see
    routes/auth.py:setup_status_endpoint for the recon-leak rationale.

    ``/api/auth/me/probe`` is a mount-time hydration
    probe whose entire contract is "never 401" — see
    routes/auth.py:me_probe_endpoint.
    """
    expected = {
        "/healthz",
        "/readyz",
        "/api/metrics",
        "/api/auth/login",
        "/api/auth/register",
        "/api/auth/setup_status",
        "/api/auth/me/probe",
    }
    assert AUTH_SKIP_PATHS == expected


def test_auth_skip_prefixes_canonical() -> None:
    """AUTH_SKIP_PREFIXES covers /static/, /assets/, and /api/share/."""
    assert "/static/" in AUTH_SKIP_PREFIXES
    assert "/assets/" in AUTH_SKIP_PREFIXES
    assert "/api/share/" in AUTH_SKIP_PREFIXES


# ---------------------------------------------------------------------------
# Stale/invalid cookie on anonymous endpoints must NOT
# be hard-401'd by the middleware before the handler runs.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_middleware_invalid_cookie_on_public_share_path_passes_through(
    store: SQLiteSessionStore,
    eng: AsyncEngine,
) -> None:
    """A stale/invalid session cookie on the public share endpoint must NOT
    401 the request. ``/api/share/{token}`` (routes/share.py:get_public_share)
    is a fully anonymous, read-only endpoint that never reads
    ``request.state.user`` — a visitor who happens to be carrying a stale
    cookie must still be able to open someone else's shared chat.

    RED-ON-REVERT: without ``/api/share/`` in ``AUTH_SKIP_PREFIXES``, the
    invalid cookie hits the ``_is_api_path`` 401 branch in
    ``AuthMiddleware.__call__`` before the stub route ever runs, and this
    test fails with a 401 instead of 200.
    """
    app = FastAPI()
    app.add_middleware(AuthMiddleware, session_store=store, engine=eng)

    @app.get("/api/share/{token}")
    async def share_stub(token: str) -> JSONResponse:
        return JSONResponse({"token": token})

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/share/abc123",
            headers={"Cookie": "lmchat_session=totally-invalid-token"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"token": "abc123"}


@pytest.mark.asyncio
async def test_auth_middleware_valid_cookie_on_public_share_path_still_passes_through(
    store: SQLiteSessionStore,
    eng: AsyncEngine,
    user_id: int,
    valid_session: Session,
) -> None:
    """A VALID session cookie on the newly-skipped ``/api/share/`` prefix
    still reaches the handler normally — skip-listing an anonymous endpoint
    must not accidentally break the case where a logged-in user follows
    their own (or someone else's) share link."""
    app = FastAPI()
    app.add_middleware(AuthMiddleware, session_store=store, engine=eng)

    @app.get("/api/share/{token}")
    async def share_stub(token: str) -> JSONResponse:
        return JSONResponse({"token": token})

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/share/abc123",
            headers={"Cookie": f"lmchat_session={valid_session.id}"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"token": "abc123"}


@pytest.mark.asyncio
async def test_auth_middleware_invalid_cookie_on_me_probe_path_passes_through(
    store: SQLiteSessionStore,
    eng: AsyncEngine,
) -> None:
    """A stale/invalid session cookie on ``/api/auth/me/probe`` must NOT
    401 — the whole point of this endpoint
    (routes/auth.py:me_probe_endpoint) is to ALWAYS return 200 (it does its
    own tolerant cookie/session handling internally) so the SPA's
    mount-time hydration never logs a red DevTools 401. Before this fix,
    ``AuthMiddleware`` hard-401'd the request before the handler ever ran,
    defeating that contract for anyone holding a stale cookie.

    RED-ON-REVERT: without ``/api/auth/me/probe`` in ``AUTH_SKIP_PATHS``,
    the invalid cookie hits the ``_is_api_path`` 401 branch in
    ``AuthMiddleware.__call__`` and this test fails with a 401 instead of
    200.
    """
    app = FastAPI()
    app.add_middleware(AuthMiddleware, session_store=store, engine=eng)

    @app.get("/api/auth/me/probe")
    async def probe_stub() -> JSONResponse:
        return JSONResponse({"user_id": None})

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/auth/me/probe",
            headers={"Cookie": "lmchat_session=totally-invalid-token"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"user_id": None}


# ---------------------------------------------------------------------------
# malformed cookie header coverage
# ---------------------------------------------------------------------------


def test_parse_cookies_malformed_no_equals_dropped() -> None:
    """Cookie pairs without '=' are silently dropped per RFC 6265."""
    from lmchat.middleware.auth import _parse_cookies

    headers = [(b"cookie", b"nokey; foo=bar; alsobogus")]
    result = _parse_cookies(headers)
    assert result == {"foo": "bar"}


def test_parse_cookies_empty_value_returns_empty_string() -> None:
    """An empty cookie value 'k=' parses as {'k': ''}."""
    from lmchat.middleware.auth import _parse_cookies

    headers = [(b"cookie", b"lmchat_session=; otherkey=v")]
    result = _parse_cookies(headers)
    assert result.get("lmchat_session") == ""
    assert result.get("otherkey") == "v"


def test_parse_cookies_non_ascii_bytes_dropped() -> None:
    """Cookie value bytes that aren't ASCII decode-clean fall back to ignoring
    that pair (the parser must not raise on UnicodeDecodeError)."""
    from lmchat.middleware.auth import _parse_cookies

    # 0xff 0xfe is not valid ASCII; the parser should not raise.
    headers = [(b"cookie", b"valid=ok; bad=\xff\xfe")]
    result = _parse_cookies(headers)
    # The "valid" pair survives; the "bad" pair is dropped silently.
    assert "valid" in result
    assert result["valid"] == "ok"


# ---------------------------------------------------------------------------
# Authorization: Bearer fallback (no cookie) — quota-bypass fix
# ---------------------------------------------------------------------------


def test_extract_bearer_token_parses_valid_header() -> None:
    """_extract_bearer_token pulls the token out of a well-formed header."""
    from lmchat.middleware.auth import _extract_bearer_token

    headers = [(b"authorization", b"Bearer abc123")]
    assert _extract_bearer_token(headers) == "abc123"


def test_extract_bearer_token_absent_header_returns_none() -> None:
    """No Authorization header at all -> None."""
    from lmchat.middleware.auth import _extract_bearer_token

    assert _extract_bearer_token([]) is None


def test_extract_bearer_token_wrong_scheme_returns_none() -> None:
    """A non-Bearer scheme (e.g. Basic) is not treated as a session id."""
    from lmchat.middleware.auth import _extract_bearer_token

    headers = [(b"authorization", b"Basic dXNlcjpwYXNz")]
    assert _extract_bearer_token(headers) is None


def test_extract_bearer_token_empty_token_returns_none() -> None:
    """'Bearer ' with nothing (or only whitespace) after it -> None."""
    from lmchat.middleware.auth import _extract_bearer_token

    headers = [(b"authorization", b"Bearer    ")]
    assert _extract_bearer_token(headers) is None


@pytest.mark.asyncio
async def test_auth_middleware_bearer_token_attaches_user_no_cookie(
    store: SQLiteSessionStore,
    eng: AsyncEngine,
    user_id: int,
    valid_session: Session,
) -> None:
    """A valid session sent ONLY via ``Authorization: Bearer`` (no cookie at
    all) results in the user being attached to ``scope["state"].user`` —
    exactly like the cookie path.

    RED-ON-REVERT: without the Bearer fallback in
    ``AuthMiddleware.__call__``, a request with no cookie is anonymous no
    matter what the Authorization header carries, so ``authenticated``
    would be ``False`` and ``user_id`` would be ``None``. This test fails
    on revert.
    """
    app = make_app(store=store, engine=eng)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/probe",
            headers={"Authorization": f"Bearer {valid_session.id}"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["authenticated"] is True
    assert data["user_id"] == user_id


@pytest.mark.asyncio
async def test_auth_middleware_invalid_bearer_token_on_api_path_returns_401(
    store: SQLiteSessionStore,
    eng: AsyncEngine,
) -> None:
    """An invalid Bearer token (no cookie) on an /api/ path still gets the
    existing invalid-session 401 treatment — same as an invalid cookie."""
    app = make_app(store=store, engine=eng)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/probe",
            headers={"Authorization": "Bearer totally-invalid-token"},  # secrets-scan-allow
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_bearer_client_over_quota_receives_429_via_quota_middleware(
    store: SQLiteSessionStore,
    eng: AsyncEngine,
    user_id: int,
    valid_session: Session,
) -> None:
    """End-to-end wiring proof: a Bearer-authenticated (no-cookie) request
    that is OVER the daily quota gets 429 from QuotaMiddleware.

    QuotaMiddleware only enforces the daily quota when
    ``scope["state"].user`` is set — and that is only ever set by
    AuthMiddleware. Before the Bearer fallback, a Bearer/API/CLI client
    had no cookie, so AuthMiddleware left ``state.user`` unset and
    QuotaMiddleware silently passed every request through regardless of
    quota (a quota-bypass hole for exactly the clients most likely to
    hammer the API). This test drives the REAL stack — AuthMiddleware
    wrapping QuotaMiddleware, matching production mount order — and pins
    that a Bearer client over quota is denied.

    RED-ON-REVERT: without the Bearer fallback, ``state.user`` stays
    unset for this no-cookie request, QuotaMiddleware's
    ``user is None`` check passes the request straight through, and the
    response is 200 instead of 429 — this test fails on revert.
    """

    async def route_app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    # Stack (outer -> inner, matching real production mount order):
    #   AuthMiddleware -> QuotaMiddleware -> route
    quota_wrapped = QuotaMiddleware(route_app)
    auth_wrapped = AuthMiddleware(quota_wrapped, session_store=store, engine=eng)

    class _FakeAppState:
        # Any non-None sentinel — consume_request is patched below, so the
        # engine itself is never actually used for a DB call.
        engine = object()

    class _FakeApp:
        state = _FakeAppState()

    class _InjectApp:
        """Sets scope["app"] so QuotaMiddleware can read app.state.engine.

        In production, Starlette's own ``__call__`` sets ``scope["app"] =
        self`` before the middleware stack runs. Here we drive
        AuthMiddleware/QuotaMiddleware directly (no Starlette app in the
        loop), so this stand-in performs that same assignment.
        """

        def __init__(self, inner: ASGIApp) -> None:
            self._inner = inner

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] == "http":
                scope["app"] = _FakeApp()
            await self._inner(scope, receive, send)

    stack = _InjectApp(auth_wrapped)

    with patch(
        "lmchat.middleware.quota.consume_request",
        new_callable=AsyncMock,
        side_effect=OverQuotaError("requests", user_id),
    ):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=stack), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/chats",
                headers={"Authorization": f"Bearer {valid_session.id}"},
            )

    assert resp.status_code == 429, (
        "Bearer-authenticated client over quota should receive 429 from "
        "QuotaMiddleware — this requires AuthMiddleware to have set "
        "state.user from the Bearer token (no cookie was sent)."
    )
    body = resp.json()
    assert body["detail"] == "request quota exceeded for the day"


# ---------------------------------------------------------------------------
# Sliding-TTL renewal: SQLiteSessionStore.extend() had zero
# production callers before this wiring — a session's expires_at never moved
# no matter how active the user was, so every session died exactly
# lm_chat_session_ttl_seconds after login. AuthMiddleware now renews the
# session (and re-issues the cookie) once a request lands inside the
# trailing half of the TTL window, and does nothing otherwise so normal
# requests don't pay a DB write + extra Set-Cookie every time.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_middleware_renews_session_inside_renewal_window(
    store: SQLiteSessionStore,
    eng: AsyncEngine,
    user_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session with little TTL remaining gets extended and re-cookied.

    RED-ON-REVERT: without the sliding-TTL wiring, AuthMiddleware never
    calls ``store.extend()``, so the DB row's ``expires_at`` is unchanged
    and no renewal ``Set-Cookie`` is emitted — this test fails on revert.
    """
    from lmchat.config import get_settings

    monkeypatch.setenv("LM_CHAT_SESSION_TTL_SECONDS", "3600")
    get_settings.cache_clear()
    try:
        # Remaining TTL (600s) sits well inside the renewal window (half of
        # 3600s = 1800s), so this request must trigger a renewal.
        session = await store.create(user_id=user_id, ttl_seconds=600)

        app = make_app(store=store, engine=eng)
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/probe", headers={"Cookie": f"lmchat_session={session.id}"}
            )
        assert resp.status_code == 200

        set_cookie = resp.headers.get("set-cookie", "")
        assert f"lmchat_session={session.id}" in set_cookie, (
            f"Expected a renewed Set-Cookie carrying the SAME token "
            f"(extend() doesn't change the id). Header: {set_cookie!r}"
        )
        assert "max-age=3600" in set_cookie.lower(), (
            f"Renewed cookie must carry the full TTL as Max-Age so the "
            f"browser's expiry tracks the server's. Header: {set_cookie!r}"
        )
        assert "httponly" in set_cookie.lower()
        assert "samesite=lax" in set_cookie.lower()

        renewed = await store.get(session.id)
        assert renewed is not None
        assert renewed.expires_at > session.expires_at + timedelta(minutes=30), (
            "Session expiry was not pushed forward by the sliding-TTL renewal"
        )
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_auth_middleware_no_renewal_when_ttl_remaining_is_healthy(
    store: SQLiteSessionStore,
    eng: AsyncEngine,
    user_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session with plenty of TTL remaining is NOT extended or re-cookied.

    Renewal must only fire inside the trailing half of the window — every
    request extending the session (and re-issuing a cookie) would mean a
    DB write on every single authenticated request.
    """
    from lmchat.config import get_settings

    monkeypatch.setenv("LM_CHAT_SESSION_TTL_SECONDS", "3600")
    get_settings.cache_clear()
    try:
        session = await store.create(user_id=user_id, ttl_seconds=3600)

        app = make_app(store=store, engine=eng)
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/probe", headers={"Cookie": f"lmchat_session={session.id}"}
            )
        assert resp.status_code == 200
        assert "set-cookie" not in resp.headers, (
            f"Renewal cookie emitted despite healthy remaining TTL: "
            f"{resp.headers.get('set-cookie')!r}"
        )

        unchanged = await store.get(session.id)
        assert unchanged is not None
        assert abs((unchanged.expires_at - session.expires_at).total_seconds()) < 2, (
            "Session expiry moved even though the request was not inside "
            "the renewal window"
        )
    finally:
        get_settings.cache_clear()
