# SPDX-License-Identifier: Apache-2.0
"""Tests for lmchat.openapi.emit.

Tests for ``openapi/emit.py``.

All tests use :func:`~lmchat.openapi.emit.emit_schema` directly — no
lifespan is started.  File-I/O tests use ``tmp_path`` so the committed
``docs/api/openapi.yaml`` is never mutated.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import FastAPI
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Module-level Pydantic models (Pydantic cannot resolve forward-refs for
# models defined inside local function scopes; module-level is required).
# ---------------------------------------------------------------------------


class _LoginPayload(BaseModel):
    """Minimal JSON body used by requestBody tests."""

    name: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_app() -> FastAPI:
    """Return a minimal FastAPI app for schema-shape tests.

    Using the real :func:`~lmchat.app.create_app` is correct for integration
    tests, but for unit-tests that only verify post-processing behaviour a
    minimal app is faster and less fragile.
    """
    from fastapi import FastAPI

    app = FastAPI(title="test-app", version="0.0.1")

    from pydantic import BaseModel

    class PingOut(BaseModel):
        pong: str

    @app.get("/ping", response_model=PingOut)
    async def ping() -> PingOut:
        """Ping."""
        return PingOut(pong="ok")

    return app


# ---------------------------------------------------------------------------
# emit_schema
# ---------------------------------------------------------------------------


def test_emit_returns_valid_openapi_3_1_schema() -> None:
    """emit_schema() returns a dict with openapi 3.1.x version string."""
    from lmchat.openapi.emit import emit_schema

    schema = emit_schema(_minimal_app())
    assert isinstance(schema, dict)
    assert schema.get("openapi", "").startswith("3.1")


def test_emit_writes_yaml_with_sort_keys_for_stable_diff(tmp_path: Path) -> None:
    """main() writes YAML with sort_keys=True, producing stable diff output."""
    from lmchat.openapi.emit import main

    out = tmp_path / "openapi.yaml"
    rc = main(["--output", str(out)])
    assert rc == 0

    text = out.read_text(encoding="utf-8")
    parsed: dict[str, Any] = yaml.safe_load(text)

    # Re-serialise and verify byte-identical (proves stable key ordering).
    re_serialised = yaml.safe_dump(
        parsed, sort_keys=True, default_flow_style=False, allow_unicode=True
    )
    assert text == re_serialised, "YAML output is not stable (key sort violation)"


def test_emit_removes_default_responses() -> None:
    """emit_schema() removes FastAPI's auto-injected 'default' response."""
    from lmchat.openapi.emit import emit_schema

    schema = emit_schema(_minimal_app())
    paths: dict[str, Any] = schema.get("paths", {})
    for path, path_item in paths.items():
        for method, operation in path_item.items():
            if not isinstance(operation, dict):
                continue
            responses = operation.get("responses", {})
            assert "default" not in responses, (
                f"'default' response found on {method.upper()} {path}"
            )


def test_emit_adds_servers_block() -> None:
    """emit_schema() adds a 'servers' block with at least one entry."""
    from lmchat.openapi.emit import emit_schema

    schema = emit_schema(_minimal_app())
    servers = schema.get("servers")
    assert isinstance(servers, list), "Expected 'servers' to be a list"
    assert len(servers) >= 1
    # Each entry must have a 'url' key.
    for entry in servers:
        assert "url" in entry, f"Server entry missing 'url': {entry}"


def test_emit_adds_info_contact() -> None:
    """emit_schema() adds info.contact with 'name' and 'url' keys."""
    from lmchat.openapi.emit import emit_schema

    schema = emit_schema(_minimal_app())
    contact: dict[str, Any] = schema.get("info", {}).get("contact", {})
    assert "name" in contact, "info.contact missing 'name'"
    assert "url" in contact, "info.contact missing 'url'"
    assert contact["name"], "info.contact.name must be non-empty"
    assert contact["url"], "info.contact.url must be non-empty"


def test_emit_validates_against_openapi_spec_validator() -> None:
    """emit_schema() calls openapi-spec-validator; invalid schema raises."""
    from openapi_spec_validator.validation.exceptions import OpenAPIValidationError

    from lmchat.openapi.emit import emit_schema

    # Happy path: minimal valid app should not raise.
    schema = emit_schema(_minimal_app())
    assert schema  # non-empty dict

    # Adversarial: manually break the schema and verify the validator fires.
    # We cannot call emit_schema() with a broken app directly, so we verify
    # the validator integration by patching app.openapi() to return junk.
    broken_app = FastAPI(title="broken", version="0.0.0")
    original_openapi = broken_app.openapi

    def _broken_openapi() -> dict[str, Any]:
        schema = original_openapi()
        # Delete the required 'info' field — guaranteed validation failure.
        schema.pop("info", None)
        return schema

    broken_app.openapi = _broken_openapi  # type: ignore[method-assign]

    with pytest.raises((OpenAPIValidationError, Exception)):
        emit_schema(broken_app)


def test_emit_main_writes_to_default_output_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """main() with no --output writes to the default docs/api/openapi.yaml path."""
    from lmchat.openapi import emit as emit_mod

    # Redirect the default output path to tmp_path so we don't touch the
    # committed docs/api/openapi.yaml.
    fake_default = tmp_path / "openapi.yaml"
    monkeypatch.setattr(emit_mod, "_DEFAULT_OUTPUT", fake_default)

    rc = emit_mod.main([])
    assert rc == 0
    assert fake_default.exists()

    # Verify the YAML is valid and parseable.
    parsed: dict[str, Any] = yaml.safe_load(fake_default.read_text(encoding="utf-8"))
    assert parsed.get("openapi", "").startswith("3.1")


def test_emit_main_custom_output_arg(tmp_path: Path) -> None:
    """main(--output <path>) writes to the specified path."""
    from lmchat.openapi.emit import main

    custom = tmp_path / "custom_openapi.yaml"
    rc = main(["--output", str(custom)])
    assert rc == 0
    assert custom.exists()
    text = custom.read_text(encoding="utf-8")
    parsed: dict[str, Any] = yaml.safe_load(text)
    assert "paths" in parsed


def test_emit_main_creates_parent_directories(tmp_path: Path) -> None:
    """main() creates missing parent directories for the output path."""
    from lmchat.openapi.emit import main

    nested = tmp_path / "nested" / "deep" / "openapi.yaml"
    rc = main(["--output", str(nested)])
    assert rc == 0
    assert nested.exists()


def test_emit_main_exits_nonzero_on_validation_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """main() exits with code 1 when emit_schema() raises a validation error."""
    from lmchat.openapi import emit as emit_mod

    def _bad_emit(_app: Any = None) -> dict[str, Any]:
        raise ValueError("forced failure")

    monkeypatch.setattr(emit_mod, "emit_schema", _bad_emit)

    out = tmp_path / "out.yaml"
    rc = emit_mod.main(["--output", str(out)])
    assert rc == 1


def test_emit_main_exits_2_on_oserror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """main() exits with code 2 when writing the output file raises OSError."""
    from lmchat.openapi import emit as emit_mod

    # Use a path in a read-only directory to trigger OSError.
    readonly_dir = tmp_path / "readonly"
    readonly_dir.mkdir()
    readonly_dir.chmod(0o444)
    bad_output = readonly_dir / "cannot_write.yaml"

    try:
        rc = emit_mod.main(["--output", str(bad_output)])
        assert rc == 2
    finally:
        readonly_dir.chmod(0o755)


def test_strip_default_responses_skips_non_dict_values() -> None:
    """_strip_default_responses() skips path-item values that are not dicts."""
    from lmchat.openapi.emit import _strip_default_responses

    # A path item may contain non-dict values (e.g. $ref strings, summary).
    schema: dict[str, Any] = {
        "paths": {
            "/ping": {
                "summary": "just a string — not a method",
                "get": {
                    "responses": {"200": {"description": "ok"}, "default": {"description": "err"}},
                    "summary": "Ping",
                },
            }
        }
    }
    _strip_default_responses(schema)
    # 'default' should be removed from the dict operation but the string key is untouched.
    assert "default" not in schema["paths"]["/ping"]["get"]["responses"]
    # The non-dict 'summary' value is unchanged (the loop skips it via isinstance guard).
    assert schema["paths"]["/ping"]["summary"] == "just a string — not a method"


# ---------------------------------------------------------------------------
# _document_auth_responses
# ---------------------------------------------------------------------------


def _make_auth_app() -> FastAPI:
    """Return a minimal FastAPI app with one dep-gated route and one middleware-public route.

    ``GET /private`` requires ``require_user`` (route-level dep).
    ``POST /api/auth/login`` is in the middleware's exact-skip set — public.
    """
    from fastapi import Depends, FastAPI

    from lmchat.routes._dependencies import require_user
    from lmchat.services.auth_service import User

    app = FastAPI(title="auth-test-app", version="0.0.1")

    @app.get("/private")
    async def private(user: User = Depends(require_user)) -> dict[str, str]:  # noqa: B008
        """Auth-gated endpoint."""
        return {"ok": "yes"}

    # /api/auth/login is in AUTH_SKIP_PATHS — the middleware skips auth for this path.
    @app.post("/api/auth/login")
    async def login() -> dict[str, str]:
        """Public login endpoint — in middleware skip list."""
        return {"ok": "yes"}

    return app


def test_document_auth_responses_adds_401_and_403_to_gated_op() -> None:
    """_document_auth_responses() adds 401 + 403 to operations with require_user."""
    from lmchat.openapi.emit import emit_schema

    schema = emit_schema(_make_auth_app())
    responses = schema["paths"]["/private"]["get"]["responses"]
    assert "401" in responses, "401 must be documented on auth-gated GET /private"
    assert "403" in responses, "403 must be documented on auth-gated GET /private"


def test_document_auth_responses_does_not_overwrite_existing_response() -> None:
    """_document_auth_responses() never overwrites an already-documented response."""
    from fastapi import Depends, FastAPI

    from lmchat.routes._dependencies import require_user
    from lmchat.services.auth_service import User

    app = FastAPI(title="preserve-test", version="0.0.1")

    @app.get(
        "/guarded",
        responses={
            401: {"description": "My custom 401."},
            403: {"description": "My custom 403."},
        },
    )
    async def guarded(user: User = Depends(require_user)) -> dict[str, str]:  # noqa: B008
        """Auth-gated endpoint with pre-documented 401/403."""
        return {"ok": "yes"}

    from lmchat.openapi.emit import emit_schema

    schema = emit_schema(app)
    responses = schema["paths"]["/guarded"]["get"]["responses"]
    # The pre-existing descriptions must not be replaced.
    assert responses["401"]["description"] == "My custom 401."
    assert responses["403"]["description"] == "My custom 403."


def test_document_auth_responses_leaves_middleware_public_op_unmodified() -> None:
    """_document_auth_responses() does not add 401/403 to paths in the middleware skip list.

    ``/api/auth/login`` is in AUTH_SKIP_PATHS — neither the middleware nor any
    route-level dependency requires authentication, so no 401/403 should appear.
    """
    from lmchat.openapi.emit import emit_schema

    schema = emit_schema(_make_auth_app())
    login_responses = schema["paths"]["/api/auth/login"]["post"]["responses"]
    assert "401" not in login_responses, (
        "401 must NOT appear on middleware-public POST /api/auth/login"
    )
    assert "403" not in login_responses, (
        "403 must NOT appear on middleware-public POST /api/auth/login"
    )


def test_document_auth_responses_require_admin_gated_op() -> None:
    """_document_auth_responses() adds 401 + 403 to operations using require_admin."""
    from fastapi import Depends, FastAPI

    from lmchat.routes._dependencies import require_admin
    from lmchat.services.auth_service import User

    app = FastAPI(title="admin-test-app", version="0.0.1")

    @app.delete("/admin/resource/{rid}")
    async def delete_resource(rid: int, user: User = Depends(require_admin)) -> dict[str, str]:  # noqa: B008
        """Admin-only delete."""
        return {"deleted": str(rid)}

    from lmchat.openapi.emit import emit_schema

    schema = emit_schema(app)
    responses = schema["paths"]["/admin/resource/{rid}"]["delete"]["responses"]
    assert "401" in responses, "401 must be documented on require_admin DELETE"
    assert "403" in responses, "403 must be documented on require_admin DELETE"


def test_document_auth_responses_real_app_has_401_403_on_protected_routes() -> None:
    """The real lm-chat app emits 401/403 on at least one known protected endpoint."""
    from lmchat.app import create_app
    from lmchat.openapi.emit import emit_schema

    app = create_app()
    schema = emit_schema(app)

    paths = schema.get("paths", {})
    # DELETE /api/chats/{chat_id} is a well-known auth-gated endpoint.
    # Verify at least one of the /api/chats paths has 401+403.
    chat_paths = [p for p in paths if "chats" in p]
    assert chat_paths, "Expected at least one /api/chats/* path in schema"

    found_401 = any(
        "401" in (paths[p].get(m, {}) or {}).get("responses", {})
        for p in chat_paths
        for m in paths[p]
        if isinstance(paths[p][m], dict)
    )
    assert found_401, (
        "Expected at least one /api/chats/* operation to document 401 after augmentation"
    )


# ---------------------------------------------------------------------------
# New tests for middleware-gated ops and requestBody 400
# ---------------------------------------------------------------------------


def test_document_auth_responses_middleware_gated_op_no_route_dep_gets_401_403() -> None:
    """An op under a non-public path with NO route-level require_user still gets 401+403.

    The global auth middleware enforces authentication for any path not in the
    skip list.  ``/api/some/protected`` is not in AUTH_SKIP_PATHS, so the
    augmenter must document 401+403 even without a route-level dependency.
    """
    from fastapi import FastAPI

    from lmchat.openapi.emit import emit_schema

    app = FastAPI(title="mw-test-app", version="0.0.1")

    @app.get("/api/some/protected")
    async def no_dep_but_middleware_gated() -> dict[str, str]:
        """Protected by middleware, no route-level dep."""
        return {"ok": "yes"}

    schema = emit_schema(app)
    responses = schema["paths"]["/api/some/protected"]["get"]["responses"]
    assert "401" in responses, (
        "401 must be documented on middleware-gated GET /api/some/protected "
        "even without a route-level require_user dependency"
    )
    assert "403" in responses, (
        "403 must be documented on middleware-gated GET /api/some/protected "
        "even without a route-level require_user dependency"
    )


def test_document_auth_responses_request_body_op_gets_400() -> None:
    """An operation with a requestBody gets 400 documented (FastAPI body-parse failure).

    Uses a module-level Pydantic model (_LoginPayload) to avoid Pydantic
    forward-reference resolution failures that occur with locally-defined models.
    """
    from fastapi import FastAPI

    from lmchat.openapi.emit import emit_schema

    app = FastAPI(title="body-test-app", version="0.0.1")

    @app.post("/api/auth/login")
    async def login_with_body(payload: _LoginPayload) -> dict[str, str]:
        """Public endpoint with a JSON body — should get 400."""
        return {"ok": "yes"}

    schema = emit_schema(app)
    responses = schema["paths"]["/api/auth/login"]["post"]["responses"]
    assert "400" in responses, (
        "400 must be documented on POST /api/auth/login which has a requestBody"
    )


def test_document_auth_responses_no_request_body_op_does_not_get_400() -> None:
    """A GET operation without a requestBody does NOT get a 400 added."""
    from fastapi import FastAPI

    from lmchat.openapi.emit import emit_schema

    app = FastAPI(title="nobody-test-app", version="0.0.1")

    @app.get("/api/auth/setup_status")
    async def setup_status() -> dict[str, str]:
        """Public GET with no body — should NOT get 400."""
        return {"ok": "yes"}

    schema = emit_schema(app)
    responses = schema["paths"]["/api/auth/setup_status"]["get"]["responses"]
    assert "400" not in responses, (
        "400 must NOT be added to GET /api/auth/setup_status which has no requestBody"
    )


def test_document_auth_responses_pre_documented_400_not_overwritten() -> None:
    """A pre-documented 400 response is preserved; the augmenter does not overwrite it.

    Uses a module-level Pydantic model (_LoginPayload) to avoid Pydantic
    forward-reference resolution failures that occur with locally-defined models.
    """
    from fastapi import FastAPI

    from lmchat.openapi.emit import emit_schema

    app = FastAPI(title="preserve-400-test", version="0.0.1")

    @app.post(
        "/api/auth/login",
        responses={400: {"description": "My custom 400."}},
    )
    async def login_custom_400(payload: _LoginPayload) -> dict[str, str]:
        """Public POST with pre-documented 400 — must not be overwritten."""
        return {"ok": "yes"}

    schema = emit_schema(app)
    responses = schema["paths"]["/api/auth/login"]["post"]["responses"]
    assert responses["400"]["description"] == "My custom 400.", (
        "Pre-existing 400 description must not be replaced by the augmenter"
    )


def test_document_auth_responses_real_app_params_endpoint_has_401() -> None:
    """The real app's GET /api/params/{model_id} gets 401 (middleware-gated, no route dep)."""
    from lmchat.app import create_app
    from lmchat.openapi.emit import emit_schema

    app = create_app()
    schema = emit_schema(app)
    paths = schema.get("paths", {})

    params_path = "/api/params/{model_id}"
    if params_path not in paths:
        pytest.skip(f"{params_path} not found in schema — route may have been renamed")

    get_resp = paths[params_path].get("get", {}).get("responses", {})
    assert "401" in get_resp, (
        f"GET {params_path} must document 401 (middleware-gated path)"
    )


def test_document_auth_responses_real_app_memory_insights_has_401() -> None:
    """The real app's PATCH /api/memory/insights/{id} gets 401."""
    from lmchat.app import create_app
    from lmchat.openapi.emit import emit_schema

    app = create_app()
    schema = emit_schema(app)
    paths = schema.get("paths", {})

    insights_path = "/api/memory/insights/{id}"
    if insights_path not in paths:
        pytest.skip(f"{insights_path} not found in schema — route may have been renamed")

    for method in ("patch", "get"):
        if method not in paths[insights_path]:
            continue
        resp = paths[insights_path][method].get("responses", {})
        assert "401" in resp, (
            f"{method.upper()} {insights_path} must document 401"
        )


def test_document_auth_responses_real_app_auth_logout_has_400_if_body() -> None:
    """The real app's POST /api/auth/logout gets 400 if it has a requestBody."""
    from lmchat.app import create_app
    from lmchat.openapi.emit import emit_schema

    app = create_app()
    schema = emit_schema(app)
    paths = schema.get("paths", {})

    logout_path = "/api/auth/logout"
    if logout_path not in paths:
        pytest.skip(f"{logout_path} not found in schema — route may have been renamed")

    for method in ("post", "delete"):
        if method not in paths[logout_path]:
            continue
        op = paths[logout_path][method]
        if "requestBody" in op:
            resp = op.get("responses", {})
            assert "400" in resp, (
                f"{method.upper()} {logout_path} has a requestBody and must document 400"
            )


def test_document_auth_responses_real_app_memory_pin_has_400_if_body() -> None:
    """The real app's POST /api/memory/pin gets 400 if it has a requestBody."""
    from lmchat.app import create_app
    from lmchat.openapi.emit import emit_schema

    app = create_app()
    schema = emit_schema(app)
    paths = schema.get("paths", {})

    pin_path = "/api/memory/pin"
    if pin_path not in paths:
        pytest.skip(f"{pin_path} not found in schema — route may have been renamed")

    for method in ("post", "put", "patch"):
        if method not in paths[pin_path]:
            continue
        op = paths[pin_path][method]
        if "requestBody" in op:
            resp = op.get("responses", {})
            assert "400" in resp, (
                f"{method.upper()} {pin_path} has a requestBody and must document 400"
            )
