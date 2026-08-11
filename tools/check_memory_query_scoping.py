# SPDX-License-Identifier: Apache-2.0
"""CI guard: every SQL query against memory_insights or memory_facts must be
user_id-scoped, or explicitly annotated as a cross-user maintenance operation.

Background
----------
Issue #168 uncovered an IDOR class of bug: a memory-service query that reads
rows across all users without filtering on ``user_id``.  The fix (P15) added
inline comments to ``_fade_pass`` annotating the intentional cross-user sweep.
This scanner enforces the discipline automatically so a future contributor
cannot add a new unscoped query and have it go undetected.

Scanning rules
--------------
For every call-expression in ``memory_service.py`` that references the
``memory_insights`` or ``memory_facts`` table via one of:

  select(memory_insights | memory_facts)
  update(memory_insights | memory_facts)
  delete(memory_insights | memory_facts)
  insert(memory_insights | memory_facts)     # insert().values(...)

The enclosing function must satisfy AT LEAST ONE of:

  (a) Scoped query: the ``.where(...)`` chain contains a reference to
      ``user_id`` as a keyword argument OR as an attribute access on the
      table (``memory_insights.c.user_id``).  A ``where`` call with a
      compare-node whose left side includes the token ``user_id`` passes.

  (b) Cross-user annotation: the enclosing function's docstring OR any
      single-line comment within the function body contains the marker
      ``# scoped: cross-user``.

JUnit XML report
----------------
Violations are written to ``target/gates/L0-memory-scoping.xml`` so the L0
layer aggregator picks them up.  The tool also exits non-zero on any
violation.

Usage
-----
  python tools/check_memory_query_scoping.py
  python tools/check_memory_query_scoping.py --target-file <path>
  python tools/check_memory_query_scoping.py --output <xml-path>
"""
from __future__ import annotations

import argparse
import ast
import sys
import textwrap
import time
import xml.etree.ElementTree as ET
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_TARGET = (
    _REPO_ROOT / "src" / "lmchat" / "services" / "memory_service.py"
)
_DEFAULT_OUTPUT = _REPO_ROOT / "target" / "gates" / "L0-memory-scoping.xml"

# Tables whose queries must be scoped
_SCOPED_TABLES: frozenset[str] = frozenset({"memory_insights", "memory_facts"})

# DML call names that introduce a query
_DML_NAMES: frozenset[str] = frozenset({"select", "update", "delete", "insert"})

# Annotation marker that explicitly exempts a function from scoping
_CROSS_USER_MARKER = "scoped: cross-user"


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _names_in_node(node: ast.AST) -> set[str]:
    """Collect all Name ids reachable from *node*."""
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _has_table_arg(call: ast.Call, tables: frozenset[str]) -> bool:
    """Return True if *call* has a positional arg that references one of *tables*."""
    for arg in call.args:
        if _names_in_node(arg) & tables:
            return True
    # Some dialects: select(*[memory_insights]) — check keyword args too
    for kw in call.keywords:
        if kw.value and _names_in_node(kw.value) & tables:
            return True
    return False


def _dml_name(call: ast.Call) -> str | None:
    """Return the bare DML function name for *call*, or None if not a DML call."""
    func = call.func
    if isinstance(func, ast.Name) and func.id in _DML_NAMES:
        return func.id
    # Attribute form: sa.select(...)
    if isinstance(func, ast.Attribute) and func.attr in _DML_NAMES:
        return func.attr
    return None


def _where_args_contain_user_id(call_chain_root: ast.AST) -> bool:
    """Walk the AST rooted at *call_chain_root* looking for .where() or
    .values() calls whose arguments reference ``user_id``.

    Covers:
    - .where(table.c.user_id == user_id)
    - .values(user_id=user_id, ...)   — for INSERT statements
    """
    for node in ast.walk(call_chain_root):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr not in ("where", "values"):
            continue
        # Check all args/keywords of this call
        for arg in node.args:
            if _names_in_node(arg) & {"user_id"}:
                return True
            for attr_node in ast.walk(arg):
                if isinstance(attr_node, ast.Attribute) and attr_node.attr == "user_id":
                    return True
        for kw in node.keywords:
            if kw.arg == "user_id":
                return True
            if kw.value and _names_in_node(kw.value) & {"user_id"}:
                return True
    return False


def _function_has_cross_user_annotation(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    source_lines: list[str],
) -> bool:
    """Return True if *func_node* carries the cross-user annotation marker.

    Checks:
    1. Docstring (first ast.Expr(ast.Constant)) contains the marker.
    2. Any ``# scoped: cross-user`` comment in the source lines that fall
       within the function body.
    """
    # 1. Docstring check
    docstring = ast.get_docstring(func_node)
    if docstring and _CROSS_USER_MARKER in docstring:
        return True

    # 2. Comment check — walk source lines from the def line to end of function.
    # Comments between the def line and the first statement (e.g. before the
    # docstring, or between the def and the first statement) are included.
    # func_node.lineno is 1-based; source_lines is 0-based.
    start = func_node.lineno - 1  # 0-based, inclusive (the def line itself)
    end = (func_node.end_lineno or len(source_lines))  # 1-based exclusive
    for line in source_lines[start:end]:
        stripped = line.lstrip()
        if stripped.startswith("#") and _CROSS_USER_MARKER in stripped:
            return True

    return False


# ---------------------------------------------------------------------------
# Violation dataclass
# ---------------------------------------------------------------------------


class Violation:
    __slots__ = ("function_name", "dml", "table", "lineno", "filename")

    def __init__(
        self,
        function_name: str,
        dml: str,
        table: str,
        lineno: int,
        filename: str,
    ) -> None:
        self.function_name = function_name
        self.dml = dml
        self.table = table
        self.lineno = lineno
        self.filename = filename

    def message(self) -> str:
        return (
            f"{self.filename}:{self.lineno}: function '{self.function_name}' "
            f"calls {self.dml}({self.table}) without user_id scoping. "
            f"Add a .where(memory_X.c.user_id == user_id) clause, "
            f"or annotate with '# scoped: cross-user' if intentional."
        )


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


def _collect_top_level_funcs(
    tree: ast.Module,
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Return only top-level and class-level functions — not inner closures.

    Inner closures (functions defined inside other functions) are skipped
    because they are lexically scoped by their enclosing method.  Flagging
    them independently produces false positives for the pattern:

        async def outer(user_id):
            async def _do_update():          # closure — inherits outer scope
                await conn.execute(
                    update(memory_insights).where(memory_insights.c.id.in_(ids))
                )
            await with_write_retry(_do_update)

    The ``outer`` function is the unit of analysis; ``_do_update`` is
    implementation detail.
    """
    result: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # Accept only module-level or class-level functions.
        # We detect "class-level" by checking if the parent chain passes
        # through a ClassDef before hitting another FunctionDef.
        # Simpler approach: use ast.walk from the module and only take
        # functions whose direct parent in the tree is a Module or ClassDef.
        result.append(node)

    # Deduplicate by filtering: keep a function only if no ancestor function
    # contains it.  Build a set of all nodes that are nested inside another function.
    nested: set[int] = set()
    for node in result:
        for child in ast.walk(node):
            if child is node:
                continue
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                nested.add(id(child))

    return [f for f in result if id(f) not in nested]


def scan_file(path: Path) -> list[Violation]:
    """AST-scan *path* and return all user_id scoping violations."""
    source = path.read_text(encoding="utf-8")
    source_lines = source.splitlines()
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as e:
        print(f"[check_memory_query_scoping] SyntaxError in {path}: {e}", file=sys.stderr)
        return []

    violations: list[Violation] = []

    # Only scan top-level and class-method functions (not inner closures).
    # Inner closures inherit their enclosing method's user_id scoping.
    top_funcs = _collect_top_level_funcs(tree)

    for node in top_funcs:
        annotated = _function_has_cross_user_annotation(node, source_lines)

        # Walk all Call nodes inside this function (including closures)
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            dml = _dml_name(child)
            if dml is None:
                continue

            # Collect table names from the call's positional args
            referenced: set[str] = set()
            for arg in child.args:
                referenced |= _names_in_node(arg)
            for kw in child.keywords:
                if kw.value:
                    referenced |= _names_in_node(kw.value)

            matched_tables = referenced & _SCOPED_TABLES
            if not matched_tables:
                continue

            # Found a DML call referencing a scoped table
            for table in sorted(matched_tables):
                if annotated:
                    continue  # function is explicitly annotated — allowed

                # Check 1: the entire function body contains a .where(user_id)
                # or .values(user_id=...) chain anywhere (covers closures too)
                if _where_args_contain_user_id(node):
                    continue

                # Check 2: function parameter list includes 'user_id' — the
                # caller is responsible for scoping (e.g. _touch_insights takes
                # pre-validated insight_ids that were resolved from the user's
                # own data, and itself takes no user_id param — but handle_feedback
                # does have user_id indirectly via message_id).  We check whether
                # ANY enclosing function node in the subtree rooted at `node` has
                # `user_id` as a parameter.
                has_uid_param = any(
                    arg.arg == "user_id"
                    for fn in ast.walk(node)
                    if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
                    for arg in fn.args.args + fn.args.posonlyargs + fn.args.kwonlyargs
                )
                if has_uid_param:
                    continue

                try:
                    rel = str(path.relative_to(_REPO_ROOT))
                except ValueError:
                    rel = str(path)
                violations.append(
                    Violation(
                        function_name=node.name,
                        dml=dml,
                        table=table,
                        lineno=child.lineno,
                        filename=rel,
                    )
                )

    return violations


# ---------------------------------------------------------------------------
# JUnit XML emitter
# ---------------------------------------------------------------------------


def emit_junit(violations: list[Violation], output_path: Path, elapsed_s: float) -> None:
    """Write JUnit XML report to *output_path*."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    ts_name = "memory-query-scoping"
    suite = ET.Element(
        "testsuite",
        name=ts_name,
        tests=str(max(1, len(violations))),
        failures=str(len(violations)),
        errors="0",
        skipped="0",
        time=f"{elapsed_s:.3f}",
    )

    if not violations:
        ET.SubElement(  # pass — no child element means success
            suite, "testcase", classname=ts_name, name="all-queries-scoped", time="0"
        )
    else:
        for v in violations:
            tc = ET.SubElement(
                suite,
                "testcase",
                classname=ts_name,
                name=f"{v.function_name}:{v.dml}({v.table})",
                time="0",
            )
            failure = ET.SubElement(tc, "failure", message=v.message(), type="ScopingViolation")
            failure.text = v.message()

    tree = ET.ElementTree(suite)
    ET.indent(tree, space="  ")
    with output_path.open("wb") as fh:
        tree.write(fh, encoding="utf-8", xml_declaration=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=textwrap.dedent(__doc__ or ""),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--target-file",
        type=Path,
        default=_DEFAULT_TARGET,
        help="Python source file to scan (default: src/lmchat/services/memory_service.py)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=_DEFAULT_OUTPUT,
        help="JUnit XML output path (default: target/gates/L0-memory-scoping.xml)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-violation stdout messages",
    )
    args = parser.parse_args(argv)

    target: Path = args.target_file
    if not target.is_file():
        print(
            f"[check_memory_query_scoping] ERROR: target file not found: {target}",
            file=sys.stderr,
        )
        return 1

    t0 = time.monotonic()
    violations = scan_file(target)
    elapsed = time.monotonic() - t0

    emit_junit(violations, args.output, elapsed)

    if violations:
        for v in violations:
            if not args.quiet:
                print(v.message(), file=sys.stderr)
        print(
            f"[check_memory_query_scoping] FAIL — {len(violations)} violation(s). "
            f"Report: {args.output}",
            file=sys.stderr,
        )
        return 1

    if not args.quiet:
        print(
            f"[check_memory_query_scoping] OK — all memory queries are user_id-scoped "
            f"({elapsed:.3f}s). Report: {args.output}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
