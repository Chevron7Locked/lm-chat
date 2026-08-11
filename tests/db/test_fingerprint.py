# SPDX-License-Identifier: Apache-2.0
"""Tests for lmchat.db.fingerprint — schema fingerprinting algorithm.

Tests for DB fingerprinting.

Covers:
- Same metadata → same fingerprint across calls (stability).
- Columns in different declaration order → same fingerprint (sort-stable).
- Column type change → different fingerprint.
- Column server_default change → different fingerprint.
- FK cascade action change → different fingerprint.
- Prints the baseline metadata fingerprint for the Alembic baseline stamp.
"""
from __future__ import annotations

import sys

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    ForeignKey,
    MetaData,
    String,
    Table,
    Text,
    text,
)

from lmchat.db.fingerprint import schema_fingerprint
from lmchat.db.schema import metadata as baseline_metadata

# ---------------------------------------------------------------------------
# Stability: same metadata yields same fingerprint
# ---------------------------------------------------------------------------


def test_fingerprint_stable_across_calls():
    """schema_fingerprint on the same metadata returns the same string twice."""
    fp1 = schema_fingerprint(baseline_metadata)
    fp2 = schema_fingerprint(baseline_metadata)
    assert fp1 == fp2


def test_fingerprint_is_hex_string():
    """Fingerprint must be a 32-char lowercase hex string (BLAKE2b 16-byte)."""
    fp = schema_fingerprint(baseline_metadata)
    assert len(fp) == 32
    assert all(c in "0123456789abcdef" for c in fp)


# ---------------------------------------------------------------------------
# Column-order independence
# ---------------------------------------------------------------------------


def _build_parallel_metadata_reordered() -> MetaData:
    """Build a MetaData with the same two-table schema but columns listed
    in reversed declaration order.  The fingerprint must match the normal
    declaration order.
    """
    m = MetaData()
    # Declare columns in reverse name order to stress the sort.
    Table(
        "widgets",
        m,
        Column("z_name", String(64), nullable=False),
        Column("a_id", BigInteger, primary_key=True),
    )
    Table(
        "gadgets",
        m,
        Column(
            "widget_id",
            BigInteger,
            ForeignKey("widgets.a_id", ondelete="CASCADE"),
            nullable=False,
        ),
        Column("note", Text, nullable=True),
        Column("g_id", BigInteger, primary_key=True),
    )
    return m


def _build_parallel_metadata_normal_order() -> MetaData:
    """Same schema as above but columns listed in forward declaration order."""
    m = MetaData()
    Table(
        "widgets",
        m,
        Column("a_id", BigInteger, primary_key=True),
        Column("z_name", String(64), nullable=False),
    )
    Table(
        "gadgets",
        m,
        Column("g_id", BigInteger, primary_key=True),
        Column("note", Text, nullable=True),
        Column(
            "widget_id",
            BigInteger,
            ForeignKey("widgets.a_id", ondelete="CASCADE"),
            nullable=False,
        ),
    )
    return m


def test_column_reorder_same_fingerprint():
    """Same schema with columns in different declaration order yields identical fingerprint."""
    m_normal = _build_parallel_metadata_normal_order()
    m_reordered = _build_parallel_metadata_reordered()
    assert schema_fingerprint(m_normal) == schema_fingerprint(m_reordered)


# ---------------------------------------------------------------------------
# Type change → different fingerprint
# ---------------------------------------------------------------------------


def _build_meta_with_column_type(col_type) -> MetaData:  # type: ignore[no-untyped-def]
    m = MetaData()
    Table("things", m, Column("id", BigInteger, primary_key=True), Column("value", col_type))
    return m


def test_column_type_change_different_fingerprint():
    """Changing a column type must change the fingerprint."""
    # Text.python_type = str; Boolean.python_type = bool — definitely different.
    m_text = _build_meta_with_column_type(Text())
    m_bool = _build_meta_with_column_type(Boolean())
    assert schema_fingerprint(m_text) != schema_fingerprint(m_bool)


# ---------------------------------------------------------------------------
# server_default change → different fingerprint
# ---------------------------------------------------------------------------


def _build_meta_with_server_default(server_default) -> MetaData:  # type: ignore[no-untyped-def]
    m = MetaData()
    Table(
        "items",
        m,
        Column("id", BigInteger, primary_key=True),
        Column("active", Boolean, nullable=False, server_default=server_default),
    )
    return m


def test_server_default_change_different_fingerprint():
    """Changing server_default from text('false') to text('true') must change fingerprint."""
    m_false = _build_meta_with_server_default(text("false"))
    m_true = _build_meta_with_server_default(text("true"))
    assert schema_fingerprint(m_false) != schema_fingerprint(m_true)


def test_server_default_none_vs_some_different_fingerprint():
    """Presence vs absence of server_default must change fingerprint."""
    m_none = _build_meta_with_server_default(None)
    m_some = _build_meta_with_server_default(text("false"))
    assert schema_fingerprint(m_none) != schema_fingerprint(m_some)


# ---------------------------------------------------------------------------
# FK cascade action change → different fingerprint
# ---------------------------------------------------------------------------


def _build_meta_with_fk_cascade(ondelete: str) -> MetaData:
    m = MetaData()
    Table("parents", m, Column("id", BigInteger, primary_key=True))
    Table(
        "children",
        m,
        Column("id", BigInteger, primary_key=True),
        Column(
            "parent_id",
            BigInteger,
            ForeignKey("parents.id", ondelete=ondelete),
            nullable=False,
        ),
    )
    return m


def test_fk_cascade_action_change_different_fingerprint():
    """Changing ondelete='CASCADE' to ondelete='SET NULL' must change fingerprint."""
    m_cascade = _build_meta_with_fk_cascade("CASCADE")
    m_set_null = _build_meta_with_fk_cascade("SET NULL")
    assert schema_fingerprint(m_cascade) != schema_fingerprint(m_set_null)


# ---------------------------------------------------------------------------
# Baseline metadata fingerprint — print for Alembic baseline migration
# ---------------------------------------------------------------------------


def test_print_baseline_fingerprint(capsys):
    """Print the fingerprint of the production baseline metadata.

    The value printed here is what ``0001_baseline.py`` must record as
    its ``SCHEMA_FINGERPRINT`` constant.  Copy the output into the
    migration file.
    """
    fp = schema_fingerprint(baseline_metadata)
    print(f"\nBaseline schema fingerprint: {fp}")
    captured = capsys.readouterr()
    assert fp in captured.out
    # Also emit to stderr so it's visible in pytest -s output.
    print(f"\n[BASELINE FINGERPRINT] {fp}", file=sys.stderr)
