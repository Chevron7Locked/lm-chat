# SPDX-License-Identifier: Apache-2.0
"""CI drift-check: committed ``openapi.yaml`` vs live FastAPI schema.

Every PR that changes a route **must** also update
``docs/api/openapi.yaml`` in the same commit.  This tool enforces that
constraint by:

1. Loading ``docs/api/openapi.yaml`` (the committed version).
2. Calling :func:`~lmchat.openapi.emit.emit_schema` to generate a fresh
   schema dict from the live FastAPI application.
3. Serialising both to stable-diff YAML strings and comparing them.
4. Exiting 0 on an exact match; exiting 1 with a :mod:`difflib` unified
   diff printed to *stderr* on any divergence.

Usage (CI)::

    python -m lmchat.openapi.drift_check
    make check-openapi-drift

The diff is written to *stderr* so that CI pipelines that collect stdout
for reporting do not get contaminated by diff output.
"""
from __future__ import annotations

import difflib
import sys
from pathlib import Path
from typing import Any

import yaml

from lmchat.logging import get_logger
from lmchat.openapi.emit import _schema_to_yaml, emit_schema

log = get_logger(__name__)

# Same repo-root resolution as emit.py — three levels up from this file.
_REPO_ROOT: Path = Path(__file__).resolve().parent.parent.parent.parent
_DEFAULT_COMMITTED: Path = _REPO_ROOT / "docs" / "api" / "openapi.yaml"


def load_committed(path: Path | None = None) -> dict[str, Any]:
    """Load and parse the committed ``openapi.yaml`` file.

    Args:
        path: Path to the committed YAML file.  Defaults to
              ``docs/api/openapi.yaml`` relative to the repo root.

    Returns:
        The parsed schema dict.

    Raises:
        FileNotFoundError: If *path* does not exist.
        yaml.YAMLError: If the file is not valid YAML.
    """
    target = path or _DEFAULT_COMMITTED
    log.debug("openapi.drift_check.loading_committed", path=str(target))
    content = target.read_text(encoding="utf-8")
    result: dict[str, Any] = yaml.safe_load(content)
    return result


def check_drift(
    committed_path: Path | None = None,
    app: Any | None = None,
) -> tuple[bool, str]:
    """Compare the committed schema against the live schema.

    Args:
        committed_path: Path to the committed YAML.  Defaults to
                        ``docs/api/openapi.yaml``.
        app: Optional pre-built :class:`~fastapi.FastAPI` instance passed
             through to :func:`~lmchat.openapi.emit.emit_schema`.

    Returns:
        A ``(identical, diff_text)`` tuple.  ``identical`` is ``True`` when
        the schemas are byte-for-byte identical after stable YAML
        serialisation.  ``diff_text`` is an empty string when identical, or
        a unified-diff string when not.
    """
    log.info("openapi.drift_check.start")

    committed_dict = load_committed(committed_path)
    live_dict = emit_schema(app)

    committed_yaml = _schema_to_yaml(committed_dict)
    live_yaml = _schema_to_yaml(live_dict)

    if committed_yaml == live_yaml:
        log.info("openapi.drift_check.identical")
        return True, ""

    committed_lines = committed_yaml.splitlines(keepends=True)
    live_lines = live_yaml.splitlines(keepends=True)
    diff = "".join(
        difflib.unified_diff(
            committed_lines,
            live_lines,
            fromfile="docs/api/openapi.yaml (committed)",
            tofile="docs/api/openapi.yaml (live emit)",
        )
    )
    log.warning("openapi.drift_check.drift_detected", diff_lines=diff.count("\n"))
    return False, diff


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: check for OpenAPI schema drift.

    Exits 0 when the committed schema matches the live schema; exits 1
    with a unified diff on *stderr* on any divergence.

    Args:
        argv: Argument list (unused; reserved for future flags).

    Returns:
        0 on no drift; 1 on drift detected.
    """
    identical, diff = check_drift()
    if identical:
        print("OpenAPI schema: no drift detected.")
        return 0

    print(
        "OpenAPI schema drift detected — run `make emit-openapi` and commit the result.",
        file=sys.stderr,
    )
    print(diff, file=sys.stderr, end="")
    return 1


if __name__ == "__main__":
    sys.exit(main())
