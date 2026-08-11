# SPDX-License-Identifier: Apache-2.0
"""Audit-log write service for lm-chat.

The audit_log table is a durable compliance record — separate from the
operational structlog stream — that persists structured events which
cannot be recovered after log rotation.

Design decisions:

- **Literal event taxonomy**: the ``AuditEvent`` type alias constrains
  the ``event`` parameter to the known event strings; typos produce a
  pyright type error at call sites rather than silently writing garbage
  to the audit table.

- **user_id=None**: supported for events where the actor is not yet known
  (e.g. a login failure for a completely unknown username — the user_id
  column allows NULL and uses ON DELETE SET NULL so existing audit rows
  survive user deletion).

- **Best-effort writes**: ``write_audit_event`` is designed to be called
  inside a try/except in auth_service so that a transient DB write failure
  does not prevent the auth response from being returned to the client.
  The exception is logged at WARNING level; the caller decides whether to
  surface it further.

- **Loud-on-failure for the security-critical subset**: for
  :data:`CRITICAL_AUDIT_EVENTS`, a silent WARNING is not
  enough — losing a login/logout/role-change/deletion row from the
  compliance record under DB write pressure must be noticed by ops.
  :func:`write_audit_event_or_alert` wraps :func:`write_audit_event` for
  those call sites: the caller's action (already-succeeded by the time the
  audit write runs) is never blocked or rolled back — this is a
  **proceed-but-loud** design, not a blocking/atomic-transaction one — but
  a failed write is escalated to an ERROR log line plus a
  :data:`AUDIT_WRITE_FAILURES_CRITICAL` metric increment, carrying the
  full event payload so ops can reconstruct the row out-of-band.
"""
from __future__ import annotations

from typing import Any, Literal

from prometheus_client import Counter
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from lmchat.db.engine import get_engine
from lmchat.db.retry import with_write_retry
from lmchat.db.schema import audit_log
from lmchat.logging import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Event taxonomy — all valid audit event strings.
# Adding a new event requires a change here + the corresponding call site.
# ---------------------------------------------------------------------------

AuditEvent = Literal[
    "auth.register",
    "auth.login.success",
    "auth.login.failure",
    "auth.logout",
    "auth.password.changed",
    "auth.password.change.failure",
    "auth.totp.setup_initiated",
    "auth.totp.verified",
    "auth.totp.setup.failure",
    "auth.totp.disabled",
    # MCP config parse/schema failures written at startup
    "mcp.config.parse_error",
    # Chat lifecycle events
    "chat.created",
    "chat.deleted",
    # /clear — all messages removed, chat shell retained
    "chat.cleared",
    "chat.compacted",
    "chat.forked",
    "chat.renamed",
    "chat.moved",
    "chat.pinned",
    "chat.reordered",
    # Incognito flag toggled on a chat (only allowed on empty chats
    # per the privacy-invariant rule in ChatService.set_incognito).
    "chat.incognito_set",
    # Message lifecycle events
    "message.appended",
    "message.edited",
    "message.deleted",
    # Streaming lifecycle events
    "stream.draft_reaped",
    "stream.disconnected",
    "stream.upstream_error",
    "stream.completed",
    "stream.aborted",
    # 2026-06-24: per-turn tool-round cap cut a runaway local tool-call loop
    # and finalized with partial content (see streaming_service tool_loop_cap).
    "stream.tool_loop_capped",
    # Admin-surface events
    "admin.users.role_changed",
    "admin.users.sessions_revoked",
    "admin.debug.viewed",
    "admin.audit_log.viewed",
    # Admin users page additions
    "admin.users.deleted",
    "admin.invite.issued",
    # Edit user message + regenerate assistant
    "message.edit_user",
    "message.delete_from_onward",
    # Resend: rewind to a user message (delete everything after it, then replay)
    "message.delete_after_for_resend",
    # Folder catalogue CRUD
    "folder.added",
    "folder.renamed",
    "folder.deleted",
    # Memory edit + refine + restore
    "memory.insight.edited",
    "memory.refine",
    "memory.restore",
]


async def write_audit_event(
    *,
    user_id: int | None,
    event: AuditEvent,
    ip: str | None,
    user_agent: str | None,
    detail: dict[str, Any] | None,
    engine: AsyncEngine | None = None,
) -> None:
    """Insert one row into the ``audit_log`` table.

    Writes are wrapped in :func:`~lmchat.db.retry.with_write_retry` to
    handle SQLite WAL contention.

    This function is intentionally *not* wrapped in a try/except — callers
    (``auth_service``) must wrap it in their own try/except so audit
    failures do not propagate to the client.  Wrapping here would hide
    unexpected exceptions from the auth service.

    Args:
        user_id:    FK into ``users.id``.  ``None`` for anonymous events
                    (e.g. a login attempt for an unknown username).
        event:      A string from the :data:`AuditEvent` taxonomy.
        ip:         Source IP address, or ``None`` if unavailable.
        user_agent: HTTP User-Agent string, or ``None``.
        detail:     Event-specific payload stored in the JSON column.
                    ``None`` is fine for events with no additional context.
        engine:     Optional engine override; defaults to the application
                    singleton from :func:`~lmchat.db.engine.get_engine`.
    """
    resolved_engine: AsyncEngine = engine if engine is not None else get_engine()

    async def _insert() -> None:
        async with resolved_engine.begin() as conn:
            await conn.execute(
                insert(audit_log).values(
                    user_id=user_id,
                    event=event,
                    ip=ip,
                    user_agent=user_agent,
                    detail=detail,
                )
            )

    await with_write_retry(_insert)

    log.debug(
        "audit event written",
        user_id=user_id,
        audit_event=event,
    )


# ---------------------------------------------------------------------------
# Loud-on-failure escalation for the security-critical event subset.
#
# Design: PROCEED-BUT-LOUD, not blocking/atomic. The
# user-visible action (login, logout, role change, user deletion) has
# already succeeded by the time the audit write happens; a failed audit
# write must NOT roll it back or delay the response. It must, however,
# stop being invisible — today every call site swallows the exception into
# a `log.warning`, which is indistinguishable from routine noise under DB
# write pressure and silently drops exactly the events a compliance/audit
# review cares about most.
#
# `write_audit_event_or_alert` centralizes that escalation: ERROR log +
# metric increment + the full event payload for reconstruction, then
# swallows the exception (never propagates) so call sites can `await` it
# directly without their own try/except. Non-critical audit sites are
# unaffected — they keep calling `write_audit_event` directly, wrapped in
# their own best-effort try/except + WARNING, exactly as before.
# ---------------------------------------------------------------------------

CRITICAL_AUDIT_EVENTS: frozenset[AuditEvent] = frozenset(
    {
        "auth.login.success",
        "auth.login.failure",
        "auth.logout",
        "admin.users.role_changed",
        "admin.users.deleted",
    }
)

AUDIT_WRITE_FAILURES_CRITICAL: Counter = Counter(
    "lmchat_audit_write_failures_critical_total",
    (
        "Count of failed audit-log writes for the security-critical event "
        "subset (login success/failure, logout, role change, user "
        "deletion). Each increment means a compliance-relevant event did "
        "not reach the durable audit_log table; see the paired ERROR log "
        "line for reconstruction context."
    ),
    labelnames=("event",),
)


async def write_audit_event_or_alert(
    *,
    user_id: int | None,
    event: AuditEvent,
    ip: str | None,
    user_agent: str | None,
    detail: dict[str, Any] | None,
    engine: AsyncEngine | None = None,
) -> None:
    """Write a security-critical audit event; escalate loudly on failure.

    Thin wrapper around :func:`write_audit_event` for the
    :data:`CRITICAL_AUDIT_EVENTS` subset (login success/failure, logout,
    role change, user deletion). The caller's user-visible action has
    already succeeded by the time this runs — a failed audit write must
    NOT block or roll it back (operator-locked "proceed-but-loud"
    decision; the audit INSERT is deliberately NOT folded into the
    mutation's own transaction, which would make audit-write latency or
    failures block the user-visible action).

    On failure this logs at ERROR — not the WARNING used by best-effort
    audit sites elsewhere — and increments
    :data:`AUDIT_WRITE_FAILURES_CRITICAL`, carrying the full event payload
    so ops can reconstruct the row out-of-band. The exception is swallowed
    here (never propagated) so callers can `await` this directly without
    their own try/except.

    Args:
        user_id:    See :func:`write_audit_event`.
        event:      One of :data:`CRITICAL_AUDIT_EVENTS`.
        ip:         See :func:`write_audit_event`.
        user_agent: See :func:`write_audit_event`.
        detail:     See :func:`write_audit_event`.
        engine:     See :func:`write_audit_event`.

    Raises:
        ValueError: If *event* is not a member of
            :data:`CRITICAL_AUDIT_EVENTS` — this helper exists specifically
            to centralize the loud-on-failure path for that subset; a
            non-critical event here would be a call-site bug, not an
            audit-write failure, so it fails fast instead of silently
            alerting on the wrong thing.
    """
    if event not in CRITICAL_AUDIT_EVENTS:
        raise ValueError(
            f"write_audit_event_or_alert called with non-critical event: {event!r}"
        )

    try:
        await write_audit_event(
            user_id=user_id,
            event=event,
            ip=ip,
            user_agent=user_agent,
            detail=detail,
            engine=engine,
        )
    except Exception as audit_exc:  # noqa: BLE001 — must not propagate (proceed-but-loud)
        AUDIT_WRITE_FAILURES_CRITICAL.labels(event=event).inc()
        log.error(
            "critical audit write failed — compliance record incomplete",
            audit_event=event,
            user_id=user_id,
            ip=ip,
            user_agent=user_agent,
            detail=detail,
            error=str(audit_exc),
            error_type=type(audit_exc).__name__,
        )
