#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Lightweight secrets scanner for lm-chat.

Scans tracked (non-.gitignored) Python and JS/TS source files for common
secret token patterns.  Falls back to scanning the full ``src/`` and ``web/src/``
trees when git is not available.

Exits 0 if no secrets found; exits 1 on any match (suitable for CI).

Usage:
    uv run python tools/secrets_scan.py [--scan-path PATH ...]

If gitleaks is installed it is preferred (more comprehensive); this script
runs automatically when gitleaks is not available.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# Common secret token patterns.  False-positives are acceptable for a CI gate;
# operators investigate and suppress specific instances by annotating them with
# a ``# secrets-scan-allow`` comment if the value is a placeholder/test fixture.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("AWS secret key", re.compile(r"(?i)aws.{0,20}secret.{0,20}['\"][A-Za-z0-9/+]{40}['\"]")),
    ("GitHub token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,255}")),
    ("Generic API key", re.compile(r"(?i)api.?key\s*[=:]\s*['\"][A-Za-z0-9_\-]{20,}['\"]")),
    # Generic secret: assignment, not a function argument (keyword=value pattern excluded).
    ("Generic secret", re.compile(r"(?i)(?:^|\s)secret\s*=\s*['\"][A-Za-z0-9_\-]{20,}['\"]")),
    # Hard-coded password: long values (≥32 chars) outside of function calls.
    # Short test fixture passwords (e.g. "correct-horse-battery") are excluded
    # because they're < 32 chars and use keyword-argument form (password=...).
    ("Hard-coded password", re.compile(r"(?i)(?:PASSWORD|PASSWD|PWD)\s*=\s*['\"][^'\"]{32,}['\"]")),
    ("Private key header", re.compile(r"-----BEGIN (?:RSA|EC|DSA|OPENSSH|PGP) PRIVATE KEY-----")),
    ("Bearer token", re.compile(r"(?i)bearer\s+[A-Za-z0-9\-._~+/]{20,}={0,2}")),
    ("Slack token", re.compile(r"xox[baprs]-[0-9A-Za-z\-]+")),
    ("Stripe key", re.compile(r"(?:sk|pk)_(?:live|test)_[A-Za-z0-9]{24,}")),
    ("Anthropic key", re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}")),
    ("OpenAI key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
]

# Files / directories to always skip.
_SKIP_DIRS = {
    ".venv", "venv", "node_modules", ".git", "dist", "build",
    "__pycache__", ".ruff_cache", ".pytest_cache", "htmlcov", "coverage",
}
_SKIP_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff", ".woff2",
    ".ttf", ".otf", ".eot", ".mp4", ".mp3", ".pdf", ".zip", ".tar", ".gz",
    ".db", ".db-shm", ".db-wal", ".lock", ".map",
}
# Annotation that suppresses a line from the scan.
_NOQA_COMMENT = "# secrets-scan-allow"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _collect_files_from_git(repo_root: Path) -> list[Path]:
    """Return tracked (non-ignored) text files via git ls-files."""
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    return [repo_root / line for line in result.stdout.splitlines() if line.strip()]


def _collect_files_from_paths(scan_paths: list[Path]) -> list[Path]:
    """Recursively collect scannable files from *scan_paths*."""
    found: list[Path] = []
    for root_path in scan_paths:
        if root_path.is_file():
            found.append(root_path)
            continue
        for p in root_path.rglob("*"):
            if p.is_file():
                found.append(p)
    return found


def _should_skip(path: Path) -> bool:
    parts = set(path.parts)
    if parts & _SKIP_DIRS:
        return True
    if path.suffix.lower() in _SKIP_EXTENSIONS:
        return True
    return False


def _scan_file(path: Path) -> list[tuple[int, str, str]]:
    """Scan *path* for secret patterns.

    Returns list of (line_number, pattern_name, line_text) matches.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []

    findings: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if _NOQA_COMMENT in line:
            continue
        for name, pattern in _PATTERNS:
            if pattern.search(line):
                findings.append((lineno, name, line.rstrip()))
    return findings


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _emit_junit(findings_map: dict[str, list[tuple[int, str, str]]], output: Path) -> None:
    """Write a JUnit XML report to *output* for working-tree secrets scan."""
    from xml.etree import ElementTree as ET

    suite_name = "working-tree-secrets"
    all_findings: list[tuple[str, int, str, str]] = []
    for rel_path, hits in findings_map.items():
        for lineno, name, line_text in hits:
            all_findings.append((rel_path, lineno, name, line_text))

    fail_count = len(all_findings)
    test_count = max(fail_count, 1)

    suite = ET.Element("testsuite", {
        "name": suite_name,
        "tests": str(test_count),
        "failures": str(fail_count),
        "errors": "0",
        "skipped": "0",
    })

    if not all_findings:
        ET.SubElement(suite, "testcase", {
            "name": "no-findings",
            "classname": "secrets-scan",
        })
    else:
        for rel_path, lineno, name, line_text in all_findings:
            tc = ET.SubElement(suite, "testcase", {
                "name": f"{rel_path}:{lineno}",
                "classname": f"secrets-scan.{name.lower().replace(' ', '-')}",
            })
            fail = ET.SubElement(tc, "failure", {
                "message": f"[{name}] potential secret at {rel_path}:{lineno}",
                "type": "working-tree-secret",
            })
            fail.text = line_text[:240]

    root = ET.Element("testsuites", {
        "name": suite_name,
        "tests": suite.get("tests", "0"),
        "failures": suite.get("failures", "0"),
        "errors": "0",
    })
    root.append(suite)
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    output.parent.mkdir(parents=True, exist_ok=True)
    tree.write(output, encoding="utf-8", xml_declaration=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan for secret token patterns.")
    parser.add_argument(
        "--scan-path",
        action="append",
        dest="scan_paths",
        default=[],
        help="Paths to scan (default: use git ls-files from repo root).",
    )
    parser.add_argument(
        "--junit-output",
        type=Path,
        default=None,
        dest="junit_output",
        help="If set, write JUnit XML to this path (testsuite: working-tree-secrets).",
    )
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[1]

    if args.scan_paths:
        files = _collect_files_from_paths([Path(p) for p in args.scan_paths])
    else:
        files = _collect_files_from_git(repo_root)
        if not files:
            # git not available or empty result — fall back to src trees.
            files = _collect_files_from_paths([
                repo_root / "src",
                repo_root / "web" / "src",
                repo_root / "tools",
                repo_root / "tests",
            ])

    total_findings = 0
    findings_map: dict[str, list[tuple[int, str, str]]] = {}
    for file_path in files:
        if _should_skip(file_path):
            continue
        file_findings = _scan_file(file_path)
        for lineno, name, line_text in file_findings:
            try:
                rel = file_path.relative_to(repo_root) if file_path.is_absolute() else file_path
            except ValueError:
                rel = file_path
            rel_str = str(rel)
            print(f"SECRETS_SCAN: {rel_str}:{lineno}: [{name}] {line_text[:120]}")
            total_findings += 1
            if rel_str not in findings_map:
                findings_map[rel_str] = []
            findings_map[rel_str].append((lineno, name, line_text))

    if args.junit_output is not None:
        _emit_junit(findings_map, args.junit_output)

    if total_findings > 0:
        print(
            f"\nsecrets_scan: {total_findings} potential secret(s) found. "
            "Review and suppress false positives with '# secrets-scan-allow'."
        )
        return 1

    print(f"secrets_scan: OK — no secret patterns found ({len(files)} files scanned).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
