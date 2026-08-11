# SPDX-License-Identifier: Apache-2.0
"""SQLAlchemy Core table definitions for lm-chat.

All nine tables share a
single ``MetaData`` instance so Alembic's autogenerate can walk them in
one pass and ``metadata.create_all()`` can bootstrap a fresh DB in tests.

Rules:
- SQLAlchemy 2.0 Core only.  No ORM, no declarative_base, no Mapper.
- ForeignKey cascade actions follow the spec: CASCADE for child rows
  whose parent is their sole owner; SET NULL for audit_log.user_id
  (we want to keep audit history even when the user is deleted).
- server_default uses text() or func.now() — both are stable under the
  schema_fingerprint algorithm in db/fingerprint.py.
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)

metadata: MetaData = MetaData()

# Portable autoincrement PK type. Postgres receives BIGINT (driven by a
# sequence); SQLite receives INTEGER, which is the rowid alias and
# auto-increments via the rowid mechanism. Plain `BigInteger` on SQLite
# renders as `BIGINT` — NOT a rowid alias — which would require explicit
# ID generation on every insert. This variant is the idiomatic SQLAlchemy
# expression of "64-bit autoincrementing PK portable across SQLite + Postgres."
_AUTO_PK_TYPE = BigInteger().with_variant(Integer(), "sqlite")

# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------
users = Table(
    "users",
    metadata,
    Column("id", _AUTO_PK_TYPE, primary_key=True),
    Column("username", String(64), nullable=False, unique=True),
    # scrypt$N$r$p$salt-b64$hash-b64  (~96-128 chars; String(192) leaves headroom)
    Column("password_hash", String(192), nullable=False),
    # enc$v1$ envelope — NULL until TOTP is configured
    Column("totp_secret", Text, nullable=True),
    Column(
        "is_admin",
        Boolean,
        nullable=False,
        server_default=text("false"),
    ),
    # User-presentable identity beyond the opaque username
    # (migration 0020). All three are nullable so existing accounts
    # don't need a backfill. Email is non-unique on purpose — we don't
    # ship email-based recovery yet, so unique constraint would just
    # block multi-tenancy use cases without functional benefit. Avatar
    # is a URL (string), not a blob; storing bytes in users would
    # bloat the auth hot-path.
    Column("email", Text, nullable=True),
    Column("display_name", Text, nullable=True),
    Column("avatar_url", Text, nullable=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
)

# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------
sessions = Table(
    "sessions",
    metadata,
    Column("id", Text, primary_key=True),  # opaque 32-byte url-safe token
    Column(
        "user_id",
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("rotated_at", DateTime(timezone=True), nullable=True),
    Index("ix_sessions_user_id", "user_id"),
    Index("ix_sessions_expires_at", "expires_at"),
)

# ---------------------------------------------------------------------------
# chats
# ---------------------------------------------------------------------------
chats = Table(
    "chats",
    metadata,
    Column("id", _AUTO_PK_TYPE, primary_key=True),
    Column(
        "user_id",
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("title", Text, nullable=False),
    Column("folder", Text, nullable=True),
    Column(
        "pinned",
        Boolean,
        nullable=False,
        server_default=text("false"),
    ),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    # Per-chat settings JSON blob. Keys used so far:
    #   rag_enabled: bool — enable RAG augmentation for this chat.
    #   reasoning_effort: "off"|"low"|"medium"|"high" — per-chat override.
    #   focused_document_id: int|null — when set, ``rag_service.augment_prompt``
    #     bypasses retrieval and injects ordered chunks of that single document
    #     via ``documents_service.get_document_chunks`` (no FTS5, no
    #     vector lookup). Use case: "what does line 40 of <doc> say?"
    # server_default=text("'{}'") gives every chat
    # an empty-dict baseline so NULL checks are unnecessary.
    Column(
        "settings",
        JSON,
        nullable=False,
        server_default=text("'{}'"),
    ),
    # DnD sort order within each folder / pinned section.
    Column(
        "display_order",
        Integer,
        nullable=False,
        server_default=text("0"),
    ),
    # Per-chat model persistence: the model selected in this chat's composer.
    # NULL means no model has been selected yet ("Select a model…" placeholder).
    Column("model_id", Text, nullable=True),
    # Incognito mode columns.
    # ``incognito`` flips memory write-paths off for this chat (see
    # MemoryService.index_message / pin_insight) and
    # marks the chat for purge on logout OR on TTL expiry.
    # ``incognito_expires_at`` is REAL UNIX epoch seconds; NULL when
    # incognito=0.  Default TTL (1h) is computed at chat creation by
    # ChatService.create() so this column is naturally NULL for legacy rows.
    # Privacy invariant: once messages exist, the incognito flag is
    # immutable (route layer rejects PATCH with 422).
    Column(
        "incognito",
        Integer,
        nullable=False,
        server_default=text("0"),
    ),
    Column(
        "incognito_expires_at",
        Float,
        nullable=True,
    ),
    # Nullable FK into projects.id (migration 0021).
    # When NULL the chat is "un-projected" (legacy / default behavior).
    # ON DELETE SET NULL: deleting a project leaves its chats intact as
    # un-projected so the admin never loses chat history.
    Column(
        "project_id",
        Integer,
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
    ),
    # JSON snapshot of the project's identity captured AT detach time
    # (migration 0023a):
    #   {project_id, name, detached_at, system_prompt_hash}
    # The chat's history can render a "Detached from X on Y" separator
    # turn even if the project is later deleted. NULL = never detached.
    Column("detached_from_project_meta", JSON, nullable=True),
    Index("ix_chats_project_id", "project_id"),
)

# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------
messages = Table(
    "messages",
    metadata,
    Column("id", _AUTO_PK_TYPE, primary_key=True),
    Column(
        "chat_id",
        BigInteger,
        ForeignKey("chats.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("role", String(16), nullable=False),  # user | assistant | system | tool
    Column("content", Text, nullable=False),
    Column("reasoning_content", Text, nullable=True),
    # Streaming state-machine column — reserved ahead of time so the
    # streaming service doesn't need a schema change later.
    # Allowed values: draft | pending_finalization | final | aborted_by_client
    Column(
        "state",
        String(24),
        nullable=False,
        server_default=text("'final'"),
    ),
    Column("response_id", Text, nullable=True),  # LM Studio response_id chain
    # Continue-chip closeout (migration 0026). Why the
    # producing stream terminated, captured from the upstream chat.end event:
    # "stop" (natural end) | "length" (max_output_tokens truncation). NULL for
    # user/system rows and for rows finalized before the migration (no
    # backfill). The FE renders the Continue chip for
    # persisted assistant rows where stop_reason == "length".
    Column("stop_reason", Text, nullable=True),
    # JSON list of the tool calls the producing stream executed (migration
    # 0024), in FE ToolCall shape:
    #   [{id, name, arguments, status, result?}, ...]
    # Written once by StreamingService._finalize_message; NULL for user/system
    # rows, for assistant turns with no tool calls, and for rows finalized
    # before the migration (no backfill). The FE renders
    # ToolCallCards from this on reload.
    Column("tool_calls", JSON, nullable=True),
    # Responses-API round-trip: the call_id that produced this tool-result
    # turn (role="tool"). Required by the stateless-provider (replay) encoder
    # so the history rebuild can echo the correct call_id on tool_result
    # items. NULL for all other roles and for rows written before
    # migration 0028.
    Column("tool_call_id", String(), nullable=True),
    # LM Studio model_id that produced this message; NULL for user/system messages.
    # Added for analytics (top_models / avg_latency_per_model queries).
    Column("model_id", Text, nullable=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    # Reaper inactivity threshold.
    # Touched on every content-bearing event in _CoalesceTimer.flush() and at
    # each tool_call.* event.  Reaper selects on `last_activity_at < now - 5min`
    # rather than `created_at`, so actively-streaming messages are never reaped.
    # Backfill: created_at.
    Column(
        "last_activity_at",
        DateTime(timezone=True),
        nullable=True,
    ),
    # Hybrid compaction (migration 0037): NULL = active (in the live context
    # window). Set = archived — the row belongs to the compactions.id span and
    # is excluded from candidate SELECTs / context assembly, but the row (and
    # any message_embeddings pointing at it) is NEVER deleted, so semantic
    # recall keeps working over archived history. ON DELETE SET NULL (not
    # CASCADE) so a hypothetical standalone compactions-row delete only
    # un-archives the messages rather than destroying them; the normal
    # cleanup path is deleting the whole chat, which cascades chats ->
    # messages independently of this FK.
    Column(
        "compaction_id",
        BigInteger,
        ForeignKey("compactions.id", ondelete="SET NULL"),
        nullable=True,
    ),
    Index("ix_messages_chat_id", "chat_id"),
    Index("ix_messages_response_id", "response_id"),
    Index("ix_messages_model_id", "model_id"),
    # Index for reaper's inactivity query.
    Index("ix_messages_last_activity_at", "last_activity_at"),
    # Composite index for cursor-based pagination (chat_id, id) DESC
    # used by MessageService.list_for_chat with before_id / since_id cursors.
    Index("ix_messages_chat_id_id", "chat_id", "id"),
    # Hybrid compaction: the scope-guard SELECT filters on this column
    # (`WHERE compaction_id IS NULL`) and the recall endpoint selects on
    # `compaction_id = :cid`, both per-chat — index accelerates both.
    Index("ix_messages_compaction_id", "compaction_id"),
)

# ---------------------------------------------------------------------------
# compactions (hybrid compaction — migration 0037)
# ---------------------------------------------------------------------------
# One row per `/compact` call that actually archived something. `messages`
# rows with `compaction_id = this.id` are the archived SET (membership, not a
# contiguous id range — invariant-protected tool pairs interleaved in the
# archived span stay active). `anchor_msg_id` is the id the FE renders the
# collapsed tab *before* (the oldest archived id at the time of archiving) —
# display position only, not itself a FK (the id may later belong to a row
# whose own compaction_id has since changed via a fork remap).
compactions = Table(
    "compactions",
    metadata,
    Column("id", _AUTO_PK_TYPE, primary_key=True),
    Column(
        "chat_id",
        BigInteger,
        ForeignKey("chats.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("summary", Text, nullable=False),
    # Model that produced the summary; NULL if unresolved at write time.
    Column("summary_model_id", Text, nullable=True),
    Column("anchor_msg_id", BigInteger, nullable=False),
    Column("original_token_count", Integer, nullable=False),
    Column("summary_token_count", Integer, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Index("ix_compactions_chat_id", "chat_id"),
)

# ---------------------------------------------------------------------------
# message_embeddings
# ---------------------------------------------------------------------------
message_embeddings = Table(
    "message_embeddings",
    metadata,
    Column(
        "message_id",
        BigInteger,
        ForeignKey("messages.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    # Widened from String(128); LM Studio model IDs are unbounded by spec.
    Column("embedding_model_id", Text, nullable=False),
    Column("embedding", LargeBinary, nullable=False),  # pickled / packed float vector
    Column("text_hash", String(64), nullable=False),  # content-hash dedup
    Index("ix_embeddings_model", "embedding_model_id"),
    Index("ix_embeddings_text_hash", "text_hash"),
)

# ---------------------------------------------------------------------------
# memory_insights
# ---------------------------------------------------------------------------
memory_insights = Table(
    "memory_insights",
    metadata,
    Column("id", _AUTO_PK_TYPE, primary_key=True),
    Column(
        "user_id",
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("text", Text, nullable=False),
    Column("text_hash", String(64), nullable=False),
    Column(
        "pinned",
        Boolean,
        nullable=False,
        server_default=text("false"),
    ),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    # Cognitive-decay + Bayesian Laplace score columns.
    Column(
        "category",
        String(32),
        nullable=False,
        server_default=text("'context'"),
    ),
    Column("use_count", Integer, nullable=False, server_default=text("0")),
    Column("ups", Float, nullable=False, server_default=text("0")),
    Column("downs", Float, nullable=False, server_default=text("0")),
    Column("last_used", Float, nullable=True),
    # Migration 0043. UNIX-epoch Float mirroring `last_used` when the row
    # has been recalled, else `created_at`'s own epoch — kept in sync by
    # `MemoryService.save_auto_insight` (INSERT) and `_touch_insights`
    # (UPDATE). Exists so recall/eviction recency ordering can use a
    # plain SQL `ORDER BY` against same-typed Float values instead of
    # fetching the active row set and sorting in Python: `last_used` is
    # Float but `created_at` is DateTime, and SQLite compares mixed
    # storage classes by class first (NULL < INTEGER/REAL < TEXT), so a
    # SQL `COALESCE(last_used, created_at)` silently mis-orders (see
    # `lmchat.services.memory_service._recency_order_expr`). Nullable —
    # a NULL here (a pre-migration row the backfill missed, or a row
    # written by something other than MemoryService) falls back through
    # `last_used` and then `created_at` at query time, matching the
    # pre-migration semantics for a row MemoryService never touched.
    Column("last_active_epoch", Float, nullable=True),
    Column("last_feedback_at", Float, nullable=True),
    Column(
        "state",
        String(16),
        nullable=False,
        server_default=text("'active'"),
    ),
    UniqueConstraint("user_id", "text_hash", name="uq_memory_user_hash"),
    # Nullable FK into projects.id (migration 0021).
    # When NULL the insight is "un-projected" (legacy / default).
    # ON DELETE SET NULL: deleting a project leaves its insights
    # intact as un-projected — admin's memory survives.
    Column(
        "project_id",
        Integer,
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
    ),
    Index("ix_memory_insights_project_id", "project_id"),
)

# ---------------------------------------------------------------------------
# insight_activations — attribution tracking table.
#
# Records the (assistant_message, insight) tuples emitted by
# ``memory_service.recall_insights`` for per-message attribution tracing.
# The write path that populated this table (``MemoryService.
# record_activations``) and its consumer (``handle_feedback``) were
# removed as an unwired dead system — the table itself is
# retained for schema/privacy-invariant continuity (see the
# privacy / cross-user-leak stress invariants, which still read it).
# ---------------------------------------------------------------------------
insight_activations = Table(
    "insight_activations",
    metadata,
    Column("id", _AUTO_PK_TYPE, primary_key=True),
    Column(
        "message_id",
        BigInteger,
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "insight_id",
        BigInteger,
        ForeignKey("memory_insights.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("created_at", Float, nullable=False),
    Index("idx_activations_message", "message_id"),
    Index("idx_activations_insight", "insight_id"),
)


# ---------------------------------------------------------------------------
# audit_log
# ---------------------------------------------------------------------------
audit_log = Table(
    "audit_log",
    metadata,
    Column("id", _AUTO_PK_TYPE, primary_key=True),
    # SET NULL so audit history survives user deletion.
    Column(
        "user_id",
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    ),
    Column("event", String(64), nullable=False),  # e.g. 'auth.login.success'
    Column("ip", String(64), nullable=True),
    Column("user_agent", Text, nullable=True),
    # JSON column: python_type == dict — stable in fingerprint algorithm.
    Column("detail", JSON, nullable=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        # Production Postgres default is clock_timestamp() — set by migration
        # 0018_p15_audit_log_clock_timestamp.  clock_timestamp() returns the
        # actual wall-clock time at INSERT execution, not transaction-start
        # time (now()), which caused ~200ms timestamp inversions under
        # concurrent load (observed in stress-baseline testing).
        # func.now() here is the portable bootstrap default for SQLite
        # (dev/test); the migration ALTER overrides it on Postgres.
        server_default=func.now(),
    ),
    Index("ix_audit_user_id", "user_id"),
    Index("ix_audit_event", "event"),
    Index("ix_audit_created_at", "created_at"),
)


# ---------------------------------------------------------------------------
# documents  (RAG pipeline)
# ---------------------------------------------------------------------------
# Columns:
#   id          — auto-incrementing PK (portable BigInt per _AUTO_PK_TYPE).
#   user_id     — FK users (CASCADE delete).
#   title       — display name; set to filename at upload time.
#   mime_type   — e.g. "text/plain", "application/pdf".
#   byte_size   — raw upload size in bytes (for quota display).
#   chunk_count — updated after chunking; 0 while upload is in progress.
#   embedding_model_id — model used to embed chunks (Text, widened to
#                        match message_embeddings).
#   sha256      — hex digest of the raw file bytes; enables dedup.
#   deleted_at  — soft-delete (NULL = active; timestamp = deleted).
#   uploaded_at — creation timestamp.
documents = Table(
    "documents",
    metadata,
    Column("id", _AUTO_PK_TYPE, primary_key=True),
    Column(
        "user_id",
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("title", Text, nullable=False),
    Column("mime_type", String(64), nullable=False),
    Column("byte_size", BigInteger, nullable=False),
    Column(
        "chunk_count",
        Integer,
        nullable=False,
        server_default=text("0"),
    ),
    Column("embedding_model_id", Text, nullable=False, server_default=text("''")),
    Column("sha256", String(64), nullable=False),
    Column("deleted_at", DateTime(timezone=True), nullable=True),
    Column(
        "uploaded_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    # Nullable FK into projects.id (migration 0021).
    # When NULL the document is "un-projected" (legacy / default).
    # ON DELETE SET NULL: deleting a project leaves its documents
    # intact as un-projected so the admin keeps their uploads.
    Column(
        "project_id",
        Integer,
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
    ),
    Index("ix_documents_user_id", "user_id"),
    Index("ix_documents_sha256", "sha256"),
    Index("ix_documents_project_id", "project_id"),
)

# ---------------------------------------------------------------------------
# document_chunks  (RAG pipeline)
# ---------------------------------------------------------------------------
# One row per chunk of a document.
#   id          — auto-incrementing PK.
#   document_id — FK documents (CASCADE delete removes chunks with document).
#   ordinal     — zero-based chunk index within the document.
#   text        — chunk text content.
#   text_hash   — blake2b(64-hex) of normalized text for dedup.
#   embedding   — packed float32 vector (same format as message_embeddings).
document_chunks = Table(
    "document_chunks",
    metadata,
    Column("id", _AUTO_PK_TYPE, primary_key=True),
    Column(
        "document_id",
        BigInteger,
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("ordinal", Integer, nullable=False),
    Column("text", Text, nullable=False),
    Column("text_hash", String(64), nullable=False),
    Column("embedding", LargeBinary, nullable=False),
    # Tracks which embedding model produced this vector so the read
    # path can embed the query under the same model.
    Column("embedding_model_id", Text, nullable=True),
    Index("ix_chunks_document_id", "document_id"),
    Index("ix_chunks_embedding_model_id", "embedding_model_id"),
    UniqueConstraint("document_id", "ordinal", name="uq_chunk_doc_ord"),
)

# ---------------------------------------------------------------------------
# prompts  (prompt library)
# ---------------------------------------------------------------------------
# User-managed prompt presets.
#   id         — auto-incrementing PK.
#   user_id    — FK users (CASCADE delete).
#   name       — short display label (unique per user).
#   content    — full prompt text.
#   created_at — creation timestamp.
#   updated_at — last-updated timestamp.
prompts = Table(
    "prompts",
    metadata,
    Column("id", _AUTO_PK_TYPE, primary_key=True),
    Column(
        "user_id",
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("name", String(128), nullable=False),
    Column("content", Text, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Index("ix_prompts_user_id", "user_id"),
    UniqueConstraint("user_id", "name", name="uq_prompt_user_name"),
)

# ---------------------------------------------------------------------------
# quotas  (per-user quota limits; adds model_admin_ops_per_day)
# ---------------------------------------------------------------------------
# One row per user; PK is user_id (no autoincrement).
#   user_id                  — FK users (CASCADE delete).
#   tokens_per_day           — daily token budget.
#   requests_per_day         — daily request budget.
#   model_admin_ops_per_day  — daily model lifecycle op budget (admin).
#   created_at / updated_at  — timestamps.
quotas = Table(
    "quotas",
    metadata,
    Column(
        "user_id",
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "tokens_per_day",
        Integer,
        nullable=False,
        server_default=text("100000"),
    ),
    Column(
        "requests_per_day",
        Integer,
        nullable=False,
        server_default=text("1000"),
    ),
    # Per-admin daily budget for model lifecycle operations.
    # Non-admins are blocked at the route layer (403) before quota check.
    Column(
        "model_admin_ops_per_day",
        Integer,
        nullable=False,
        server_default=text("1000"),
    ),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
)

# ---------------------------------------------------------------------------
# quota_usage  (daily consumption counters; adds model_admin_ops)
# ---------------------------------------------------------------------------
# Composite PK (user_id, date) — one row per user per calendar day.
#   user_id                   — FK users (CASCADE delete).
#   date                      — calendar date (DATE type).
#   tokens_consumed           — tokens used today.
#   requests_consumed         — requests made today.
#   model_admin_ops_consumed  — model lifecycle ops today.
quota_usage = Table(
    "quota_usage",
    metadata,
    Column(
        "user_id",
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    ),
    Column("date", Date, primary_key=True, nullable=False),
    Column(
        "tokens_consumed",
        Integer,
        nullable=False,
        server_default=text("0"),
    ),
    Column(
        "requests_consumed",
        Integer,
        nullable=False,
        server_default=text("0"),
    ),
    # Model lifecycle operation consumption counter.
    Column(
        "model_admin_ops_consumed",
        Integer,
        nullable=False,
        server_default=text("0"),
    ),
    Index("ix_quota_usage_user_id", "user_id"),
)


# ---------------------------------------------------------------------------
# user_lm_studio_overrides  (per-user LM Studio config override)
# ---------------------------------------------------------------------------
# Per-user override of the LM Studio connection parameters.  Encrypted at
# rest via the ``enc$v1$`` envelope, with per-record KDF inputs
# (kind=``lm_studio_api_key``, record_id=user_id).
#
# Schema invariants:
#   - ``user_id`` is the PK and FK to users (CASCADE delete).
#   - Any column may be NULL; NULL means "fall through to the next tier".
#   - ``api_key_enc`` is opaque ``enc$v1$`` ciphertext; never queried by
#     value.
user_lm_studio_overrides = Table(
    "user_lm_studio_overrides",
    metadata,
    Column(
        "user_id",
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("base_url", Text, nullable=True),
    # enc$v1$ envelope; kind="lm_studio_api_key", record_id=user_id
    Column("api_key_enc", Text, nullable=True),
    Column("default_model", Text, nullable=True),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
)

# ---------------------------------------------------------------------------
# server_lm_studio_default  (admin-set server-wide LM Studio default)
# ---------------------------------------------------------------------------
# Single-row table; only ``id=1`` is ever inserted (enforced by
# ``LmStudioOverridesService.set_admin_default`` via UPSERT on id=1).
# Admin endpoint
# ``PATCH /api/admin/lmstudio/default`` is the sole writer.  Encrypted at
# rest using the ``enc$v1$`` envelope with kind=``lm_studio_admin_default``,
# record_id=1.
server_lm_studio_default = Table(
    "server_lm_studio_default",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("base_url", Text, nullable=True),
    # enc$v1$ envelope; kind="lm_studio_admin_default", record_id=1
    Column("api_key_enc", Text, nullable=True),
    Column("default_model", Text, nullable=True),
    # Preferred embedding model key — written by resolve_active_embedding_model_key
    # on first indexing call (persist_default=True) or explicitly via set_admin_default.
    # NULL = not yet chosen; resolver then picks lexicographically first loaded key.
    Column("preferred_embedding_model_id", Text, nullable=True),
    # Preferred BACKGROUND-tasks model key — out-of-band auxiliary LLM calls
    # (auto-memory distillation, chat-title generation, follow-up chips) use
    # this instead of the chat's model so they stop competing with the user's
    # next turn on a single local model. NULL = "Same as chat model" (default).
    # Read by resolve_background_model_id (FAIL-SOFT: falls back to the chat
    # model when unset or not currently loaded). Quality modes are NOT routed
    # through this — they must stay on the chat model.
    Column("preferred_background_model_id", Text, nullable=True),
    # Endpoint-mode toggle: which HTTP surface LM Chat drives when talking to
    # LM Studio — and, because context_mode keys off this, which MCP system
    # runs the tool loop.
    #   NULL / "native"  — /api/v1/chat. LM Studio executes MCP tools itself
    #                      from ~/.lmstudio/mcp.json; server-side conversation
    #                      chaining via previous_response_id.
    #   "openai_compat"  — /v1/chat/completions. LM Chat drives tool calls
    #                      through its own MCP Store (AgenticMcpProvider);
    #                      full history is replayed each turn (no server-side
    #                      chaining). Set/read via set_endpoint_mode /
    #                      fetch_endpoint_mode / resolve_lm_studio_endpoint_mode.
    # NULL means "native" (mirrors the NULL="default" convention used by the
    # preference columns above).
    Column("lm_studio_endpoint_mode", Text, nullable=True),
    # App-level admin overrides (promoted from env-only config).
    # NULL = "use the Settings env-var default".  Written by
    # ``app_settings_service.py`` resolver and PATCH /api/settings/app.
    Column("memory_distillation_enabled", Boolean, nullable=True),
    Column("subsession_memory_distillation_enabled", Boolean, nullable=True),
    Column("web_search_provider", String(16), nullable=True),
    Column("searxng_url", String(512), nullable=True),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
)

# ---------------------------------------------------------------------------
# chat_shares — public share tokens for chats
# ---------------------------------------------------------------------------
# One row per active share token for a chat.  The presence of a row makes the
# chat publicly readable at /share/:token (no authentication required).  DELETE
# the row to revoke the share.
#
# Privacy invariant: the route layer MUST refuse to create a
# share token for an incognito chat (ChatService.is_shareable returns False).
# This is a route-level guard, not a DB constraint, because the incognito flag
# lives on the chats table.
#
# Token shape: ``secrets.token_urlsafe(24)`` → 32-char URL-safe string.  The
# unique index makes lookups O(log n).
chat_shares = Table(
    "chat_shares",
    metadata,
    Column("id", _AUTO_PK_TYPE, primary_key=True),
    Column(
        "chat_id",
        BigInteger,
        ForeignKey("chats.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "token",
        String(64),
        nullable=False,
        unique=True,
    ),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Index("ix_chat_shares_chat_id", "chat_id"),
)


# ---------------------------------------------------------------------------
# admin_invites
# ---------------------------------------------------------------------------
# Admin-issued one-shot registration tokens.  An admin clicks "Invite admin"
# in `/admin/users`; the backend generates a random `secrets.token_urlsafe(24)`
# token, persists it here with an expires_at, and returns the token to the UI.
#
# Consumption: the invitee POSTs to /api/auth/register with the token via
# `?token=` or X-Setup-Token header (same wire format as the bootstrap setup
# token — see `register_endpoint` in routes/auth.py for the dispatch logic).
# On success the row is marked used_at + used_by and the registrant is
# granted is_admin=True regardless of whether they are the first user.
#
# Expiry: rows past expires_at are rejected at consumption time.  No background
# sweeper required — they're cheap to retain for audit.
admin_invites = Table(
    "admin_invites",
    metadata,
    Column("id", _AUTO_PK_TYPE, primary_key=True),
    # Unique URL-safe token; secrets.token_urlsafe(24) → 32 chars.
    Column("token", String(64), nullable=False, unique=True),
    # Admin who issued the invite (SET NULL if that admin is later deleted —
    # the invite history survives even when the issuer is gone).
    Column(
        "created_by",
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    ),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("used_at", DateTime(timezone=True), nullable=True),
    # The user_id of the consumer.  CASCADE-equivalent: SET NULL so deleting a
    # freshly-promoted admin doesn't leave a dangling FK; the invite row itself
    # stays for audit.
    Column(
        "used_by",
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    ),
    Index("ix_admin_invites_expires_at", "expires_at"),
)


# ---------------------------------------------------------------------------
# user_prefs (feature-parity closure)
# ---------------------------------------------------------------------------
# One row per user.  Holds user-named folder buckets that exist in the
# sidebar even before any chat is moved into them, plus a forward-compat
# slot for additional preferences (theme, language, notification settings,
# panel layout) that share the same row.
#
# Schema invariants:
#   - ``user_id`` is the PK + FK (CASCADE delete with the user).
#   - ``folders`` is a JSON array of strings; default empty array.
#   - The visible folder set in the sidebar is
#     ``prefs.folders ∪ DISTINCT chats.folder WHERE user_id=?``.
#
# Why a dedicated table:
#   theme/language/notifications/panel-layout prefs will follow; a typed
#   table keeps the schema queryable and round-trips cleanly through Alembic.
user_prefs = Table(
    "user_prefs",
    metadata,
    Column(
        "user_id",
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    # JSON list[str] — user-named folder bucket names.
    Column(
        "folders",
        JSON,
        nullable=False,
        server_default=text("'[]'"),
    ),
    # JSON dict — per-preset model/provider defaults (migration 0031).
    # Shape: {"<presetId>": {"provider": "<slug>", "model_id": "<id>"}, ...}
    # NULL / {} means no per-preset defaults; fall back to caller-supplied model.
    Column(
        "preset_models",
        JSON,
        nullable=True,
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
)


# ---------------------------------------------------------------------------
# projects (migration 0021)
# ---------------------------------------------------------------------------
# A project is a persistent container that owns chats, documents, and
# memory_insights via nullable FKs on each of those tables. When a chat
# (or document, or insight) has project_id IS NULL it is "un-projected"
# — the legacy / default mode, behavior identical to the pre-projects
# substrate. When project_id is set, retrieval scopes to the project's
# content (see memory_service.recall / recall_insights / list_pinned
# and retrieval_service.retrieve — all of those accept project_id and
# JOIN through to the per-table column).
#
# Schema invariants:
#   - user_id: the owning admin. CASCADE delete with the user.
#   - name: required non-empty short label (validated via
#     text_input_policy in the service layer).
#   - description / system_prompt: free text; "" sentinel for unset.
#     system_prompt is the active instructions for any chat with this
#     project_id; injected at stream-time in streaming_service.
#   - folders: JSON list[str] of folder names for chats inside this
#     project. Same JSON shape as user_prefs.folders; the folder_service
#     refactor parameterizes by source so the mutation
#     logic lives once.
#   - created_at / updated_at: REAL UNIX epoch seconds. Service writes
#     time.time() at create/update; no server_default per the v0.5.x
#     convention.
projects = Table(
    "projects",
    metadata,
    Column("id", _AUTO_PK_TYPE, primary_key=True),
    Column(
        "user_id",
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("name", Text, nullable=False),
    Column("description", Text, nullable=False, server_default=text("''")),
    Column("system_prompt", Text, nullable=False, server_default=text("''")),
    # Dropped ``projects.folders`` (migration 0023b). Archive lives in
    # ``projects.meta.folders``.
    # Embedding model pinned by the
    # first document attach; subsequent attaches under a different
    # active model raise EmbeddingModelPinConflict → 409. NULL means
    # "no docs attached yet" → retrieval-time read falls
    # back to the user's currently active embedding model.
    Column("embedding_model_id", Text, nullable=True),
    # Seeds chats.model_id on
    # POST /api/projects/{id}/chats. NULL falls through to the user's
    # global default model. Chats keep their own override.
    Column("default_model_id", Text, nullable=True),
    # Per-project override for the RAG-mode inline/hybrid threshold (in
    # tokens; migration 0023a).
    # NULL falls back to the
    # ``ctx_window * inline_fraction`` formula at runtime
    # (rag_mode_resolver.compute_rag_threshold). Set non-NULL by the
    # power-user knob in the UI.
    Column("rag_threshold", Integer, nullable=True),
    # Generic per-project metadata home (migration 0023a). v1.0 use:
    # archives the soon-to-be-
    # dropped ``projects.folders`` JSON into ``meta.folders`` so the
    # data survives the column drop and future per-project meta lands
    # here without its own column (one-shot extensibility surface for
    # short-lived experiments).
    Column("meta", JSON, nullable=False, server_default=text("'{}'")),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    # Soft-archive, NOT delete.
    # NULL = active project (default listing); non-NULL = archived at
    # that timestamp. Same "NULL = active" convention as
    # ``documents.deleted_at``. Archiving never touches children —
    # chats / documents stay attached, the project just drops out of
    # the default sidebar/list until unarchived.
    Column("archived_at", DateTime(timezone=True), nullable=True),
    # Rolling auto-summary (migration 0039). Accumulates understanding of
    # the project's conversations over
    # time; regenerated out-of-band
    # by ``project_summary_service.refresh_project_summary`` and injected
    # into project chats' composed system prompt alongside
    # ``system_prompt`` (see streaming_service). "" (not NULL) = no
    # summary generated yet, matching this table's other free-text
    # columns' "" sentinel convention.
    Column("summary", Text, nullable=False, server_default=text("''")),
    # Wall-clock time of the last regeneration; NULL until the first
    # summary is generated. Surfaced to the FE as "Updated <relative
    # time>" on the project page's summary card.
    Column("summary_updated_at", DateTime(timezone=True), nullable=True),
    # The project's total message count (across all its chats) at the
    # last regeneration — the throttle watermark. The auto-refresh
    # trigger (StreamingService._safe_refresh_project_summary) only
    # regenerates once (current count - watermark) >= the refresh
    # threshold, so a fast back-and-forth doesn't call the OOB
    # summarizer on every turn.
    Column(
        "summary_message_watermark",
        Integer,
        nullable=False,
        server_default=text("'0'"),
    ),
    Index("ix_projects_user_id", "user_id"),
)


# ---------------------------------------------------------------------------
# memory_insights_history (feature-parity closure)
# ---------------------------------------------------------------------------
# Append-only audit-and-restore table written before destructive memory
# operations (refine, future: bulk delete).  Stores a JSON snapshot of the
# user's insights at the moment the destructive op runs so the admin can
# undo it with one click.  See ``MemoryService.refine`` for the writer and
# ``MemoryService.restore_from_history`` for the reader.
#
# Schema invariants:
#   - ``user_id`` is the owning user (CASCADE delete with the user).
#   - ``event`` is a short tag describing the destructive op
#     (``"refine"`` for now; future-proof for ``"delete"`` / ``"bulk_purge"``).
#   - ``insights_before`` is a JSON list of
#     ``{id, text, created_at, project_id}`` tuples captured pre-op.
#     IDs are recorded for forensic value only — restore mints fresh
#     PKs because old rows may have been freed in the interim.
#     The tuple shape extends from {id, text, created_at} to add the
#     ``project_id`` key (migration 0022b) so per-project refine/restore
#     preserves scope.
#     Legacy snapshots without the key restore as un-projected
#     (``project_id IS NULL``) — backward-compat read in
#     ``MemoryService.restore_from_history``.
memory_insights_history = Table(
    "memory_insights_history",
    metadata,
    Column("id", _AUTO_PK_TYPE, primary_key=True),
    Column(
        "user_id",
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("event", Text, nullable=False),
    Column("insights_before", JSON, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Index("ix_memory_insights_history_user_id", "user_id"),
)


# ---------------------------------------------------------------------------
# provider_configs (multi-provider credential persistence)
# ---------------------------------------------------------------------------
# One row per external LLM provider (openai / openrouter / groq / custom).
# Single-admin app → unique provider slug enforces one config per provider.
# The API key is stored under the ``enc$v1$`` envelope; see
# lmchat.services.provider_config_service for the encryption details.
#
# Schema invariants:
#   - ``provider`` is a short slug: 'openai', 'openrouter', 'groq', or an
#     arbitrary string for custom HTTP-compat providers.  Unique index
#     enforces one row per provider.
#   - ``base_url`` is the API base URL (e.g. "https://api.openai.com/v1").
#     Required (non-nullable) — a config without a URL is not usable.
#   - ``api_key_enc`` holds the encrypted bearer token; NULL when
#     the provider does not require auth.
#   - ``default_model`` is the model ID to use when no per-chat override is
#     present; NULL falls through to the provider's own default.
#   - ``extra_headers`` is a JSON dict for provider-specific headers
#     (e.g. HTTP-Referer and X-Title for OpenRouter's attribution policy).
#   - ``enabled`` controls whether the provider is offered in the model
#     picker.  Defaults True so the UX is "add → immediately available".
#   - ``created_at`` / ``updated_at`` mirror the DateTime(timezone=True)
#     + server_default=func.now() pattern used throughout this schema.
provider_configs = Table(
    "provider_configs",
    metadata,
    Column("id", _AUTO_PK_TYPE, primary_key=True),
    # Short unique slug: 'openai', 'openrouter', 'groq', or custom.
    Column("provider", String(64), nullable=False, unique=True),
    Column("base_url", Text, nullable=False),
    # enc$v1$ envelope; kind="provider_api_key", record_id=id.
    # NULL when the provider requires no auth (e.g. local custom server).
    Column("api_key_enc", Text, nullable=True),
    Column("default_model", Text, nullable=True),
    # JSON list[str] — explicit model id allowlist for the picker.
    # NULL or [] means all models from this provider are visible.
    # Non-empty list: only those model ids appear in /api/models for this provider.
    # Governs the PICKER only; dispatch is not blocked (the id is still valid upstream).
    Column("allowed_models", JSON, nullable=True),
    # JSON dict: e.g. {"HTTP-Referer": "...", "X-Title": "..."} for OpenRouter.
    Column("extra_headers", JSON, nullable=True),
    Column(
        "enabled",
        Boolean,
        nullable=False,
        server_default=text("true"),
    ),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Index("ix_provider_configs_provider", "provider"),
)

# ---------------------------------------------------------------------------
# mcp_servers — MCP Store.
#
# One row per installed MCP server (from the curated catalog or BYO).
# Design notes:
#   - ``slug`` mirrors the catalog ``id`` for curated entries (e.g. "github")
#     and is user-supplied for BYO entries.  Unique constraint enforces
#     single-install per server.
#   - ``secrets_enc`` stores a JSON list of ``{"key": str, "enc_value": str}``
#     pairs.  Each secret value is independently encrypted via the
#     ``enc$v1$`` envelope (kind="mcp_server_secret", record_id=row.id).
#   - ``enabled`` / ``consented`` are separate gates: enabled controls UI
#     visibility; consented is the explicit admin consent gate before the
#     backend spawns the child process (security gate).
#   - ``source`` / ``trust`` mirror catalog metadata for provenance tracking.
mcp_servers = Table(
    "mcp_servers",
    metadata,
    Column("id", _AUTO_PK_TYPE, primary_key=True),
    Column("slug", String(128), nullable=False, unique=True),
    Column("name", String(256), nullable=False),
    Column("transport", String(16), nullable=False),
    Column("command", Text, nullable=True),
    Column("args", JSON, nullable=True),
    Column("url", Text, nullable=True),
    # Encrypted secrets: list[{"key": str, "enc_value": str}]
    Column("secrets_enc", JSON, nullable=True),
    Column(
        "enabled",
        Boolean,
        nullable=False,
        server_default=text("true"),
    ),
    Column(
        "source",
        String(32),
        nullable=False,
        server_default=text("'byo'"),
    ),
    Column(
        "trust",
        String(32),
        nullable=False,
        server_default=text("'byo'"),
    ),
    Column(
        "consented",
        Boolean,
        nullable=False,
        server_default=text("false"),
    ),
    # Per-tool denylist: list[str] of namespaced tool names that are blocked
    # when this server is active in the agentic loop.  null / empty ⇒ all
    # tools from this server are advertised.  Security enforcement.
    Column("tool_policy", JSON, nullable=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Index("ix_mcp_servers_slug", "slug"),
)
