# SPDX-License-Identifier: Apache-2.0
"""Authentication routes for lm-chat.

All endpoints use ``application/x-www-form-urlencoded`` request bodies
(``python-multipart`` dependency; already required by LoginRateLimitMiddleware).
All responses are JSON.

Cookie name: ``lmchat_session`` — imported from auth_service via
``_SESSION_COOKIE``.  If the export is removed from auth_service, the
constant is duplicated here with an inline comment referencing that constant.

Cookie flags:
- ``HttpOnly=True`` — the SPA must not read the session token; JS access
  would widen the XSS surface.
- ``Secure=True`` when the request arrived over HTTPS (``request.url.scheme
  == "https"``).  For plain HTTP (local dev), the flag is omitted so the
  browser accepts the cookie.  A note in the code documents this decision.
- ``SameSite=Lax`` — allows navigation-triggered GETs across origins (e.g.
  opening a link) but blocks cross-origin POSTs, mitigating CSRF.
- ``Path=/`` — cookie is sent on all paths.

Rate limiting:
The ``POST /api/auth/login`` endpoint is rate-limited by
:class:`~lmchat.middleware.rate_limit.LoginRateLimitMiddleware`, which is
applied in ``app.py``'s middleware stack.  The route handler itself does
not implement rate limiting — the middleware returns a ``429`` response
before the handler is invoked when the bucket is empty.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Request, Response
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from lmchat.config import get_settings
from lmchat.db.retry import with_write_retry
from lmchat.db.schema import admin_invites as admin_invites_table
from lmchat.db.schema import users as users_table
from lmchat.logging import get_logger
from lmchat.routes._dependencies import (
    get_default_session_store_dep,
    get_engine_dep,
    require_user,
)
from lmchat.services.auth_errors import (
    AuthError,
    BadCredentialsError,
    TotpRequiredError,
    UsernameTakenError,
)
from lmchat.services.auth_service import (
    _SESSION_COOKIE,
    User,
    change_password,
    count_users,
    disable_totp,
    login,
    logout,
    register,
    setup_totp,
    verify_totp_setup,
)
from lmchat.session.base import SessionStore
from lmchat.utils.password_policy import (
    PasswordPolicyError,
    validate_new_password,
)

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Re-export from auth_service so routes don't hard-code the string.
# If _SESSION_COOKIE is ever unexported, duplicate the value here and add:
#   # Must match _SESSION_COOKIE in auth_service.py.
_COOKIE: Final[str] = _SESSION_COOKIE
_COOKIE_PATH: Final[str] = "/"
_COOKIE_SAMESITE: Final[str] = "lax"

router: APIRouter = APIRouter(prefix="/api/auth", tags=["auth"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _set_session_cookie(response: Response, token: str, request: Request) -> None:
    """Attach ``lmchat_session`` cookie to *response*.

    Sets ``HttpOnly=True`` unconditionally.  Sets ``Secure=True`` when the
    request arrived over HTTPS (``request.url.scheme == "https"``).  This
    allows plain-HTTP local dev to work while ensuring ``Secure`` is present
    in any TLS-terminated production deploy.

    Args:
        response: The FastAPI ``Response`` to set the cookie on.
        token:    The session token to store in the cookie.
        request:  The incoming request (used to determine the scheme).
    """
    secure = request.url.scheme == "https"
    response.set_cookie(
        key=_COOKIE,
        value=token,
        httponly=True,
        secure=secure,
        samesite=_COOKIE_SAMESITE,
        path=_COOKIE_PATH,
        # Persist the cookie for the session lifetime so the login survives a
        # browser restart. Without Max-Age the cookie is session-scoped
        # (dropped on browser close) even though the server-side session is
        # valid far longer. Matches lm_chat_session_ttl_seconds.
        max_age=get_settings().lm_chat_session_ttl_seconds,
    )


def _clear_session_cookie(response: Response) -> None:
    """Delete ``lmchat_session`` by setting ``Max-Age=0``.

    Args:
        response: The FastAPI ``Response`` to delete the cookie on.
    """
    response.set_cookie(
        key=_COOKIE,
        value="",
        httponly=True,
        secure=False,  # Both HTTP and HTTPS clients receive the delete.
        samesite=_COOKIE_SAMESITE,
        path=_COOKIE_PATH,
        max_age=0,
    )


# ---------------------------------------------------------------------------
# POST /api/auth/register
# ---------------------------------------------------------------------------


@router.post(
    "/register",
    status_code=201,
    responses={
        403: {"description": "Registration disabled — setup token required or admin exists"},
    },
)
async def register_endpoint(
    request: Request,  # noqa: ARG001  (kept for future middleware access)
    response: Response,
    username: str = Form(..., pattern=r"^[a-zA-Z0-9_]{3,64}$"),
    password: str = Form(...),
    token: str | None = None,
    x_setup_token: str | None = Header(default=None, alias="X-Setup-Token"),
    engine: AsyncEngine = Depends(get_engine_dep),
) -> dict[str, Any]:
    """Create a new user account.

    Two token mechanisms share the same wire surface (``?token=`` query
    parameter or ``X-Setup-Token`` header):

    1. **Bootstrap setup token.** When
       ``settings.lm_chat_setup_token`` is set AND no users have registered
       yet, the request must supply a token matching that value.  After
       the first user exists this gate lifts automatically.

    2. **Admin invite token.** Independently
       of the bootstrap gate, if the supplied token matches an unused,
       unexpired row in ``admin_invites``, the row is consumed (``used_at``
       + ``used_by`` populated) and the new user is granted
       ``is_admin=True`` regardless of whether they are the first user.
       This lets an admin issue an invite via ``POST /api/admin/invite``
       and have the registrant land as an admin without any other change.

    Dispatch order:
        a. If a token is supplied, look it up in ``admin_invites``.  If
           found and valid, mark this registration as invite-driven (so
           we know to promote-to-admin after the INSERT) AND treat the
           token as satisfying the setup gate.
        b. Otherwise apply the setup-token gate as before.

    The 403 path returns ``detail="setup token required"`` for both the
    missing-token-with-gate-active case and the invalid-invite-token
    case.  The body deliberately doesn't distinguish — that would let an
    attacker enumerate which tokens are real.

    Args:
        username:       Desired username (3–64 alphanumeric + underscore).
        password:       Plaintext password.
        token:          Optional token from the ``?token=`` query parameter.
        x_setup_token:  Optional token from the ``X-Setup-Token`` header.
        engine:         Engine dependency.

    Returns:
        ``{"id": <user_id>, "username": <username>}`` on success.

    Raises:
        HTTPException 400: If the username is already taken.
        HTTPException 403: If the setup token is required but missing or
            does not match the configured value.
        HTTPException 422: On validation failure (FastAPI default for
            missing / malformed form fields).
    """
    supplied = token if token else x_setup_token

    # --- step 1: try invite-token consumption ---------------------------------
    # An invite, if redeemed, also satisfies the setup-token gate.
    invite_row_id: int | None = None
    grant_admin_via_invite: bool = False
    if supplied is not None:
        async with engine.connect() as conn:
            invite_row = (
                await conn.execute(
                    select(admin_invites_table).where(
                        admin_invites_table.c.token == supplied
                    )
                )
            ).fetchone()

        if invite_row is not None:
            row_map = invite_row._mapping
            expires_at = row_map["expires_at"]
            # Normalise to aware UTC for the comparison.  SQLite returns naive
            # datetimes for TIMESTAMP columns regardless of the timezone=True
            # flag at the SQLAlchemy layer — adjust here.
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            unexpired = expires_at > datetime.now(UTC)
            unused = row_map["used_at"] is None
            if unexpired and unused:
                invite_row_id = int(row_map["id"])
                grant_admin_via_invite = True

    # --- step 2: setup-token gate ----------------------------------------------
    settings = get_settings()
    configured_token = settings.lm_chat_setup_token
    if configured_token and not grant_admin_via_invite:
        existing = await count_users(engine)
        if existing == 0:
            if supplied is None or supplied != configured_token:
                # Do NOT echo the supplied token nor the configured value.
                raise HTTPException(
                    status_code=403, detail="setup token required"
                )

    # --- step 2a: single-admin registration gate (anti-enumeration) -----------
    # SECURITY: run the closed-registration gate BEFORE the
    # duplicate-username lookup so a CLOSED endpoint returns 403 uniformly
    # whether or not the username exists. The previous order (dup-check first)
    # was an enumeration oracle: an anonymous caller diffed 400 "username
    # already taken" vs 403 "registration is closed" to discover valid
    # usernames — defeating login()'s uniform anti-enumeration hardening. The
    # first-user path (count==0) and a valid invite both stay open, so the
    # dup-check below only runs for callers who are authorized to register.
    if not grant_admin_via_invite and await count_users(engine) > 0:
        raise HTTPException(
            status_code=403,
            detail="Registration is closed. Ask an administrator for an invite.",
        )

    # --- step 2b: duplicate-username check (only reached when open) ------------
    # Reached only when registration is open (first user, or a valid invite), so
    # the caller is authorized to register — reporting a duplicate as 400 is the
    # correct input-error precedence and leaks nothing an unauthorized caller
    # could exploit (the gate above already blocked the closed case).
    async with engine.connect() as _dup_conn:
        _dup_count: int = int(
            (
                await _dup_conn.execute(
                    select(func.count())
                    .select_from(users_table)
                    .where(users_table.c.username == username)
                )
            ).scalar_one()
        )
    if _dup_count > 0:
        raise HTTPException(status_code=400, detail="username already taken")

    # --- step 2b: password policy --------------------------------------------
    # Server-side strength check. The frontend already gates on
    # minLength={8} but that's a client-only hint; without this server-side
    # check POST /api/auth/register would accept single-character passwords.
    # Validation MUST happen before register() so we never persist a hash
    # for a non-compliant secret.
    try:
        validate_new_password(password)
    except PasswordPolicyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # --- step 3: create the user ---------------------------------------------
    try:
        user = await register(username=username, password=password, engine=engine)
    except UsernameTakenError:
        raise HTTPException(status_code=400, detail="username already taken")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    # --- step 4: consume invite + promote, if applicable ---------------------
    if grant_admin_via_invite and invite_row_id is not None:
        now = datetime.now(UTC)

        async def _consume_and_promote() -> None:
            async with engine.begin() as conn:
                # Atomic compare-and-swap on used_at — defeats races where two
                # concurrent registrations submit the same token.  Only the
                # transaction that sees used_at IS NULL wins.
                result = await conn.execute(
                    update(admin_invites_table)
                    .where(
                        admin_invites_table.c.id == invite_row_id,
                        admin_invites_table.c.used_at.is_(None),
                    )
                    .values(used_at=now, used_by=user.id)
                )
                if result.rowcount == 0:
                    # Lost the race — another registration consumed the token
                    # first.  Do NOT promote this user; the invite is spent.
                    return

                await conn.execute(
                    update(users_table)
                    .where(users_table.c.id == user.id)
                    .values(is_admin=True)
                )

        await with_write_retry(_consume_and_promote)

        log.info(
            "auth.register.invite_consumed",
            user_id=user.id,
            invite_id=invite_row_id,
        )

    response.status_code = 201
    return {"id": user.id, "username": user.username}


# ---------------------------------------------------------------------------
# POST /api/auth/login
# ---------------------------------------------------------------------------


@router.post(
    "/login",
    status_code=200,
    responses={
        400: {"description": "Malformed request body — unparseable form data"},
        401: {"description": "Invalid credentials or missing TOTP code"},
        429: {"description": "Too many login attempts — rate-limited"},
    },
)
async def login_endpoint(
    request: Request,
    response: Response,
    username: str = Form(..., min_length=1),
    password: str = Form(..., min_length=1),
    totp_code: str | None = Form(default=None),
    store: SessionStore = Depends(get_default_session_store_dep),
    engine: AsyncEngine = Depends(get_engine_dep),
) -> dict[str, Any]:
    """Authenticate a user and issue a session cookie.

    The ``POST /api/auth/login`` path is rate-limited by
    :class:`~lmchat.middleware.rate_limit.LoginRateLimitMiddleware`, which
    buffers and replays the form body.  FastAPI's ``Form(...)`` dependency
    reads from the replayed body transparently.

    Args:
        username:   Plaintext username.
        password:   Plaintext password.
        totp_code:  Optional 6-digit TOTP code.
        store:      Session store dependency.
        engine:     Engine dependency.

    Returns:
        ``{"user_id": <id>, "expires_at": <ISO-8601>, "username":
        <str>, "is_admin": <bool>, "totp_enabled": <bool>}`` with
        ``Set-Cookie: lmchat_session=...``. The ``username``,
        ``is_admin``, and ``totp_enabled`` fields let the SPA hydrate
        its auth store on login without a separate ``/me`` round-trip
        (``totp_enabled`` added in the SA-gaps fix so Settings can render
        the correct TOTP state on first paint). These fields are hydrated
        directly from the ``User`` row ``login()`` already loaded during
        verification — no separate post-login SELECT.

    Raises:
        HTTPException 401: On bad credentials or missing TOTP code.
            Response body ``{"detail": "invalid credentials"}`` for
            credential failures (uniform — does not leak which field
            was wrong).  ``{"detail": "totp required"}`` when TOTP is
            configured but no code was supplied.
    """
    ip: str | None = request.client.host if request.client else None
    ua: str | None = request.headers.get("user-agent")

    try:
        session, user = await login(
            username=username,
            password=password,
            totp_code=totp_code if totp_code else None,
            ip=ip,
            user_agent=ua,
            session_store=store,
            engine=engine,
        )
    except TotpRequiredError:
        raise HTTPException(status_code=401, detail="totp required")
    except (BadCredentialsError, AuthError):
        # Uniform: do not leak whether the username or password was wrong.
        raise HTTPException(status_code=401, detail="invalid credentials")

    _set_session_cookie(response, session.id, request)

    log.info(
        "login_endpoint success",
        user_id=session.user_id,
        session_prefix=session.id[:8] + "...",
    )

    # Hydrate the SPA's auth store directly from the User row login()
    # already loaded during verification — no separate post-login SELECT.
    # Fix: is_admin was previously hardcoded false in authStore.
    # /me is now available; the login response still
    # carries this for zero-round-trip hydration on initial login.
    # `totp_enabled` is carried here so SecuritySettings renders the
    # correct state without a /me follow-up.
    return {
        "user_id": session.user_id,
        "expires_at": session.expires_at.isoformat(),
        "username": user.username,
        "is_admin": bool(user.is_admin),
        "totp_enabled": user.totp_secret is not None,
    }


# ---------------------------------------------------------------------------
# POST /api/auth/logout
# ---------------------------------------------------------------------------


@router.post("/logout", status_code=200)
async def logout_endpoint(
    request: Request,
    response: Response,
    store: SessionStore = Depends(get_default_session_store_dep),
    engine: AsyncEngine = Depends(get_engine_dep),
) -> dict[str, str]:
    """Revoke the current session and clear the session cookie.

    Idempotent when no cookie is present: returns 200 without raising.
    If a cookie IS present but is invalid or stale, ``AuthMiddleware``
    returns 401 before this handler runs — the handler is never reached
    in that case.

    Logout sweep: BEFORE revoking the session, the user's incognito
    chats are purged so an incognito session leaves no on-disk trace
    once the session ends.  Errors during sweep are logged but do NOT
    block logout — the cookie is always cleared.

    Args:
        request: The incoming request (for cookie extraction).
        store:   Session store dependency.
        engine:  Engine dependency.

    Returns:
        ``{"status": "ok"}``.
    """
    cookie_value = request.cookies.get(_COOKIE)
    if cookie_value:
        # Logout sweep: look up the user_id BEFORE revoking the
        # session so we know who owns the chats to purge.  If the
        # session has already expired (None), there is nothing to sweep.
        try:
            existing = await store.get(cookie_value)
            chat_service = getattr(request.app.state, "chat_service", None)
            if existing is not None and chat_service is not None:
                try:
                    deleted = await chat_service.purge_user_incognito(
                        user_id=existing.user_id
                    )
                    if deleted > 0:
                        log.info(
                            "auth.logout.incognito_sweep",
                            user_id=existing.user_id,
                            deleted_count=deleted,
                        )
                except Exception as sweep_exc:  # noqa: BLE001
                    # Non-fatal: log and keep going — clearing the
                    # cookie still matters even if the sweep fails.
                    log.warning(
                        "auth.logout.incognito_sweep_failed",
                        user_id=existing.user_id,
                        error=str(sweep_exc),
                    )
        except Exception as lookup_exc:  # noqa: BLE001
            log.warning(
                "auth.logout.session_lookup_failed",
                error=str(lookup_exc),
            )

        await logout(session_id=cookie_value, session_store=store, engine=engine)

    _clear_session_cookie(response)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# POST /api/auth/password
# ---------------------------------------------------------------------------


@router.post("/password", status_code=200)
async def change_password_endpoint(
    request: Request,
    old_password: str = Form(...),
    new_password: str = Form(...),
    user: User = Depends(require_user),
    store: SessionStore = Depends(get_default_session_store_dep),
    engine: AsyncEngine = Depends(get_engine_dep),
) -> dict[str, str]:
    """Change the authenticated user's password.

    Revokes all other active sessions for the user while keeping the
    current session alive (the cookie value is preserved).

    Args:
        request:      The incoming request (for current session cookie).
        old_password: Current plaintext password (re-auth required).
        new_password: New plaintext password.
        user:         The authenticated user from :func:`require_user`.
        store:        Session store dependency.
        engine:       Engine dependency.

    Returns:
        ``{"status": "ok"}``.

    Raises:
        HTTPException 401: If ``old_password`` is incorrect.
        HTTPException 401: If no valid session (from ``require_user``).
    """
    # Server-side password policy. Frontend-only validation can be
    # bypassed (a 1-character new_password would otherwise be accepted).
    try:
        validate_new_password(new_password)
    except PasswordPolicyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    current_session_id = request.cookies.get(_COOKIE)

    try:
        await change_password(
            user_id=user.id,
            old_password=old_password,
            new_password=new_password,
            current_session_id=current_session_id,
            session_store=store,
            engine=engine,
        )
    except BadCredentialsError:
        raise HTTPException(status_code=400, detail="old password is incorrect")

    return {"status": "ok"}


# ---------------------------------------------------------------------------
# POST /api/auth/totp/setup
# ---------------------------------------------------------------------------


@router.post("/totp/setup", status_code=200)
async def totp_setup_endpoint(
    user: User = Depends(require_user),
    engine: AsyncEngine = Depends(get_engine_dep),
) -> dict[str, str]:
    """Initiate TOTP setup — returns a provisioning URI + raw secret.

    The secret is returned in the response so the user can manually enter
    it into their authenticator app if QR-code scanning is not available.
    The secret is NOT yet persisted server-side; the client must call
    ``POST /api/auth/totp/verify`` with a valid code to confirm setup.

    Args:
        user:   The authenticated user from :func:`require_user`.
        engine: Engine dependency.

    Returns:
        ``{"provisioning_uri": "otpauth://...", "secret": "<base32>"}``.
    """
    uri, secret = await setup_totp(user_id=user.id, engine=engine)
    return {"provisioning_uri": uri, "secret": secret}


# ---------------------------------------------------------------------------
# POST /api/auth/totp/verify
# ---------------------------------------------------------------------------


@router.post("/totp/verify", status_code=200)
async def totp_verify_endpoint(
    secret: str = Form(...),
    code: str = Form(...),
    user: User = Depends(require_user),
    engine: AsyncEngine = Depends(get_engine_dep),
) -> dict[str, str]:
    """Confirm a TOTP code and persist the encrypted secret.

    Called after :ref:`totp-setup` once the user has scanned the QR code
    and generated a valid code.  On success, the secret is encrypted with
    the ``enc$v1$`` envelope and stored in ``users.totp_secret``.

    Args:
        secret: The raw base32 secret returned by ``totp/setup``.
        code:   6-digit TOTP code from the user's authenticator app.
        user:   The authenticated user.
        engine: Engine dependency.

    Returns:
        ``{"status": "ok"}``.

    Raises:
        HTTPException 400: If the TOTP code is invalid.
    """
    try:
        await verify_totp_setup(
            user_id=user.id, secret=secret, code=code, engine=engine
        )
    except BadCredentialsError:
        raise HTTPException(status_code=400, detail="invalid TOTP code")

    return {"status": "ok"}


# ---------------------------------------------------------------------------
# POST /api/auth/totp/disable
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# GET /api/auth/me
# ---------------------------------------------------------------------------


@router.get("/me/probe", status_code=200)
async def me_probe_endpoint(
    request: Request,
    engine: AsyncEngine = Depends(get_engine_dep),
    store: SessionStore = Depends(get_default_session_store_dep),
) -> dict[str, Any]:
    """Mount-time hydration probe that ALWAYS returns 200.

    The SPA's `authStore.refresh()` runs once on mount to hydrate the
    session cookie state. Previously it hit `/me` which 401s when
    unauthenticated — browser DevTools then logged a red `Failed to
    load resource: 401` line on every cold load, which appeared across
    multiple surfaces.

    This endpoint mirrors `/me`'s response shape but returns
    ``{user_id: null, ...}`` instead of 401 when no session is
    present. Real protected endpoints continue to use `require_user`
    and still 401 on bad sessions — only the mount-time hydration
    is allowed to ask "is there a session?" without generating the
    noise.
    """
    from sqlalchemy import select  # noqa: PLC0415

    session_id = request.cookies.get(_COOKIE)
    user_id: int | None = None
    if session_id is not None:
        sess = await store.get(session_id)
        if sess is not None:
            user_id = int(sess.user_id)

    if user_id is None:
        return {
            "user_id": None,
            "username": None,
            "is_admin": False,
            "totp_enabled": False,
            "needs_setup": (await count_users(engine)) == 0,
            "email": None,
            "display_name": None,
            "avatar_url": None,
        }

    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(
                    users_table.c.username,
                    users_table.c.is_admin,
                    users_table.c.totp_secret,
                    users_table.c.email,
                    users_table.c.display_name,
                    users_table.c.avatar_url,
                ).where(users_table.c.id == user_id)
            )
        ).first()
    if row is None:
        # Session points to a deleted user — treat as unauth without 401.
        return {
            "user_id": None,
            "username": None,
            "is_admin": False,
            "totp_enabled": False,
            "needs_setup": (await count_users(engine)) == 0,
            "email": None,
            "display_name": None,
            "avatar_url": None,
        }
    return {
        "user_id": user_id,
        "username": row.username,
        "is_admin": bool(row.is_admin),
        "totp_enabled": row.totp_secret is not None,
        "needs_setup": False,
        "email": row.email,
        "display_name": row.display_name,
        "avatar_url": row.avatar_url,
    }


@router.get("/me", status_code=200)
async def me_endpoint(
    user: User = Depends(require_user),
    engine: AsyncEngine = Depends(get_engine_dep),
) -> dict[str, Any]:
    """Return the current authenticated user's public profile.

    Used by the SPA's ``authStore.refresh()`` to hydrate user state on
    page reload without requiring a new login.

    Args:
        user:   The authenticated user from :func:`require_user`.
        engine: Engine dependency (used to confirm the row still exists and
                to surface any out-of-band admin-flag changes).

    Returns:
        ``{"user_id": <int>, "username": <str>, "is_admin": <bool>,
        "totp_enabled": <bool>, "needs_setup": <bool>}``.  The shape
        mirrors the ``/login`` response so the SPA can use the same
        ``MeResponse`` interface for both paths.  ``totp_enabled`` is
        ``True`` iff the user row has a non-null ``totp_secret`` — the
        SPA uses this to hydrate the Settings TOTP surface on mount
        (SA-gaps: SecuritySettings was resetting to "not configured" on
        every reload).  ``needs_setup`` is ``True`` iff the
        ``users`` table is empty — when authenticated this is always
        ``False``, but the field is included for shape parity with
        ``GET /api/auth/setup_status`` so the SPA can rely on a single
        response shape.

    Raises:
        HTTPException 401: If no valid session cookie is present.
    """
    from sqlalchemy import select  # noqa: PLC0415 (local import avoids circular)

    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(
                    users_table.c.username,
                    users_table.c.is_admin,
                    users_table.c.totp_secret,
                    users_table.c.email,
                    users_table.c.display_name,
                    users_table.c.avatar_url,
                ).where(users_table.c.id == user.id)
            )
        ).first()

    # Row should always exist (session is valid), but guard defensively.
    if row is None:
        raise HTTPException(status_code=401, detail="authentication required")

    # An authenticated /me request implies at least one user exists in the
    # DB; the bootstrap window is closed. We still query count_users for
    # symmetry with /setup_status to avoid divergent code paths if a future
    # change introduces a "session valid but user row missing" state.
    user_count = await count_users(engine)

    return {
        "user_id": user.id,
        "username": row.username,
        "is_admin": bool(row.is_admin),
        "totp_enabled": row.totp_secret is not None,
        "needs_setup": user_count == 0,
        # Profile fields (migration 0020). All three are nullable until
        # the user populates them via PATCH /api/auth/profile.
        "email": row.email,
        "display_name": row.display_name,
        "avatar_url": row.avatar_url,
    }


# ---------------------------------------------------------------------------
# PATCH /api/auth/profile — user-presentable identity
# ---------------------------------------------------------------------------


@router.patch("/profile", status_code=200)
async def update_profile_endpoint(
    email: str | None = Form(default=None),
    display_name: str | None = Form(default=None),
    avatar_url: str | None = Form(default=None),
    clear: str | None = Form(default=None),
    user: User = Depends(require_user),
    engine: AsyncEngine = Depends(get_engine_dep),
) -> dict[str, Any]:
    """Patch the authenticated user's profile.

    Form-encoded fields, all optional:

    * ``email`` / ``display_name`` / ``avatar_url`` — non-empty values
      overwrite the stored column. An omitted OR empty value is a
      no-op (FastAPI's Form() collapses both into ``None``).
    * ``clear`` — comma-separated list of field names to NULL out.
      Mirrors the ``PUT /api/settings/lmstudio`` "clear" semantics so
      callers can explicitly remove a value without needing a sentinel.

    Returns the updated public-profile shape from `/api/auth/me`.

    Raises:
        HTTPException 422: When any field exceeds the max length, when
            ``email`` isn't a syntactically valid address, when
            ``avatar_url`` isn't http(s), or when ``clear`` names an
            unknown field.
    """
    from urllib.parse import urlparse

    # Caps from the shared text_input_policy so the profile route
    # aligns with the rest of the batch (original used a magic `1024`
    # everywhere). Email + display_name
    # are short identifiers; avatar_url gets the URL-sensible 2048
    # so popular CDN paths fit.
    from lmchat.utils.text_input_policy import (
        SHORT_FIELD_MAX_LENGTH as _SHORT_MAX,
    )

    _EMAIL_MAX = _SHORT_MAX
    _DISPLAY_NAME_MAX = _SHORT_MAX
    _AVATAR_URL_MAX = 2048
    _allowed_clear = {"email", "display_name", "avatar_url"}
    _patch: dict[str, Any] = {}

    def _check_max(name: str, value: str, max_len: int) -> None:
        if len(value) > max_len:
            raise HTTPException(
                status_code=422,
                detail=f"{name} must be at most {max_len} characters",
            )

    if clear is not None and clear.strip() != "":
        for raw in clear.split(","):
            field = raw.strip()
            if field not in _allowed_clear:
                raise HTTPException(
                    status_code=422,
                    detail=f"unknown clear field: {field}",
                )
            _patch[field] = None

    if email is not None and email.strip() != "":
        cleaned = email.strip()
        _check_max("email", cleaned, _EMAIL_MAX)
        # Minimal RFC-flavor check: single @, non-empty local + domain,
        # at least one dot in the domain. Deliverability is the
        # sender's problem; this is a syntactic floor.
        # Syntactic floor — deliverability is the sender's problem.
        # These gaps in the original check: multi-@ ("a@b@c.d") slipped
        # past partition; dot-prefix ("a@.tld") and dot-suffix ("a@b.")
        # domains passed. We also reject local/domain segments < 1 char.
        local, sep, domain = cleaned.partition("@")
        valid = (
            sep == "@"
            and len(local) > 0
            and len(domain) > 0
            and " " not in cleaned
            and "@" not in domain  # no multi-@
            and "." in domain
            and not domain.startswith(".")
            and not domain.endswith(".")
        )
        if not valid:
            raise HTTPException(
                status_code=422, detail="email is not a valid address"
            )
        _patch["email"] = cleaned

    if display_name is not None and display_name.strip() != "":
        cleaned = display_name.strip()
        _check_max("display_name", cleaned, _DISPLAY_NAME_MAX)
        if "\x00" in cleaned:
            raise HTTPException(
                status_code=422,
                detail="display_name must not contain null bytes",
            )
        _patch["display_name"] = cleaned

    if avatar_url is not None and avatar_url.strip() != "":
        cleaned = avatar_url.strip()
        _check_max("avatar_url", cleaned, _AVATAR_URL_MAX)
        parsed = urlparse(cleaned)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise HTTPException(
                status_code=422,
                detail="avatar_url must be an http(s) URL",
            )
        _patch["avatar_url"] = cleaned

    if not _patch:
        # Nothing to update; return current state.
        log.info("auth.profile.noop", user_id=user.id)
    else:
        async def _do_update() -> None:
            async with engine.begin() as conn:
                await conn.execute(
                    update(users_table)
                    .where(users_table.c.id == user.id)
                    .values(**_patch)
                )

        await with_write_retry(_do_update)
        log.info(
            "auth.profile.updated",
            user_id=user.id,
            fields=list(_patch.keys()),
        )

    # Mirror the `/me` response shape.
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(
                    users_table.c.username,
                    users_table.c.is_admin,
                    users_table.c.totp_secret,
                    users_table.c.email,
                    users_table.c.display_name,
                    users_table.c.avatar_url,
                ).where(users_table.c.id == user.id)
            )
        ).first()
    assert row is not None  # require_user already verified
    return {
        "user_id": user.id,
        "username": row.username,
        "is_admin": bool(row.is_admin),
        "totp_enabled": row.totp_secret is not None,
        "needs_setup": False,
        "email": row.email,
        "display_name": row.display_name,
        "avatar_url": row.avatar_url,
    }


@router.post("/totp/disable", status_code=200)
async def totp_disable_endpoint(
    password: str = Form(...),
    user: User = Depends(require_user),
    engine: AsyncEngine = Depends(get_engine_dep),
) -> dict[str, str]:
    """Disable TOTP after re-authenticating with the account password.

    Re-authentication prevents an attacker who has stolen a session from
    downgrading the account's second factor.

    Args:
        password: Current plaintext password.
        user:     The authenticated user.
        engine:   Engine dependency.

    Returns:
        ``{"status": "ok"}``.

    Raises:
        HTTPException 400: If ``password`` is incorrect.
    """
    try:
        await disable_totp(user_id=user.id, password=password, engine=engine)
    except BadCredentialsError:
        raise HTTPException(status_code=400, detail="password is incorrect")

    return {"status": "ok"}



# ---------------------------------------------------------------------------
# GET /api/auth/setup_status  (anonymous, no auth required)
# ---------------------------------------------------------------------------


@router.get("/setup_status", status_code=200)
async def setup_status_endpoint(
    engine: AsyncEngine = Depends(get_engine_dep),
) -> dict[str, bool]:
    """Return the one-bit ``needs_setup`` signal for anonymous callers.

    Used by the React Login page on mount: when ``needs_setup === true``
    the page swaps the standard sign-in form for the ``WelcomeWizard``
    (intro + create-admin-account flow).  Once the first user has
    registered the value flips to ``false`` and the standard sign-in
    form renders.

    Recon-leak shape: the response is a single
    boolean, NOT the raw row count.  Exposing the count to an
    unauthenticated probe would give an attacker free reconnaissance
    about deployment scale.  The binary is all the Login page needs.

    This endpoint is intentionally listed in the auth middleware's
    ``_SKIP_EXACT`` set so it can be called with no session cookie.

    Args:
        engine: Engine dependency.

    Returns:
        ``{"needs_setup": <bool>}`` — ``True`` iff the ``users`` table
        is empty.
    """
    user_count = await count_users(engine)
    return {"needs_setup": user_count == 0}
