# SPDX-License-Identifier: Apache-2.0
"""Unit tests for tools/check_memory_query_scoping.py.

Coverage targets:
  - Scoped select passes
  - Scoped update passes
  - Scoped delete passes
  - Unscoped select on memory_insights FAILS
  - Unscoped select on memory_facts FAILS
  - Unscoped update FAILS
  - Unscoped delete FAILS
  - Cross-user annotation in docstring exempts the function
  - Cross-user annotation as inline comment exempts the function
  - Non-scoped table (e.g. chats) is not flagged
  - Annotation marker is case-sensitive (wrong marker → still fails)
  - Multiple violations in one file all reported
  - Scanner against the real memory_service.py returns clean
  - Empty file → no violations
  - Syntax error file → graceful return (no violations, no crash)
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.check_memory_query_scoping import Violation, scan_file

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _write_py(tmp_path: Path, name: str, source: str) -> Path:
    p = tmp_path / name
    p.write_text(textwrap.dedent(source), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Passing cases
# ---------------------------------------------------------------------------


def test_scoped_select_passes(tmp_path: Path) -> None:
    """select(memory_insights).where(memory_insights.c.user_id == uid) → clean."""
    src = _write_py(
        tmp_path,
        "svc.py",
        """\
        from sqlalchemy import select
        memory_insights = None  # mock

        async def recall(user_id):
            result = await conn.execute(
                select(memory_insights).where(
                    memory_insights.c.user_id == user_id
                )
            )
        """,
    )
    assert scan_file(src) == []


def test_scoped_update_passes(tmp_path: Path) -> None:
    """update(memory_insights).where(...user_id...) → clean."""
    src = _write_py(
        tmp_path,
        "svc.py",
        """\
        from sqlalchemy import update
        memory_insights = None

        async def mark_used(user_id, insight_id):
            await conn.execute(
                update(memory_insights)
                .where(memory_insights.c.user_id == user_id)
                .values(use_count=1)
            )
        """,
    )
    assert scan_file(src) == []


def test_scoped_delete_passes(tmp_path: Path) -> None:
    """delete(memory_insights).where(user_id=...) → clean."""
    src = _write_py(
        tmp_path,
        "svc.py",
        """\
        from sqlalchemy import delete
        memory_insights = None

        async def remove_insight(user_id, iid):
            await conn.execute(
                delete(memory_insights).where(
                    memory_insights.c.user_id == user_id,
                    memory_insights.c.id == iid,
                )
            )
        """,
    )
    assert scan_file(src) == []


def test_memory_facts_scoped_select_passes(tmp_path: Path) -> None:
    """select(memory_facts) with user_id in where → clean."""
    src = _write_py(
        tmp_path,
        "svc.py",
        """\
        from sqlalchemy import select
        memory_facts = None

        async def get_facts(user_id):
            return await conn.execute(
                select(memory_facts).where(memory_facts.c.user_id == user_id)
            )
        """,
    )
    assert scan_file(src) == []


def test_cross_user_docstring_annotation_passes(tmp_path: Path) -> None:
    """Function with '# scoped: cross-user' in docstring → exempt."""
    src = _write_py(
        tmp_path,
        "svc.py",
        """\
        from sqlalchemy import select
        memory_insights = None

        async def _fade_pass():
            \"\"\"Maintenance sweep.

            # scoped: cross-user — background fade only.
            \"\"\"
            result = await conn.execute(
                select(memory_insights).where(memory_insights.c.state == 'active')
            )
        """,
    )
    assert scan_file(src) == []


def test_cross_user_inline_comment_passes(tmp_path: Path) -> None:
    """Function body has '# scoped: cross-user' comment → exempt."""
    src = _write_py(
        tmp_path,
        "svc.py",
        """\
        from sqlalchemy import select
        memory_insights = None

        async def _admin_sweep():
            # scoped: cross-user — maintenance only
            result = await conn.execute(
                select(memory_insights).where(memory_insights.c.state == 'active')
            )
        """,
    )
    assert scan_file(src) == []


def test_non_scoped_table_not_flagged(tmp_path: Path) -> None:
    """select(chats) without user_id is not a memory-scoping violation."""
    src = _write_py(
        tmp_path,
        "svc.py",
        """\
        from sqlalchemy import select
        chats = None

        async def get_chats():
            return await conn.execute(select(chats))
        """,
    )
    assert scan_file(src) == []


# ---------------------------------------------------------------------------
# Failing cases
# ---------------------------------------------------------------------------


def test_unscoped_select_fails(tmp_path: Path) -> None:
    """select(memory_insights) with no where(user_id) → violation."""
    src = _write_py(
        tmp_path,
        "svc.py",
        """\
        from sqlalchemy import select
        memory_insights = None

        async def bad_recall():
            result = await conn.execute(
                select(memory_insights).where(memory_insights.c.state == 'active')
            )
        """,
    )
    violations = scan_file(src)
    assert len(violations) == 1
    v = violations[0]
    assert isinstance(v, Violation)
    assert v.function_name == "bad_recall"
    assert v.dml == "select"
    assert v.table == "memory_insights"
    assert "user_id" in v.message()


def test_unscoped_select_memory_facts_fails(tmp_path: Path) -> None:
    """select(memory_facts) with no where(user_id) → violation."""
    src = _write_py(
        tmp_path,
        "svc.py",
        """\
        from sqlalchemy import select
        memory_facts = None

        async def bad_facts():
            return await conn.execute(select(memory_facts))
        """,
    )
    violations = scan_file(src)
    assert len(violations) == 1
    assert violations[0].table == "memory_facts"


def test_unscoped_update_fails(tmp_path: Path) -> None:
    """update(memory_insights) with no where(user_id) → violation."""
    src = _write_py(
        tmp_path,
        "svc.py",
        """\
        from sqlalchemy import update
        memory_insights = None

        async def bulk_reset():
            await conn.execute(
                update(memory_insights).values(state='active')
            )
        """,
    )
    violations = scan_file(src)
    assert len(violations) == 1
    assert violations[0].dml == "update"


def test_unscoped_delete_fails(tmp_path: Path) -> None:
    """delete(memory_insights) with no user_id filter → violation."""
    src = _write_py(
        tmp_path,
        "svc.py",
        """\
        from sqlalchemy import delete
        memory_insights = None

        async def wipe_all():
            await conn.execute(delete(memory_insights))
        """,
    )
    violations = scan_file(src)
    assert len(violations) == 1
    assert violations[0].dml == "delete"


def test_wrong_annotation_marker_still_fails(tmp_path: Path) -> None:
    """A comment 'cross_user' (underscore) is NOT the magic marker → violation."""
    src = _write_py(
        tmp_path,
        "svc.py",
        """\
        from sqlalchemy import select
        memory_insights = None

        async def bad():
            # cross_user sweep
            return await conn.execute(select(memory_insights))
        """,
    )
    violations = scan_file(src)
    assert len(violations) == 1


def test_multiple_violations_all_reported(tmp_path: Path) -> None:
    """Two unscoped functions → two violations."""
    src = _write_py(
        tmp_path,
        "svc.py",
        """\
        from sqlalchemy import select, delete
        memory_insights = None

        async def bad_one():
            return await conn.execute(select(memory_insights))

        async def bad_two():
            await conn.execute(delete(memory_insights))
        """,
    )
    violations = scan_file(src)
    assert len(violations) == 2
    names = {v.function_name for v in violations}
    assert names == {"bad_one", "bad_two"}


# ---------------------------------------------------------------------------
# Live tree check
# ---------------------------------------------------------------------------


def test_real_memory_service_is_clean() -> None:
    """The real src/lmchat/services/memory_service.py must pass the scanner."""
    target = _REPO_ROOT / "src" / "lmchat" / "services" / "memory_service.py"
    assert target.is_file(), "memory_service.py not found — adjust repo root?"
    violations = scan_file(target)
    messages = [v.message() for v in violations]
    assert violations == [], "Unexpected violations:\n" + "\n".join(messages)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_file_no_violations(tmp_path: Path) -> None:
    """Empty Python file → no violations, no crash."""
    src = _write_py(tmp_path, "empty.py", "")
    assert scan_file(src) == []


def test_syntax_error_no_crash(tmp_path: Path) -> None:
    """Syntax error in scanned file → graceful empty result (no exception)."""
    src = tmp_path / "bad.py"
    src.write_text("def oops(\n", encoding="utf-8")
    result = scan_file(src)
    assert result == []
