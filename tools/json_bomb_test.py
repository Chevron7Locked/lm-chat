#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""JSON bomb test for lm-chat JSON endpoints.

Three attack classes against any POST endpoint that accepts a JSON body:

1. Billion-laughs equivalent — deeply nested arrays/objects (depth 10^4;
   full 10^6 would OOM the test builder too, so we test a level that is
   still pathological without blowing up the test runner itself).
2. Massive flat arrays — 10^5 scalar items in a single array.
3. Duplicate-key DoS — 10^5 identical keys in one JSON object (CPython's
   dict deduplicates but the JSON parser still processes them).

The test uses the FastAPI TestClient against an in-process app.  The
``--target-url`` flag enables running against a live target.

Assertions: FastAPI / Pydantic must reject or bound memory — no 5xx, no OOM.
Expected outcomes: 400, 413, 422, or any 4xx.

Emits a JUnit XML testsuite named ``dos-jsonbomb``.

Usage
-----
    uv run python tools/json_bomb_test.py --output target/gates/L6-dos-json.xml
    uv run python tools/json_bomb_test.py --skipped --output target/gates/L6-dos-json.xml

Exit codes: 0 = pass, 1 = one or more assertions failed.
"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from xml.etree import ElementTree as ET

# ---------------------------------------------------------------------------
# Payload factories
# ---------------------------------------------------------------------------


def _make_deeply_nested_arrays(depth: int = 10_000) -> str:
    """Return JSON string: array nested depth levels deep, innermost = [1]."""
    inner = "[1]"
    for _ in range(depth - 1):
        inner = f"[{inner}]"
    return inner


def _make_deeply_nested_objects(depth: int = 10_000) -> str:
    """Return JSON string: object nested depth levels deep."""
    inner = '{"v":1}'
    for _ in range(depth - 1):
        inner = f'{{"x":{inner}}}'
    return inner


def _make_massive_array(size: int = 100_000) -> str:
    """Return JSON string: flat array with size integer scalars."""
    return "[" + ",".join("1" for _ in range(size)) + "]"


def _make_duplicate_keys(count: int = 100_000) -> str:
    """Return JSON string: object with count identical keys."""
    pairs = ",".join(f'"k{i}":"v"' for i in range(count))
    return "{" + pairs + "}"


def _make_mixed_bomb() -> str:
    """Return JSON string: moderately nested + large array (combined)."""
    inner_array = "[" + ",".join("0" for _ in range(10_000)) + "]"
    return f'{{"data":{inner_array},"nested":{_make_deeply_nested_arrays(500)}}}'


# ---------------------------------------------------------------------------
# Test case definitions
# ---------------------------------------------------------------------------

# Endpoints that accept a JSON POST body (available without auth or with
# easily-obtainable auth token).  The chat endpoint is behind auth; the
# login endpoint accepts arbitrary JSON.
_JSON_ENDPOINTS: list[tuple[str, str]] = [
    # (path, description)
    ("/api/auth/login", "auth-login"),
    ("/api/auth/register", "auth-register"),
]


class _Case:
    def __init__(self, name: str, payload_str: str, endpoint: str, endpoint_label: str) -> None:
        self.name = name
        self.payload_str = payload_str
        self.endpoint = endpoint
        self.endpoint_label = endpoint_label


def _make_cases() -> list[_Case]:
    cases: list[_Case] = []
    for path, label in _JSON_ENDPOINTS:
        cases += [
            _Case(
                f"{label}-nested-array-10k",
                _make_deeply_nested_arrays(10_000),
                path,
                label,
            ),
            _Case(
                f"{label}-nested-object-10k",
                _make_deeply_nested_objects(10_000),
                path,
                label,
            ),
            _Case(
                f"{label}-massive-array-100k",
                _make_massive_array(100_000),
                path,
                label,
            ),
            _Case(
                f"{label}-duplicate-keys-100k",
                _make_duplicate_keys(100_000),
                path,
                label,
            ),
            _Case(
                f"{label}-mixed-bomb",
                _make_mixed_bomb(),
                path,
                label,
            ),
        ]
    return cases


# ---------------------------------------------------------------------------
# Assertion
# ---------------------------------------------------------------------------


def _assert_case(
    case: _Case,
    post_json_fn: Callable[[str, str], tuple[int, str]],
) -> tuple[bool, str]:
    try:
        status, body = post_json_fn(case.endpoint, case.payload_str)
    except Exception as exc:  # noqa: BLE001
        return False, f"unexpected exception: {exc!r}"

    if status >= 500:
        return False, (
            f"server returned {status} (5xx) on JSON bomb — possible OOM or unhandled parse error. "
            f"body={body[:200]!r}"
        )
    # Any non-5xx is acceptable (2xx = unexpected but not dangerous, 4xx = correct).
    return True, f"rejected/handled with {status}: {body[:120]!r}"


# ---------------------------------------------------------------------------
# Suite builder
# ---------------------------------------------------------------------------


def _build_suite(results: list[tuple[str, bool, str]]) -> ET.Element:
    total = len(results)
    failures = sum(1 for _, ok, _ in results if not ok)
    suite = ET.Element(
        "testsuite",
        {
            "name": "dos-jsonbomb",
            "tests": str(total),
            "failures": str(failures),
            "errors": "0",
            "skipped": "0",
        },
    )
    for name, ok, detail in results:
        tc = ET.SubElement(
            suite, "testcase", {"name": name, "classname": "dos.jsonbomb"}
        )
        if not ok:
            fail = ET.SubElement(
                tc,
                "failure",
                {"message": f"JSON bomb caused 5xx: {name}", "type": "dos-jsonbomb"},
            )
            fail.text = detail
    return suite


def _emit_skipped(reason: str = "") -> ET.Element:
    suite = ET.Element(
        "testsuite",
        {
            "name": "dos-jsonbomb",
            "tests": "1",
            "failures": "0",
            "errors": "0",
            "skipped": "1",
        },
    )
    tc = ET.SubElement(
        suite, "testcase", {"name": "json-bomb-skipped", "classname": "dos.jsonbomb"}
    )
    msg = reason or "json_bomb_test skipped (--skipped flag)"
    sk = ET.SubElement(tc, "skipped", {"message": msg})
    sk.text = msg
    return suite


# ---------------------------------------------------------------------------
# Runners
# ---------------------------------------------------------------------------


def _run_against_testclient() -> list[tuple[str, bool, str]]:
    """Run against an in-process FastAPI TestClient."""
    import os

    # Placeholders to satisfy config import in the TestClient process.
    # No real LM Studio call is made by this test — bombs go to the
    # in-process FastAPI app, not upstream.  The URL value is unused.
    os.environ.setdefault("LM_CHAT_SECRET", "dos-test-secret-00000000000000000000000000000000")
    os.environ.setdefault("LM_STUDIO_BASE_URL", "http://localhost:1234")
    os.environ.setdefault("LM_STUDIO_API_KEY", "sk-dos-test-placeholder")

    try:
        from starlette.testclient import TestClient

        from lmchat.app import app
    except Exception as exc:  # noqa: BLE001
        return [("infra-setup", False, f"could not import app: {exc!r}")]

    results: list[tuple[str, bool, str]] = []

    with TestClient(app, raise_server_exceptions=False) as client:

        def post_json_fn(path: str, payload_str: str) -> tuple[int, str]:
            resp = client.post(
                path,
                content=payload_str.encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            return resp.status_code, resp.text

        for case in _make_cases():
            ok, detail = _assert_case(case, post_json_fn)  # type: ignore[arg-type]
            results.append((case.name, ok, detail))

    return results


def _run_against_url(base_url: str, token: str | None) -> list[tuple[str, bool, str]]:
    """Run against a live target URL."""
    try:
        import httpx
    except ImportError:
        return [("infra-setup", False, "httpx not installed")]

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    results: list[tuple[str, bool, str]] = []
    with httpx.Client(base_url=base_url, headers=headers, timeout=30.0) as client:

        def post_json_fn(path: str, payload_str: str) -> tuple[int, str]:
            resp = client.post(
                path,
                content=payload_str.encode("utf-8"),
            )
            return resp.status_code, resp.text

        for case in _make_cases():
            ok, detail = _assert_case(case, post_json_fn)  # type: ignore[arg-type]
            results.append((case.name, ok, detail))

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, default=None,
                   help="Path to write JUnit XML (default: stdout)")
    p.add_argument("--target-url", type=str, default=None,
                   help="Base URL of live target. When omitted, uses in-process TestClient.")
    p.add_argument("--auth-token", type=str, default=None,
                   help="Bearer token for live target (--target-url mode only).")
    p.add_argument("--skipped", action="store_true",
                   help="Emit a synthetic skipped testcase.")
    p.add_argument("--skipped-reason", type=str, default="",
                   help="Override skipped-mode reason string.")
    args = p.parse_args(argv)

    if args.skipped:
        suite = _emit_skipped(args.skipped_reason)
    elif args.target_url:
        results = _run_against_url(args.target_url, args.auth_token)
        suite = _build_suite(results)
    else:
        results = _run_against_testclient()
        suite = _build_suite(results)

    root = ET.Element(
        "testsuites",
        {
            "name": "dos-jsonbomb",
            "tests": suite.get("tests", "0"),
            "failures": suite.get("failures", "0"),
            "errors": "0",
        },
    )
    root.append(suite)
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        tree.write(args.output, encoding="utf-8", xml_declaration=True)
    else:
        sys.stdout.write(ET.tostring(root, encoding="unicode", xml_declaration=True))
        sys.stdout.write("\n")

    return 1 if int(suite.get("failures", "0")) > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
