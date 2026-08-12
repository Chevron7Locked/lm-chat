# SPDX-License-Identifier: Apache-2.0
"""Create ``sub_sessions`` + ``sub_session_messages`` (durable sub-sessions).

Sub-sessions (/research, /code, /write, /analyze, /architect) previously ran
clean-context with NO DB writes, so a mobile app reload lost the whole
analysis (confirmed data-loss bug). This migration gives them durable,
DEDICATED tables so a sub-session survives a reload like any chat message —
and, critically, keeps sub-session content SCHEMA-ISOLATED from ``messages``:
it can never leak into the main-chat message list, the model replay context,
search/FTS5, analytics, or project summaries, all of which query ``messages``.

Two fresh tables (NO change to ``messages`` — so the 0037 ADD-COLUMN / FTS5
sync-trigger hazard does not apply here; ``CREATE TABLE`` supports inline FKs
on both SQLite and Postgres):

  sub_sessions          per-session metadata (preset, title, status, model)
  sub_session_messages  the transcript; reuses the same
                        draft -> pending_finalization -> final /
                        aborted_by_client streaming state machine as
                        ``messages`` (``_stream_state.py``, parameterized on
                        the target table)

Reversibility: ``downgrade()`` drops both tables (child first).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0045"
down_revision: str = "0044"
branch_labels: str | None = None
depends_on: str | None = None

# id PK mirrors schema._AUTO_PK_TYPE (BigInteger; Integer on SQLite so the
# INTEGER PRIMARY KEY autoincrements via rowid).
_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    """Create sub_sessions + sub_session_messages + their indexes."""
    op.create_table(
        "sub_sessions",
        sa.Column("id", _ID, primary_key=True),
        sa.Column(
            "chat_id",
            sa.BigInteger(),
            sa.ForeignKey("chats.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("preset_id", sa.String(length=32), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=24),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column("model_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_sub_sessions_chat_id", "sub_sessions", ["chat_id"])

    op.create_table(
        "sub_session_messages",
        sa.Column("id", _ID, primary_key=True),
        sa.Column(
            "sub_session_id",
            sa.BigInteger(),
            sa.ForeignKey("sub_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("reasoning_content", sa.Text(), nullable=True),
        sa.Column(
            "state",
            sa.String(length=24),
            nullable=False,
            server_default=sa.text("'final'"),
        ),
        sa.Column("tool_calls", sa.JSON(), nullable=True),
        sa.Column("response_id", sa.Text(), nullable=True),
        sa.Column("stop_reason", sa.Text(), nullable=True),
        sa.Column("model_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_sub_session_messages_sid_id",
        "sub_session_messages",
        ["sub_session_id", "id"],
    )
    op.create_index(
        "ix_sub_session_messages_last_activity_at",
        "sub_session_messages",
        ["last_activity_at"],
    )


def downgrade() -> None:
    """Drop sub_session_messages + sub_sessions (child first)."""
    op.drop_index(
        "ix_sub_session_messages_last_activity_at",
        table_name="sub_session_messages",
    )
    op.drop_index(
        "ix_sub_session_messages_sid_id", table_name="sub_session_messages"
    )
    op.drop_table("sub_session_messages")
    op.drop_index("ix_sub_sessions_chat_id", table_name="sub_sessions")
    op.drop_table("sub_sessions")
