# SPDX-License-Identifier: Apache-2.0
"""Hybrid compaction: add ``compactions`` table + ``messages.compaction_id``.

Replaces the old head-trim-and-DELETE ``/compact`` behavior with
summarize-and-archive (PLAN `docs/phases/hybrid-compaction/PLAN.md` v3).

New table
---------
``compactions`` — one row per ``/compact`` call that archived something:

    id                    PK.
    chat_id               FK -> chats.id, ON DELETE CASCADE (deleting the
                          chat removes its compaction spans too).
    summary               The LLM-generated running summary of the archived
                          span. NOT NULL — a compaction row is only ever
                          inserted alongside a successful summary (fail
                          policy = abort; see PLAN §4.1 / §9 Q1).
    summary_model_id      Model that produced the summary; nullable.
    anchor_msg_id         The id the FE renders the collapsed tab *before*
                          (the oldest archived id at archive time) — display
                          position only, no FK (see schema.py comment).
    original_token_count  Token count of the archived span BEFORE
                          summarization (for the FE's "~X -> ~Y tok" hint).
    summary_token_count   Token count of the generated summary.
    created_at            Row creation timestamp.

Column addition
----------------
``messages.compaction_id`` — nullable FK -> compactions.id, ON DELETE SET
NULL. NULL means the row is active (in the live context window); a non-NULL
value means the row has been archived into that compaction's span. Existing
rows get NULL (no backfill; locked decision 3 pattern) — every message ever
written before this migration is implicitly "active", which is correct: no
compaction event happened to them.

Why NOT ``batch_alter_table`` on SQLite
---------------------------------------
On SQLite, ``batch_alter_table`` performs a copy-and-move table
RE-CREATION. The ``messages`` table carries the FTS5 sync triggers
(``messages_ai`` / ``messages_au`` / ``messages_ad``, created by migration
0002) — and re-creating the table SILENTLY DROPS those triggers, which
leaves ``messages_fts`` unpopulated and breaks message search entirely.
So on SQLite we add the column with a plain ``ALTER TABLE ADD COLUMN``
(nullable, NULL default) which does NOT re-create the table and therefore
leaves the triggers intact. SQLite's ``ALTER TABLE ADD COLUMN`` cannot add
an inline FOREIGN KEY (alembic raises "No support for ALTER of constraints
in SQLite dialect"), so the FK is declared only in ``db/schema.py`` (the
SSOT used by ``metadata.create_all`` + the fingerprint) and, for real
referential integrity, added inline on Postgres. The app never relies on
the SET NULL cascade for correctness: ``chat_service.delete`` cascades
chats -> messages and chats -> compactions independently, and
``chat_service.clear_messages`` deletes both explicitly.

Rows are never deleted by compaction. ``message_embeddings`` are untouched
(no cascade), so retained embeddings keep participating in semantic recall
per ADR-008.

Reversibility
-------------
``downgrade()`` drops the ``ix_messages_compaction_id`` index and the
``messages.compaction_id`` column (plain ``ALTER TABLE DROP COLUMN`` — no
table re-creation, so the FTS triggers survive here too), then drops the
``compactions`` table. Any archived-vs-active distinction on ``messages``
is lost on downgrade — the rows themselves are never destroyed by either
direction.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0037"
down_revision: str = "0036"
branch_labels: str | None = None
depends_on: str | None = None

# Portable autoincrement PK — mirrors schema.py's _AUTO_PK_TYPE.
_PK_TYPE = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    """Create ``compactions`` then add ``messages.compaction_id``."""
    op.create_table(
        "compactions",
        sa.Column("id", _PK_TYPE, primary_key=True),
        sa.Column(
            "chat_id",
            sa.BigInteger(),
            sa.ForeignKey("chats.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("summary_model_id", sa.Text(), nullable=True),
        sa.Column("anchor_msg_id", sa.BigInteger(), nullable=False),
        sa.Column("original_token_count", sa.Integer(), nullable=False),
        sa.Column("summary_token_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_compactions_chat_id", "compactions", ["chat_id"])

    bind = op.get_bind()
    dialect = bind.dialect.name  # type: ignore[attr-defined]

    if dialect == "sqlite":
        # Plain ADD COLUMN — does NOT re-create the table, so the FTS5
        # triggers on `messages` survive (batch_alter_table would drop
        # them; see the module docstring). No inline FK on SQLite — it's
        # declared in db/schema.py and enforced on Postgres.
        op.add_column(
            "messages",
            sa.Column("compaction_id", sa.BigInteger(), nullable=True),
        )
    else:
        op.add_column(
            "messages",
            sa.Column(
                "compaction_id",
                sa.BigInteger(),
                sa.ForeignKey(
                    "compactions.id",
                    ondelete="SET NULL",
                    name="fk_messages_compaction_id",
                ),
                nullable=True,
            ),
        )
    op.create_index("ix_messages_compaction_id", "messages", ["compaction_id"])


def downgrade() -> None:
    """Drop ``messages.compaction_id`` then drop ``compactions``."""
    # Plain drop (no batch_alter_table) so the FTS5 triggers on `messages`
    # are preserved here too. On Postgres, dropping the column drops its FK
    # automatically.
    op.drop_index("ix_messages_compaction_id", table_name="messages")
    op.drop_column("messages", "compaction_id")

    op.drop_index("ix_compactions_chat_id", table_name="compactions")
    op.drop_table("compactions")
