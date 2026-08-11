# SPDX-License-Identifier: Apache-2.0
"""Authentication service for lm-chat.

Free functions (not a class) so callers/tests can inject an explicit
``engine`` and ``session_store`` rather than relying on hidden instance
state.  Production routes use ``Depends(get_default_session_store)``.

Constant-time dummy verify: unknown-username logins run a real scrypt
verify against a lazily-cached dummy hash (computed on first ``login()``
call, at production cost) so the response takes the same wall-clock time
as a wrong-password attempt.  A literal pre-baked digest would make the
unknown-username path near-instant and leak nothing — the dummy MUST run
the real verify_password path.  The goal is order-of-magnitude timing
equivalence, not millisecond equality; CI noise would flake a tighter bound.

Single-session enforcement: when ``lm_chat_single_session`` is True,
``login()`` revokes all prior sessions for the user before creating the
new one, serialized per-user-id via ``_single_session_locks`` (see that
dict's docstring — the lock only closes the race within a single worker
process; multi-worker/replica deployments need a distributed store).

Audit writes (``audit_log`` table) never block the auth response.  Login/
logout events route through ``write_audit_event_or_alert`` for escalated
(ERROR + counter) logging on write failure since they're security-critical.
"""
from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime
from typing import Final, cast

from fastapi import Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from lmchat.config import get_settings
from lmchat.db.engine import get_engine
from lmchat.db.retry import with_write_retry
from lmchat.db.schema import users
from lmchat.logging import get_logger
from lmchat.services.audit_service import write_audit_event, write_audit_event_or_alert
from lmchat.services.auth_errors import (
    AuthError,
    BadCredentialsError,
    TotpNotConfiguredError,
    TotpRequiredError,
    UsernameTakenError,
)
from lmchat.session.base import Session, SessionStore
from lmchat.session.sqlite_store import SQLiteSessionStore
from lmchat.utils.encryption import decrypt, encrypt
from lmchat.utils.hashing import (
    DEFAULT_N,
    DEFAULT_P,
    DEFAULT_R,
    hash_password,
    needs_rehash,
    verify_password,
)
from lmchat.utils.totp import generate_secret, provisioning_uri
from lmchat.utils.totp import verify as totp_verify

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_USERNAME_RE: Final[re.Pattern[str]] = re.compile(r"^[a-zA-Z0-9_]{3,64}$")
_SESSION_COOKIE: Final[str] = "lmchat_session"
_BEARER_PREFIX: Final[str] = "Bearer "

# Dummy-hash cache for the constant-time unknown-username path. Populated
# lazily on the first login() call (production cost params); asyncio.Lock
# avoids redundant concurrent computation.
_dummy_hash_cache: str | None = None
_dummy_hash_lock: asyncio.Lock = asyncio.Lock()

# Per-user-id lock: without it, two concurrent login() calls for the SAME
# user can each create()/rotate() their own session without colliding,
# leaving two live sessions and violating single-session. Only closes the
# race within THIS process (SQLite sessions are single-process — see
# SessionStore.supports_distributed_revoke); unbounded dict growth is
# accepted for this local-first, single-admin app.
_single_session_locks: dict[int, asyncio.Lock] = {}


def _reset_dummy_hash_cache() -> None:
    """Clear the dummy hash cache (test-only; forces recomputation at a
    different scrypt cost).  Safe to call from sync fixtures.
    """
    global _dummy_hash_cache
    _dummy_hash_cache = None


def _reset_single_session_locks() -> None:
    """Clear the per-user-id single-session lock dict (test-only).

    ``asyncio.Lock`` only binds to an event loop when first contended, so
    reusing a lock across pytest-asyncio's per-test event loops is safe
    unless two tests contend the same user_id's lock concurrently — reset
    between tests exercising single-session mode to avoid that.  Safe to
    call from sync fixtures.
    """
    _single_session_locks.clear()


async def _get_dummy_hash(*, n: int = DEFAULT_N, r: int = DEFAULT_R, p: int = DEFAULT_P) -> str:
    """Return the cached dummy scrypt hash, computing it once on first call.

    Computed at full cost (DEFAULT_N) so timing matches the real verify
    path within an order of magnitude — not millisecond-exact, since CI
    noise would flake a tighter bound.  ``n``/``r``/``p`` let tests override
    the cost (first call wins the cache); production always uses the
    default.
    """
    global _dummy_hash_cache
    if _dummy_hash_cache is not None:
        return _dummy_hash_cache
    async with _dummy_hash_lock:
        # Double-checked locking: another coroutine may have computed it
        # while we were waiting for the lock.
        if _dummy_hash_cache is not None:
            return _dummy_hash_cache
        # Executor avoids blocking the event loop for scrypt's ~50ms.
        loop = asyncio.get_event_loop()
        _dummy_hash_cache = await loop.run_in_executor(
            None,
            lambda: hash_password(
                "dummy-password-for-timing-attack-mitigation",
                n=n,
                r=r,
                p=p,
            ),
        )
    # cast(): pyright can't narrow the module-level Optional through the
    # lock scope; it's non-None here since the executor assigned it above.
    return cast(str, _dummy_hash_cache)


async def _dummy_verify(
    password: str, *, n: int = DEFAULT_N, r: int = DEFAULT_R, p: int = DEFAULT_P
) -> bool:
    """Verify *password* against the cached dummy hash — always False.

    Called on the unknown-username path so it takes the same time as the
    wrong-password path (defeats username enumeration via latency).  Must
    call the real verify_password, not a literal compare_digest, or the
    unknown-username path would be near-instant and leak nothing.

    Args:
        password: Plaintext candidate password from the login request.
    """
    dummy = await _get_dummy_hash(n=n, r=r, p=p)
    # Executor avoids blocking the event loop for scrypt's ~50ms.
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: verify_password(password, dummy))


# ---------------------------------------------------------------------------
# User model
# ---------------------------------------------------------------------------


class User(BaseModel):
    """Immutable view of a ``users`` row.

    ``password_hash``/``totp_secret`` are present for internal use only —
    callers MUST NOT serialize this model directly to API responses; routes
    should project to a response-specific schema that omits them.
    ``totp_secret`` holds the raw ``enc$v1$`` ciphertext, or ``None``.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    is_admin: bool
    created_at: datetime
    updated_at: datetime
    # Profile columns (nullable; old accounts hydrate as None).
    email: str | None = None
    display_name: str | None = None
    avatar_url: str | None = None
    # Internal-only fields — never expose in API response schemas.
    password_hash: str
    totp_secret: str | None  # enc$v1$ ciphertext, or NULL


# ---------------------------------------------------------------------------
# Module-level default store accessor
# ---------------------------------------------------------------------------


def get_default_session_store() -> SessionStore:
    """Return the application-global ``SQLiteSessionStore`` (FastAPI Depends target).

    Tests should construct a store directly with a test engine instead.
    """
    return SQLiteSessionStore(engine=get_engine())


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _validate_username(username: str) -> None:
    """Raise ``ValueError`` if *username* fails ``^[a-zA-Z0-9_]{3,64}$``."""
    if not _USERNAME_RE.fullmatch(username):
        raise ValueError(
            "username must be 3–64 characters and contain only letters, "
            "digits, and underscores"
        )


def _row_to_user(row: object) -> User:
    """Hydrate a :class:`User` from a SQLAlchemy ``Row``."""
    return User.model_validate(row, from_attributes=True)


async def _get_user_by_id(user_id: int, engine: AsyncEngine) -> User | None:
    """Fetch a user by primary key, or ``None`` if not found."""
    async with engine.connect() as conn:
        result = await conn.execute(select(users).where(users.c.id == user_id))
        row = result.fetchone()
    if row is None:
        return None
    return _row_to_user(row)


async def _get_user_by_username(username: str, engine: AsyncEngine) -> User | None:
    """Fetch a user by username, or ``None`` if not found."""
    async with engine.connect() as conn:
        result = await conn.execute(select(users).where(users.c.username == username))
        row = result.fetchone()
    if row is None:
        return None
    return _row_to_user(row)


async def count_users(engine: AsyncEngine | None = None) -> int:
    """Return the total row count in ``users``.

    Used by ``/api/auth/setup_status`` (exposes only the derived boolean
    ``needs_setup = count == 0`` — never the raw count, to avoid leaking
    deployment scale to an unauthenticated probe) and by the bootstrap-admin
    window gate in :func:`register_endpoint`.
    """
    resolved_engine = engine if engine is not None else get_engine()
    async with resolved_engine.connect() as conn:
        result = await conn.execute(select(func.count()).select_from(users))
        return int(result.scalar_one())


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


async def register(
    *,
    username: str,
    password: str,
    engine: AsyncEngine | None = None,
    _hash_n: int = DEFAULT_N,
    _hash_r: int = DEFAULT_R,
    _hash_p: int = DEFAULT_P,
) -> User:
    """Create a new user account and return the created :class:`User`.

    Validates the username, hashes the password at production scrypt cost,
    and writes an ``auth.register`` audit event.

    Bootstrap admin: the first user to register is granted ``is_admin=True``.
    The COUNT check and INSERT run in one write-locked transaction (BEGIN
    IMMEDIATE on SQLite) so two concurrent first-registers can't both
    observe ``count == 0`` — exactly one wins the grant.

    Raises:
        ValueError:         If the username fails the format check.
        UsernameTakenError: If the username is already in use.
    """
    _validate_username(username)

    resolved_engine = engine if engine is not None else get_engine()

    # Hash the password in an executor — scrypt is CPU/memory bound.
    loop = asyncio.get_event_loop()
    pw_hash = await loop.run_in_executor(
        None, lambda: hash_password(password, n=_hash_n, r=_hash_r, p=_hash_p)
    )

    is_sqlite = resolved_engine.dialect.name == "sqlite"

    async def _insert_user() -> tuple[int, bool]:
        # Bootstrap-admin COUNT + INSERT in one write-locked transaction so
        # two concurrent first-registers can't both observe count==0.
        async with resolved_engine.begin() as conn:
            if is_sqlite:
                # aiosqlite's implicit BEGIN DEFERRED doesn't take the write
                # lock until the first write; this no-op UPDATE promotes the
                # txn to RESERVED immediately (SQLAlchemy owns the txn
                # boundary, so we can't issue BEGIN IMMEDIATE directly).
                await conn.exec_driver_sql(
                    "UPDATE users SET is_admin = is_admin WHERE 0 = 1"
                )
            count_result = await conn.execute(
                select(func.count()).select_from(users)
            )
            existing_count = count_result.scalar_one()
            is_first = existing_count == 0
            result = await conn.execute(
                insert(users).values(
                    username=username,
                    password_hash=pw_hash,
                    is_admin=is_first,
                )
            )
            inserted = result.inserted_primary_key
            if inserted is None:
                raise RuntimeError("INSERT into users did not return a primary key")
            return int(inserted[0]), is_first

    try:
        user_id, granted_admin = await with_write_retry(_insert_user)
    except IntegrityError as exc:
        raise UsernameTakenError(f"username already taken: {username!r}") from exc

    # Re-fetch to get created_at/updated_at set by the DB.
    created_user = await _get_user_by_id(user_id, resolved_engine)
    if created_user is None:
        raise RuntimeError(f"user {user_id!r} not found after INSERT — unexpected")

    log.info(
        "user registered",
        user_id=user_id,
        username=username,
        granted_admin=granted_admin,
    )

    try:
        await write_audit_event(
            user_id=user_id,
            event="auth.register",
            ip=None,
            user_agent=None,
            detail={"username": username, "granted_admin": granted_admin},
            engine=resolved_engine,
        )
    except Exception as audit_exc:  # noqa: BLE001
        log.warning(
            "audit write failed after register",
            user_id=user_id,
            error=str(audit_exc),
        )

    return created_user


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------


async def login(
    *,
    username: str,
    password: str,
    totp_code: str | None,
    ip: str | None,
    user_agent: str | None,
    session_store: SessionStore,
    engine: AsyncEngine | None = None,
    _hash_n: int = DEFAULT_N,
    _hash_r: int = DEFAULT_R,
    _hash_p: int = DEFAULT_P,
) -> tuple[Session, User]:
    """Authenticate a user and return a new :class:`~lmchat.session.base.Session`.

    Verification chain: look up user (dummy verify on unknown username to
    defeat timing enumeration) → verify password (transparent rehash if
    cost params are stale) → TOTP if configured → single-session
    enforcement (see ``_single_session_locks``) → create + immediately
    rotate the session (defense-in-depth against fixation).

    Every path writes an ``auth_log`` audit row; audit failures never
    block the response.

    Args:
        username:      Plaintext username from the login form.
        password:      Plaintext password from the login form.
        totp_code:     6-digit TOTP code, or ``None`` if not supplied.
        ip:            Requester IP (for audit and rate-limit context).
        user_agent:    HTTP User-Agent string (for audit).
        session_store: Store to create the new session in.
        engine:        Optional engine override.

    Returns:
        ``(session, user)`` — the caller (e.g. the ``/login`` route) can
        use the already-loaded user without re-querying the DB.

    Raises:
        BadCredentialsError: Username not found, or password/TOTP wrong.
        TotpRequiredError:   User has TOTP but no ``totp_code`` was supplied.
    """
    resolved_engine = engine if engine is not None else get_engine()

    user = await _get_user_by_username(username, resolved_engine)

    if user is None:
        # Timing-equivalent to the wrong-password path.
        await _dummy_verify(password, n=_hash_n, r=_hash_r, p=_hash_p)
        log.warning("login attempt for unknown username", username=username[:32])
        await write_audit_event_or_alert(
            user_id=None,
            event="auth.login.failure",
            ip=ip,
            user_agent=user_agent,
            detail={"reason": "unknown_user"},
            engine=resolved_engine,
        )
        raise BadCredentialsError("invalid credentials")

    # --- password verification ---
    loop = asyncio.get_event_loop()
    pw_ok = await loop.run_in_executor(
        None, lambda: verify_password(password, user.password_hash)
    )
    if not pw_ok:
        log.warning("login bad password", user_id=user.id)
        await write_audit_event_or_alert(
            user_id=user.id,
            event="auth.login.failure",
            ip=ip,
            user_agent=user_agent,
            detail={"reason": "bad_password"},
            engine=resolved_engine,
        )
        raise BadCredentialsError("invalid credentials")

    # --- transparent rehash on successful verify ---
    rehash_needed = await loop.run_in_executor(
        None, lambda: needs_rehash(user.password_hash)
    )
    if rehash_needed:
        try:
            new_hash = await loop.run_in_executor(
                None, lambda: hash_password(password, n=_hash_n, r=_hash_r, p=_hash_p)
            )

            async def _rehash() -> None:
                async with resolved_engine.begin() as conn:
                    await conn.execute(
                        update(users)
                        .where(users.c.id == user.id)
                        .values(password_hash=new_hash)
                    )

            await with_write_retry(_rehash)
            log.info("password rehashed to current cost params", user_id=user.id)
        except (ValueError, SQLAlchemyError, TimeoutError) as rehash_exc:
            # Narrow catch: hash_password (ValueError), with_write_retry's DB
            # transients (SQLAlchemyError) and its retry deadline
            # (TimeoutError) are all non-fatal — the user already verified.
            # Broader Exception would hide real bugs.
            scrypt_memory_bytes = _hash_n * 128 * _hash_r * _hash_p
            log.warning(
                "password rehash failed (non-fatal)",
                user_id=user.id,
                error=str(rehash_exc),
                error_type=type(rehash_exc).__name__,
                scrypt_n=_hash_n,
                scrypt_r=_hash_r,
                scrypt_p=_hash_p,
                scrypt_memory_bytes=scrypt_memory_bytes,
                remediation_hint=(
                    f"increase OpenSSL maxmem above {scrypt_memory_bytes} bytes"
                    " if rehash repeatedly fails"
                ),
            )

    # --- TOTP verification ---
    if user.totp_secret is not None:
        if totp_code is None:
            await write_audit_event_or_alert(
                user_id=user.id,
                event="auth.login.failure",
                ip=ip,
                user_agent=user_agent,
                detail={"reason": "totp_required"},
                engine=resolved_engine,
            )
            raise TotpRequiredError("TOTP code required")

        secret_bytes = decrypt(user.totp_secret, kind="totp", record_id=user.id)
        secret = secret_bytes.decode()

        if not totp_verify(secret, totp_code):
            log.warning("login bad TOTP code", user_id=user.id)
            await write_audit_event_or_alert(
                user_id=user.id,
                event="auth.login.failure",
                ip=ip,
                user_agent=user_agent,
                detail={"reason": "bad_totp"},
                engine=resolved_engine,
            )
            raise BadCredentialsError("invalid credentials")

    # --- single-session enforcement + session creation ---
    settings = get_settings()

    async def _create_and_rotate_session() -> Session:
        """Create, rotate, and (single-session mode) re-assert exactly one
        live session for ``user.id``. Split out so only this sequence runs
        under the per-user-id lock, not the slower password/TOTP checks above.
        """
        if settings.lm_chat_single_session:
            if (
                not session_store.supports_distributed_revoke
                and os.environ.get("LM_CHAT_REPLICAS_DETECTED") == "true"
            ):
                log.warning(
                    "single_session_warning",
                    reason="non_distributed_store_in_multi_replica_deploy",
                    user_id=user.id,
                )
            await session_store.revoke(user_id=user.id)

        # --- create session ---
        new_session = await session_store.create(
            user_id=user.id,
            ttl_seconds=settings.lm_chat_session_ttl_seconds,
        )

        # --- rotate immediately (defense-in-depth) ---
        # Fixation isn't exploitable today (create() always mints a fresh
        # token, no pre-auth session concept), but rotating on every auth
        # transition (OWASP guidance) is cheap insurance against a future
        # code path introducing one.
        try:
            new_session = await session_store.rotate(new_session.id)
        except KeyError:
            # Single-session race: a concurrent login's revoke() can delete
            # the just-minted row before rotate() runs. Credentials already
            # verified are still good, so re-mint (not 500) instead of
            # rotating — revoke_others() below re-asserts single-session
            # regardless of which branch ran.
            log.info(
                "session rotate lost race with a concurrent single-session"
                " revoke; re-minting instead of 500ing a valid login",
                user_id=user.id,
            )
            new_session = await session_store.create(
                user_id=user.id,
                ttl_seconds=settings.lm_chat_session_ttl_seconds,
            )

        if settings.lm_chat_single_session:
            # Runs on BOTH paths above (not just the KeyError branch) so a
            # success/success race between two concurrent logins for the
            # same user can't leave two live sessions; whichever reaches
            # here last wins, the others are revoked.
            await session_store.revoke_others(
                user_id=user.id, except_session_id=new_session.id
            )

        return new_session

    if settings.lm_chat_single_session:
        # Single-session correctness (this lock + revoke_others) assumes a
        # SINGLE WORKER PROCESS: the lock only closes the in-process race;
        # across replicas, concurrent logins could still race revoke_others()
        # down to zero sessions. Deployment pins `--workers 1`
        # (deploy/systemd/lmchat.service); a distributed store (e.g. Redis)
        # is required before enabling this across multiple workers/replicas.
        lock = _single_session_locks.setdefault(user.id, asyncio.Lock())
        async with lock:
            session = await _create_and_rotate_session()
    else:
        session = await _create_and_rotate_session()

    log.info(
        "login success",
        user_id=user.id,
        session_prefix=session.id[:8] + "...",
    )

    await write_audit_event_or_alert(
        user_id=user.id,
        event="auth.login.success",
        ip=ip,
        user_agent=user_agent,
        detail={"session_id_prefix": session.id[:8]},
        engine=resolved_engine,
    )

    return session, user


# ---------------------------------------------------------------------------
# logout
# ---------------------------------------------------------------------------


async def logout(
    *,
    session_id: str,
    session_store: SessionStore,
    engine: AsyncEngine | None = None,
) -> None:
    """Revoke a single session and write an audit log row.

    Idempotent no-op if the session is already gone. Uses
    ``revoke_session()`` (revoke exactly this token) rather than
    ``revoke()`` (which would wipe all of the user's sessions).
    """
    resolved_engine = engine if engine is not None else get_engine()

    # Fetch first so we have user_id for the audit row.
    existing = await session_store.get(session_id)
    if existing is None:
        return

    await session_store.revoke_session(session_id)

    log.info("logout", user_id=existing.user_id, session_prefix=session_id[:8] + "...")

    await write_audit_event_or_alert(
        user_id=existing.user_id,
        event="auth.logout",
        ip=None,
        user_agent=None,
        detail={"session_id_prefix": session_id[:8]},
        engine=resolved_engine,
    )


# ---------------------------------------------------------------------------
# change_password
# ---------------------------------------------------------------------------


async def change_password(
    *,
    user_id: int,
    old_password: str,
    new_password: str,
    current_session_id: str | None,
    session_store: SessionStore,
    engine: AsyncEngine | None = None,
    _hash_n: int = DEFAULT_N,
    _hash_r: int = DEFAULT_R,
    _hash_p: int = DEFAULT_P,
) -> None:
    """Change a user's password after verifying the current one.

    Updates ``users.password_hash`` and revokes every other session
    (``revoke_others``, keeping ``current_session_id`` logged in — the
    alternative, revoke()+create(), would mint a new token the caller
    would then need to return).

    Args:
        current_session_id: Session to preserve; pass ``None`` to revoke
            ALL sessions (e.g. admin reset).

    Raises:
        BadCredentialsError: If ``old_password`` does not match the stored hash.
        AuthError:           If the user does not exist.
    """
    resolved_engine = engine if engine is not None else get_engine()

    user = await _get_user_by_id(user_id, resolved_engine)
    if user is None:
        raise AuthError(f"user {user_id!r} not found")

    loop = asyncio.get_event_loop()
    pw_ok = await loop.run_in_executor(
        None, lambda: verify_password(old_password, user.password_hash)
    )
    if not pw_ok:
        try:
            await write_audit_event(
                user_id=user_id,
                event="auth.password.change.failure",
                ip=None,
                user_agent=None,
                detail={"reason": "bad_old_password"},
                engine=resolved_engine,
            )
        except Exception as audit_exc:  # noqa: BLE001
            log.warning(
                "audit write failed on password change failure",
                user_id=user_id,
                error=str(audit_exc),
            )
        raise BadCredentialsError("old password is incorrect")

    new_hash = await loop.run_in_executor(
        None, lambda: hash_password(new_password, n=_hash_n, r=_hash_r, p=_hash_p)
    )

    async def _update_pw_cas() -> int:
        # CAS on the verified password_hash: if a concurrent change beat us
        # to it, rowcount is 0 and we raise the same error as a wrong old
        # password.
        async with resolved_engine.begin() as conn:
            result = await conn.execute(
                update(users)
                .where(users.c.id == user_id)
                .where(users.c.password_hash == user.password_hash)
                .values(password_hash=new_hash)
            )
            return result.rowcount

    rowcount = await with_write_retry(_update_pw_cas)
    if rowcount == 0:
        # Concurrent change won the race.
        try:
            await write_audit_event(
                user_id=user_id,
                event="auth.password.change.failure",
                ip=None,
                user_agent=None,
                detail={"reason": "concurrent_change_lost_race"},
                engine=resolved_engine,
            )
        except Exception as audit_exc:  # noqa: BLE001
            log.warning(
                "audit write failed on concurrent password change",
                user_id=user_id,
                error=str(audit_exc),
            )
        raise BadCredentialsError("password change conflicted with a concurrent update")

    if current_session_id is not None:
        await session_store.revoke_others(
            user_id=user_id, except_session_id=current_session_id
        )
    else:
        await session_store.revoke(user_id=user_id)

    log.info("password changed", user_id=user_id)

    try:
        await write_audit_event(
            user_id=user_id,
            event="auth.password.changed",
            ip=None,
            user_agent=None,
            detail=None,
            engine=resolved_engine,
        )
    except Exception as audit_exc:  # noqa: BLE001
        log.warning(
            "audit write failed on password changed",
            user_id=user_id,
            error=str(audit_exc),
        )


# ---------------------------------------------------------------------------
# setup_totp / verify_totp_setup / disable_totp
# ---------------------------------------------------------------------------


async def setup_totp(
    *,
    user_id: int,
    engine: AsyncEngine | None = None,
) -> tuple[str, str]:
    """Generate a fresh TOTP secret and provisioning URI; does NOT persist.

    Two-step design: caller shows the provisioning URI (QR code) to the
    user, then calls :func:`verify_totp_setup` with a valid code to confirm
    and persist — this avoids locking a user out with an unreadable secret.

    Returns:
        ``(provisioning_uri, secret)`` — keep ``secret`` in memory only
        until :func:`verify_totp_setup` confirms it.
    """
    resolved_engine = engine if engine is not None else get_engine()

    user = await _get_user_by_id(user_id, resolved_engine)
    if user is None:
        raise AuthError(f"user {user_id!r} not found")

    settings = get_settings()
    secret = generate_secret()
    uri = provisioning_uri(
        secret,
        name=user.username,
        issuer=settings.lm_chat_totp_issuer,
    )

    log.info("TOTP setup initiated", user_id=user_id)

    try:
        await write_audit_event(
            user_id=user_id,
            event="auth.totp.setup_initiated",
            ip=None,
            user_agent=None,
            detail=None,
            engine=resolved_engine,
        )
    except Exception as audit_exc:  # noqa: BLE001
        log.warning(
            "audit write failed on totp setup_initiated",
            user_id=user_id,
            error=str(audit_exc),
        )

    return uri, secret


async def verify_totp_setup(
    *,
    user_id: int,
    secret: str,
    code: str,
    engine: AsyncEngine | None = None,
) -> None:
    """Verify a TOTP code against *secret*, then encrypt and persist it.

    Called after :func:`setup_totp` once the user proves they can generate
    valid codes.

    Raises:
        BadCredentialsError: If the code is invalid.
    """
    resolved_engine = engine if engine is not None else get_engine()

    if not totp_verify(secret, code):
        log.warning("TOTP setup verification failed", user_id=user_id)
        try:
            await write_audit_event(
                user_id=user_id,
                event="auth.totp.setup.failure",
                ip=None,
                user_agent=None,
                detail={"reason": "bad_code"},
                engine=resolved_engine,
            )
        except Exception as audit_exc:  # noqa: BLE001
            log.warning(
                "audit write failed on totp setup failure",
                user_id=user_id,
                error=str(audit_exc),
            )
        raise BadCredentialsError("TOTP code is invalid")

    encrypted = encrypt(secret.encode(), kind="totp", record_id=user_id)

    async def _store_secret() -> None:
        async with resolved_engine.begin() as conn:
            await conn.execute(
                update(users)
                .where(users.c.id == user_id)
                .values(totp_secret=encrypted)
            )

    await with_write_retry(_store_secret)

    log.info("TOTP setup verified and secret stored", user_id=user_id)

    try:
        await write_audit_event(
            user_id=user_id,
            event="auth.totp.verified",
            ip=None,
            user_agent=None,
            detail=None,
            engine=resolved_engine,
        )
    except Exception as audit_exc:  # noqa: BLE001
        log.warning(
            "audit write failed on totp verified",
            user_id=user_id,
            error=str(audit_exc),
        )


async def disable_totp(
    *,
    user_id: int,
    password: str,
    engine: AsyncEngine | None = None,
) -> None:
    """Disable TOTP after re-authenticating with the password.

    Re-auth is required so an attacker who merely gains a session cannot
    downgrade the account's security by dropping the second factor.

    Raises:
        BadCredentialsError:     If ``password`` does not match.
        TotpNotConfiguredError:  If the user has no TOTP configured.
        AuthError:               If the user does not exist.
    """
    resolved_engine = engine if engine is not None else get_engine()

    user = await _get_user_by_id(user_id, resolved_engine)
    if user is None:
        raise AuthError(f"user {user_id!r} not found")

    if user.totp_secret is None:
        raise TotpNotConfiguredError("TOTP is not configured for this user")

    loop = asyncio.get_event_loop()
    pw_ok = await loop.run_in_executor(
        None, lambda: verify_password(password, user.password_hash)
    )
    if not pw_ok:
        raise BadCredentialsError("password is incorrect")

    async def _clear_secret() -> None:
        async with resolved_engine.begin() as conn:
            await conn.execute(
                update(users)
                .where(users.c.id == user_id)
                .values(totp_secret=None)
            )

    await with_write_retry(_clear_secret)

    log.info("TOTP disabled", user_id=user_id)

    try:
        await write_audit_event(
            user_id=user_id,
            event="auth.totp.disabled",
            ip=None,
            user_agent=None,
            detail=None,
            engine=resolved_engine,
        )
    except Exception as audit_exc:  # noqa: BLE001
        log.warning(
            "audit write failed on totp disabled",
            user_id=user_id,
            error=str(audit_exc),
        )


# ---------------------------------------------------------------------------
# get_current_user — FastAPI dependency
# ---------------------------------------------------------------------------


async def get_current_user(
    request: Request,
    session_store: SessionStore | None = None,
    engine: AsyncEngine | None = None,
) -> User | None:
    """FastAPI dependency: resolve the requesting user from the session.

    Priority: HttpOnly cookie ``lmchat_session`` (standard, SameSite=Lax,
    CSRF surface minimised since the SPA never reads it), then
    ``Authorization: Bearer <token>`` (for API/CLI clients). Returns
    ``None`` if neither is present or the session is expired/absent —
    routes requiring auth wrap this with ``require_user()`` (401 on None).
    """
    resolved_engine = engine if engine is not None else get_engine()
    store = session_store if session_store is not None else get_default_session_store()

    # 1. Cookie (preferred).
    session_id: str | None = request.cookies.get(_SESSION_COOKIE)

    # 2. Authorization header fallback.
    if not session_id:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith(_BEARER_PREFIX):
            session_id = auth_header[len(_BEARER_PREFIX) :].strip()

    if not session_id:
        return None

    session = await store.get(session_id)
    if session is None:
        return None

    user = await _get_user_by_id(session.user_id, resolved_engine)
    return user
