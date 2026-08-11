#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Verify pyproject.toml floors match PHASES.md §1 stack baseline.

pyproject.toml floor check: parse
the §1 dependency table from PHASES.md, parse pyproject.toml's
``[project] dependencies`` list, and assert each PHASES.md
entry has a matching pyproject.toml pin at the documented
floor.

Tolerated mismatches:
    - PHASES.md lists ``pyright`` and ``ruff`` but they live in
      ``[dependency-groups] dev``, not the runtime dependency
      list. The script checks BOTH lists.
    - PHASES.md may list extras-only deps (asyncpg under
      ``postgres``). The script checks
      ``[project.optional-dependencies]`` too.

Exit codes:
    0 — every documented floor is present in pyproject.toml.
    1 — at least one floor is missing or mismatched.

Usage:
    uv run python tools/check_pyproject_floors.py
    make check-pyproject-floors
"""
from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path
from typing import Final

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
_PYPROJECT: Final[Path] = _REPO_ROOT / "pyproject.toml"
_PHASES: Final[Path] = _REPO_ROOT / "docs" / "PHASES.md"
# Markdown row pattern (re.MULTILINE so `^` matches each line start).
_TABLE_ROW: Final[re.Pattern[str]] = re.compile(
    r"^\|\s+(?P<name>[a-zA-Z0-9_.\-/\[\] ]+?)\s+\|\s+[^|]+\|\s+(?P<floor>[>=<\s\.\d~]+?)\s+\|",
    re.MULTILINE,
)
# Backend §1 sub-section is bounded by "### Backend" and the next "###".
_BACKEND_SECTION: Final[re.Pattern[str]] = re.compile(
    r"^### Backend.*?(?=^###\s)",
    re.MULTILINE | re.DOTALL,
)


def _parse_phases_floors() -> dict[str, str]:
    """Extract {dep_name_lower: floor_spec} from the PHASES.md §1 Backend table.

    Only the Backend section is parsed; the Frontend section names
    JS/TS deps that live in web/package.json, not pyproject.toml.

    The library name may contain extras (e.g., 'pytest-asyncio'),
    slash-separated combos ('pytest / pytest-asyncio'), or
    bracketed extras ('uvicorn[standard]'). Slash combos are
    split into individual entries with the same floor.
    """
    text = _PHASES.read_text(encoding="utf-8")
    backend_match = _BACKEND_SECTION.search(text)
    if backend_match is None:
        return {}
    backend_text = backend_match.group(0)

    floors: dict[str, str] = {}
    for match in _TABLE_ROW.finditer(backend_text):
        raw_name = match.group("name").strip()
        floor = match.group("floor").strip()
        if raw_name.lower() in {"library", "package", "tool"}:
            continue
        if not floor.startswith(">="):
            continue
        for piece in raw_name.split("/"):
            piece = piece.strip().lower()
            if not piece:
                continue
            piece = re.sub(r"\[.*?\]", "", piece).strip()
            if piece:
                floors[piece] = floor
    return floors


def _parse_pyproject_pins() -> dict[str, str]:
    """Extract {dep_name_lower: pin_spec} from pyproject.toml.

    Collects runtime dependencies, dev dependency-groups, and
    optional-dependencies extras into one flat dict.
    """
    with _PYPROJECT.open("rb") as fh:
        data = tomllib.load(fh)
    pins: dict[str, str] = {}
    sources: list[list[str]] = []
    sources.append(data.get("project", {}).get("dependencies", []))
    for group in data.get("dependency-groups", {}).values():
        sources.append(group)
    for extra in data.get("project", {}).get("optional-dependencies", {}).values():
        sources.append(extra)
    for source in sources:
        for entry in source:
            # Parse "fastapi>=0.136" / "uvicorn[standard]>=0.47"
            stripped = entry.strip()
            match = re.match(r"^([a-zA-Z0-9_.\-]+)", stripped)
            if match is None:
                continue
            name = match.group(1).lower()
            spec = stripped[len(match.group(1)):].strip()
            pins[name] = spec
    return pins


def main() -> int:
    """Compare PHASES.md floors against pyproject.toml pins."""
    if not _PYPROJECT.is_file():
        print(f"ERROR: pyproject.toml not found at {_PYPROJECT}", file=sys.stderr)
        return 1
    if not _PHASES.is_file():
        print(f"ERROR: PHASES.md not found at {_PHASES}", file=sys.stderr)
        return 1

    documented = _parse_phases_floors()
    pinned = _parse_pyproject_pins()

    failures: list[str] = []
    for dep, floor in documented.items():
        pin = pinned.get(dep)
        if pin is None:
            failures.append(
                f"{dep}: documented floor {floor!r} in PHASES.md, "
                "but NOT pinned in pyproject.toml"
            )
            continue
        # Soft check: the pyproject pin should contain the documented floor
        # comparator (>= X). Floor specs like ">= 2.0" should match
        # pyproject ">=2.0" (spacing differences are tolerated).
        normalized_floor = floor.replace(" ", "")
        normalized_pin = pin.replace(" ", "")
        if normalized_floor not in normalized_pin:
            failures.append(
                f"{dep}: PHASES.md floor {floor!r} but pyproject pin {pin!r} (floors must match)"
            )

    if failures:
        print(f"pyproject floor check FAILED ({len(failures)} mismatches):", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print(f"pyproject floors OK ({len(documented)} documented deps checked against pyproject.toml)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
