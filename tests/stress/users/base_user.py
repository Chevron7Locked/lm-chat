# SPDX-License-Identifier: Apache-2.0
"""Base Locust user — authenticated session bootstrap shared by all scenarios.

Authentication posture:

- lm-chat does NOT use a CSRF token middleware.
- The CSRF defense is ``SameSite=Lax`` on the ``lm_chat_session`` cookie + same-origin SPA.
- Locust users therefore POST ``/api/auth/login`` (form-encoded), receive
  the cookie, and the underlying ``requests.Session`` preserves it
  automatically.  NO ``X-CSRF-Token`` header anywhere.

Account provisioning
--------------------
The orchestrator (``run_stress.py``) creates the stress user pool before
firing Locust.  Each user gets a deterministic username
``stress_u{NNNN}`` and password ``stress-pw-{NNNN}``.  When a user-class
``on_start`` runs, it picks one credential from the pool keyed off
Locust's ``environment.runner.user_count`` so multiple workers don't
collide on the same identity.

TOTP-enabled accounts (scenario S4) follow the documented two-step login:

    1. POST username + password -> 401 ``{"detail": "totp required"}``
    2. POST username + password + totp_code (Form) -> 200 + cookie

The TOTP secret for those accounts is provisioned by the orchestrator
and passed via the ``STRESS_TOTP_SECRETS_FILE`` env var, a JSON map
``{username: base32_secret}``.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

from locust import HttpUser
from locust.clients import HttpSession

# Try to import pyotp for the TOTP scenario.  pyotp is already a runtime
# dep (used by the auth router); no extra installation needed.
try:
    import pyotp
except ImportError:  # pragma: no cover - pyotp is in the runtime deps
    pyotp = None  # type: ignore[assignment]


log = logging.getLogger("stress.base_user")

DEFAULT_HOST = os.environ.get("STRESS_HOST", "http://127.0.0.1:8000")
USER_POOL_FILE = os.environ.get(
    "STRESS_USER_POOL_FILE", "target/stress/user_pool.json"
)
TOTP_SECRETS_FILE = os.environ.get(
    "STRESS_TOTP_SECRETS_FILE", "target/stress/totp_secrets.json"
)


# ---------------------------------------------------------------------------
# Credential pool — process-wide round-robin so concurrent users don't
# collide on the same identity.  Locust runs greenlets on one process
# (default); a threading.Lock + module-level cursor is sufficient.
# ---------------------------------------------------------------------------

_CRED_LOCK = threading.Lock()
_CRED_CURSOR = 0
_CRED_POOL: list[dict[str, Any]] | None = None


def _load_credential_pool() -> list[dict[str, Any]]:
    """Load the credential pool from ``USER_POOL_FILE``.

    The orchestrator writes this file before invoking Locust.  Each entry
    is a dict ``{"username": ..., "password": ..., "totp": <secret|None>}``.

    Raises:
        FileNotFoundError: when the pool file is missing — indicates the
            orchestrator failed to set up users.  Re-raised verbatim so
            the admin sees a clear diagnostic.
    """
    global _CRED_POOL
    if _CRED_POOL is None:
        with open(USER_POOL_FILE, encoding="utf-8") as fh:
            _CRED_POOL = json.load(fh)
        if not isinstance(_CRED_POOL, list) or not _CRED_POOL:
            raise RuntimeError(
                f"credential pool at {USER_POOL_FILE} is empty or wrong shape"
            )
    return _CRED_POOL


def _next_credential() -> dict[str, Any]:
    """Round-robin next credential, wrapping at pool end.

    Returns:
        ``{"username", "password", "totp"}`` dict.
    """
    global _CRED_CURSOR
    pool = _load_credential_pool()
    with _CRED_LOCK:
        cred = pool[_CRED_CURSOR % len(pool)]
        _CRED_CURSOR += 1
    return cred


# ---------------------------------------------------------------------------
# Login helper — used by on_start on every user class.
# ---------------------------------------------------------------------------


def login_with_credential(
    client: HttpSession,
    credential: dict[str, Any],
    *,
    request_name: str = "POST /api/auth/login",
) -> None:
    """Perform the form-encoded login flow.

    Side effects:
        The ``lm_chat_session`` cookie is set on ``client``'s cookie jar.
        All subsequent requests on the same client ride the cookie
        automatically — no header injection, no CSRF token.

    Args:
        client:        The Locust HTTP session (or any
                       ``requests.Session``-like object).
        credential:    ``{"username", "password", "totp": secret|None}``.
        request_name:  The Locust event name used to bucket login latency.
                       Override to ``... ::warmup`` during the warm-up
                       phase so the SLO reporter excludes it.

    Raises:
        RuntimeError: when the final POST does not return 200.
    """
    username = credential["username"]
    password = credential["password"]
    totp_secret = credential.get("totp")

    # First step — username + password.
    resp = client.post(
        "/api/auth/login",
        data={"username": username, "password": password},
        name=request_name,
        catch_response=True,
    )

    needs_totp = False
    with resp:  # locust catch_response context
        if resp.status_code == 401 and totp_secret:
            # Per the auth router, missing-TOTP returns 401 with
            # ``detail="totp required"``.  Treat this as expected and
            # proceed to step 2.
            try:
                body = resp.json()
            except Exception:  # noqa: BLE001
                body = {}
            if body.get("detail") == "totp required":
                needs_totp = True
                resp.success()
            else:
                resp.failure(
                    f"login step 1 failed: 401 with body {body!r}"
                )
                raise RuntimeError(
                    f"login step 1 failed for {username}: {body!r}"
                )
        elif resp.status_code == 200:
            resp.success()
        else:
            resp.failure(
                f"login failed: status={resp.status_code} body={resp.text[:200]!r}"
            )
            raise RuntimeError(
                f"login failed for {username}: status={resp.status_code}"
            )

    if needs_totp:
        if pyotp is None:  # pragma: no cover
            raise RuntimeError("pyotp not installed but TOTP login required")
        assert isinstance(totp_secret, str), (
            "needs_totp set without a string secret in credential pool"
        )
        code = pyotp.TOTP(totp_secret).now()
        resp2 = client.post(
            "/api/auth/login",
            data={
                "username": username,
                "password": password,
                "totp_code": code,
            },
            name=request_name + " (totp)",
            catch_response=True,
        )
        with resp2:
            if resp2.status_code != 200:
                resp2.failure(
                    f"totp login failed: status={resp2.status_code} "
                    f"body={resp2.text[:200]!r}"
                )
                raise RuntimeError(
                    f"totp login failed for {username}: {resp2.status_code}"
                )
            resp2.success()


def logout(client: HttpSession) -> None:
    """Best-effort session cleanup on user_stop.

    A failure here is not fatal — the orchestrator's TTL sweep will
    cull stale sessions either way.  Errors are downgraded to debug.
    """
    try:
        client.post(
            "/api/auth/logout",
            name="POST /api/auth/logout",
            catch_response=False,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("logout (best-effort) failed: %s", exc)


# ---------------------------------------------------------------------------
# Base classes
# ---------------------------------------------------------------------------


class StressUser(HttpUser):
    """All-purpose Locust user with authenticated session bootstrap.

    Subclasses must:
      - declare ``wait_time`` (Locust convention) and any ``@task``
        methods.  See ``streaming_user.py`` for the canonical pattern.

    The host defaults to ``STRESS_HOST`` env var (``http://127.0.0.1:8000``
    when unset).  Set on the Locust command line with ``--host`` to
    override.

    Subclasses generally do not need to override ``on_start`` /
    ``on_stop``; this base does the canonical login + logout.  If a
    scenario needs to log in as a SPECIFIC user (e.g. session-revoke
    needs to know its own session ID for admin revocation in another
    user), override ``credential`` to pin one entry from the pool.
    """

    abstract = True  # don't instantiate StressUser directly
    host = DEFAULT_HOST

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # The credential picked at on_start; subclasses may inspect it.
        self.credential: dict[str, Any] = {}

    def on_start(self) -> None:
        """Pick credentials + perform login."""
        self.credential = _next_credential()
        login_with_credential(self.client, self.credential)

    def on_stop(self) -> None:
        """Best-effort logout to free server-side session rows."""
        logout(self.client)


# ---------------------------------------------------------------------------
# Helpers shared by multiple user classes
# ---------------------------------------------------------------------------


def warmup_tag(name: str) -> str:
    """Return ``<name> ::warmup`` if the harness is in warmup phase.

    The SLO reporter strips events whose name ends with ``::warmup``
    from p95/p99/error-budget arithmetic.
    """
    if os.environ.get("STRESS_PHASE") == "warmup":
        return f"{name} ::warmup"
    return name


@contextmanager
def measured_block(
    client: HttpSession, name: str, *, expected_status: int = 200
) -> Iterator[Any]:
    """Wrap an arbitrary block as a Locust event with explicit name.

    Useful for combining several requests into one measured outcome
    (e.g. share-token end-to-end).

    Args:
        client:          The Locust HTTP session.
        name:            Event name reported to Locust.
        expected_status: Status code that counts as success when set on
                         the response object via ``set_status_code``.

    Yields:
        A mutable state dict; setting ``state["ok"] = False`` marks the
        event as failure.  Setting ``state["error"] = "..."`` records an
        explanatory message.
    """
    state: dict[str, Any] = {"ok": True, "error": ""}
    start = time.perf_counter()
    try:
        yield state
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        if state["ok"]:
            cast(Any, client).environment.events.request.fire(
                request_type="BLOCK",
                name=name,
                response_time=elapsed_ms,
                response_length=0,
                exception=None,
                context={"expected_status": expected_status},
            )
        else:
            cast(Any, client).environment.events.request.fire(
                request_type="BLOCK",
                name=name,
                response_time=elapsed_ms,
                response_length=0,
                exception=RuntimeError(state["error"] or "block failed"),
                context={"expected_status": expected_status},
            )
    except Exception as exc:  # noqa: BLE001
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        cast(Any, client).environment.events.request.fire(
            request_type="BLOCK",
            name=name,
            response_time=elapsed_ms,
            response_length=0,
            exception=exc,
            context={"expected_status": expected_status},
        )
        raise
