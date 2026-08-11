# SPDX-License-Identifier: Apache-2.0
"""Tests for tools/reprobe_lm_studio_surface.py.

Uses a real stub HTTP server (threading + httpx) to exercise all four
probe-outcome paths:

  1. All probes match → exit 0
  2. One probe mismatches (stub returns 500 on /api/v1/models) → exit 1
  3. LM Studio unreachable (stub not running) → exit 0 + WARNING (default)
  4. LM Studio unreachable + ``--strict`` → exit 1

The stub is a minimal in-process httpserver (Python stdlib
``http.server``) rather than the heavyweight integration conftest, to keep
this test file self-contained and fast.  It is started on a free OS port,
serves the probe paths expected by the catalog, and is torn down after
each test.

Port allocation: ``socket.socket.bind(("127.0.0.1", 0))`` lets the OS
choose a free port; we read it back and point the prober at it.
"""
from __future__ import annotations

import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# Put the tools/ directory on sys.path so reprobe_lm_studio_surface is
# importable as a top-level module (same pattern as tools/test_panel.py).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_TOOLS_DIR = _REPO_ROOT / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from reprobe_lm_studio_surface import (  # noqa: E402  # type: ignore[import-not-found]
    _PROBES,
    _exit_code,
    main,
    run_all_probes,
)

# ---------------------------------------------------------------------------
# Stub server helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Return a free TCP port on 127.0.0.1."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _StubHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler that serves static responses for each probe path.

    Class-level ``_overrides`` dict maps (method, path) → status_code,
    allowing individual tests to inject faults.
    """

    _overrides: dict[tuple[str, str], int] = {}

    def log_message(self, format: str, *args: Any) -> None:  # noqa: ANN401, A002
        # Suppress all access log output during tests.
        pass

    def _respond(self, status: int, body: bytes = b"{}") -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self) -> None:
        key = (self.command, self.path)
        if key in self._overrides:
            self._respond(self._overrides[key])
            return

        # Default responses for every probe path.
        path = self.path
        method = self.command

        if method == "GET":
            if path == "/api/v1/models":
                body = b'{"models": [], "data": []}'
                self._respond(200, body)
            elif path in ("/", "/health", "/status", "/info", "/api", "/v1/models"):
                self._respond(200, b'{"ok": true}')
            else:
                self._respond(404)

        elif method == "POST":
            if path == "/api/v1/chat":
                # Documented: 400 with error.param = "model"
                body = (
                    b'{"error": {"type": "bad_request", "param": "model", '
                    b'"message": "model required"}}'
                )
                self._respond(400, body)
            elif path in (
                "/api/v1/models/load",
                "/api/v1/models/unload",
            ):
                self._respond(200, b'{"instance_id": "probe-stub", "status": "loaded"}')
            elif path == "/api/v1/models/download":
                # Documented: 400 when "model" is missing
                body = b'{"error": {"type": "bad_request", "param": "model"}}'
                self._respond(400, body)
            elif path == "/v1/chat/completions":
                self._respond(400, b'{"error": {"message": "missing model"}}')
            elif path == "/v1/responses":
                self._respond(400, b'{"error": {"message": "missing model"}}')
            elif path == "/v1/embeddings":
                self._respond(400, b'{"error": {"message": "missing model"}}')
            elif path == "/v1/completions":
                self._respond(400, b'{"error": {"message": "missing model"}}')
            elif path == "/v1/messages":
                # Live wire: {"type":"error","error":{"type":"invalid_request_error",...}}
                body = (
                    b'{"type": "error", "error": {'
                    b'"type": "invalid_request_error", '
                    b'"message": "request.max_tokens: Required"}}'
                )
                self._respond(400, body)
            else:
                self._respond(404)
        else:
            self._respond(405)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch()

    def do_POST(self) -> None:  # noqa: N802
        # Read and discard body to keep the connection clean.
        length_str = self.headers.get("Content-Length", "0")
        try:
            length = int(length_str)
        except ValueError:
            length = 0
        if length > 0:
            self.rfile.read(length)
        self._dispatch()


class _StubServer:
    """Context manager that starts a stub HTTP server on a free port."""

    def __init__(self, overrides: dict[tuple[str, str], int] | None = None) -> None:
        self._overrides = overrides or {}
        self._server: HTTPServer | None = None
        self.port: int = 0

    def __enter__(self) -> _StubServer:
        self.port = _free_port()
        # Build a handler subclass with the test-specific overrides.
        overrides = self._overrides

        class _Handler(_StubHandler):
            _overrides = overrides  # type: ignore[assignment]

        self._server = HTTPServer(("127.0.0.1", self.port), _Handler)
        t = threading.Thread(target=self._server.serve_forever, daemon=True)
        t.start()
        return self

    def __exit__(self, *_: object) -> None:
        if self._server:
            self._server.shutdown()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_all_probes_pass_exit_0() -> None:
    """All probes match the stub → exit 0, all results PASS."""
    with _StubServer() as stub:
        results, connectivity_ok = run_all_probes(stub.base_url, api_key="")
    assert connectivity_ok is True
    code = _exit_code(results, connectivity_ok, strict=False)
    assert code == 0
    # At least the GET endpoints should all be PASS.
    get_results = [r for r in results if r.probe.method == "GET"]
    assert all(r.status == "PASS" for r in get_results), [
        (r.probe.path, r.status, r.detail) for r in get_results if r.status != "PASS"
    ]


def test_one_probe_mismatch_exit_1() -> None:
    """Stub returns 500 on GET /api/v1/models → DIFF → exit 1."""
    # We inject a fault only on GET /api/v1/models.
    with _StubServer(overrides={("GET", "/api/v1/models"): 500}) as stub:
        results, connectivity_ok = run_all_probes(stub.base_url, api_key="")

    assert connectivity_ok is True
    models_result = next(
        r for r in results if r.probe.path == "/api/v1/models" and r.probe.method == "GET"
    )
    assert models_result.status == "DIFF"
    assert models_result.actual_status == 500

    code = _exit_code(results, connectivity_ok, strict=False)
    assert code == 1


def test_unreachable_default_mode_exit_0(capsys: pytest.CaptureFixture[str]) -> None:
    """LM Studio unreachable (port not open) → exit 0 + WARNING in default mode."""
    # Use a port we know nothing is listening on.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        closed_port = s.getsockname()[1]
    # Port is now closed (not listening).

    base_url = f"http://127.0.0.1:{closed_port}"
    results, connectivity_ok = run_all_probes(base_url, api_key="")

    assert connectivity_ok is False
    assert all(r.status == "WARN" for r in results)

    code = _exit_code(results, connectivity_ok, strict=False)
    assert code == 0


def test_unreachable_strict_mode_exit_1() -> None:
    """LM Studio unreachable + --strict → exit 1."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        closed_port = s.getsockname()[1]

    base_url = f"http://127.0.0.1:{closed_port}"
    results, connectivity_ok = run_all_probes(base_url, api_key="", strict=True)

    assert connectivity_ok is False
    code = _exit_code(results, connectivity_ok, strict=True)
    assert code == 1


def test_main_all_pass_exit_0() -> None:
    """main() integration: stub server → exits 0."""
    import os

    with _StubServer() as stub:
        with patch.dict(os.environ, {"LM_STUDIO_BASE_URL": stub.base_url}):
            code = main(["--no-append"])
    assert code == 0


def test_main_diff_exit_1() -> None:
    """main() integration: stub injects fault on /health → exits 1."""
    import os

    with _StubServer(overrides={("GET", "/health"): 503}) as stub:
        with patch.dict(os.environ, {"LM_STUDIO_BASE_URL": stub.base_url}):
            code = main(["--no-append"])
    assert code == 1


def test_main_unreachable_default_exit_0() -> None:
    """main() integration: unreachable → exits 0 (non-strict)."""
    import os

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        closed_port = s.getsockname()[1]

    with patch.dict(os.environ, {"LM_STUDIO_BASE_URL": f"http://127.0.0.1:{closed_port}"}):
        code = main(["--no-append"])
    assert code == 0


def test_main_unreachable_strict_exit_1() -> None:
    """main() integration: unreachable + --strict → exits 1."""
    import os

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        closed_port = s.getsockname()[1]

    with patch.dict(os.environ, {"LM_STUDIO_BASE_URL": f"http://127.0.0.1:{closed_port}"}):
        code = main(["--no-append", "--strict"])
    assert code == 1


def test_probe_catalog_not_empty() -> None:
    """Sanity: the catalog has a non-zero number of probes from each section."""
    sections = {p.section for p in _PROBES}
    assert "§2.1" in sections
    assert "§2.2" in sections
    assert "§2.3" in sections
    assert "§2.4" in sections
    assert len(_PROBES) >= 10


def test_main_json_output_exit_0() -> None:
    """main() with --json: stub server → exits 0, output is valid JSON."""
    import io
    import json
    import os

    with _StubServer() as stub:
        with patch.dict(os.environ, {"LM_STUDIO_BASE_URL": stub.base_url}):
            # Capture stdout.
            import contextlib

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = main(["--no-append", "--json"])

    assert code == 0
    output = buf.getvalue()
    data = json.loads(output)
    assert "results" in data
    assert "timestamp" in data
    assert "host" in data
