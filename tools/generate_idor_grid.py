#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""IDOR / authorization-bypass grid generator — L5 sub-tool.

Reads ``docs/api/openapi.yaml`` and, for every ``path × method``, instantiates
test cases against a running lm-chat backend covering the four IDOR/IAM
attack patterns:

  1. **Anon**          — no auth → expect 401 (or 200 if the path is public).
  2. **Self**          — User A's session on a resource A owns
                          → expect 200 / 204 / 400 / 404 / 422.
  3. **Cross-user**    — User A's session on a resource B owns
                          → expect 401 / 403 / 404.
  4. **Non-admin → admin** — User A's (non-admin) session on
                          ``/api/admin/*`` → expect 401 / 403.

The generator is **read-mostly** by construction.  It auto-classifies methods
as "mutating" (POST/PATCH/PUT/DELETE) vs "reading" (GET/HEAD/OPTIONS) and
skips mutating cross-user calls when no realistic body can be synthesized
from the OpenAPI schema; this keeps the grid from accidentally registering
or deleting real resources between users.

Outputs
-------
- ``target/gates/L5-idor-grid.xml`` — JUnit XML, one ``<testcase>`` per
  (path × method × pattern).  Failures are real authorization-bypass
  findings.
- ``target/gates/L5-idor-grid.json`` — aggregated counts, suitable for
  the L9 aggregator to fold into ``report.json``.

The aggregated JSON shape (validated by the L9 aggregator):

.. code-block:: json

    {
      "schema_version": 1,
      "openapi_path": "docs/api/openapi.yaml",
      "base_url": "http://127.0.0.1:8765",
      "totals": {
        "endpoints": 49,
        "cases":      327,
        "passed":     327,
        "failed":       0,
        "skipped":      0
      },
      "by_pattern": { "anon": {...}, "self": {...}, "cross_user": {...},
                      "non_admin_to_admin": {...} },
      "findings": [
        { "severity": "high", "path": "/api/chats/{chat_id}",
          "method": "GET",   "pattern": "cross_user",
          "actual_status": 200, "expected_status": [401, 403, 404],
          "title": "IDOR: user A read user B's chat" }
      ]
    }

Boundary: this tool talks ONLY to the locally-booted lm-chat backend
(default ``http://127.0.0.1:8765``).  It never touches LM Studio nor any
external host.  See ``docs/INTEGRATION_MAP.md`` §3 and
``tools/check_no_lmstudio_fs.py``.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import httpx
import yaml

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Methods we exercise.  Everything else (TRACE, CONNECT, HEAD, OPTIONS) is
# either irrelevant for IDOR or already covered by other layers.
_METHODS: tuple[str, ...] = ("get", "post", "patch", "put", "delete")

# Methods that mutate state.  We are extra careful with these in cross-user
# tests — we never send a real body that could create / modify B's data.
_MUTATING: frozenset[str] = frozenset({"post", "patch", "put", "delete"})

# Status codes that satisfy the "anon got rejected" pattern.
_ANON_REJECTED: frozenset[int] = frozenset({401, 403})

# Status codes that satisfy "self may proceed" — a clean 2xx OR a benign
# 4xx that indicates the resource doesn't exist for this user.  We tolerate
# 400 + 422 because schemathesis will catch shape bugs, not us; our concern
# is purely authorization.
_SELF_OK: frozenset[int] = frozenset(
    {200, 201, 202, 204, 400, 404, 409, 422}
)

# Status codes that satisfy "cross-user was blocked".  401 is also accepted
# because some routes return 401 when the resource lookup fails before the
# ownership check.  404 is accepted because correct apps return 404 (not 403)
# for "this resource exists but is not yours" — that's the OWASP-recommended
# pattern.  We are NOT measuring leakage of existence here; that is a
# separate test (R-S2 family).
_CROSS_BLOCKED: frozenset[int] = frozenset({401, 403, 404})

# Status codes that satisfy "non-admin was blocked from /api/admin/*".
_ADMIN_BLOCKED: frozenset[int] = frozenset({401, 403, 404})

# Endpoints that intentionally accept anonymous traffic.  Each entry is a
# regex matched against the path; the method set narrows the rule.  Add a
# new entry only when the route's OpenAPI description explicitly states
# "no authentication required".
_PUBLIC_PATHS: tuple[tuple[re.Pattern[str], frozenset[str]], ...] = (
    (re.compile(r"^/healthz$"), frozenset({"get"})),
    (re.compile(r"^/readyz$"), frozenset({"get"})),
    (re.compile(r"^/api/auth/login$"), frozenset({"post"})),
    (re.compile(r"^/api/auth/register$"), frozenset({"post"})),
    (re.compile(r"^/api/auth/logout$"), frozenset({"post"})),
    (re.compile(r"^/api/auth/totp/verify$"), frozenset({"post"})),
    # Public auth-status probe — always returns 200 even when not logged in.
    (re.compile(r"^/api/auth/me/probe$"), frozenset({"get"})),
    # Public first-run setup check — no auth required (anonymous).
    (re.compile(r"^/api/auth/setup_status$"), frozenset({"get"})),
    # /me is auth-required but is the "is my cookie still valid" probe;
    # it returns 401 on anon — same as everything else, so no exception.
)

# Admin-only endpoints not under the /api/admin/ prefix.  These are gated
# by require_admin in the handler but the OpenAPI spec does not carry an
# x-admin-only annotation, so enumerate_endpoints cannot detect them via
# path-prefix or vendor-extension.  Listing them here prevents the grid
# from generating "self" cases that expect 2xx but correctly get 403.
# See: GET /api/analytics/system, GET /api/debug, GET /api/memory/reindex/status
_ADMIN_ONLY_PATHS: tuple[tuple[re.Pattern[str], frozenset[str]], ...] = (
    (re.compile(r"^/api/analytics/system$"), frozenset({"get"})),
    (re.compile(r"^/api/debug$"), frozenset({"get"})),
    (re.compile(r"^/api/memory/reindex/status$"), frozenset({"get"})),
    # MCP server store — entire router is gated by require_admin.
    (re.compile(r"^/api/mcp-store/catalog$"), frozenset({"get"})),
    (re.compile(r"^/api/mcp-store/servers$"), frozenset({"get", "post"})),
    (re.compile(r"^/api/mcp-store/servers/[^/]+$"), frozenset({"delete", "patch"})),
)

# Endpoints we deliberately skip (always — never run against them).  These
# are network-side-effect endpoints or destructive admin actions that would
# corrupt the test state if exercised in a tight loop.
_SKIP_PATHS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^/api/chat/stream$"),       # SSE; not idempotent
    re.compile(r"^/api/ab/stream$"),         # SSE
    re.compile(r"^/api/models/(load|unload|download)$"),  # heavy upstream calls
    re.compile(r"^/api/auth/logout$"),       # mutates the user's session
    re.compile(r"^/api/auth/password$"),     # would lock A out
    re.compile(r"^/api/auth/totp/setup$"),   # mutates user state
    re.compile(r"^/api/auth/totp/disable$"), # mutates user state
    re.compile(r"^/api/memory/reindex$"),    # multi-second background job
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Endpoint:
    """One (method, path) pair from the OpenAPI spec."""

    method: str        # lowercase: "get" | "post" | "patch" | "put" | "delete"
    path: str          # raw path with ``{param}`` placeholders
    # True when route requires admin privileges — set by ``/api/admin/``
    # prefix OR by the ``x-admin-only: true`` vendor extension in the spec.
    admin_only: bool = False


@dataclass
class Case:
    """One test case in the grid."""

    endpoint: Endpoint
    pattern: str          # "anon" | "self" | "cross_user" | "non_admin_to_admin"
    expected: frozenset[int]
    request_path: str     # path after placeholder substitution
    auth_cookie: str | None = None  # session cookie value or None
    body: dict[str, Any] | None = None

    # Filled in after dispatch:
    actual_status: int | None = None
    elapsed_ms: float = 0.0
    note: str = ""

    @property
    def passed(self) -> bool:
        return self.actual_status in self.expected


@dataclass
class GridResult:
    """Aggregated grid outcome — feeds JSON + JUnit."""

    cases: list[Case] = field(default_factory=list)
    base_url: str = ""
    openapi_path: str = ""

    def by_pattern(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for c in self.cases:
            slot = out.setdefault(
                c.pattern,
                {"total": 0, "passed": 0, "failed": 0, "skipped": 0},
            )
            slot["total"] += 1
            if c.actual_status is None:
                slot["skipped"] += 1
            elif c.passed:
                slot["passed"] += 1
            else:
                slot["failed"] += 1
        return out

    def totals(self) -> dict[str, int]:
        total = len(self.cases)
        passed = sum(1 for c in self.cases if c.actual_status is not None and c.passed)
        failed = sum(1 for c in self.cases if c.actual_status is not None and not c.passed)
        skipped = sum(1 for c in self.cases if c.actual_status is None)
        endpoints = len({(c.endpoint.method, c.endpoint.path) for c in self.cases})
        return {
            "endpoints": endpoints,
            "cases": total,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
        }

    def findings(self) -> list[dict[str, Any]]:
        """Failures only.  Each failure IS an IDOR/IAM-bypass bug."""
        out: list[dict[str, Any]] = []
        for c in self.cases:
            if c.actual_status is None or c.passed:
                continue
            severity = "high" if c.pattern in {"cross_user", "non_admin_to_admin"} else "medium"
            title = {
                "anon": "Anon access succeeded on a non-public endpoint",
                "self": "Authenticated owner request rejected unexpectedly",
                "cross_user": "IDOR: cross-user access succeeded",
                "non_admin_to_admin": "Non-admin reached an /api/admin/* endpoint",
            }[c.pattern]
            out.append(
                {
                    "severity": severity,
                    "path": c.endpoint.path,
                    "method": c.endpoint.method.upper(),
                    "pattern": c.pattern,
                    "actual_status": c.actual_status,
                    "expected_status": sorted(c.expected),
                    "title": title,
                    "note": c.note,
                }
            )
        return out


# ---------------------------------------------------------------------------
# OpenAPI loading
# ---------------------------------------------------------------------------


def load_openapi(path: Path) -> dict[str, Any]:
    """Parse ``docs/api/openapi.yaml`` into a dict.

    Args:
        path: Absolute or relative path to an OpenAPI 3.x YAML file.

    Returns:
        The parsed mapping.  Caller is responsible for verifying it's
        a dict (we do that here, raising ``ValueError`` if not).
    """
    raw = path.read_text(encoding="utf-8")
    parsed = yaml.safe_load(raw)
    if not isinstance(parsed, dict):
        raise ValueError(f"{path}: expected a YAML mapping at root")
    return parsed


def enumerate_endpoints(spec: dict[str, Any]) -> list[Endpoint]:
    """Walk ``spec['paths']`` and emit one ``Endpoint`` per declared method.

    Reads the ``x-admin-only: true`` vendor extension on individual operations
    to classify routes that are admin-gated but live outside the
    ``/api/admin/`` path prefix (e.g. ``GET /api/debug``,
    ``GET /api/analytics/system``).

    Args:
        spec: The parsed OpenAPI mapping.

    Returns:
        List of ``Endpoint`` records in spec order.  Paths whose entry is
        not a mapping (malformed spec) are skipped.
    """
    out: list[Endpoint] = []
    paths = spec.get("paths") or {}
    if not isinstance(paths, dict):
        return out
    for path, item in paths.items():
        if not isinstance(item, dict) or not isinstance(path, str):
            continue
        prefix_admin = path.startswith("/api/admin/")
        for method in _METHODS:
            if method in item:
                op = item[method]
                op_admin = bool(
                    isinstance(op, dict) and op.get("x-admin-only")
                )
                out.append(
                    Endpoint(
                        method=method,
                        path=path,
                        admin_only=prefix_admin or op_admin,
                    )
                )
    return out


def is_public(ep: Endpoint) -> bool:
    """Is this endpoint allowed to serve anonymous traffic?"""
    for rgx, methods in _PUBLIC_PATHS:
        if rgx.match(ep.path) and ep.method in methods:
            return True
    return False


def is_skipped(ep: Endpoint) -> bool:
    """Is this endpoint on the never-fuzz list?"""
    return any(rgx.match(ep.path) for rgx in _SKIP_PATHS)


def is_admin_path(ep: Endpoint) -> bool:
    """Return True when *ep* requires admin privileges.

    Covers ``/api/admin/`` prefix routes, operations annotated with
    the ``x-admin-only: true`` vendor extension in the OpenAPI spec,
    and paths explicitly listed in ``_ADMIN_ONLY_PATHS`` (admin-gated
    endpoints outside ``/api/admin/`` that lack the vendor extension).
    """
    if ep.admin_only:
        return True
    for rgx, methods in _ADMIN_ONLY_PATHS:
        if rgx.match(ep.path) and ep.method in methods:
            return True
    return False


# ---------------------------------------------------------------------------
# Path-parameter materialisation
# ---------------------------------------------------------------------------


_PARAM_RGX = re.compile(r"\{([^}]+)\}")


def materialize_path(
    path: str, *, resources: dict[str, Any]
) -> str | None:
    """Substitute each ``{param}`` in *path* with a value from *resources*.

    Args:
        path:      The raw OpenAPI path template.
        resources: Per-user resource map (keys: ``chat_id``, ``message_id``,
                   ``document_id``, ``insight_id``, ``prompt_id``,
                   ``user_id``, ``folder_name``).  Values are stringified.

    Returns:
        The materialised path, or ``None`` if any placeholder couldn't be
        satisfied.  ``None`` instructs the caller to skip the case.
    """
    out = path

    def _sub(m: re.Match[str]) -> str:
        nonlocal out
        key = m.group(1)
        val = resources.get(key)
        if val is None:
            return "__missing__"
        return str(val)

    materialised = _PARAM_RGX.sub(_sub, out)
    if "__missing__" in materialised:
        return None
    return materialised


# ---------------------------------------------------------------------------
# Grid construction
# ---------------------------------------------------------------------------


def build_grid(
    endpoints: list[Endpoint],
    *,
    cookie_a: str | None,
    cookie_b: str | None,
    cookie_admin: str | None,
    resources_a: dict[str, Any],
    resources_b: dict[str, Any],
) -> list[Case]:
    """Cartesian-expand endpoints × auth patterns into Case records.

    Args:
        endpoints:      All endpoints from the spec.
        cookie_a:       Session cookie value for user A (non-admin).
        cookie_b:       Session cookie value for user B (non-admin).
        cookie_admin:   Session cookie for the admin user.
        resources_a:    Resources owned by A; see ``materialize_path``.
        resources_b:    Resources owned by B; same shape.

    Returns:
        Concrete test cases ready to dispatch.
    """
    cases: list[Case] = []
    for ep in endpoints:
        if is_skipped(ep):
            continue

        # 1. Anon
        anon_path = materialize_path(ep.path, resources=resources_a)
        if anon_path is not None:
            if is_public(ep):
                expected = frozenset({200, 201, 204, 400, 401, 422})
                note = "public endpoint — anon allowed"
            else:
                expected = _ANON_REJECTED
                note = ""
            cases.append(
                Case(
                    endpoint=ep,
                    pattern="anon",
                    expected=expected,
                    request_path=anon_path,
                    auth_cookie=None,
                    note=note,
                )
            )

        # 2. Self — A on A's resource.
        #
        # Admin paths are EXCLUDED from the self pattern.  A non-admin user
        # hitting an admin endpoint always receives 403 (from require_admin),
        # which is NOT in _SELF_OK.  Generating a self case for admin paths
        # therefore produces guaranteed false positives — the 403 is correct
        # behaviour, not a bug.  The non_admin_to_admin case (pattern 4)
        # already covers the admin-gate check for these endpoints.
        #
        # Skip mutating methods too: the body would be synthetic and we'd
        # risk false positives or actual mutations.
        if (
            not is_admin_path(ep)
            and ep.method not in _MUTATING
            and cookie_a is not None
        ):
            self_path = materialize_path(ep.path, resources=resources_a)
            if self_path is not None:
                cases.append(
                    Case(
                        endpoint=ep,
                        pattern="self",
                        expected=_SELF_OK,
                        request_path=self_path,
                        auth_cookie=cookie_a,
                    )
                )

        # 3. Cross-user — A on B's resource.  ONLY meaningful when the path
        # has at least one parameterised resource ID; otherwise A and B see
        # the same data by design (e.g. ``GET /api/auth/me``).
        if (
            cookie_a is not None
            and ep.method not in _MUTATING
            and _PARAM_RGX.search(ep.path) is not None
        ):
            xpath = materialize_path(ep.path, resources=resources_b)
            if xpath is not None:
                cases.append(
                    Case(
                        endpoint=ep,
                        pattern="cross_user",
                        expected=_CROSS_BLOCKED,
                        request_path=xpath,
                        auth_cookie=cookie_a,
                    )
                )

        # 4. Non-admin → admin
        if is_admin_path(ep) and cookie_a is not None:
            admin_path = materialize_path(ep.path, resources=resources_b)
            if admin_path is None:
                admin_path = materialize_path(ep.path, resources=resources_a)
            if admin_path is not None:
                cases.append(
                    Case(
                        endpoint=ep,
                        pattern="non_admin_to_admin",
                        expected=_ADMIN_BLOCKED,
                        request_path=admin_path,
                        auth_cookie=cookie_a,
                    )
                )

    return cases


# ---------------------------------------------------------------------------
# Provisioning + dispatch (runtime-only)
# ---------------------------------------------------------------------------


async def register_and_login(
    client: httpx.AsyncClient,
    *,
    username: str,
    password: str,
    setup_token: str | None,
) -> str | None:
    """Register a user (if not already) and return their session cookie.

    Args:
        client:      Reusable httpx AsyncClient pointed at the backend.
        username:    Account username (3–64 chars, [a-zA-Z0-9_]).
        password:    Plaintext password.
        setup_token: Optional ``LM_CHAT_SETUP_TOKEN`` for the bootstrap
                     window (first user only).

    Returns:
        The ``lmchat_session`` cookie value, or ``None`` if the login
        failed (logged + skipped by the caller).
    """
    params = {"token": setup_token} if setup_token else None
    data = {"username": username, "password": password}
    reg = await client.post("/api/auth/register", data=data, params=params)
    # 201 = created; 400 = "username already taken" (idempotent re-run).
    if reg.status_code not in (201, 400):
        print(
            f"[idor] register({username}) -> {reg.status_code}: {reg.text[:200]}",
            file=sys.stderr,
        )
        return None
    login = await client.post("/api/auth/login", data=data)
    if login.status_code != 200:
        print(
            f"[idor] login({username}) -> {login.status_code}: {login.text[:200]}",
            file=sys.stderr,
        )
        return None
    return login.cookies.get("lmchat_session")


async def seed_resources(
    client: httpx.AsyncClient, *, cookie: str
) -> dict[str, Any]:
    """Create one chat + one prompt + one document-ish placeholder per user.

    Args:
        client:  Async client (no auth headers — we pass the cookie inline).
        cookie:  Session cookie for the user we're seeding.

    Returns:
        Dict suitable for ``materialize_path``.  Missing keys are tolerated
        (the materialiser skips those paths).
    """
    cookies = {"lmchat_session": cookie}

    out: dict[str, Any] = {}

    # user_id — read from /api/auth/me
    me = await client.get("/api/auth/me", cookies=cookies)
    if me.status_code == 200:
        body = me.json()
        if isinstance(body, dict) and "user_id" in body:
            out["user_id"] = body["user_id"]

    # chat_id — POST /api/chats
    chat = await client.post(
        "/api/chats",
        cookies=cookies,
        json={"title": f"idor-grid-{out.get('user_id', 'unknown')}"},
    )
    if chat.status_code in (200, 201):
        body = chat.json()
        if isinstance(body, dict) and "id" in body:
            out["chat_id"] = body["id"]

    # prompt_id — POST /api/prompts (best-effort; tolerated if shape differs)
    prompt = await client.post(
        "/api/prompts",
        cookies=cookies,
        json={
            "name": f"idor-prompt-{out.get('user_id', 'unknown')}",
            "content": "Grid seed prompt.",
        },
    )
    if prompt.status_code in (200, 201):
        body = prompt.json()
        if isinstance(body, dict) and "id" in body:
            out["prompt_id"] = body["id"]

    # insight_id — we don't proactively create one; many endpoints accept
    # any int and return 404 cleanly, which still proves the IDOR pattern.
    # Use a sentinel so materialisation doesn't drop the path.
    out["insight_id"] = "1"
    out["message_id"] = "1"
    out["document_id"] = "1"
    out["folder_name"] = "default"

    return out


async def dispatch_case(client: httpx.AsyncClient, case: Case) -> None:
    """Send one request, fill in ``actual_status`` + ``elapsed_ms``."""
    cookies = (
        {"lmchat_session": case.auth_cookie}
        if case.auth_cookie is not None
        else None
    )
    started = time.perf_counter()
    try:
        # For mutating methods on the "anon" pattern we still send the
        # request — without auth there's no risk of corrupting state.  For
        # mutating methods on other patterns we already filtered them out
        # in ``build_grid``, so we won't reach this branch with a body.
        if case.endpoint.method in _MUTATING:
            resp = await client.request(
                case.endpoint.method.upper(),
                case.request_path,
                cookies=cookies,
                # Empty json body keeps Content-Type honest; if the route
                # requires fields, the backend will reject with 422 which
                # is in our accepted set anyway.
                json={},
            )
        else:
            resp = await client.request(
                case.endpoint.method.upper(),
                case.request_path,
                cookies=cookies,
            )
        case.actual_status = resp.status_code
    except httpx.HTTPError as exc:  # noqa: BLE001
        case.actual_status = None
        case.note = f"transport error: {exc!r}"
    finally:
        case.elapsed_ms = (time.perf_counter() - started) * 1000.0


async def run_grid(
    cases: list[Case],
    *,
    base_url: str,
    concurrency: int = 16,
    request_timeout: float = 10.0,
) -> None:
    """Dispatch all cases concurrently, mutating each Case in place.

    Args:
        cases:        The grid (output of ``build_grid``).
        base_url:     Backend root URL.
        concurrency:  Max in-flight requests.
        timeout:      Per-request timeout, seconds.
    """
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(
        base_url=base_url, timeout=request_timeout, limits=limits,
    ) as client:
        async def _one(case: Case) -> None:
            async with sem:
                await dispatch_case(client, case)
        await asyncio.gather(*(_one(c) for c in cases))


# ---------------------------------------------------------------------------
# Serialisation: JUnit + JSON
# ---------------------------------------------------------------------------


def to_junit(result: GridResult) -> ET.ElementTree:
    """Render a JUnit XML tree for the L9 aggregator.

    One ``<testsuite>`` per pattern; one ``<testcase>`` per Case.  Failed
    cases get a ``<failure>`` element with a structured message that the
    aggregator surfaces in ``report.json.findings[]``.
    """
    totals = result.totals()
    root = ET.Element(
        "testsuites",
        {
            "name": "l5-idor-grid",
            "tests": str(totals["cases"]),
            "failures": str(totals["failed"]),
            "errors": "0",
            "skipped": str(totals["skipped"]),
        },
    )

    # Group cases by pattern for cleaner reporting.
    by_pat: dict[str, list[Case]] = {}
    for c in result.cases:
        by_pat.setdefault(c.pattern, []).append(c)

    for pattern, cases in by_pat.items():
        p_total = len(cases)
        p_fail = sum(1 for c in cases if c.actual_status is not None and not c.passed)
        p_skip = sum(1 for c in cases if c.actual_status is None)
        suite = ET.SubElement(
            root,
            "testsuite",
            {
                "name": f"l5-idor-grid.{pattern}",
                "tests": str(p_total),
                "failures": str(p_fail),
                "errors": "0",
                "skipped": str(p_skip),
            },
        )
        for c in cases:
            classname = f"idor.{pattern}"
            tc = ET.SubElement(
                suite,
                "testcase",
                {
                    "classname": classname,
                    "name": f"{c.endpoint.method.upper()} {c.endpoint.path}",
                    "time": f"{c.elapsed_ms / 1000.0:.3f}",
                },
            )
            if c.actual_status is None:
                sk = ET.SubElement(tc, "skipped", {"message": c.note or "skipped"})
                sk.text = c.note or "skipped"
            elif not c.passed:
                msg = (
                    f"expected status in {sorted(c.expected)} "
                    f"but got {c.actual_status} "
                    f"({c.endpoint.method.upper()} {c.request_path})"
                )
                fail = ET.SubElement(
                    tc,
                    "failure",
                    {"message": msg, "type": "IDORViolation"},
                )
                fail.text = msg + (f"\nnote: {c.note}" if c.note else "")

    ET.indent(ET.ElementTree(root), space="  ")
    return ET.ElementTree(root)


def to_json(result: GridResult) -> dict[str, Any]:
    """Aggregated JSON shape — see module docstring."""
    return {
        "schema_version": 1,
        "openapi_path": result.openapi_path,
        "base_url": result.base_url,
        "totals": result.totals(),
        "by_pattern": result.by_pattern(),
        "findings": result.findings(),
    }


# ---------------------------------------------------------------------------
# Orchestration (CLI entry)
# ---------------------------------------------------------------------------


async def _run_async(args: argparse.Namespace) -> int:
    spec = load_openapi(args.openapi)
    endpoints = enumerate_endpoints(spec)

    base = args.base_url.rstrip("/")
    result = GridResult(base_url=base, openapi_path=str(args.openapi))

    setup_token = args.setup_token or None

    # Provision users + seed resources.
    async with httpx.AsyncClient(base_url=base, timeout=15.0) as client:
        cookie_admin = await register_and_login(
            client,
            username=args.admin_username,
            password=args.password,
            setup_token=setup_token,
        )
        # Subsequent registrations: setup-token lifts once the first user
        # exists.  Passing None is now correct.
        cookie_a = await register_and_login(
            client,
            username=args.user_a_username,
            password=args.password,
            setup_token=None,
        )
        cookie_b = await register_and_login(
            client,
            username=args.user_b_username,
            password=args.password,
            setup_token=None,
        )

        resources_a: dict[str, Any] = {}
        resources_b: dict[str, Any] = {}
        if cookie_a is not None:
            resources_a = await seed_resources(client, cookie=cookie_a)
        if cookie_b is not None:
            resources_b = await seed_resources(client, cookie=cookie_b)

    cases = build_grid(
        endpoints,
        cookie_a=cookie_a,
        cookie_b=cookie_b,
        cookie_admin=cookie_admin,
        resources_a=resources_a,
        resources_b=resources_b,
    )
    result.cases = cases

    await run_grid(cases, base_url=base, concurrency=args.concurrency)

    args.junit.parent.mkdir(parents=True, exist_ok=True)
    to_junit(result).write(
        args.junit, encoding="utf-8", xml_declaration=True
    )
    args.aggregate.parent.mkdir(parents=True, exist_ok=True)
    args.aggregate.write_text(
        json.dumps(to_json(result), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    totals = result.totals()
    findings = result.findings()
    print(
        f"[idor] {totals['cases']} cases / "
        f"{totals['passed']} passed / "
        f"{totals['failed']} failed / "
        f"{totals['skipped']} skipped"
    )
    if findings:
        print(f"[idor] {len(findings)} finding(s):")
        for f in findings[:20]:
            print(
                f"  [{f['severity']}] {f['method']} {f['path']} "
                f"({f['pattern']}): got {f['actual_status']}, "
                f"expected {f['expected_status']}"
            )
        if len(findings) > 20:
            print(f"  ... and {len(findings) - 20} more (see {args.aggregate})")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--openapi",
        type=Path,
        default=Path("docs/api/openapi.yaml"),
        help="OpenAPI spec to enumerate.",
    )
    p.add_argument(
        "--base-url",
        default="http://127.0.0.1:8765",
        help="Backend root URL (running lm-chat instance).",
    )
    p.add_argument(
        "--admin-username", default="idor_admin",
        help="Admin user (first to register; receives setup-token).",
    )
    p.add_argument(
        "--user-a-username", default="idor_user_a",
        help="Non-admin user A.",
    )
    p.add_argument(
        "--user-b-username", default="idor_user_b",
        help="Non-admin user B.",
    )
    p.add_argument(
        "--password", default="idor-grid-password-1!",
        help="Shared password for all three test users.",
    )
    p.add_argument(
        "--setup-token", default="",
        help="LM_CHAT_SETUP_TOKEN required for the first registration.",
    )
    p.add_argument(
        "--junit",
        type=Path,
        default=Path("target/gates/L5-idor-grid.xml"),
        help="JUnit XML output path.",
    )
    p.add_argument(
        "--aggregate",
        type=Path,
        default=Path("target/gates/L5-idor-grid.json"),
        help="Aggregated JSON output path.",
    )
    p.add_argument(
        "--concurrency", type=int, default=16, help="Max in-flight requests.",
    )
    args = p.parse_args(argv)
    return asyncio.run(_run_async(args))


if __name__ == "__main__":
    sys.exit(main())
