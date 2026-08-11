# SPDX-License-Identifier: Apache-2.0
"""CI guard: lm-chat must never read or write under ~/.lmstudio/.

lm-chat talks to LM Studio over HTTP only. Any direct filesystem access
under ~/.lmstudio/ (or any LM-Studio-owned path) is an architectural
violation — see docs/INTEGRATION_MAP.md §1 and
docs/release/INCIDENT_REPORT_2026_05_21.md.

Two passes on Python source (src/, tests/) — default:

  Pass 1 — grep equivalent: scan .py files for the string literals
  ``.lmstudio``, ``LM_STUDIO_HOME``, and ``LMSTUDIO_HOME``.  Comment
  lines are whitelisted.  Lines that are pure string-constant
  expressions (docstrings) are whitelisted by looking up the AST
  docstring set for each file.

  Pass 2 — Python AST scan: walk the AST of every .py file, flagging:
    - pathlib constructions: Path.home() / ".lmstudio" ...
    - os.path.join(..., ".lmstudio") patterns
    - string literals containing "~/.lmstudio" or ".lmstudio/"
      (excluding docstring nodes)

One additional pass on docs/ — opt-in with --docs (P11d):

  Pass 3 — docs scan: scan .md files under docs/ for references to
  removed surfaces (``plugins_service``, ``mcp_service``,
  ``PluginsService``, ``McpService``, ``routes/plugins.py``,
  ``routes/mcp.py``, ``/api/plugins``, ``/api/mcp/``).  Lines are OK
  if any of these annotation markers appear on the same line:
  ``REMOVED``, ``RESOLVED``, ``resolved-by-removal``, ``N/A``,
  ``historical``, ``superseded``, strikethrough (``~~``), or a link
  to INCIDENT_REPORT_2026_05_21.

  Path-whitelist (entire file skipped): docs/phases/**, docs/adr/**,
  docs/panels/**, docs/release/INCIDENT_REPORT_*, CHANGELOG.md,
  docs/release/SECURITY_ADVISORY.md.  These are historical records
  by definition.

Whitelist:
  - Comment-only lines (# ...).
  - Docstrings (first ast.Expr(ast.Constant) in module/class/function).
  - The file tools/check_no_lmstudio_fs.py itself (this file).

Exit codes:
  0 — no violations found.
  1 — one or more violations found; details written to stderr.

Usage:
  python tools/check_no_lmstudio_fs.py
  python tools/check_no_lmstudio_fs.py --paths src/ tests/
  python tools/check_no_lmstudio_fs.py --docs                # also sweep docs/
  python tools/check_no_lmstudio_fs.py --docs-only           # docs/ only
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SELF = Path(__file__).resolve()

# String patterns that flag a filesystem path violation.
# Matches ".lmstudio" as a path component (preceded by "/" or "~" or as a
# quoted standalone string like ".lmstudio/" or "/.lmstudio").
# Does NOT match Python module names like "lmchat.lmstudio" or "lmstudio_adapter".
#
# The pattern requires one of:
#   - the literal appears inside a quoted string as a path segment:
#       "~/.lmstudio", "/.lmstudio", ".lmstudio/" (as filesystem path)
#   - OR the env var names LM_STUDIO_HOME / LMSTUDIO_HOME appear literally
#     outside of a comment
_GREP_PATTERN = re.compile(
    r"""(["'`]~?/\.lmstudio|["'`]\.lmstudio/|LM_STUDIO_HOME|LMSTUDIO_HOME)"""
)

# Comment-only line pattern (leading whitespace then #).
_COMMENT_LINE = re.compile(r"^\s*#")

# Per-line opt-out marker for tests / fixtures that intentionally embed
# the forbidden literal as INPUT to a negative-assertion (e.g. asserting
# that the URL validator REJECTS the literal). Must be on the same line
# as the literal, in a comment, with a non-empty reason:
#
#     "~/.lmstudio/config.json",  # allow-lmstudio-literal: validator-rejection-test
#
# The reason is logged in the audit trail to keep this honest.
_ALLOW_MARKER = re.compile(r"#\s*allow-lmstudio-literal:\s*\S")


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


class Violation(NamedTuple):
    path: Path
    line: int
    col: int
    reason: str
    text: str


# ---------------------------------------------------------------------------
# Docstring line collection (AST-based, per file)
# ---------------------------------------------------------------------------


def _collect_docstring_lines(tree: ast.AST) -> set[int]:
    """Return the set of line numbers that are docstring-only lines.

    Looks for the first statement in each module/class/function body.
    If it is an ``ast.Expr`` wrapping an ``ast.Constant``, all lines
    from start to end of that constant are added.
    """
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                start = getattr(body[0], "lineno", None)
                end = getattr(body[0], "end_lineno", start)
                if start is not None and end is not None:
                    for ln in range(start, end + 1):
                        lines.add(ln)
    return lines


# ---------------------------------------------------------------------------
# Pass 1 — grep scan (raw text, comment-aware)
# ---------------------------------------------------------------------------


def grep_scan(paths: list[Path]) -> list[Violation]:
    """Scan .py files for forbidden string literals via regex."""
    violations: list[Violation] = []
    for py_file in _iter_py_files(paths):
        if py_file.resolve() == _SELF:
            continue
        try:
            source = py_file.read_text(encoding="utf-8")
        except OSError:
            continue
        # Build docstring line set for this file.
        try:
            tree = ast.parse(source, filename=str(py_file))
            docstring_lines = _collect_docstring_lines(tree)
        except SyntaxError:
            docstring_lines = set()

        for lineno, line in enumerate(source.splitlines(), start=1):
            if _COMMENT_LINE.match(line):
                continue
            if lineno in docstring_lines:
                continue
            if _ALLOW_MARKER.search(line):
                continue
            m = _GREP_PATTERN.search(line)
            if m:
                violations.append(
                    Violation(
                        path=py_file,
                        line=lineno,
                        col=m.start() + 1,
                        reason=f"forbidden literal {m.group()!r}",
                        text=line.strip(),
                    )
                )
    return violations


# ---------------------------------------------------------------------------
# Pass 2 — AST scan
# ---------------------------------------------------------------------------


class _LmstudioFsVisitor(ast.NodeVisitor):
    """AST visitor that flags ~/.lmstudio/ filesystem construction patterns."""

    def __init__(
        self,
        source_path: Path,
        docstring_node_ids: set[int],
        allow_lines: set[int],
    ) -> None:
        self.source_path = source_path
        self.violations: list[Violation] = []
        self._docstring_node_ids = docstring_node_ids
        self._allow_lines = allow_lines

    def _record(self, node: ast.AST, reason: str, text: str) -> None:
        # Honour the per-line `# allow-lmstudio-literal:` opt-out so
        # negative-assertion tests can embed the forbidden literal as
        # input under audit.
        line = getattr(node, "lineno", 0)
        if line in self._allow_lines:
            return
        self.violations.append(
            Violation(
                path=self.source_path,
                line=line,
                col=getattr(node, "col_offset", 0) + 1,
                reason=reason,
                text=text,
            )
        )

    @staticmethod
    def _is_lmstudio_str(value: object) -> bool:
        """Return True only for filesystem path strings, not Python module names.

        A violation is: a string that contains ".lmstudio" as a PATH component,
        meaning it is preceded by "/" or "~" or is ".lmstudio/" (directory prefix).
        Strings like "lmchat.lmstudio.native" (Python module dotted path) are
        NOT violations — the dot there separates package names, not path segments.
        """
        if not isinstance(value, str):
            return False
        # Filesystem path indicators: "~/.lmstudio", "/.lmstudio", ".lmstudio/"
        return (
            "~/.lmstudio" in value
            or "/.lmstudio" in value
            or value.startswith(".lmstudio/")
            or value == ".lmstudio"
        )

    def visit_Constant(self, node: ast.Constant) -> None:  # noqa: N802
        if id(node) not in self._docstring_node_ids and self._is_lmstudio_str(node.value):
            self._record(node, f"string literal {node.value!r}", str(node.value))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        # Detect: os.path.join(..., ".lmstudio") patterns.
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "join"
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "path"
        ):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and self._is_lmstudio_str(arg.value):
                    self._record(node, "os.path.join with .lmstudio argument", str(arg.value))
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:  # noqa: N802
        # Detect: Path.home() / ".lmstudio" and chained variants.
        if (
            isinstance(node.op, ast.Div)
            and isinstance(node.right, ast.Constant)
            and self._is_lmstudio_str(node.right.value)
            and _has_path_home(node.left)
        ):
            text = ast.unparse(node) if hasattr(ast, "unparse") else ".lmstudio path construction"
            self._record(node, "Path.home() / '.lmstudio' construction", text)
        self.generic_visit(node)


def _has_path_home(node: ast.AST) -> bool:
    """Return True if *node* tree contains a Path.home() call."""
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr == "home"
        ):
            return True
    return False


def _collect_docstring_node_ids(tree: ast.AST) -> set[int]:
    """Return id() of every docstring Constant node."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def ast_scan(paths: list[Path]) -> list[Violation]:
    """AST-scan .py files for forbidden filesystem construction patterns."""
    violations: list[Violation] = []
    for py_file in _iter_py_files(paths):
        if py_file.resolve() == _SELF:
            continue
        try:
            source = py_file.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError:
            continue
        docstring_ids = _collect_docstring_node_ids(tree)
        allow_lines = {
            lineno
            for lineno, line in enumerate(source.splitlines(), start=1)
            if _ALLOW_MARKER.search(line)
        }
        visitor = _LmstudioFsVisitor(py_file, docstring_ids, allow_lines)
        visitor.visit(tree)
        violations.extend(visitor.violations)
    return violations


# ---------------------------------------------------------------------------
# Pass 3 — docs scan (markdown, removed-surface references)
# ---------------------------------------------------------------------------


_DOCS_FORBIDDEN = re.compile(
    r"(plugins_service|mcp_service|PluginsService|McpService|"
    r"routes/plugins\.py|routes/mcp\.py|/api/plugins|/api/mcp/)"
)

_DOCS_ANNOTATION_OK = re.compile(
    r"(removed|resolved|deleted|\bN/A\b|historical|superseded|no longer exist|"
    r"~~|INCIDENT_REPORT_2026_05_21|P11a|REMOVED 2026-05-21|resolved-by-removal)",
    re.IGNORECASE,
)

_DOCS_PATH_WHITELIST_PREFIXES = (
    "docs/phases/",
    "docs/adr/",
    "docs/panels/",
    "docs/audit/",
)

_DOCS_PATH_WHITELIST_FILES = (
    "CHANGELOG.md",
    "docs/release/SECURITY_ADVISORY.md",
)


def _is_docs_path_whitelisted(rel: str) -> bool:
    if rel.startswith("docs/release/INCIDENT_REPORT_"):
        return True
    if rel in _DOCS_PATH_WHITELIST_FILES:
        return True
    for prefix in _DOCS_PATH_WHITELIST_PREFIXES:
        if rel.startswith(prefix):
            return True
    return False


def _scan_md_for_unannotated_refs(
    path: Path,
    source: str,
    window: int = 3,
) -> list[Violation]:
    """Scan one markdown file for unannotated removed-surface references.

    A reference is OK if the annotation marker appears anywhere in a
    sliding window of ``window`` lines BEFORE or AFTER the line that
    contains the reference. This tolerates multi-line bullets where the
    annotation sits on an adjacent line (e.g., the bullet's title line
    above or a continuation line below).
    """
    violations: list[Violation] = []
    lines = source.splitlines()
    for idx, line in enumerate(lines):
        m = _DOCS_FORBIDDEN.search(line)
        if not m:
            continue
        start = max(0, idx - window)
        end = min(len(lines), idx + window + 1)
        context = "\n".join(lines[start:end])
        if _DOCS_ANNOTATION_OK.search(context):
            continue
        violations.append(
            Violation(
                path=path,
                line=idx + 1,
                col=m.start() + 1,
                reason=f"unannotated reference to removed surface {m.group()!r}",
                text=line.strip(),
            )
        )
    return violations


def docs_scan(repo_root: Path, docs_dir: Path) -> list[Violation]:
    """Scan markdown files under docs/ for unannotated removed-surface refs."""
    violations: list[Violation] = []
    if docs_dir.is_dir():
        for md_file in sorted(docs_dir.rglob("*.md")):
            try:
                rel = str(md_file.relative_to(repo_root))
            except ValueError:
                rel = str(md_file)
            if _is_docs_path_whitelisted(rel):
                continue
            try:
                source = md_file.read_text(encoding="utf-8")
            except OSError:
                continue
            violations.extend(_scan_md_for_unannotated_refs(md_file, source))
    # Also check README.md and AGENTS.md at repo root.
    for top in ("README.md", "AGENTS.md"):
        top_path = repo_root / top
        if not top_path.is_file():
            continue
        try:
            source = top_path.read_text(encoding="utf-8")
        except OSError:
            continue
        violations.extend(_scan_md_for_unannotated_refs(top_path, source))
    return violations


# ---------------------------------------------------------------------------
# File iteration
# ---------------------------------------------------------------------------


def _iter_py_files(paths: list[Path]) -> list[Path]:
    result: list[Path] = []
    for base in paths:
        if base.is_file() and base.suffix == ".py":
            result.append(base)
        elif base.is_dir():
            result.extend(sorted(base.rglob("*.py")))
    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="CI guard: no ~/.lmstudio/ filesystem access in src/ or tests/."
    )
    parser.add_argument(
        "--paths",
        nargs="*",
        default=["src/", "tests/"],
        metavar="PATH",
        help="Directories or files to scan (default: src/ tests/)",
    )
    parser.add_argument(
        "--docs",
        action="store_true",
        help=(
            "Also sweep docs/ + README.md + AGENTS.md for unannotated "
            "references to removed surfaces (P11d)."
        ),
    )
    parser.add_argument(
        "--docs-only",
        action="store_true",
        help="Only sweep docs/ + README.md + AGENTS.md (skip the src/, tests/ Python passes).",
    )
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[1]
    scan_paths = [repo_root / p for p in args.paths]

    all_violations: list[Violation] = []
    if not args.docs_only:
        all_violations.extend(grep_scan(scan_paths))
        all_violations.extend(ast_scan(scan_paths))
    if args.docs or args.docs_only:
        all_violations.extend(docs_scan(repo_root, repo_root / "docs"))

    # Deduplicate by (path, line, col).
    seen: set[tuple[Path, int, int]] = set()
    unique: list[Violation] = []
    for v in all_violations:
        key = (v.path, v.line, v.col)
        if key not in seen:
            seen.add(key)
            unique.append(v)

    if not unique:
        return 0

    print(
        f"check_no_lmstudio_fs: {len(unique)} violation(s) found\n",
        file=sys.stderr,
    )
    for v in sorted(unique, key=lambda x: (str(x.path), x.line, x.col)):
        try:
            rel = v.path.relative_to(repo_root)
        except ValueError:
            rel = v.path
        print(f"  {rel}:{v.line}:{v.col}: {v.reason}", file=sys.stderr)
        print(f"    {v.text}", file=sys.stderr)
    print(
        "\nlm-chat talks to LM Studio over HTTP only. "
        "No filesystem access under ~/.lmstudio/ is permitted.\n"
        "See docs/phases/P11/PLAN.md §1 for the architectural rule.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
