# SPDX-License-Identifier: Apache-2.0
"""OpenAPI schema auto-emitter for lm-chat.

Generates a validated, stable-diff OpenAPI 3.1 YAML document from the live
FastAPI application and writes it to ``docs/api/openapi.yaml``.

Usage (CLI)::

    python -m lmchat.openapi.emit                      # default output path
    python -m lmchat.openapi.emit --output /tmp/out.yaml

The public surface is :func:`emit_schema` (pure function, returns the
processed schema dict) and :func:`main` (handles file I/O + CLI arg parsing).

Drift-check companion: ``lmchat.openapi.drift_check``.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI
from fastapi.routing import APIRoute
from openapi_spec_validator import validate as _ov_validate

from lmchat.logging import get_logger
from lmchat.middleware.auth import AUTH_SKIP_PATHS, AUTH_SKIP_PREFIXES
from lmchat.routes._dependencies import require_admin, require_user

log = get_logger(__name__)

# Repo root is three levels up from this file:
# src/lmchat/openapi/emit.py → src/lmchat/openapi → src/lmchat → src → repo-root
_REPO_ROOT: Path = Path(__file__).resolve().parent.parent.parent.parent
_DEFAULT_OUTPUT: Path = _REPO_ROOT / "docs" / "api" / "openapi.yaml"

# Static server entry — all lm-chat deployments are root-relative.
# Override via LM_CHAT_OPENAPI_SERVER_URL env var (picked up from settings
# when the settings layer has a matching field; otherwise hard-coded to "/").
_DEFAULT_SERVER_URL: str = "/"

_INFO_CONTACT: dict[str, str] = {
    "name": "lm-chat maintainers",
    "url": "https://github.com/lm-chat/lm-chat-v1",
}


def _get_server_url() -> str:
    """Return the OpenAPI server URL, reading settings when available.

    Falls back to :data:`_DEFAULT_SERVER_URL` (``"/"``).  The settings object
    is imported lazily so this module can be imported without a fully
    configured environment (e.g. in tests that supply their own output path).

    Returns:
        The server URL string.
    """
    try:
        from lmchat.config import get_settings  # noqa: PLC0415 (lazy import)

        settings = get_settings()
        url: str | None = getattr(settings, "lm_chat_openapi_server_url", None)
        if url:
            return url
    except Exception as exc:  # noqa: BLE001  — best-effort; validation errors at startup
        # Settings may not be configured (e.g. emitting at build time without
        # env vars).  Fall through to the hard-coded default.
        log.debug("openapi.emit.server_url_settings_unavailable", error=str(exc))
    return _DEFAULT_SERVER_URL


def _strip_default_responses(schema: dict[str, Any]) -> None:
    """Remove FastAPI's auto-injected ``default`` response from every operation.

    FastAPI adds a ``default`` response entry (pointing at ``HTTPValidationError``)
    to every path operation.  This entry is not part of lm-chat's public contract
    and creates noise in the committed YAML diff.

    Mutates *schema* in-place.

    Args:
        schema: The mutable OpenAPI schema dict returned by ``app.openapi()``.
    """
    paths: dict[str, Any] = schema.get("paths", {})
    for _path, path_item in paths.items():
        for _method, operation in path_item.items():
            if not isinstance(operation, dict):
                continue
            responses: dict[str, Any] = operation.get("responses", {})
            responses.pop("default", None)


def _collect_dep_calls(dependant: Any) -> set[Any]:
    """Flatten a FastAPI ``Dependant`` tree and return all ``.call`` values.

    Performs a depth-first walk of the dependency graph rooted at *dependant*,
    collecting every ``.call`` attribute into a set.  This includes transitive
    dependencies (e.g. ``require_admin`` → ``require_user`` → ``get_current_user_dep``).

    Args:
        dependant: A :class:`fastapi.dependencies.models.Dependant` instance.

    Returns:
        A set of all callable objects found in the dependency tree.
    """
    seen: set[Any] = set()
    stack: list[Any] = [dependant]
    while stack:
        d = stack.pop()
        call = getattr(d, "call", None)
        if call is not None:
            seen.add(call)
        stack.extend(getattr(d, "dependencies", []) or [])
    return seen


def _collect_api_routes(routes: list[Any]) -> list[APIRoute]:
    """Recursively collect all :class:`~fastapi.routing.APIRoute` objects.

    FastAPI wraps included sub-routers as ``_IncludedRouter`` objects that hold
    an ``original_router`` attribute.  This function walks the full route tree
    — including nested sub-routers — and returns every ``APIRoute`` leaf found.

    Args:
        routes: The top-level route list (e.g. ``app.router.routes``).

    Returns:
        A flat list of every :class:`~fastapi.routing.APIRoute` in the tree.
    """
    result: list[APIRoute] = []
    for route in routes:
        if isinstance(route, APIRoute):
            result.append(route)
        else:
            # _IncludedRouter (FastAPI internal) and Starlette Mount both
            # expose nested routes: prefer original_router.routes (FastAPI)
            # then fall back to route.routes (Starlette Mount / Router).
            orig = getattr(route, "original_router", None)
            sub_routes: list[Any] = (
                getattr(orig, "routes", [])
                if orig is not None
                else getattr(route, "routes", [])
            )
            if sub_routes:
                result.extend(_collect_api_routes(sub_routes))
    return result


def _is_middleware_protected(path: str) -> bool:
    """Return True if *path* is gated by the global auth middleware.

    Mirrors :func:`~lmchat.middleware.auth._is_skipped` exactly: a path is
    **public** (skipped by the middleware) when it matches one of the
    :data:`~lmchat.middleware.auth.AUTH_SKIP_PATHS` exact entries OR starts
    with one of the :data:`~lmchat.middleware.auth.AUTH_SKIP_PREFIXES` prefixes.
    Every other path is middleware-protected.

    Args:
        path: The route path string (e.g. ``"/api/params/{model_id}"``).

    Returns:
        ``True`` if the middleware will enforce authentication for this path.
    """
    if path in AUTH_SKIP_PATHS:
        return False
    if any(path.startswith(prefix) for prefix in AUTH_SKIP_PREFIXES):
        return False
    return True


def _document_auth_responses(app: FastAPI, schema: dict[str, Any]) -> None:
    """Add ``401``/``403`` to every auth-gated operation and ``400`` to every
    operation that accepts a request body.

    **Protected-path detection (belt-and-suspenders)**:

    An operation is considered *auth-gated* when EITHER of these is true:

    1. The route path is NOT covered by the global auth middleware's public-path
       allowlist (mirrors :func:`~lmchat.middleware.auth._is_skipped`).
    2. The route's dependency tree contains
       :func:`~lmchat.routes._dependencies.require_user` or
       :func:`~lmchat.routes._dependencies.require_admin`.

    **Request-body 400**:

    For every operation (protected or public) that has a ``requestBody`` key in
    the schema, ``400`` is added to document FastAPI's JSON-parse failure
    response.

    Existing documented responses are never overwritten — only missing entries
    are added via :meth:`dict.setdefault`.  The response objects are
    description-only (no ``content``/body schema) so schemathesis does not
    attempt body validation on them.

    Mutates *schema* in-place.

    Auth detection strategy:

    - Each ``APIRoute`` has a ``.dependant`` attribute that is the root of the
      FastAPI-built dependency tree.  We flatten this tree via
      :func:`_collect_dep_calls`.
    - ``require_user`` and ``require_admin`` are NOT :class:`~fastapi.security.SecurityBase`
      subclasses, so FastAPI does not emit a ``security`` field for them.  The
      only reliable detection path is inspecting the dependency tree directly.
    - Router-level dependencies (``APIRoute.dependencies``) store the raw
      ``Depends`` wrappers; we unwrap them via ``.dependency`` and add them to
      the stack so they are also considered.
    - FastAPI's ``include_router`` wraps sub-routers as ``_IncludedRouter``
      objects; :func:`_collect_api_routes` traverses them recursively.
    - Middleware-level enforcement is detected via
      :func:`_is_middleware_protected`, which imports the public-path constants
      directly from :mod:`lmchat.middleware.auth` and mirrors its exact
      matching semantics.

    Args:
        app:    The :class:`~fastapi.FastAPI` application instance.
        schema: The mutable OpenAPI schema dict (after ``_strip_default_responses``).
    """
    _AUTH_GUARDS = {require_user, require_admin}
    _SKIP_METHODS = {"head", "options"}

    paths_in_schema: dict[str, Any] = schema.get("paths", {})

    for route in _collect_api_routes(app.router.routes):
        route_path: str = route.path
        if route_path not in paths_in_schema:
            continue

        # Collect all dependency callables in the dependant tree.
        calls: set[Any] = _collect_dep_calls(route.dependant)

        # Also collect the route-level Depends() wrappers (router-level deps).
        for dep in getattr(route, "dependencies", []) or []:
            dependency_fn = getattr(dep, "dependency", None)
            if dependency_fn is not None:
                calls.add(dependency_fn)

        # An operation is auth-gated when either:
        # (a) the middleware would enforce auth on this path, OR
        # (b) the dependency tree includes a require_user/require_admin guard.
        is_protected: bool = _is_middleware_protected(route_path) or bool(
            _AUTH_GUARDS & calls
        )

        path_item: dict[str, Any] = paths_in_schema[route_path]
        for method in route.methods or set():
            method_lc = method.lower()
            if method_lc in _SKIP_METHODS:
                continue
            operation: dict[str, Any] | None = path_item.get(method_lc)
            if not isinstance(operation, dict):
                continue
            responses: dict[str, Any] = operation.setdefault("responses", {})

            if is_protected:
                responses.setdefault("401", {"description": "Authentication required."})
                responses.setdefault(  # noqa: E501
                    "403",
                    {"description": "Forbidden — insufficient privileges or cross-tenant access."},
                )

            # 400 for any operation that carries a request body (FastAPI returns
            # 400 when the JSON body cannot be parsed / validated).
            if "requestBody" in operation:
                responses.setdefault("400", {"description": "Malformed request body."})


def emit_schema(app: FastAPI | None = None) -> dict[str, Any]:
    """Build, post-process, and validate the OpenAPI schema dict.

    This is the pure function used by both the CLI (:func:`main`) and the
    drift-check (:mod:`lmchat.openapi.drift_check`).  It performs no file I/O.

    Steps:

    1. Import :func:`~lmchat.app.create_app` (or accept an existing app
       instance via *app*) and call ``app.openapi()`` to get the raw schema.
    2. Strip ``default`` responses.
    3. Document ``401``/``403`` on every auth-gated operation.
    4. Inject ``servers`` block.
    5. Inject ``info.contact``.
    6. Validate via ``openapi_spec_validator``.

    Args:
        app: Optional pre-built :class:`~fastapi.FastAPI` instance.  When
             *None*, a fresh application is created via
             :func:`~lmchat.app.create_app`.

    Returns:
        The post-processed, validated OpenAPI 3.1 schema dict.

    Raises:
        openapi_spec_validator.OpenAPIValidationError: If the emitted schema
            fails OpenAPI 3.1 validation.
    """
    if app is None:
        from lmchat.app import create_app  # noqa: PLC0415 (lazy import)

        app = create_app()

    log.info("openapi.emit.start")
    schema: dict[str, Any] = app.openapi()

    # 1. Remove FastAPI's auto-injected ``default`` response objects.
    _strip_default_responses(schema)
    log.debug("openapi.emit.stripped_default_responses")

    # 2. Document 401/403 on every auth-gated operation.
    _document_auth_responses(app, schema)
    log.debug("openapi.emit.auth_responses_documented")

    # 4. Inject ``servers`` block.
    server_url = _get_server_url()
    schema["servers"] = [{"url": server_url}]
    log.debug("openapi.emit.servers_injected", url=server_url)

    # 5. Inject ``info.contact``.
    info: dict[str, Any] = schema.setdefault("info", {})
    info["contact"] = _INFO_CONTACT
    log.debug("openapi.emit.info_contact_injected")

    # 6. Validate.
    log.info("openapi.emit.validating")
    _ov_validate(schema)
    log.info("openapi.emit.validation_passed")

    return schema


def _schema_to_yaml(schema: dict[str, Any]) -> str:
    """Serialise *schema* to a stable-diff YAML string.

    Uses ``sort_keys=True`` and ``default_flow_style=False`` so every run
    produces byte-identical output for the same logical schema.  This is
    required for deterministic committed diffs.

    Args:
        schema: The OpenAPI schema dict.

    Returns:
        A YAML string with keys sorted and block-style scalars.
    """
    return yaml.safe_dump(schema, sort_keys=True, default_flow_style=False, allow_unicode=True)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: emit the OpenAPI schema to a YAML file.

    Args:
        argv: Argument list (defaults to ``sys.argv[1:]``).

    Returns:
        0 on success; 1 on validation failure; 2 on I/O error.
    """
    parser = argparse.ArgumentParser(
        prog="python -m lmchat.openapi.emit",
        description="Emit the lm-chat OpenAPI 3.1 schema to a YAML file.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=_DEFAULT_OUTPUT,
        help=f"Output path (default: {_DEFAULT_OUTPUT})",
    )
    args = parser.parse_args(argv)
    output: Path = args.output

    try:
        schema = emit_schema()
    except Exception as exc:  # noqa: BLE001
        log.error("openapi.emit.validation_failed", error=str(exc))
        print(f"ERROR: OpenAPI validation failed: {exc}", file=sys.stderr)
        return 1

    yaml_text = _schema_to_yaml(schema)

    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(yaml_text, encoding="utf-8")
    except OSError as exc:
        log.error("openapi.emit.write_failed", path=str(output), error=str(exc))
        print(f"ERROR: could not write {output}: {exc}", file=sys.stderr)
        return 2

    log.info("openapi.emit.wrote", path=str(output), bytes=len(yaml_text))
    print(f"Wrote {len(yaml_text)} bytes to {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
