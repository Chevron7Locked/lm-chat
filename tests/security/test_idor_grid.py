# SPDX-License-Identifier: Apache-2.0
"""Unit tests for ``tools/generate_idor_grid.py``.

The generator has two layers we can test offline:

  1. **Spec → endpoints**: ``enumerate_endpoints``, ``is_public``,
     ``is_admin_path``, ``is_skipped`` — pure functions.
  2. **Endpoints + cookies + resources → grid**: ``build_grid`` and
     ``materialize_path`` — also pure.

Online dispatch (``run_grid``, ``register_and_login``) is covered by the
L5 layer's end-to-end run in ``make target/gates/.L5-passed``.  Unit
tests here mock just enough to assert the grid construction is correct
for a small set of synthetic OpenAPI specs.

Coverage: IDOR grid cross-user endpoint isolation.
"""
from __future__ import annotations

import importlib.util as _ilu
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

# Side-load ``tools/generate_idor_grid.py`` the same way the L4 adapter
# tests load their tooling — ``tools/`` is not on ``sys.path`` because it's
# a script directory, not a package.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_TOOLS_DIR = _REPO_ROOT / "tools"


def _load_module(name: str, file: Path) -> Any:
    spec = _ilu.spec_from_file_location(name, file)
    assert spec is not None and spec.loader is not None
    mod = _ilu.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_gig = _load_module(
    "generate_idor_grid", _TOOLS_DIR / "generate_idor_grid.py"
)

Case = _gig.Case
Endpoint = _gig.Endpoint
GridResult = _gig.GridResult
build_grid = _gig.build_grid
enumerate_endpoints = _gig.enumerate_endpoints
is_admin_path = _gig.is_admin_path
is_public = _gig.is_public
is_skipped = _gig.is_skipped
load_openapi = _gig.load_openapi
materialize_path = _gig.materialize_path
to_json = _gig.to_json
to_junit = _gig.to_junit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spec_from(paths: dict[str, Any]) -> dict[str, Any]:
    """Wrap a paths dict in a minimal valid OpenAPI 3.x envelope."""
    return {
        "openapi": "3.1.0",
        "info": {"title": "test", "version": "0.0.0"},
        "paths": paths,
    }


def _write(tmp_path: Path, spec: dict[str, Any]) -> Path:
    p = tmp_path / "spec.yaml"
    p.write_text(yaml.safe_dump(spec), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_load_openapi_parses_yaml(tmp_path: Path) -> None:
    """``load_openapi`` returns the mapping for a well-formed file."""
    spec = _spec_from({"/api/x": {"get": {"responses": {"200": {"description": "ok"}}}}})
    p = _write(tmp_path, spec)
    out = load_openapi(p)
    assert out["paths"]["/api/x"]["get"]["responses"]["200"]["description"] == "ok"


def test_load_openapi_rejects_non_mapping(tmp_path: Path) -> None:
    """A YAML list at the root raises ValueError."""
    p = tmp_path / "spec.yaml"
    p.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
    with pytest.raises(ValueError, match="expected a YAML mapping"):
        load_openapi(p)


def test_enumerate_endpoints_emits_one_per_method() -> None:
    """``enumerate_endpoints`` walks every (path, method) declared."""
    spec = _spec_from(
        {
            "/api/things": {
                "get": {"responses": {}},
                "post": {"responses": {}},
                "parameters": [],  # non-method key — ignored
            },
            "/api/things/{thing_id}": {
                "get": {"responses": {}},
                "delete": {"responses": {}},
            },
        }
    )
    eps = enumerate_endpoints(spec)
    assert sorted((e.method, e.path) for e in eps) == [
        ("delete", "/api/things/{thing_id}"),
        ("get", "/api/things"),
        ("get", "/api/things/{thing_id}"),
        ("post", "/api/things"),
    ]


def test_enumerate_endpoints_tolerates_missing_paths() -> None:
    """A spec without 'paths' yields an empty list."""
    assert enumerate_endpoints({"openapi": "3.1.0"}) == []
    assert enumerate_endpoints({"paths": "not-a-dict"}) == []


def test_is_public_matches_register_and_login() -> None:
    """Public endpoints listed in the regex table return True."""
    assert is_public(Endpoint("post", "/api/auth/register"))
    assert is_public(Endpoint("post", "/api/auth/login"))
    assert is_public(Endpoint("get", "/healthz"))
    # Methods outside the rule set are not public.
    assert not is_public(Endpoint("get", "/api/auth/register"))
    # Random endpoints are not public.
    assert not is_public(Endpoint("get", "/api/chats"))


def test_is_admin_path_filters_admin_subtree() -> None:
    """Paths under ``/api/admin/`` are admin-only via prefix."""
    assert is_admin_path(Endpoint("get", "/api/admin/users", admin_only=True))
    assert is_admin_path(Endpoint("patch", "/api/admin/quotas/{user_id}", admin_only=True))
    assert not is_admin_path(Endpoint("get", "/api/chats", admin_only=False))
    assert not is_admin_path(Endpoint("get", "/api/auth/me", admin_only=False))


def test_is_skipped_catches_streaming_and_destructive() -> None:
    """SSE + destructive admin actions are skipped from the grid."""
    assert is_skipped(Endpoint("post", "/api/chat/stream"))
    assert is_skipped(Endpoint("post", "/api/ab/stream"))
    assert is_skipped(Endpoint("post", "/api/models/load"))
    assert is_skipped(Endpoint("post", "/api/auth/logout"))
    assert is_skipped(Endpoint("post", "/api/auth/password"))
    assert not is_skipped(Endpoint("get", "/api/chats"))


def test_materialize_path_substitutes_known_params() -> None:
    """All ``{param}`` placeholders are substituted from the resources dict."""
    out = materialize_path(
        "/api/chats/{chat_id}/messages",
        resources={"chat_id": 42},
    )
    assert out == "/api/chats/42/messages"


def test_materialize_path_returns_none_when_param_missing() -> None:
    """An un-substitutable param returns None (caller skips the case)."""
    out = materialize_path(
        "/api/chats/{chat_id}/extras/{extra_id}",
        resources={"chat_id": 42},  # no extra_id
    )
    assert out is None


def test_materialize_path_no_params_is_identity() -> None:
    """Static paths pass through unchanged."""
    assert materialize_path("/api/auth/me", resources={}) == "/api/auth/me"


# ---------------------------------------------------------------------------
# Grid construction — only-anon spec
# ---------------------------------------------------------------------------


def test_build_grid_only_anon_when_no_cookies() -> None:
    """With no logged-in users, the grid contains anon cases only."""
    eps = [
        Endpoint("get", "/api/chats"),
        Endpoint("post", "/api/auth/login"),
    ]
    cases = build_grid(
        eps,
        cookie_a=None,
        cookie_b=None,
        cookie_admin=None,
        resources_a={},
        resources_b={},
    )
    assert {c.pattern for c in cases} == {"anon"}
    # Two endpoints, two anon cases.
    assert len(cases) == 2
    by_path = {c.endpoint.path: c for c in cases}
    # /api/chats: anon should be rejected → expected in {401, 403}.
    assert 401 in by_path["/api/chats"].expected
    # /api/auth/login is public → expected should include 2xx codes.
    assert 200 in by_path["/api/auth/login"].expected


# ---------------------------------------------------------------------------
# Grid construction — mixed user/admin spec
# ---------------------------------------------------------------------------


def test_build_grid_emits_four_patterns_on_admin_path() -> None:
    """An admin GET with cookies + resources yields anon + cross_user +
    non_admin_to_admin cases.  The ``self`` pattern is suppressed for admin
    paths because a non-admin user always receives 403, which is not in
    ``_SELF_OK`` and would produce guaranteed false positives."""
    eps = [Endpoint("get", "/api/admin/users/{user_id}/role", admin_only=True)]
    cases = build_grid(
        eps,
        cookie_a="cookieA",
        cookie_b="cookieB",
        cookie_admin="cookieAdmin",
        resources_a={"user_id": 1},
        resources_b={"user_id": 2},
    )
    patterns = {c.pattern for c in cases}
    # self is intentionally absent for admin paths.
    assert patterns == {"anon", "cross_user", "non_admin_to_admin"}
    assert "self" not in patterns
    # cross_user uses B's resource.
    xc = next(c for c in cases if c.pattern == "cross_user")
    assert xc.request_path == "/api/admin/users/2/role"
    # non_admin_to_admin falls back to B's resource when available.
    na = next(c for c in cases if c.pattern == "non_admin_to_admin")
    assert na.request_path == "/api/admin/users/2/role"


def test_build_grid_skips_cross_user_when_no_param_in_path() -> None:
    """``GET /api/auth/me`` — no {param}, so no cross-user case is created."""
    eps = [Endpoint("get", "/api/auth/me")]
    cases = build_grid(
        eps,
        cookie_a="cookieA",
        cookie_b="cookieB",
        cookie_admin=None,
        resources_a={},
        resources_b={},
    )
    assert {c.pattern for c in cases} == {"anon", "self"}


def test_build_grid_skips_mutating_self_and_cross_user() -> None:
    """POST / PATCH / PUT / DELETE only ever yield an anon case (and an
    admin-promotion case if it's an admin path)."""
    eps = [
        Endpoint("post", "/api/chats"),
        Endpoint("patch", "/api/chats/{chat_id}"),
        Endpoint("delete", "/api/chats/{chat_id}"),
    ]
    cases = build_grid(
        eps,
        cookie_a="cookieA",
        cookie_b="cookieB",
        cookie_admin=None,
        resources_a={"chat_id": 1},
        resources_b={"chat_id": 2},
    )
    # Three endpoints × one anon pattern each.
    assert all(c.pattern == "anon" for c in cases)
    assert len(cases) == 3


def test_build_grid_emits_non_admin_to_admin_for_admin_mutations() -> None:
    """Admin DELETE still gets a non_admin_to_admin case — admin gating
    matters regardless of HTTP method."""
    eps = [Endpoint("delete", "/api/admin/users/{user_id}", admin_only=True)]
    cases = build_grid(
        eps,
        cookie_a="cookieA",
        cookie_b="cookieB",
        cookie_admin="cookieAdmin",
        resources_a={"user_id": 1},
        resources_b={"user_id": 2},
    )
    patterns = {c.pattern for c in cases}
    assert "non_admin_to_admin" in patterns
    # Self / cross_user are NOT generated for mutating methods.
    assert "self" not in patterns
    assert "cross_user" not in patterns


def test_build_grid_skips_listed_paths_entirely() -> None:
    """Paths on the ``_SKIP_PATHS`` regex list never reach the grid."""
    eps = [
        Endpoint("post", "/api/chat/stream"),
        Endpoint("post", "/api/auth/logout"),
        Endpoint("get", "/api/chats"),  # baseline that survives
    ]
    cases = build_grid(
        eps,
        cookie_a="cookieA",
        cookie_b="cookieB",
        cookie_admin=None,
        resources_a={},
        resources_b={},
    )
    paths_in_grid = {c.endpoint.path for c in cases}
    assert "/api/chat/stream" not in paths_in_grid
    assert "/api/auth/logout" not in paths_in_grid
    assert "/api/chats" in paths_in_grid


# ---------------------------------------------------------------------------
# x-admin-only vendor extension + no-self-on-admin correctness
# ---------------------------------------------------------------------------


def test_enumerate_endpoints_reads_x_admin_only() -> None:
    """Operations with ``x-admin-only: true`` are marked ``admin_only=True``
    regardless of path prefix — covers routes like ``GET /api/debug`` and
    ``GET /api/analytics/system`` that live outside ``/api/admin/``."""
    spec = _spec_from(
        {
            "/api/debug": {
                "get": {
                    "x-admin-only": True,
                    "responses": {"200": {"description": "ok"}},
                }
            },
            "/api/analytics/system": {
                "get": {
                    "x-admin-only": True,
                    "responses": {"200": {"description": "ok"}},
                }
            },
            "/api/chats": {
                "get": {"responses": {"200": {"description": "ok"}}}
            },
        }
    )
    eps = enumerate_endpoints(spec)
    by_path = {ep.path: ep for ep in eps}
    assert by_path["/api/debug"].admin_only is True
    assert by_path["/api/analytics/system"].admin_only is True
    assert by_path["/api/chats"].admin_only is False


def test_enumerate_endpoints_prefix_sets_admin_only() -> None:
    """``/api/admin/`` prefix routes are marked ``admin_only=True`` without
    needing the vendor extension."""
    spec = _spec_from(
        {
            "/api/admin/users": {
                "get": {"responses": {"200": {"description": "ok"}}}
            },
        }
    )
    eps = enumerate_endpoints(spec)
    assert eps[0].admin_only is True


def test_is_admin_path_reads_admin_only_flag() -> None:
    """``is_admin_path`` checks both ``Endpoint.admin_only`` and
    ``_ADMIN_ONLY_PATHS``."""
    assert is_admin_path(Endpoint("get", "/api/debug", admin_only=True))
    assert is_admin_path(Endpoint("get", "/api/analytics/system", admin_only=True))
    # /api/debug is in _ADMIN_ONLY_PATHS so it's admin-only regardless
    # of the Endpoint flag.
    assert is_admin_path(Endpoint("get", "/api/debug", admin_only=False))
    # A path NOT listed in _ADMIN_ONLY_PATHS and NOT under /api/admin/
    # and NOT x-admin-only should return False.
    assert not is_admin_path(Endpoint("get", "/api/chats", admin_only=False))


def test_build_grid_no_self_for_x_admin_only_routes() -> None:
    """Routes marked ``x-admin-only`` (outside ``/api/admin/``) must not
    generate ``self`` cases.  A non-admin user always gets 403 on these
    routes; a self case expecting ``_SELF_OK`` would be a guaranteed false
    positive."""
    eps = [Endpoint("get", "/api/debug", admin_only=True)]
    cases = build_grid(
        eps,
        cookie_a="cookieA",
        cookie_b="cookieB",
        cookie_admin="cookieAdmin",
        resources_a={},
        resources_b={},
    )
    patterns = {c.pattern for c in cases}
    assert "self" not in patterns
    assert "non_admin_to_admin" in patterns


def test_build_grid_no_self_for_admin_prefix_get_routes() -> None:
    """GET routes under ``/api/admin/`` must not generate ``self`` cases —
    the 7 false positives that prompted this fix."""
    admin_gets = [
        Endpoint("get", "/api/admin/audit-log", admin_only=True),
        Endpoint("get", "/api/admin/quotas", admin_only=True),
        Endpoint("get", "/api/admin/users", admin_only=True),
        Endpoint("get", "/api/admin/users/count", admin_only=True),
        Endpoint("get", "/api/admin/quotas/{user_id}", admin_only=True),
    ]
    cases = build_grid(
        admin_gets,
        cookie_a="cookieA",
        cookie_b="cookieB",
        cookie_admin="cookieAdmin",
        resources_a={"user_id": 1},
        resources_b={"user_id": 2},
    )
    assert not any(c.pattern == "self" for c in cases), (
        "self cases on admin routes produce guaranteed false positives"
    )


def test_build_grid_self_still_works_for_non_admin_gets() -> None:
    """Non-admin GET routes continue to produce ``self`` cases as before."""
    eps = [
        Endpoint("get", "/api/auth/me", admin_only=False),
        Endpoint("get", "/api/quotas/me", admin_only=False),
        Endpoint("get", "/api/chats/{chat_id}", admin_only=False),
    ]
    cases = build_grid(
        eps,
        cookie_a="cookieA",
        cookie_b="cookieB",
        cookie_admin=None,
        resources_a={"chat_id": 1},
        resources_b={"chat_id": 2},
    )
    self_paths = {c.endpoint.path for c in cases if c.pattern == "self"}
    assert "/api/auth/me" in self_paths
    assert "/api/quotas/me" in self_paths
    assert "/api/chats/{chat_id}" in self_paths


def test_real_spec_no_self_cases_on_admin_routes() -> None:
    """Regression: loading the real ``docs/api/openapi.yaml`` must produce
    zero ``self`` cases for any admin-only endpoint — no false positives."""
    spec_path = _REPO_ROOT / "docs" / "api" / "openapi.yaml"
    if not spec_path.exists():
        pytest.skip("openapi.yaml not present")
    spec = load_openapi(spec_path)
    eps = enumerate_endpoints(spec)
    cases = build_grid(
        eps,
        cookie_a="cookieA",
        cookie_b="cookieB",
        cookie_admin="cookieAdmin",
        resources_a={"user_id": 1, "chat_id": 1, "prompt_id": 1,
                     "insight_id": 1, "message_id": 1, "document_id": 1,
                     "folder_name": "default"},
        resources_b={"user_id": 2, "chat_id": 2, "prompt_id": 2,
                     "insight_id": 2, "message_id": 2, "document_id": 2,
                     "folder_name": "default"},
    )
    admin_self_cases = [
        c for c in cases
        if c.pattern == "self" and is_admin_path(c.endpoint)
    ]
    assert admin_self_cases == [], (
        f"Found {len(admin_self_cases)} false-positive self cases on admin "
        f"routes: {[c.endpoint.path for c in admin_self_cases]}"
    )


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def test_to_junit_round_trips_a_failure() -> None:
    """A failed case produces a JUnit ``<failure>`` element."""
    ep = Endpoint("get", "/api/chats/{chat_id}")
    failed = Case(
        endpoint=ep,
        pattern="cross_user",
        expected=frozenset({401, 403, 404}),
        request_path="/api/chats/2",
        auth_cookie="cookieA",
    )
    failed.actual_status = 200  # IDOR violation
    passed = Case(
        endpoint=ep,
        pattern="self",
        expected=frozenset({200, 204, 404}),
        request_path="/api/chats/1",
        auth_cookie="cookieA",
    )
    passed.actual_status = 200
    result = GridResult(cases=[failed, passed], base_url="http://x", openapi_path="x")
    tree = to_junit(result)
    root = tree.getroot()
    assert root.tag == "testsuites"
    assert root.get("name") == "l5-idor-grid"
    failures = list(root.iter("failure"))
    assert len(failures) == 1
    assert "expected status in" in (failures[0].text or "")


def test_to_json_summarises_findings() -> None:
    """The aggregated JSON contains a finding for the failed case only."""
    ep = Endpoint("get", "/api/chats/{chat_id}")
    failed = Case(
        endpoint=ep,
        pattern="cross_user",
        expected=frozenset({401, 403, 404}),
        request_path="/api/chats/2",
        auth_cookie="cookieA",
    )
    failed.actual_status = 200
    passed = Case(
        endpoint=Endpoint("get", "/api/auth/me"),
        pattern="self",
        expected=frozenset({200}),
        request_path="/api/auth/me",
        auth_cookie="cookieA",
    )
    passed.actual_status = 200
    result = GridResult(cases=[failed, passed], base_url="http://x", openapi_path="x")
    data = to_json(result)
    assert data["totals"]["cases"] == 2
    assert data["totals"]["passed"] == 1
    assert data["totals"]["failed"] == 1
    assert len(data["findings"]) == 1
    f = data["findings"][0]
    assert f["pattern"] == "cross_user"
    assert f["actual_status"] == 200
    assert f["severity"] == "high"
