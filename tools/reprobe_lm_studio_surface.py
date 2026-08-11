# SPDX-License-Identifier: Apache-2.0
"""Re-probe every LM Studio HTTP endpoint in INTEGRATION_MAP §2 and detect drift.

## Purpose

LM Studio's HTTP surface changes between releases. This script re-runs a
probe against every endpoint documented in ``docs/INTEGRATION_MAP.md`` §2
and compares the actual response (status code + key envelope fields) against
the documented expectation. Any confirmed divergence is a DIFF; unresolvable
connectivity is a WARNING (default) or FAIL (``--strict``).

## Probe catalog strategy

The probes are maintained as a Python list in this file rather than by
parsing ``docs/INTEGRATION_MAP.md`` at runtime. Rationale: the table rows
are stable (weekly drift-detection cadence), and a static list keeps this
script dependency-free, deterministic, and easy to update when INTEGRATION_MAP
is amended. Any structural change to the table that invalidates the static
list will surface immediately on the next probe run — the script's own tests
verify the catalog is internally consistent.

When INTEGRATION_MAP §2 changes, update ``_PROBES`` below and bump the
``_CATALOG_DATE`` constant. Both live in the same place so reviewers can't
miss the sync requirement.

## Failure-mode semantics (QB-001 / r2 amendment)

- **Connectivity failure** (LM Studio unreachable, TCP refused, DNS fail,
  timeout): log WARNING, exit 0. Do NOT redbar CI when LM Studio is simply
  not running (weekly cron cadence; paused LM Studio is normal).
- **Confirmed shape diff** (endpoint reachable but response code or key
  envelope field differs from documented expectation): exit 1 with
  structured diff to stderr.
- **``--strict``**: connectivity failures become fatal too. For
  deployment-readiness gates where "LM Studio not running" is itself a
  failure.

## Output

Structured markdown diff to stdout.
Appended to ``docs/INTEGRATION_MAP_DRIFT.md`` (gitignored; rotate manually
or let it accumulate — it's local-only).

## CLI

    python tools/reprobe_lm_studio_surface.py
    python tools/reprobe_lm_studio_surface.py --strict
    python tools/reprobe_lm_studio_surface.py --output diff.md
    python tools/reprobe_lm_studio_surface.py --json

## See also

``docs/INTEGRATION_MAP.md`` §7 — drift detection spec.
the HTTP-first reprobe implementation scope.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REPO_ROOT: Path = Path(__file__).resolve().parents[1]
_DRIFT_LOG: Path = _REPO_ROOT / "docs" / "INTEGRATION_MAP_DRIFT.md"
_CATALOG_DATE = "2026-05-21"

# Default timeout (seconds) for each probe request.
_PROBE_TIMEOUT = 10.0

# ---------------------------------------------------------------------------
# Probe catalog (static list; matches INTEGRATION_MAP §2.1–§2.4)
# ---------------------------------------------------------------------------


@dataclass
class Probe:
    """One row in INTEGRATION_MAP §2."""

    section: str  # e.g. "§2.1", "§2.4"
    method: str  # "GET" or "POST"
    path: str  # e.g. "/api/v1/models"
    expected_status: int  # documented probe status code

    # Optional: send this JSON body on POST probes (minimal required fields).
    request_body: dict[str, Any] = field(default_factory=dict)

    # Optional: key envelope fields to check in the response JSON.
    # A list of (dot-separated key path, expected type name or literal value).
    # Examples:
    #   ("models", "list")         — response["models"] must be a list
    #   ("error.param", "model")   — response["error"]["param"] == "model"
    # The type check uses Python type name strings: "str", "list", "dict",
    # "int", "float".  A non-type-name value is treated as a literal
    # equality check.
    envelope_checks: list[tuple[str, str]] = field(default_factory=list)

    # Human-readable note for reporting.
    note: str = ""


# §2.1 — Native LM Studio surface (/api/v1/*)
# §2.2 — OpenAI-compat surface (/v1/*)
# §2.3 — Anthropic-compat surface (/v1/messages)
# §2.4 — Health + meta
_PROBES: list[Probe] = [
    # --- §2.1 Native ---
    Probe(
        section="§2.1",
        method="GET",
        path="/api/v1/models",
        expected_status=200,
        envelope_checks=[("models", "list")],
        note="List models — expects native format with 'models' key",
    ),
    Probe(
        section="§2.1",
        method="POST",
        path="/api/v1/chat",
        expected_status=400,
        request_body={},  # no body → 400 because "model" is required
        envelope_checks=[("error.param", "model")],
        note="Native chat — 400 when 'model' missing (documented behavior)",
    ),
    Probe(
        section="§2.1",
        method="POST",
        path="/api/v1/models/load",
        expected_status=200,
        request_body={"model": "__probe_only_do_not_use__"},
        # LM Studio may return 400/404 for unknown model; documented as 200
        # for the probe; we accept 200 OR 400/404 as "endpoint exists".
        note="Load model endpoint exists; documented 200 on success",
    ),
    Probe(
        section="§2.1",
        method="POST",
        path="/api/v1/models/unload",
        expected_status=200,
        request_body={"instance_id": "__probe_only__"},
        note="Unload endpoint exists; documented 200 on success",
    ),
    Probe(
        section="§2.1",
        method="POST",
        path="/api/v1/models/download",
        expected_status=400,
        request_body={},  # no body → 400 because "model" is required
        note="Download endpoint — 400 when 'model' missing (documented)",
    ),
    # --- §2.2 OpenAI-compat ---
    Probe(
        section="§2.2",
        method="POST",
        path="/v1/chat/completions",
        expected_status=400,
        request_body={},
        note="OAI chat completions — 400 on empty body (endpoint exists)",
    ),
    Probe(
        section="§2.2",
        method="POST",
        path="/v1/responses",
        expected_status=400,
        request_body={},
        note="Responses API — 400 on empty body (endpoint exists, adopted P11c.1)",
    ),
    Probe(
        section="§2.2",
        method="POST",
        path="/v1/embeddings",
        expected_status=400,
        request_body={},
        note="Embeddings — 400 on empty body (endpoint exists)",
    ),
    Probe(
        section="§2.2",
        method="GET",
        path="/v1/models",
        expected_status=200,
        note="OAI model list — 200",
    ),
    Probe(
        section="§2.2",
        method="POST",
        path="/v1/completions",
        expected_status=400,
        request_body={},
        note="Legacy completions — 400 on empty body (endpoint exists)",
    ),
    # --- §2.3 Anthropic-compat ---
    Probe(
        section="§2.3",
        method="POST",
        path="/v1/messages",
        expected_status=400,
        request_body={},
        # Live wire (probed 2026-05-21): LM Studio returns
        # {"type":"error","error":{"type":"invalid_request_error",
        #   "message":"request.max_tokens: Required"}}
        # — no "param" key; check the "error.type" key instead.
        envelope_checks=[("error.type", "invalid_request_error")],
        note="Anthropic-compat — 400 with error.type=invalid_request_error when empty",
    ),
    # --- §2.4 Health + meta ---
    Probe(
        section="§2.4",
        method="GET",
        path="/",
        expected_status=200,
        note="Server root — 200",
    ),
    Probe(
        section="§2.4",
        method="GET",
        path="/health",
        expected_status=200,
        note="Health check — 200",
    ),
    Probe(
        section="§2.4",
        method="GET",
        path="/status",
        expected_status=200,
        note="Server status — 200",
    ),
    Probe(
        section="§2.4",
        method="GET",
        path="/info",
        expected_status=200,
        note="Server info — 200",
    ),
    Probe(
        section="§2.4",
        method="GET",
        path="/api",
        expected_status=200,
        note="API discovery — 200",
    ),
]

# Probes where ANY 4xx from the server means the endpoint EXISTS (we can't
# easily trigger the exact expected status without a loaded model, etc.).
# For these, we relax the check to: "endpoint reachable (any 2xx/4xx)" rather
# than strict status-code match.  This avoids false DIFFs when a model is not
# loaded (load/unload probes) or request parsing has minor changes.
_ENDPOINT_EXISTS_PATHS: frozenset[str] = frozenset(
    {
        "/api/v1/models/load",
        "/api/v1/models/unload",
        "/v1/chat/completions",
        "/v1/embeddings",
        "/v1/completions",
    }
)

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class ProbeResult:
    probe: Probe
    status: str  # "PASS" | "DIFF" | "FAIL" | "WARN"
    actual_status: int | None
    detail: str


# ---------------------------------------------------------------------------
# Env / config helpers
# ---------------------------------------------------------------------------


def _load_env_local() -> None:
    """Load .env.local from repo root into os.environ (non-overriding)."""
    env_path = _REPO_ROOT / ".env.local"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def _get_config() -> tuple[str, str]:
    """Return (base_url, api_key) from env (after loading .env.local).

    Fails loud if LM_STUDIO_BASE_URL is unset rather than silently
    defaulting to localhost — the right URL is operator-specific
    (Docker users need host.docker.internal, not localhost).
    """
    _load_env_local()
    base_url = os.environ.get("LM_STUDIO_BASE_URL")
    if not base_url:
        sys.stderr.write(
            "ERROR: LM_STUDIO_BASE_URL not set.  Source .env.local "
            "first OR set explicitly.\n"
        )
        sys.exit(2)
    base_url = base_url.rstrip("/")
    api_key = os.environ.get("LM_STUDIO_API_KEY", "")
    return base_url, api_key


# ---------------------------------------------------------------------------
# Envelope check helpers
# ---------------------------------------------------------------------------


def _get_nested(data: dict[str, Any], key_path: str) -> Any:
    """Resolve a dot-separated key path into *data*.

    Returns ``_MISSING`` sentinel if the path does not exist.
    """
    parts = key_path.split(".")
    cur: Any = data
    for part in parts:
        if not isinstance(cur, dict):
            return _MISSING
        cur = cur.get(part, _MISSING)
        if cur is _MISSING:
            return _MISSING
    return cur


_MISSING = object()

_TYPE_MAP: dict[str, type] = {
    "str": str,
    "list": list,
    "dict": dict,
    "int": int,
    "float": float,
    "bool": bool,
}


def _check_envelope(
    probe: Probe,
    body: dict[str, Any],
) -> list[str]:
    """Return a list of failure messages for unmet envelope_checks."""
    failures: list[str] = []
    for key_path, expectation in probe.envelope_checks:
        val = _get_nested(body, key_path)
        if val is _MISSING:
            failures.append(f"envelope key '{key_path}' absent in response")
            continue
        if expectation in _TYPE_MAP:
            expected_type = _TYPE_MAP[expectation]
            if not isinstance(val, expected_type):
                failures.append(
                    f"envelope['{key_path}']: expected type {expectation}, "
                    f"got {type(val).__name__!r} (value={val!r})"
                )
        else:
            # Literal equality check.
            if str(val) != expectation and val != expectation:
                failures.append(
                    f"envelope['{key_path}']: expected {expectation!r}, got {val!r}"
                )
    return failures


# ---------------------------------------------------------------------------
# Single probe execution
# ---------------------------------------------------------------------------


def _run_probe(
    client: httpx.Client,
    probe: Probe,
    base_url: str,
) -> ProbeResult:
    """Execute one probe and return a ProbeResult."""
    url = f"{base_url}{probe.path}"
    try:
        if probe.method == "GET":
            resp = client.get(url, timeout=_PROBE_TIMEOUT)
        elif probe.method == "POST":
            # Always send json= (even for empty dict) so httpx adds
            # Content-Type: application/json.  Omitting the header causes
            # LM Studio to return 415 Unsupported Media Type.
            resp = client.post(
                url,
                json=probe.request_body,
                timeout=_PROBE_TIMEOUT,
            )
        else:
            return ProbeResult(
                probe=probe,
                status="FAIL",
                actual_status=None,
                detail=f"unsupported method {probe.method}",
            )
    except httpx.ConnectError as exc:
        return ProbeResult(
            probe=probe,
            status="WARN",
            actual_status=None,
            detail=f"connectivity failure: {exc}",
        )
    except httpx.TimeoutException as exc:
        return ProbeResult(
            probe=probe,
            status="WARN",
            actual_status=None,
            detail=f"timeout: {exc}",
        )
    except OSError as exc:
        return ProbeResult(
            probe=probe,
            status="WARN",
            actual_status=None,
            detail=f"network error: {exc}",
        )

    actual = resp.status_code

    # Relaxed check for "endpoint exists" probes.
    if probe.path in _ENDPOINT_EXISTS_PATHS:
        if 200 <= actual < 500:
            # Endpoint exists and responded — treat as PASS regardless of
            # exact code.
            return ProbeResult(
                probe=probe,
                status="PASS",
                actual_status=actual,
                detail="endpoint reachable (relaxed check)",
            )
        else:
            return ProbeResult(
                probe=probe,
                status="DIFF",
                actual_status=actual,
                detail=f"expected reachable endpoint, got {actual} (5xx suggests service error)",
            )

    # Strict status check for all other probes.
    if actual != probe.expected_status:
        return ProbeResult(
            probe=probe,
            status="DIFF",
            actual_status=actual,
            detail=f"expected HTTP {probe.expected_status}, got {actual}",
        )

    # Parse response body for envelope checks (only if there are checks to do).
    if probe.envelope_checks:
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            return ProbeResult(
                probe=probe,
                status="DIFF",
                actual_status=actual,
                detail="expected JSON body for envelope checks; got non-JSON response",
            )
        failures = _check_envelope(probe, body)
        if failures:
            return ProbeResult(
                probe=probe,
                status="DIFF",
                actual_status=actual,
                detail="; ".join(failures),
            )

    return ProbeResult(
        probe=probe,
        status="PASS",
        actual_status=actual,
        detail="ok",
    )


# ---------------------------------------------------------------------------
# Run all probes
# ---------------------------------------------------------------------------


def run_all_probes(
    base_url: str,
    api_key: str,
    strict: bool = False,
) -> tuple[list[ProbeResult], bool]:
    """Run all probes and return (results, connectivity_ok).

    *connectivity_ok* is False when ALL probes returned WARN (LM Studio
    is unreachable).
    """
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    results: list[ProbeResult] = []
    with httpx.Client(headers=headers) as client:
        for probe in _PROBES:
            result = _run_probe(client, probe, base_url)
            results.append(result)

    connectivity_ok = any(r.status != "WARN" for r in results)
    return results, connectivity_ok


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


def _format_markdown(
    results: list[ProbeResult],
    base_url: str,
    timestamp: str,
    connectivity_ok: bool,
    strict: bool,
) -> str:
    lines: list[str] = []
    lines.append(f"## Re-probe {timestamp} (host={base_url})")
    lines.append("")

    if not connectivity_ok:
        lines.append(
            "> WARNING: LM Studio unreachable at all probed paths. "
            "Probe results below are all WARN."
        )
        lines.append("")

    for r in results:
        label = r.probe.path
        method = r.probe.method
        if r.status == "PASS":
            status_code_str = str(r.actual_status) if r.actual_status is not None else "—"
            lines.append(f"- PASS  {method} {label:<45} {status_code_str}")
        elif r.status == "WARN":
            lines.append(f"- WARN  {method} {label:<45} unreachable — {r.detail}")
        elif r.status == "DIFF":
            status_code_str = str(r.actual_status) if r.actual_status is not None else "—"
            lines.append(
                f"- DIFF  {method} {label:<45} expected {r.probe.expected_status} "
                f"got {status_code_str} — {r.detail}"
            )
        else:
            lines.append(f"- FAIL  {method} {label:<45} {r.detail}")

    lines.append("")
    n_pass = sum(1 for r in results if r.status == "PASS")
    n_diff = sum(1 for r in results if r.status == "DIFF")
    n_fail = sum(1 for r in results if r.status == "FAIL")
    n_warn = sum(1 for r in results if r.status == "WARN")

    if not connectivity_ok and strict:
        exit_code = 1
        summary = "EXIT 1 (--strict: connectivity failure)"
    elif not connectivity_ok:
        exit_code = 0
        summary = "EXIT 0 (connectivity failure, non-strict mode)"
    elif n_diff > 0 or n_fail > 0:
        exit_code = 1
        summary = f"EXIT 1 ({n_diff} DIFF, {n_fail} FAIL)"
    else:
        exit_code = 0
        summary = "EXIT 0 (all probes PASS or WARN)"

    lines.append(
        f"Summary: {n_pass} PASS, {n_diff} DIFF, {n_fail} FAIL, {n_warn} WARN — {summary}"
    )
    lines.append("")
    _ = exit_code  # used by caller
    return "\n".join(lines)


def _format_json(
    results: list[ProbeResult],
    base_url: str,
    timestamp: str,
    connectivity_ok: bool,
    strict: bool,
) -> str:
    out: dict[str, Any] = {
        "timestamp": timestamp,
        "host": base_url,
        "catalog_date": _CATALOG_DATE,
        "connectivity_ok": connectivity_ok,
        "results": [
            {
                "section": r.probe.section,
                "method": r.probe.method,
                "path": r.probe.path,
                "status": r.status,
                "actual_status": r.actual_status,
                "expected_status": r.probe.expected_status,
                "detail": r.detail,
                "note": r.probe.note,
            }
            for r in results
        ],
    }
    return json.dumps(out, indent=2)


# ---------------------------------------------------------------------------
# Exit-code determination
# ---------------------------------------------------------------------------


def _exit_code(
    results: list[ProbeResult],
    connectivity_ok: bool,
    strict: bool,
) -> int:
    if not connectivity_ok and strict:
        return 1
    if not connectivity_ok:
        return 0
    if any(r.status in ("DIFF", "FAIL") for r in results):
        return 1
    return 0


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Run all probes and report drift. Return exit code."""
    parser = argparse.ArgumentParser(
        description=(
            "Re-probe every LM Studio HTTP endpoint in INTEGRATION_MAP §2 "
            "and detect surface drift."
        ),
        epilog=(
            "Exit 0: all probes pass (or connectivity failure in non-strict mode). "
            "Exit 1: confirmed endpoint-shape diff OR connectivity failure with --strict."
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat connectivity failures as fatal (exit 1). "
        "Use in deployment-readiness checks.",
    )
    parser.add_argument(
        "--output",
        metavar="FILE",
        help="Write diff markdown to FILE instead of (or in addition to) stdout.",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit machine-readable JSON to stdout instead of markdown.",
    )
    parser.add_argument(
        "--no-append",
        action="store_true",
        help=f"Do not append to {_DRIFT_LOG.relative_to(_REPO_ROOT)} (default: append).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s reprobe_lm_studio_surface: %(message)s",
        stream=sys.stderr,
    )

    base_url, api_key = _get_config()
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    results, connectivity_ok = run_all_probes(base_url, api_key, strict=args.strict)

    if not connectivity_ok:
        logging.warning(
            "LM Studio at %s is unreachable. "
            "All probes returned connectivity failures.",
            base_url,
        )
        if args.strict:
            logging.warning("--strict mode: treating connectivity failure as fatal.")

    if args.as_json:
        output = _format_json(results, base_url, timestamp, connectivity_ok, args.strict)
    else:
        output = _format_markdown(results, base_url, timestamp, connectivity_ok, args.strict)

    # Always print to stdout.
    print(output)

    # Append to drift log (unless suppressed).
    if not args.no_append and not args.as_json:
        try:
            _DRIFT_LOG.parent.mkdir(parents=True, exist_ok=True)
            with _DRIFT_LOG.open("a", encoding="utf-8") as f:
                f.write(output)
                f.write("\n---\n\n")
        except OSError as exc:
            logging.warning("could not append to drift log %s: %s", _DRIFT_LOG, exc)

    # Write to --output file if specified.
    if args.output:
        try:
            Path(args.output).write_text(output, encoding="utf-8")
        except OSError as exc:
            logging.warning("could not write output file %s: %s", args.output, exc)

    code = _exit_code(results, connectivity_ok, args.strict)
    return code


if __name__ == "__main__":
    sys.exit(main())
