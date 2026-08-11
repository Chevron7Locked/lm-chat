# SPDX-License-Identifier: Apache-2.0
"""Admin routes for lm-chat.

All routes require both ``Depends(require_admin)`` (HTTP 403 on non-admin,
HTTP 401 on unauthenticated) and ``Depends(admin_rate_limit)`` (HTTP 429
when the per-user admin bucket is exhausted — 30 req/min default).

Routes
------
GET  /api/admin/users                        — list users (paginated).
POST /api/admin/users/{user_id}/role         — set/clear admin bit.
POST /api/admin/users/{user_id}/revoke-sessions — revoke all sessions.
GET  /api/admin/audit-log                    — paginated audit_log view.
GET  /api/debug                              — server-state diagnostics (sanitised).

``UserResponse`` and ``AuditLogResponse`` are Pydantic projections that
exclude secret columns (``password_hash``, ``totp_secret``).
"""
from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from lmchat import __version__
from lmchat.db.retry import with_write_retry
from lmchat.db.schema import admin_invites as admin_invites_table
from lmchat.db.schema import audit_log as audit_log_table
from lmchat.db.schema import users as users_table
from lmchat.logging import get_logger
from lmchat.routes._dependencies import (
    admin_rate_limit,
    get_engine_dep,
    get_session_store_dep,
    require_admin,
)
from lmchat.services.audit_service import write_audit_event, write_audit_event_or_alert
from lmchat.services.auth_service import User

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_HTTP_200: Final[int] = 200
_HTTP_204: Final[int] = 204
_HTTP_404: Final[int] = 404

_DEFAULT_LIMIT: Final[int] = 50
_MAX_LIMIT: Final[int] = 200

# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class UserResponse(BaseModel):
    """Sanitised projection of the ``users`` table row.

    Deliberately excludes ``password_hash`` and ``totp_secret`` — those
    columns must never appear in any API response.

    Attributes:
        id:         User PK.
        username:   Login name.
        is_admin:   Whether the user holds admin privileges.
        created_at: Row creation timestamp.
        updated_at: Last modification timestamp.
        last_login: Timestamp of the most recent ``auth.login.success``
                    event for this user, derived from ``audit_log`` at
                    query time.  ``None`` when the user has never
                    successfully logged in (e.g. just-registered users).
                    Added for the AdminUsers page.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    is_admin: bool
    created_at: datetime
    updated_at: datetime
    last_login: datetime | None = None


class AuditLogResponse(BaseModel):
    """Sanitised projection of one ``audit_log`` row.

    Attributes:
        id:         Row PK.
        user_id:    FK to users (NULL when the user has been deleted).
        event:      Event string from the AuditEvent taxonomy.
        ip:         Source IP at event time, or None.
        user_agent: HTTP User-Agent at event time, or None.
        detail:     Event-specific JSON payload, or None.
        created_at: Event timestamp.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int | None
    event: str
    ip: str | None
    user_agent: str | None
    detail: Any
    created_at: datetime


class AuditLogPage(BaseModel):
    """Paginated audit_log response with total count.

    Bare list didn't let admin dashboards display "page X of Y" without
    re-fetching every row.

    Attributes:
        items:  Paginated rows on this page.
        total:  Total matching rows in the table (post-filter, pre-paginate).
        limit:  Effective limit applied (after _MAX_LIMIT clamp).
        offset: Offset used for this page.
    """

    model_config = ConfigDict(extra="forbid")

    items: list[AuditLogResponse]
    total: int
    limit: int
    offset: int



class AdminInviteResponse(BaseModel):
    """Response shape for ``POST /api/admin/invite``.

    Attributes:
        token:      One-shot URL-safe token (32 chars).  The admin copies
                    this and shares it with the invitee, who passes it on
                    ``POST /api/auth/register`` via ``?token=`` or
                    ``X-Setup-Token`` header.
        expires_at: UTC timestamp after which the token is rejected.
    """

    model_config = ConfigDict(extra="forbid")

    token: str
    expires_at: datetime


# ---------------------------------------------------------------------------
# Constants — admin invite
# ---------------------------------------------------------------------------

# Token lifetime for admin invites.  Long enough for an out-of-band share
# (Signal, email, etc.) but short enough that a leaked token from a stale
# chat log can't be redeemed weeks later.
_INVITE_TTL: Final[timedelta] = timedelta(hours=24)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router: APIRouter = APIRouter(prefix="/api", tags=["admin"])

# ---------------------------------------------------------------------------
# GET /api/admin/users
# ---------------------------------------------------------------------------


@router.get("/admin/users", response_model=list[UserResponse])
async def list_users(
    limit: int = _DEFAULT_LIMIT,
    offset: int = 0,
    admin: User = Depends(require_admin),
    _rl: None = Depends(admin_rate_limit),
    engine: AsyncEngine = Depends(get_engine_dep),
) -> list[UserResponse]:
    """Return a paginated list of all users.

    Results are ordered by ``id`` ascending.  ``limit`` is capped at
    :data:`_MAX_LIMIT` to prevent runaway queries.

    Each row also carries ``last_login`` — the timestamp of the most
    recent ``auth.login.success`` event for that user, derived in-query
    from ``audit_log`` so no schema change is needed on the ``users`` table.

    Args:
        limit:  Maximum number of rows (default 50, max 200).
        offset: Skip this many rows (for pagination).
        admin:  Authenticated admin user from :func:`require_admin`.
        engine: DB engine dependency.

    Returns:
        List of :class:`UserResponse` rows.
    """
    effective_limit = min(max(1, limit), _MAX_LIMIT)

    log.info(
        "admin.users.list",
        admin_id=admin.id,
        limit=effective_limit,
        offset=offset,
    )

    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(users_table)
                .order_by(users_table.c.id)
                .limit(effective_limit)
                .offset(offset)
            )
        ).fetchall()

        # Derive last_login per user from audit_log in a single grouped query.
        # Restricting by user_id IN (<page>) keeps the scan bounded to the
        # page size; ix_audit_user_id + ix_audit_event accelerate the lookup.
        user_ids = [r._mapping["id"] for r in rows]
        last_login_map: dict[int, datetime] = {}
        if user_ids:
            last_login_rows = (
                await conn.execute(
                    select(
                        audit_log_table.c.user_id,
                        func.max(audit_log_table.c.created_at).label("last_login"),
                    )
                    .where(
                        audit_log_table.c.event == "auth.login.success",
                        audit_log_table.c.user_id.in_(user_ids),
                    )
                    .group_by(audit_log_table.c.user_id)
                )
            ).fetchall()
            last_login_map = {
                int(r._mapping["user_id"]): r._mapping["last_login"]
                for r in last_login_rows
                if r._mapping["user_id"] is not None
            }

    out: list[UserResponse] = []
    for row in rows:
        mapping = dict(row._mapping)
        mapping["last_login"] = last_login_map.get(int(mapping["id"]))
        out.append(UserResponse(**mapping))
    return out


# ---------------------------------------------------------------------------
# POST /api/admin/users/{user_id}/role
# ---------------------------------------------------------------------------


async def _is_last_admin(engine: AsyncEngine, user_id: int) -> bool:
    """True if *user_id* is currently an admin AND the only remaining admin.

    Used to block demote/delete operations that would leave the deployment with
    zero admins — an unrecoverable lockout, since registration is closed and
    ``POST /api/admin/invite`` itself requires admin. Every invite grants full
    admin, so this is the sole safety net against a co-admin (or the last admin
    self-demoting) locking everyone out.
    """
    async with engine.connect() as conn:
        target_is_admin = (
            await conn.execute(
                select(users_table.c.is_admin).where(users_table.c.id == user_id)
            )
        ).scalar()
        admin_count = (
            await conn.execute(
                select(func.count())
                .select_from(users_table)
                .where(users_table.c.is_admin)
            )
        ).scalar_one()
    return bool(target_is_admin) and admin_count <= 1


def _not_sole_admin_clause() -> Any:
    """A WHERE fragment that is True unless the row is the sole remaining admin.

    Appended to a demotion UPDATE / admin DELETE so the last-admin condition is
    evaluated ATOMICALLY inside the statement's write lock — closing the
    check-then-act TOCTOU that the ``_is_last_admin`` pre-check alone cannot (two
    concurrent demotions both passing the pre-check and dropping the admin count
    to zero). Excludes the row when it is currently an admin AND the admin count
    is <= 1.
    """
    admin_count = (
        select(func.count())
        .select_from(users_table)
        .where(users_table.c.is_admin)
        .scalar_subquery()
    )
    return ~(users_table.c.is_admin & (admin_count <= 1))


@router.post("/admin/users/{user_id}/role", response_model=UserResponse)
async def set_user_role(
    user_id: int,
    request: Request,
    is_admin: bool = Form(...),
    admin: User = Depends(require_admin),
    _rl: None = Depends(admin_rate_limit),
    engine: AsyncEngine = Depends(get_engine_dep),
) -> UserResponse:
    """Set or clear the ``is_admin`` flag for *user_id*.

    Admin-only.  Emits an ``admin.users.role_changed`` audit event.
    Returns 404 if the target user does not exist (never 403 — cross-user
    access policy applies even on admin mutations).

    Args:
        user_id:  PK of the user whose role is being changed.
        request:  FastAPI Request (for IP + user-agent logging).
        is_admin: New admin status (form field).
        admin:    Performing admin user from :func:`require_admin`.
        engine:   DB engine dependency.

    Returns:
        The updated :class:`UserResponse`.

    Raises:
        HTTPException: 404 if ``user_id`` does not exist.
    """
    ip: str | None = request.client.host if request.client else None
    ua: str | None = request.headers.get("user-agent")

    log.info(
        "admin.users.role_change",
        admin_id=admin.id,
        target_user_id=user_id,
        new_is_admin=is_admin,
    )

    # LOCKOUT GUARD: refuse a demotion that would leave the
    # deployment with zero admins — an unrecoverable lockout (registration is
    # closed and /admin/invite itself requires admin). Every invite grants full
    # admin, so the sole admin self-demoting, or a co-admin demoting the last
    # other admin, would otherwise permanently lock everyone out.
    if not is_admin and await _is_last_admin(engine, user_id):
        raise HTTPException(
            status_code=400, detail="cannot remove the last remaining admin"
        )

    updated_rows: list[dict[str, Any]] = []

    async def _update() -> None:
        async with engine.begin() as conn:
            _where = [users_table.c.id == user_id]
            if not is_admin:
                # ATOMIC backstop for the pre-check above: evaluate the
                # last-admin condition INSIDE the UPDATE (write lock) so two
                # concurrent demotions can't both pass and drop admins to zero.
                _where.append(_not_sole_admin_clause())
            result = await conn.execute(
                update(users_table)
                .where(*_where)
                .values(is_admin=is_admin)
                .returning(*users_table.c)
            )
            row = result.fetchone()
            if row is not None:
                updated_rows.append(dict(row._mapping))

    await with_write_retry(_update)

    if not updated_rows:
        # rowcount 0 → either the user does not exist (404) or a concurrent
        # demotion won the race and this would now drop the admin count below 1
        # (400 — the atomic guard blocked it after the pre-check had passed).
        async with engine.connect() as conn:
            _still_exists = (
                await conn.execute(
                    select(users_table.c.id).where(users_table.c.id == user_id)
                )
            ).fetchone()
        if _still_exists is None:
            raise HTTPException(status_code=_HTTP_404, detail="user not found")
        raise HTTPException(
            status_code=400, detail="cannot remove the last remaining admin"
        )

    await write_audit_event_or_alert(
        user_id=admin.id,
        event="admin.users.role_changed",
        ip=ip,
        user_agent=ua,
        detail={"target_user_id": user_id, "new_is_admin": is_admin},
        engine=engine,
    )

    return UserResponse(**updated_rows[0])


# ---------------------------------------------------------------------------
# POST /api/admin/users/{user_id}/revoke-sessions
# ---------------------------------------------------------------------------


@router.post("/admin/users/{user_id}/revoke-sessions", status_code=_HTTP_200)
async def revoke_user_sessions(
    user_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    _rl: None = Depends(admin_rate_limit),
    engine: AsyncEngine = Depends(get_engine_dep),
    session_store=Depends(get_session_store_dep),
) -> dict[str, str]:
    """Revoke all sessions for *user_id*.

    Admin-only.  Calls ``session_store.revoke(user_id)`` to invalidate all
    active sessions for that user, forcing re-authentication.  Emits an
    ``admin.users.sessions_revoked`` audit event.

    Returns 404 if the target user does not exist.

    Args:
        user_id:       PK of the user whose sessions are to be revoked.
        request:       FastAPI Request (for IP + user-agent logging).
        admin:         Performing admin user from :func:`require_admin`.
        engine:        DB engine dependency.
        session_store: Session store dependency.

    Returns:
        ``{"status": "ok"}`` on success.

    Raises:
        HTTPException: 404 if ``user_id`` does not exist.
    """
    ip: str | None = request.client.host if request.client else None
    ua: str | None = request.headers.get("user-agent")

    log.info(
        "admin.users.revoke_sessions",
        admin_id=admin.id,
        target_user_id=user_id,
    )

    # Verify the target user exists first (404 if not).
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(users_table.c.id).where(users_table.c.id == user_id)
            )
        ).fetchone()

    if row is None:
        raise HTTPException(status_code=_HTTP_404, detail="user not found")

    await session_store.revoke(user_id)

    try:
        await write_audit_event(
            user_id=admin.id,
            event="admin.users.sessions_revoked",
            ip=ip,
            user_agent=ua,
            detail={"target_user_id": user_id},
            engine=engine,
        )
    except Exception as audit_exc:  # noqa: BLE001
        log.warning(
            "admin.revoke_sessions.audit_failed",
            target_user_id=user_id,
            error=str(audit_exc),
        )

    return {"status": "ok"}


# ---------------------------------------------------------------------------
# GET /api/admin/audit-log
# ---------------------------------------------------------------------------


@router.get("/admin/audit-log", response_model=AuditLogPage)
async def get_audit_log(
    request: Request,
    limit: int = _DEFAULT_LIMIT,
    offset: int = 0,
    event: str | None = None,
    admin: User = Depends(require_admin),
    _rl: None = Depends(admin_rate_limit),
    engine: AsyncEngine = Depends(get_engine_dep),
) -> AuditLogPage:
    """Return paginated rows from the ``audit_log`` table with a total
    count so admin dashboards can paginate client-side.

    Optionally filter by ``event`` string (exact match).  Results are
    ordered by ``created_at`` descending (most recent first).

    Emits an ``admin.audit_log.viewed`` audit event on every access so
    the audit log itself is auditable.

    Args:
        request: FastAPI Request (for IP + user-agent logging).
        limit:   Maximum rows (default 50, max 200).
        offset:  Pagination offset.
        event:   Optional exact-match filter on the ``event`` column.
        admin:   Authenticated admin user.
        engine:  DB engine dependency.

    Returns:
        :class:`AuditLogPage` containing ``items`` + ``total`` + the
        effective ``limit`` + ``offset``.
    """
    ip: str | None = request.client.host if request.client else None
    ua: str | None = request.headers.get("user-agent")

    effective_limit = min(max(1, limit), _MAX_LIMIT)

    log.info(
        "admin.audit_log.view",
        admin_id=admin.id,
        limit=effective_limit,
        offset=offset,
        event_filter=event,
    )

    stmt = (
        select(audit_log_table)
        .order_by(audit_log_table.c.created_at.desc())
        .limit(effective_limit)
        .offset(offset)
    )
    count_stmt = select(func.count()).select_from(audit_log_table)
    if event is not None:
        stmt = stmt.where(audit_log_table.c.event == event)
        count_stmt = count_stmt.where(audit_log_table.c.event == event)

    async with engine.connect() as conn:
        rows = (await conn.execute(stmt)).fetchall()
        total = int((await conn.execute(count_stmt)).scalar_one())

    try:
        await write_audit_event(
            user_id=admin.id,
            event="admin.audit_log.viewed",
            ip=ip,
            user_agent=ua,
            detail={"limit": effective_limit, "offset": offset, "event_filter": event},
            engine=engine,
        )
    except Exception as audit_exc:  # noqa: BLE001
        log.warning(
            "admin.audit_log.self_audit_failed",
            error=str(audit_exc),
        )

    items = [AuditLogResponse.model_validate(r, from_attributes=True) for r in rows]
    return AuditLogPage(
        items=items,
        total=total,
        limit=effective_limit,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# GET /api/debug
# ---------------------------------------------------------------------------


@router.get("/debug")
async def debug_info(
    request: Request,
    admin: User = Depends(require_admin),
    _rl: None = Depends(admin_rate_limit),
    engine: AsyncEngine = Depends(get_engine_dep),
) -> dict[str, Any]:
    """Return sanitised server-state diagnostics.

    Admin-only.  The response NEVER includes secrets, session tokens,
    encryption keys, or any user PII beyond counts.  Suitable for
    include-in-schema=True (visible in OpenAPI), since it is admin-gated
    by construction.

    Returns:
        dict with:
        - ``version``:      Application version string.
        - ``db_dialect``:   SQLAlchemy dialect name (``"sqlite"``, ``"postgresql"``).
        - ``model_count``:  Number of loaded LM Studio models (from ``app.state``).
        - ``stream_bucket_count``: Number of active stream-rate-limit buckets.
        - ``admin_bucket_count``:  Number of active admin-rate-limit buckets.
        - ``has_pg_trgm``:  Whether the Postgres ``pg_trgm`` extension is present.

    Raises:
        HTTPException: 403 if the requesting user is not an admin.
    """
    ip: str | None = request.client.host if request.client else None
    ua: str | None = request.headers.get("user-agent")

    log.info("admin.debug.viewed", admin_id=admin.id)

    db_dialect: str = engine.dialect.name

    # Count models from app.state.models_service (may not be set in tests).
    model_count: int = 0
    models_svc = getattr(request.app.state, "models_service", None)
    if models_svc is not None:
        try:
            loaded = await models_svc.list_loaded()
            model_count = len(loaded)
        except Exception as exc:  # noqa: BLE001
            log.warning("admin.debug.models_count_failed", error=str(exc))

    stream_bucket_count: int = 0
    stream_buckets = getattr(request.app.state, "stream_buckets", None)
    if stream_buckets is not None:
        stream_bucket_count = len(getattr(stream_buckets, "_buckets", {}))

    admin_bucket_count: int = 0
    admin_buckets = getattr(request.app.state, "admin_buckets", None)
    if admin_buckets is not None:
        admin_bucket_count = len(getattr(admin_buckets, "_buckets", {}))

    has_pg_trgm: bool = bool(getattr(request.app.state, "has_pg_trgm", False))

    try:
        await write_audit_event(
            user_id=admin.id,
            event="admin.debug.viewed",
            ip=ip,
            user_agent=ua,
            detail=None,
            engine=engine,
        )
    except Exception as audit_exc:  # noqa: BLE001
        log.warning("admin.debug.audit_failed", error=str(audit_exc))

    return {
        "version": __version__,
        "db_dialect": db_dialect,
        "model_count": model_count,
        "stream_bucket_count": stream_bucket_count,
        "admin_bucket_count": admin_bucket_count,
        "has_pg_trgm": has_pg_trgm,
    }


# ---------------------------------------------------------------------------
# GET /api/admin/users — count helper used by tests
# ---------------------------------------------------------------------------


@router.get("/admin/users/count")
async def count_users(
    admin: User = Depends(require_admin),
    _rl: None = Depends(admin_rate_limit),
    engine: AsyncEngine = Depends(get_engine_dep),
) -> dict[str, int]:
    """Return the total count of users.

    Used by admin dashboards and tests that need a lightweight total without
    fetching all rows.

    Args:
        admin:  Authenticated admin user.
        engine: DB engine dependency.

    Returns:
        ``{"count": <int>}``
    """
    async with engine.connect() as conn:
        total = await conn.scalar(select(func.count()).select_from(users_table))

    return {"count": int(total or 0)}



# ---------------------------------------------------------------------------
# DELETE /api/admin/users/{user_id}
# ---------------------------------------------------------------------------


@router.delete("/admin/users/{user_id}", status_code=_HTTP_200)
async def delete_user(
    user_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    _rl: None = Depends(admin_rate_limit),
    engine: AsyncEngine = Depends(get_engine_dep),
    session_store=Depends(get_session_store_dep),
) -> dict[str, str]:
    """Permanently delete a user account.

    Destructive action.  Cascading FKs (chats, sessions, messages, …)
    handle child rows; ``audit_log.user_id`` uses SET NULL so the audit
    trail survives.  Sessions for the doomed user are also revoked
    explicitly via ``session_store.revoke`` in case the store doesn't see
    cascade events (in-memory stores especially).

    An admin cannot delete themselves — that path returns 400 to avoid
    accidentally locking the deployment out of admin access.

    Args:
        user_id:       PK of the user to delete.
        request:       FastAPI Request (for IP + user-agent logging).
        admin:         Performing admin user from :func:`require_admin`.
        engine:        DB engine dependency.
        session_store: Session store dependency.

    Returns:
        ``{"status": "ok"}`` on success.

    Raises:
        HTTPException 400: If ``user_id`` equals the performing admin.
        HTTPException 404: If ``user_id`` does not exist.
    """
    if user_id == admin.id:
        raise HTTPException(
            status_code=400, detail="cannot delete the currently signed-in admin"
        )

    ip: str | None = request.client.host if request.client else None
    ua: str | None = request.headers.get("user-agent")

    log.info(
        "admin.users.delete",
        admin_id=admin.id,
        target_user_id=user_id,
    )

    # Verify the target exists first; returning 404 before destructive work
    # is cleaner than relying on rowcount semantics.
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(users_table.c.id).where(users_table.c.id == user_id)
            )
        ).fetchone()

    if row is None:
        raise HTTPException(status_code=_HTTP_404, detail="user not found")

    # LOCKOUT GUARD: refuse deleting the last remaining admin.
    # The self-delete guard above already blocks self-deletion; this makes the
    # "never drop to zero admins" invariant explicit and robust.
    if await _is_last_admin(engine, user_id):
        raise HTTPException(
            status_code=400, detail="cannot delete the last remaining admin"
        )

    # Revoke sessions BEFORE the row delete so the in-memory store records
    # the revocation; the FK CASCADE on `sessions` would purge the rows
    # anyway, but session_store may keep an independent index.
    try:
        await session_store.revoke(user_id)
    except Exception as revoke_exc:  # noqa: BLE001
        log.warning(
            "admin.users.delete.revoke_failed",
            target_user_id=user_id,
            error=str(revoke_exc),
        )

    deleted_count: list[int] = []

    async def _delete() -> None:
        async with engine.begin() as conn:
            # ATOMIC backstop (same as set_user_role): block deleting the sole
            # admin inside the DELETE's write lock, closing the mutual-concurrent-
            # deletion TOCTOU where two admins delete each other to zero.
            result = await conn.execute(
                delete(users_table).where(
                    users_table.c.id == user_id,
                    _not_sole_admin_clause(),
                )
            )
            deleted_count.append(result.rowcount)

    await with_write_retry(_delete)

    if not deleted_count or deleted_count[0] == 0:
        # The atomic guard blocked deleting the last admin — a concurrent
        # deletion won the race after the pre-check passed (existence was
        # already confirmed above, so this is the race-blocked case, not a 404).
        raise HTTPException(
            status_code=400, detail="cannot delete the last remaining admin"
        )

    await write_audit_event_or_alert(
        user_id=admin.id,
        event="admin.users.deleted",
        ip=ip,
        user_agent=ua,
        detail={"target_user_id": user_id},
        engine=engine,
    )

    return {"status": "ok"}


# ---------------------------------------------------------------------------
# POST /api/admin/invite
# ---------------------------------------------------------------------------


@router.post("/admin/invite", response_model=AdminInviteResponse)
async def issue_admin_invite(
    request: Request,
    admin: User = Depends(require_admin),
    _rl: None = Depends(admin_rate_limit),
    engine: AsyncEngine = Depends(get_engine_dep),
) -> AdminInviteResponse:
    """Generate a one-shot admin-invite token.

    The token is consumed by ``POST /api/auth/register`` (see
    ``register_endpoint`` in ``routes/auth.py``).  On consumption the
    registering user is granted ``is_admin=True`` regardless of whether
    they are the first user.

    The token rides the same wire format as
    the bootstrap ``LM_CHAT_SETUP_TOKEN`` (``?token=`` query param or
    ``X-Setup-Token`` header).  Keeps the registration endpoint surface
    flat — no second `/api/auth/register_via_invite` to maintain.

    Args:
        request: FastAPI Request (for IP + user-agent logging).
        admin:   Performing admin user.
        engine:  DB engine dependency.

    Returns:
        :class:`AdminInviteResponse` containing the token + UTC expiry.
    """
    ip: str | None = request.client.host if request.client else None
    ua: str | None = request.headers.get("user-agent")

    token = secrets.token_urlsafe(24)
    expires_at = datetime.now(UTC) + _INVITE_TTL

    async def _insert() -> None:
        async with engine.begin() as conn:
            await conn.execute(
                insert(admin_invites_table).values(
                    token=token,
                    created_by=admin.id,
                    expires_at=expires_at,
                )
            )

    await with_write_retry(_insert)

    log.info(
        "admin.invite.issued",
        admin_id=admin.id,
        token_prefix=token[:8] + "...",
        expires_at=expires_at.isoformat(),
    )

    try:
        await write_audit_event(
            user_id=admin.id,
            event="admin.invite.issued",
            ip=ip,
            user_agent=ua,
            # Store only the prefix — the full token is the secret.
            detail={
                "token_prefix": token[:8],
                "expires_at": expires_at.isoformat(),
            },
            engine=engine,
        )
    except Exception as audit_exc:  # noqa: BLE001
        log.warning("admin.invite.audit_failed", error=str(audit_exc))

    return AdminInviteResponse(token=token, expires_at=expires_at)
