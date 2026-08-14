# SPDX-License-Identifier: Apache-2.0
"""baseline schema

Revision ID: 0001
Revises:
Create Date: 2026-05-19 00:00:00.000000

This migration is the v1 baseline.  It creates all nine tables as
defined in ``src/lmchat/db/schema.py`` at the time the P1 module was
shipped (2026-05-19).

The ``SCHEMA_FINGERPRINT`` constant is the BLAKE2b fingerprint of the
live ``MetaData`` produced by ``schema_fingerprint(metadata)`` from
``lmchat.db.fingerprint``.  The startup stamp logic in
``src/lmchat/db/startup.py`` compares the live fingerprint against
this constant to detect un-migrated model drift.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None

# Schema fingerprint at the time this baseline was created.
# Verified against live metadata (matches the current src/lmchat/db/schema.py).
# Bumped 2026-05-21 by P12h migration 0010 — added memory_insights score
# columns (category/use_count/ups/downs/last_used/last_feedback_at/state)
# and the new insight_activations table per ADR-008 §6.7.
# Bumped 2026-05-22 by migration 0011 — added chats.incognito +
# chats.incognito_expires_at columns for incognito mode.
# Bumped 2026-05-22 by P13k migration 0014 — added admin_invites table
# for the admin-issued one-shot registration tokens.
# Bumped 2026-05-28 by migration 0019 — added chats.model_id nullable TEXT
# column for per-chat model persistence (onModelChange → PATCH model_id).
# Bumped 2026-06-04 by migration 0021 — added projects table and
# nullable project_id FK columns on chats / documents / memory_insights
# for LMChat Projects v1 (project workspace containers).
# Bumped 2026-06-04 by migration 0022 — added projects.embedding_model_id
# + projects.default_model_id (nullable Text) for Projects additions
# Phase 8 (B1 + B2).
# Bumped 2026-06-05 by migration 0023a — added
# chats.detached_from_project_meta JSON, projects.rag_threshold Integer,
# and projects.meta JSON for Projects additions Phase 10 (D1 + D2 + E1).
# Bumped 2026-06-05 by migration 0023b — dropped projects.folders
# (per-project folder feature removed; ADR-028; archive in
# projects.meta.folders survives the drop).
# Bumped 2026-06-10 by migration 0025 (Cluster 5 Task 2) — added
# messages.last_activity_at nullable DateTime + index, for reaper
# COALESCE(last_activity_at, created_at) threshold guard.
# Bumped 2026-06-10 by migration 0026 (Continue-chip closeout) — added
# messages.stop_reason nullable Text ("stop" | "length" | NULL), persisted
# from chat.end so the FE Continue chip survives reload.
# Bumped 2026-06-11 by Cluster 3b Task 4 closeout — added
# messages.tool_calls nullable JSON (migration 0024 had landed without its
# schema.py column; this closes the orphan). Persisted by
# StreamingService._finalize_message in FE ToolCall shape so ToolCallCards
# survive reload.
# Bumped 2026-06-17 by migration 0027 (F1b semantic-retrieval fix) — added
# document_chunks.embedding_model_id nullable Text + ix_chunks_embedding_model_id,
# so the read path can embed the query under the same model each chunk was
# written with (cross-model retrieval correctness).
# Bumped 2026-06-18 by migration 0028 (MF-1 Responses-API replay fix) — added
# messages.tool_call_id nullable String so role="tool" turns persist the
# call_id required by the stateless-provider (Responses API) encoder for
# history reconstruction across turns.
# Bumped 2026-06-18 by migration 0029 (A4 — multi-provider credential
# persistence) — added provider_configs table for cloud provider CRUD
# (openai / openrouter / groq / custom); API key stored under ADR-007
# enc$v1$ envelope; enabled Boolean default True; extra_headers JSON.
# Bumped 2026-06-18 by migration 0030 (A4 allowlist) — added
# provider_configs.allowed_models nullable JSON column; NULL/[] = all models
# visible; non-empty list restricts picker to those model ids.
# Value verified via `schema_fingerprint(metadata)` against the updated
# db/schema.py before this bump.
#
# Bumped 2026-06-19 by migration 0032 (Workstream C MCP Store) — added
# mcp_servers table with slug, transport, command, args, url, secrets_enc,
# enabled, source, trust, consented, created_at, updated_at columns.
# Value verified via `schema_fingerprint(metadata)` against updated schema.py.
# 0034 added server_lm_studio_default.preferred_embedding_model_id.
# 0035 added server_lm_studio_default.preferred_background_model_id.
# 0036 added server_lm_studio_default app_settings columns (P13h).
# Bumped 2026-07-06 by migration 0037 (hybrid compaction) — added the
# compactions table (chat_id, summary, summary_model_id, anchor_msg_id,
# original_token_count, summary_token_count, created_at) and nullable
# messages.compaction_id FK (ON DELETE SET NULL). Value verified via
# `schema_fingerprint(metadata)` against the updated db/schema.py.
# Bumped 2026-07-07 by migration 0038 (project archiving) — added
# nullable projects.archived_at (DateTime(timezone=True), NULL = active).
# Value verified via `schema_fingerprint(metadata)` against the updated
# db/schema.py.
# Bumped 2026-07-07 by migration 0039 (rolling project auto-summary) —
# added projects.summary (Text, '' default), summary_updated_at
# (nullable DateTime(timezone=True)), and summary_message_watermark
# (Integer, 0 default). Value verified via `schema_fingerprint(metadata)`
# against the updated db/schema.py.
# Bumped 2026-07-08 by migration 0040 (LM Studio endpoint-mode toggle) —
# added server_lm_studio_default.lm_studio_endpoint_mode (Text, nullable,
# NULL="native"). Value verified via `schema_fingerprint(metadata)` against
# the updated db/schema.py.
# Bumped 2026-07-19 by migrations 0041 + 0042 (defrag sweep — dead-system
# removal) — dropped the unused ``api_tokens`` table (never wired to any
# route/service; LMChat auths via session cookies only) and the unused
# ``feedback`` table (P4 thumbs-up/down loop was built but never wired to
# any UI; its service methods had zero production callers). Value verified
# via `schema_fingerprint(metadata)` against the updated db/schema.py.
# Bumped 2026-08-08 by migration 0043 (memory recency ordering) — added
# memory_insights.last_active_epoch (nullable Float), a UNIX-epoch column
# mirroring last_used once a row has been recalled (else created_at's own
# epoch), kept in sync by MemoryService.save_auto_insight / _touch_insights
# so recall/eviction recency ordering can run as a plain SQL ORDER BY
# instead of a per-turn full-active-set Python scan. Value verified via
# `schema_fingerprint(metadata)` against the updated db/schema.py.
# Bumped 2026-08-11 by migration 0044 (repeat-loop cut threshold admin
# override) — added server_lm_studio_default.repeat_warning_cut_k (nullable
# Integer, NULL = "use the Settings env-var default"). Global-admin half of
# the tool-call repeat-loop cut K resolution chain; see
# app_settings_service.resolve_repeat_warning_cut_k. Value verified via
# `schema_fingerprint(metadata)` against the updated db/schema.py.
# Bumped 2026-08-12 by migration 0045 (durable sub-sessions) — added two
# dedicated tables, sub_sessions (per-session metadata: preset_id, title,
# status, model_id) and sub_session_messages (the transcript; reuses the
# messages draft->final streaming state machine). Kept OUT of the messages
# table on purpose so sub-session content is schema-isolated from the main
# chat / replay context / search / analytics / summaries. Value verified via
# `schema_fingerprint(metadata)` against the updated db/schema.py.
# Bumped 2026-08-13 by migration 0046 (chat tags + archive) — added
# chats.tags (JSON list[str], '[]' default) and chats.archived_at
# (nullable DateTime(timezone=True), NULL = active — same convention as
# projects.archived_at from migration 0038). Value verified via
# `schema_fingerprint(metadata)` against the updated db/schema.py.
SCHEMA_FINGERPRINT: str = "b59fb1cd5cd5134b0cfbd675cd5652b3"


def upgrade() -> None:
    """Create all nine baseline tables.

    Tables are created in foreign-key dependency order:
    users → sessions, chats → messages → message_embeddings,
    users → memory_insights, messages → feedback,
    users → audit_log, users → api_tokens.
    """
    # -----------------------------------------------------------------------
    # users
    # -----------------------------------------------------------------------
    op.create_table(
        "users",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("password_hash", sa.String(192), nullable=False),
        sa.Column("totp_secret", sa.Text(), nullable=True),
        sa.Column(
            "is_admin",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )

    # -----------------------------------------------------------------------
    # sessions
    # -----------------------------------------------------------------------
    op.create_table(
        "sessions",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"])

    # -----------------------------------------------------------------------
    # chats
    # -----------------------------------------------------------------------
    op.create_table(
        "chats",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("folder", sa.Text(), nullable=True),
        sa.Column(
            "pinned",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    # -----------------------------------------------------------------------
    # messages
    # -----------------------------------------------------------------------
    op.create_table(
        "messages",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("reasoning_content", sa.Text(), nullable=True),
        sa.Column(
            "state",
            sa.String(24),
            nullable=False,
            server_default=sa.text("'final'"),
        ),
        sa.Column("response_id", sa.Text(), nullable=True),
        sa.Column("model_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["chat_id"], ["chats.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_messages_chat_id", "messages", ["chat_id"])
    op.create_index("ix_messages_response_id", "messages", ["response_id"])
    op.create_index("ix_messages_model_id", "messages", ["model_id"])

    # -----------------------------------------------------------------------
    # message_embeddings
    # -----------------------------------------------------------------------
    op.create_table(
        "message_embeddings",
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("embedding_model_id", sa.Text(), nullable=False),
        sa.Column("embedding", sa.LargeBinary(), nullable=False),
        sa.Column("text_hash", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ["message_id"], ["messages.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("message_id"),
    )
    op.create_index("ix_embeddings_model", "message_embeddings", ["embedding_model_id"])
    op.create_index("ix_embeddings_text_hash", "message_embeddings", ["text_hash"])

    # -----------------------------------------------------------------------
    # memory_insights
    # -----------------------------------------------------------------------
    op.create_table(
        "memory_insights",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_hash", sa.String(64), nullable=False),
        sa.Column(
            "pinned",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "text_hash", name="uq_memory_user_hash"),
    )

    # -----------------------------------------------------------------------
    # feedback
    # -----------------------------------------------------------------------
    op.create_table(
        "feedback",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("rating", sa.SmallInteger(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["message_id"], ["messages.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    # -----------------------------------------------------------------------
    # audit_log
    # -----------------------------------------------------------------------
    op.create_table(
        "audit_log",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        # SET NULL so audit history survives user deletion.
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("event", sa.String(64), nullable=False),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_user_id", "audit_log", ["user_id"])
    op.create_index("ix_audit_event", "audit_log", ["event"])
    op.create_index("ix_audit_created_at", "audit_log", ["created_at"])

    # -----------------------------------------------------------------------
    # api_tokens
    # -----------------------------------------------------------------------
    op.create_table(
        "api_tokens",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )


def downgrade() -> None:
    """Drop all nine baseline tables in reverse FK dependency order.

    Reverse order ensures FK constraints don't block the drops:
    api_tokens, audit_log, feedback, memory_insights,
    message_embeddings, messages, chats, sessions, users.
    """
    op.drop_table("api_tokens")
    op.drop_index("ix_audit_created_at", table_name="audit_log")
    op.drop_index("ix_audit_event", table_name="audit_log")
    op.drop_index("ix_audit_user_id", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_table("feedback")
    op.drop_table("memory_insights")
    op.drop_index("ix_embeddings_text_hash", table_name="message_embeddings")
    op.drop_index("ix_embeddings_model", table_name="message_embeddings")
    op.drop_table("message_embeddings")
    op.drop_index("ix_messages_model_id", table_name="messages")
    op.drop_index("ix_messages_response_id", table_name="messages")
    op.drop_index("ix_messages_chat_id", table_name="messages")
    op.drop_table("messages")
    op.drop_table("chats")
    op.drop_index("ix_sessions_expires_at", table_name="sessions")
    op.drop_index("ix_sessions_user_id", table_name="sessions")
    op.drop_table("sessions")
    op.drop_table("users")
