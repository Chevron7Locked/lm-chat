# SPDX-License-Identifier: Apache-2.0
"""Unit tests for project_scope_clause."""
from __future__ import annotations

from sqlalchemy.sql.elements import BinaryExpression, Null

from lmchat.db.schema import chats
from lmchat.db.scope import project_scope_clause


def test_project_id_set_returns_eq_clause() -> None:
    """project_id=42 → chats.project_id == 42 clause."""
    clause = project_scope_clause(
        chats.c.project_id, project_id=42, unscoped=False
    )
    assert isinstance(clause, BinaryExpression)
    assert str(clause) == "chats.project_id = :project_id_1"


def test_unscoped_true_no_project_returns_is_null() -> None:
    """unscoped=True + project_id=None → chats.project_id IS NULL."""
    clause = project_scope_clause(
        chats.c.project_id, project_id=None, unscoped=True
    )
    assert clause is not None
    assert "IS NULL" in str(clause).upper()
    assert "project_id" in str(clause)


def test_both_unset_returns_none() -> None:
    """project_id=None + unscoped=False (default) → no clause."""
    assert (
        project_scope_clause(
            chats.c.project_id, project_id=None, unscoped=False
        )
        is None
    )


def test_project_id_takes_precedence_over_unscoped() -> None:
    """When project_id is set, unscoped is ignored — exact match wins."""
    clause = project_scope_clause(
        chats.c.project_id, project_id=99, unscoped=True
    )
    assert isinstance(clause, BinaryExpression)
    assert "= :project_id_1" in str(clause)


def test_clause_uses_provided_column() -> None:
    """The helper uses whatever column the caller passes; not hardcoded."""
    # Just verify the column reference comes from the caller — the SQL
    # text reflects the table the column belongs to.
    from lmchat.db.schema import documents

    clause = project_scope_clause(
        documents.c.project_id, project_id=7, unscoped=False
    )
    assert clause is not None
    assert "documents.project_id" in str(clause)


_ = Null  # appease "imported but unused" if the unused symbol lingers
