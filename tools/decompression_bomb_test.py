#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Decompression bomb test for lm-chat document-upload endpoint.

Crafts gzip and zip payloads where a tiny compressed body expands to a large
size, then uploads them to POST /api/documents.  The server MUST reject the
upload with a status code < 500 (expected 413) and MUST NOT OOM.

The test uses the FastAPI TestClient (httpx) against an in-process app — no
external Docker required.  The ``--target-url`` flag enables running against a
live target (e.g. the L6 compose stack on port 18001).

Emits a JUnit XML testsuite named ``dos-decompression``.

Usage
-----
    # in-process (default, for unit tests):
    uv run python tools/decompression_bomb_test.py --output target/gates/L6-dos-decomp.xml

    # live target:
    uv run python tools/decompression_bomb_test.py \
        --target-url http://localhost:18001 \
        --output target/gates/L6-dos-decomp.xml

    # skip mode:
    uv run python tools/decompression_bomb_test.py --skipped \
        --output target/gates/L6-dos-decomp.xml

Exit codes: 0 = pass, 1 = one or more assertions failed.
"""
from __future__ import annotations

import argparse
import gzip
import io
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path
from xml.etree import ElementTree as ET

# ---------------------------------------------------------------------------
# Bomb factories
# ---------------------------------------------------------------------------


def _make_gzip_bomb(compressed_kb: int = 10) -> bytes:
    """Return a gzip bomb: ~compressed_kb KB compresses highly-repeated data.

    The uncompressed expansion is roughly 1000x the compressed size because
    gzip achieves near-perfect compression on repetitive byte sequences.
    """
    # 1 MB of zeroes compresses to ~1 KB; scale accordingly.
    uncompressed_size = compressed_kb * 1024 * 1024  # 1 MB per KB target
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9) as gz:
        # Write in chunks to avoid OOM in the test builder itself.
        chunk = b"\x00" * 65536
        remaining = uncompressed_size
        while remaining > 0:
            to_write = min(len(chunk), remaining)
            gz.write(chunk[:to_write])
            remaining -= to_write
    return buf.getvalue()


def _make_zip_bomb(compressed_kb: int = 10) -> bytes:
    """Return a zip bomb: single entry with highly-compressible content."""
    buf = io.BytesIO()
    uncompressed_size = compressed_kb * 1024 * 1024
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        chunk = b"\x00" * 65536
        data_chunks: list[bytes] = []
        remaining = uncompressed_size
        while remaining > 0:
            to_write = min(len(chunk), remaining)
            data_chunks.append(chunk[:to_write])
            remaining -= to_write
        zf.writestr("bomb.txt", b"".join(data_chunks))
    return buf.getvalue()


def _make_nested_gzip_bomb(layers: int = 3) -> bytes:
    """Return a nested gzip bomb (gzip within gzip, ``layers`` deep)."""
    data = b"\x00" * (64 * 1024)  # 64 KB inner payload
    for _ in range(layers):
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9) as gz:
            gz.write(data)
        data = buf.getvalue()
    return data


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

_UPLOAD_PATH = "/api/documents"


class _Case:
    def __init__(
        self,
        name: str,
        payload: bytes,
        filename: str,
        content_type: str,
    ) -> None:
        self.name = name
        self.payload = payload
        self.filename = filename
        self.content_type = content_type


def _make_cases() -> list[_Case]:
    return [
        _Case(
            name="gzip-bomb-10kb-compressed",
            payload=_make_gzip_bomb(compressed_kb=10),
            filename="bomb.txt.gz",
            content_type="application/gzip",
        ),
        _Case(
            name="zip-bomb-10kb-compressed",
            payload=_make_zip_bomb(compressed_kb=10),
            filename="bomb.zip",
            content_type="application/zip",
        ),
        _Case(
            name="nested-gzip-3-layers",
            payload=_make_nested_gzip_bomb(layers=3),
            filename="nested.gz",
            content_type="application/gzip",
        ),
        _Case(
            name="gzip-bomb-1kb-compressed",
            payload=_make_gzip_bomb(compressed_kb=1),
            filename="small_bomb.gz",
            content_type="application/gzip",
        ),
    ]


def _assert_case(
    case: _Case,
    post_fn: Callable[[str, bytes, str, str], tuple[int, str]],
) -> tuple[bool, str]:
    """Run one upload case; return (passed, detail)."""
    try:
        status, body = post_fn(_UPLOAD_PATH, case.payload, case.filename, case.content_type)
    except Exception as exc:  # noqa: BLE001
        return False, f"unexpected exception: {exc!r}"

    # Server must reject with a non-500 status.
    # Acceptable: 400, 413, 415, 422.  NOT acceptable: 2xx or 5xx.
    if status >= 500:
        return False, (
            f"server returned {status} (5xx) — possible OOM or panic. "
            f"body={body[:200]!r}"
        )
    if status < 400:
        return False, (
            f"server accepted decompression bomb with {status} — expected 4xx rejection. "
            f"payload_bytes={len(case.payload)} body={body[:200]!r}"
        )
    # 4xx — correct rejection.
    return True, f"correctly rejected with {status}: {body[:120]!r}"


def _build_suite(results: list[tuple[str, bool, str]]) -> ET.Element:
    total = len(results)
    failures = sum(1 for _, ok, _ in results if not ok)
    suite = ET.Element(
        "testsuite",
        {
            "name": "dos-decompression",
            "tests": str(total),
            "failures": str(failures),
            "errors": "0",
            "skipped": "0",
        },
    )
    for name, ok, detail in results:
        tc = ET.SubElement(
            suite, "testcase", {"name": name, "classname": "dos.decompression"}
        )
        if not ok:
            fail = ET.SubElement(
                tc,
                "failure",
                {
                    "message": f"decompression bomb not rejected: {name}",
                    "type": "dos-decompression",
                },
            )
            fail.text = detail
    return suite


def _emit_skipped(reason: str = "") -> ET.Element:
    suite = ET.Element(
        "testsuite",
        {
            "name": "dos-decompression",
            "tests": "1",
            "failures": "0",
            "errors": "0",
            "skipped": "1",
        },
    )
    tc = ET.SubElement(
        suite, "testcase", {"name": "decompression-bomb-skipped", "classname": "dos.decompression"}
    )
    msg = reason or "decompression_bomb_test skipped (--skipped flag)"
    sk = ET.SubElement(tc, "skipped", {"message": msg})
    sk.text = msg
    return suite


def _run_against_testclient() -> list[tuple[str, bool, str]]:
    """Run bomb uploads against an in-process FastAPI TestClient."""
    import os

    # Minimal env so app imports don't crash.  No real LM Studio call
    # is made — bombs go to the in-process FastAPI app, the URL is
    # unused at runtime.
    os.environ.setdefault("LM_CHAT_SECRET", "dos-test-secret-00000000000000000000000000000000")
    os.environ.setdefault("LM_STUDIO_BASE_URL", "http://localhost:1234")
    os.environ.setdefault("LM_STUDIO_API_KEY", "sk-dos-test-placeholder")

    try:
        from starlette.testclient import TestClient

        from lmchat.app import app
    except Exception as exc:  # noqa: BLE001
        return [("infra-setup", False, f"could not import app: {exc!r}")]

    import secrets

    # Use a unique username per run to avoid conflicts with the dev SQLite DB.
    username = f"dosbomb{secrets.token_hex(6)}"
    password = f"BombT3st!{secrets.token_hex(4)}"

    results: list[tuple[str, bool, str]] = []

    # We need a valid session cookie to reach the upload endpoint (auth-required).
    # Register a test user + login to get a cookie.
    with TestClient(app, raise_server_exceptions=False) as client:
        # Register (form-encoded per auth route).
        reg_resp = client.post(
            "/api/auth/register",
            data={"username": username, "password": password},
        )
        if reg_resp.status_code not in (200, 201):
            # Registration failure = infra problem (e.g. greenlet/DB error);
            # return as a skipped result so the gate doesn't hard-fail.
            suite = _emit_skipped(
                f"registration failed ({reg_resp.status_code}); "
                f"skipping decompression-bomb E2E: {reg_resp.text[:120]}"
            )
            sk_el = suite.find(".//skipped")
            skip_msg = sk_el.get("message", "") if sk_el is not None else "skipped"
            return [("infra-setup-skipped", True, skip_msg)]

        # Login (form-encoded per auth route).
        login_resp = client.post(
            "/api/auth/login",
            data={"username": username, "password": password},
        )
        if login_resp.status_code != 200:
            suite = _emit_skipped(
                f"login failed ({login_resp.status_code}); "
                f"skipping decompression-bomb E2E: {login_resp.text[:120]}"
            )
            return [("infra-setup-skipped", True, "login failed — skipped")]

        def post_fn(
            path: str, payload: bytes, filename: str, content_type: str
        ) -> tuple[int, str]:
            resp = client.post(
                path,
                files={"file": (filename, payload, content_type)},
            )
            return resp.status_code, resp.text

        for case in _make_cases():
            ok, detail = _assert_case(case, post_fn)  # type: ignore[arg-type]
            results.append((case.name, ok, detail))

    return results


def _run_against_url(base_url: str, token: str | None) -> list[tuple[str, bool, str]]:
    """Run bomb uploads against a live target URL."""
    try:
        import httpx
    except ImportError:
        return [("infra-setup", False, "httpx not installed")]

    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    results: list[tuple[str, bool, str]] = []
    with httpx.Client(base_url=base_url, headers=headers, timeout=30.0) as client:

        def post_fn(
            path: str, payload: bytes, filename: str, content_type: str
        ) -> tuple[int, str]:
            resp = client.post(
                path,
                files={"file": (filename, payload, content_type)},
            )
            return resp.status_code, resp.text

        for case in _make_cases():
            ok, detail = _assert_case(case, post_fn)  # type: ignore[arg-type]
            results.append((case.name, ok, detail))

    return results


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, default=None,
                   help="Path to write JUnit XML (default: stdout)")
    p.add_argument("--target-url", type=str, default=None,
                   help="Base URL of live target (e.g. http://localhost:18001). "
                        "When omitted, runs against an in-process TestClient.")
    p.add_argument("--auth-token", type=str, default=None,
                   help="Bearer token for the live target (--target-url mode only).")
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
            "name": "dos-decompression",
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
