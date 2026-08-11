# SPDX-License-Identifier: Apache-2.0
"""Tests for lmchat.openapi.drift_check.

Tests for ``openapi/drift_check.py``.

All file I/O in these tests uses ``tmp_path`` — the committed
``docs/api/openapi.yaml`` is never modified.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_schema() -> dict[str, Any]:
    """Return a minimal valid OpenAPI 3.1 schema dict for fabricating YAML files."""
    return {
        "openapi": "3.1.0",
        "info": {
            "contact": {"name": "lm-chat maintainers", "url": "https://example.com"},
            "title": "test-app",
            "version": "0.0.1",
        },
        "paths": {
            "/ping": {
                "get": {
                    "operationId": "ping_ping_get",
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/PingOut"}
                                }
                            },
                            "description": "Successful Response",
                        }
                    },
                    "summary": "Ping",
                }
            }
        },
        "servers": [{"url": "/"}],
    }


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    """Serialise *data* to *path* with stable YAML formatting."""
    path.write_text(
        yaml.safe_dump(data, sort_keys=True, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# load_committed
# ---------------------------------------------------------------------------


def test_load_committed_reads_yaml(tmp_path: Path) -> None:
    """load_committed() parses a YAML file into a dict."""
    from lmchat.openapi.drift_check import load_committed

    p = tmp_path / "openapi.yaml"
    _write_yaml(p, _minimal_schema())

    result = load_committed(p)
    assert isinstance(result, dict)
    assert result["openapi"] == "3.1.0"


def test_load_committed_raises_on_missing_file(tmp_path: Path) -> None:
    """load_committed() raises FileNotFoundError for a non-existent path."""
    from lmchat.openapi.drift_check import load_committed

    with pytest.raises(FileNotFoundError):
        load_committed(tmp_path / "does_not_exist.yaml")


# ---------------------------------------------------------------------------
# check_drift
# ---------------------------------------------------------------------------


def test_drift_check_identical_schemas_returns_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """check_drift() returns (True, '') when committed == live schema."""
    from lmchat.openapi import drift_check as dc_mod

    schema = _minimal_schema()
    committed_path = tmp_path / "openapi.yaml"
    _write_yaml(committed_path, schema)

    # Patch emit_schema to return the same schema.
    monkeypatch.setattr(dc_mod, "emit_schema", lambda app=None: schema)

    identical, diff = dc_mod.check_drift(committed_path=committed_path)
    assert identical is True
    assert diff == ""


def test_drift_check_detects_missing_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """check_drift() detects when a route present in committed is absent from live."""
    from lmchat.openapi import drift_check as dc_mod

    committed_schema = _minimal_schema()
    committed_path = tmp_path / "openapi.yaml"
    _write_yaml(committed_path, committed_schema)

    # Live schema is missing the /ping route.
    live_schema: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {
            "contact": {"name": "lm-chat maintainers", "url": "https://example.com"},
            "title": "test-app",
            "version": "0.0.1",
        },
        "paths": {},
        "servers": [{"url": "/"}],
    }
    monkeypatch.setattr(dc_mod, "emit_schema", lambda app=None: live_schema)

    identical, diff = dc_mod.check_drift(committed_path=committed_path)
    assert identical is False
    assert "/ping" in diff


def test_drift_check_detects_changed_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """check_drift() detects when a schema field changes between committed and live."""
    from lmchat.openapi import drift_check as dc_mod

    committed_schema = _minimal_schema()
    committed_path = tmp_path / "openapi.yaml"
    _write_yaml(committed_path, committed_schema)

    # Live schema bumps the version string.
    live_schema = _minimal_schema()
    live_schema["info"]["version"] = "9.9.9"  # type: ignore[index]
    monkeypatch.setattr(dc_mod, "emit_schema", lambda app=None: live_schema)

    identical, diff = dc_mod.check_drift(committed_path=committed_path)
    assert identical is False
    assert "9.9.9" in diff


def test_drift_check_prints_diff_on_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """main() prints the unified diff to stderr and exits 1 on mismatch."""
    from lmchat.openapi import drift_check as dc_mod

    committed_schema = _minimal_schema()
    committed_path = tmp_path / "openapi.yaml"
    _write_yaml(committed_path, committed_schema)

    live_schema = _minimal_schema()
    live_schema["info"]["version"] = "2.0.0"  # type: ignore[index]

    monkeypatch.setattr(dc_mod, "emit_schema", lambda app=None: live_schema)
    # Redirect committed path to tmp_path so main() uses our fabricated file.
    monkeypatch.setattr(dc_mod, "_DEFAULT_COMMITTED", committed_path)

    rc = dc_mod.main()
    assert rc == 1

    captured = capsys.readouterr()
    # Diff must be on stderr, not stdout.
    assert "2.0.0" in captured.err
    assert "2.0.0" not in captured.out


def test_drift_check_main_exits_zero_on_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """main() exits 0 and prints a success message to stdout on no drift."""
    from lmchat.openapi import drift_check as dc_mod

    schema = _minimal_schema()
    committed_path = tmp_path / "openapi.yaml"
    _write_yaml(committed_path, schema)

    monkeypatch.setattr(dc_mod, "emit_schema", lambda app=None: schema)
    monkeypatch.setattr(dc_mod, "_DEFAULT_COMMITTED", committed_path)

    rc = dc_mod.main()
    assert rc == 0

    captured = capsys.readouterr()
    assert "no drift" in captured.out.lower()


def test_drift_check_diff_on_stderr_not_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Drift output is written exclusively to stderr; stdout stays clean for CI pipes."""
    from lmchat.openapi import drift_check as dc_mod

    committed_schema = _minimal_schema()
    committed_path = tmp_path / "openapi.yaml"
    _write_yaml(committed_path, committed_schema)

    # Introduce a structural change.
    live_schema = _minimal_schema()
    live_schema["paths"]["/extra"] = {  # type: ignore[index]
        "get": {
            "operationId": "extra_extra_get",
            "responses": {"200": {"description": "ok"}},
            "summary": "Extra",
        }
    }
    monkeypatch.setattr(dc_mod, "emit_schema", lambda app=None: live_schema)
    monkeypatch.setattr(dc_mod, "_DEFAULT_COMMITTED", committed_path)

    rc = dc_mod.main()
    assert rc == 1

    captured = capsys.readouterr()
    # Diff lines start with +/- — those must be on stderr only.
    diff_lines = [ln for ln in captured.err.splitlines() if ln.startswith(("+++", "---", "+", "-"))]
    assert diff_lines, "Expected unified diff output on stderr"
    stdout_diff_lines = [ln for ln in captured.out.splitlines() if ln.startswith(("+++", "---"))]
    assert not stdout_diff_lines, "Diff must not appear on stdout"
