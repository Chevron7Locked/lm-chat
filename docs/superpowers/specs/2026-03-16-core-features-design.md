# lm-chat Core Features Design Spec
**Date:** 2026-03-16
**Features:** Per-Chat Settings · Message UI · Response Feedback · Message Pinning
**Status:** Approved

---

## 0. Context

lm-chat is a two-file (server.py + index.html/app.js/style.css) LM Studio companion UI. AGPL-3.0, stdlib Python, SQLite, zero external dependencies. Target users are self-hosters running multiple local models simultaneously. All features must work with LM Studio's native `/api/v1/chat` endpoint only — no OpenAI compat layer.

---

## 1. Per-Chat Settings (3rd Column)

### Problem
The app currently stores all inference parameters (temperature, top_p, etc.) as global localStorage values. Users running multiple simultaneous chats with different models (e.g., Qwen3.5 for brainstorming, Mistral for coding) cannot give each chat its own settings. This capability existed in 0.1.0 and regressed.

### Design

**Settings layering (first non-null wins, per parameter):**
```
chat DB settings → global localStorage → LM Studio instance config
```

**What can be overridden per chat:**
- System prompt
- Temperature, top_p, top_k, min_p, repeat_penalty, presence_penalty, max_output_tokens
- Reasoning mode (off / medium / high)
- SC enabled (bool)
- CoVe enabled (bool)

**DB change:** Add `settings TEXT` JSON column to `chats` table via migration.
```sql
ALTER TABLE chats ADD COLUMN settings TEXT;
-- NULL = no overrides, use global defaults
```

**API:**
- `GET /api/chats/:id/settings` → returns parsed JSON or `{}`
- `PATCH /api/chats/:id/settings` → merges provided keys into the existing JSON blob (partial update)
  - To remove a single key without resetting all settings, send `{key: null}` in the PATCH body. The server removes any key whose value is explicitly `null` before merging. Unknown keys with `null` value are silently ignored (nothing to remove). Unknown keys with non-null value are rejected (allowlist violation).
  - Server-side merge sequence: (1) read existing JSON from DB or `{}`; (2) for each key in body: if `null`, delete from dict; else validate and set; (3) write result back. If the result is an empty dict, write `NULL` (not `'{}'`) so the chat appears as having no overrides — equivalent to calling `DELETE`.
  - An empty `PATCH {}` is a no-op.
- `DELETE /api/chats/:id/settings` → sets `chats.settings = NULL` (full reset to global defaults)
  - The "Reset to defaults" button calls `DELETE`, not `PATCH {}`

**Route patterns (matching existing server.py routing conventions):**
```
r'^/api/chats/([^/]+)/settings$'  →  GET, PATCH, DELETE
```

**Settings allowlist (server-side validation — reject unknown keys, enforce types and ranges):**
| Key | Type | Valid range |
|-----|------|-------------|
| `system_prompt` | str | max 8000 chars |
| `temperature` | float | 0.0 – 2.0 |
| `top_p` | float | 0.0 – 1.0 |
| `top_k` | int | 0 – 500 |
| `min_p` | float | 0.0 – 1.0 |
| `repeat_penalty` | float | 0.0 – 3.0 |
| `presence_penalty` | float | 0.0 – 2.0 |
| `max_output_tokens` | int | -1 or 1 – 32768 |
| `reasoning` | str | `"off"`, `"medium"`, `"high"` |
| `sc_enabled` | bool | true/false |
| `cove_enabled` | bool | true/false |

**Request builder layering — `_resolve_chat_settings(chat_id, user_id, body)`:**
A named helper called at the start of both `_handle_chat` and `_handle_chat_stream` (before `_build_lmstudio_payload`). It merges settings in order, mutating `body` in place:

```python
def _resolve_chat_settings(self, chat_id, user_id, body):
    """Merge per-chat DB settings into the request body as fallback values.
    Layering (first non-null wins, per parameter):
      client body → chat DB settings → global localStorage defaults → LM Studio instance config
    DB settings only fill keys that the client body omitted or sent as None.
    LM Studio instance config is read-only and managed by the frontend via syncModelSettings().
    """
    if not chat_id:
        return
    db = get_db()
    row = db.execute(
        "SELECT settings FROM chats WHERE id = ? AND user_id = ?",
        (chat_id, user_id)
    ).fetchone()
    if not row or not row[0]:
        return
    try:
        chat_settings = json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return
    # Only apply keys the client has NOT already specified (client body takes priority)
    for key, value in chat_settings.items():
        if key not in body or body[key] is None:
            body[key] = value
```

Note: LM Studio instance config (context_length, flash_attention, etc.) is already read by the frontend via `syncModelSettings()` and stored in localStorage/passed in the request body. No live round-trip to `/api/v1/models` is needed per request.

**UI — 3rd Column Panel:**
- Toggled by a sliders/config icon (⚙ or ≡) in the topbar, right side
- Opens as a fixed-width right panel (~280px), does not push chat content — overlays with a semi-transparent backdrop on mobile, sits alongside on desktop
- Panel header: "Chat Settings" + "Reset to defaults" button (clears `chats.settings`, reverts to global)
- Visual indicator: small accent dot on the toggle button when the chat has any active overrides
- Toggle open/closed state: localStorage only (UI state, not persisted to DB)

**Panel sections:**
1. **System Prompt** — textarea (same as global settings but per-chat)
2. **Inference** — temperature, top_p, top_k, min_p, repeat_penalty, presence_penalty, max_tokens sliders/inputs
3. **Enhancements** — SC toggle, CoVe toggle, reasoning mode selector
4. All controls show the global default as placeholder when no chat override is set

**Frontend:** On chat load (`loadChat()`), fetch chat settings and populate the 3rd column panel. On any change in the panel, debounce 400ms then `PATCH /api/chats/:id/settings` with the changed key (prevents hammering the server on slider drag). On "Reset to defaults", call `DELETE /api/chats/:id/settings`.

### Security
- Server validates user owns the chat before reading/writing settings (`WHERE id = ? AND user_id = ?`)
- Settings JSON is sanitized: only known keys accepted, values type-checked server-side
- No arbitrary key injection

---

## 2. Message Bottom Row (Shared UI Foundation)

### Design

Every message gets a persistent bottom row — always visible, not hover-dependent.

**Assistant message row:**
```
[128 tokens · 41 tok/s · TTFT 0.6s]    [copy] [fork] [regen] [thumb-up] [thumb-down]
```

**User message row:**
```
                                        [copy] [fork] [edit]
```

**Specs:**
- Icons: SVG, not emoji. `--dim` (#8494A7) default color.
- Thumbs-up voted: `--accent` (#C084FC) fill
- Thumbs-down voted: `rgba(239, 68, 68, 0.8)` fill
- Voted state persists in DB (loaded with message data on chat load)
- Clicking a voted icon a second time removes the vote (toggle off)
- Clicking the opposite icon switches the vote
- Row sits below message content with `margin-top: 6px`, `padding-top: 6px`, `border-top: 1px solid var(--border)`

**Pin indicator (always-visible when pinned):**
- Small thumbtack SVG icon appears at the end of the action row when a message is pinned
- `--accent` color, filled. Click opens the in-chat pin navigator.
- Pin *action* (to pin an unpinned message) appears on hover only — appended to the row on `mouseenter`, removed on `mouseleave` (unless already pinned)
- **Mobile/touch:** No hover state on touch devices. The pin action appears in the message long-press context menu as "Pin this response." Swipe-right (leading) on a message also reveals the pin action (iOS/Android convention for positive actions).

**Existing behavior preserved:**
- Token stats already rendered by `addMsgStats()` — moved from current position into this row
- Copy, fork, regenerate already exist — consolidated into this row
- Edit (user messages) already exists — consolidated into this row

---

## 3. Response Feedback

### Problem
Thumbs up/down on assistant messages provides explicit signal to the adaptive memory system. Good responses reinforce the insights that were active; bad responses penalize them. This closes the loop between model output quality and memory curation.

### DB Schema

```sql
-- Tracks which insights were injected for each response
CREATE TABLE IF NOT EXISTS insight_activations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id  INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    insight_id  TEXT    NOT NULL REFERENCES user_insights(id) ON DELETE CASCADE,
    created_at  REAL    NOT NULL
);
CREATE INDEX idx_activations_message ON insight_activations(message_id);
CREATE INDEX idx_activations_insight ON insight_activations(insight_id);

-- Explicit user feedback on responses
CREATE TABLE IF NOT EXISTS message_feedback (
    message_id  INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    user_id     TEXT    NOT NULL REFERENCES users(id)    ON DELETE CASCADE,
    rating      INTEGER NOT NULL CHECK(rating IN (-1, 1)),  -- 0 = remove-vote, handled at app layer (never persisted)
    created_at  REAL    NOT NULL,
    PRIMARY KEY (message_id, user_id)
);

-- Add feedback columns to user_insights for scoring
-- (via migration)
ALTER TABLE user_insights ADD COLUMN ups   REAL DEFAULT 0;
ALTER TABLE user_insights ADD COLUMN downs REAL DEFAULT 0;
ALTER TABLE user_insights ADD COLUMN last_feedback_at REAL;
```

### API
- `POST /api/messages/:id/feedback` — body: `{rating: 1}` or `{rating: -1}` or `{rating: 0}` (0 = remove)
- Rating is upserted (one vote per user per message)
- Response: `{ok: true, rating: <current>}`
- No GET endpoint needed — feedback state loaded as part of message data (`GET /api/chats/:id/messages` includes `feedback` field per message)

### Algorithm

**SQLite `exp()` registration:** Standard SQLite does not include `exp()` as a built-in. `server.py` already registers `ln` via `create_function` (line 240). Add alongside it:
```python
import math
db.create_function("exp", 1, math.exp)
```
This goes in `get_db()` with the existing `ln` registration.

**Scoring formula (computed at injection time, pure SQL):**
```sql
-- Bayesian Laplace with 90-day exponential decay
-- Requires exp() registered as Python function (see above)
SELECT id, content, category,
  MAX(
    CAST(ups + 1 AS REAL) / (ups + downs + 2)
    * CASE
        WHEN last_feedback_at IS NULL THEN 1.0
        ELSE exp(-0.693 * (julianday('now') - julianday(last_feedback_at)) / 90.0)
      END,
    0.1  -- floor: no insight is permanently silenced
  ) AS feedback_score
FROM user_insights
WHERE user_id = ? AND state = 'active'
ORDER BY feedback_score * weight DESC
LIMIT ?;
```

**On feedback write (`_apply_feedback`):**

The entire operation runs inside a single `BEGIN IMMEDIATE` transaction to prevent race conditions on rating reversal (concurrent requests for the same message_id/user_id could otherwise double-subtract).

```python
def _apply_feedback(self, db, message_id, user_id, rating):
    """Upsert feedback and adjust insight weights atomically."""
    # rating: 1 (up), -1 (down), 0 (remove)
    # Use explicit BEGIN IMMEDIATE — Python's `with conn:` only issues a deferred BEGIN
    # and cannot prevent write-lock races. IMMEDIATE acquires the write lock upfront.
    try:
        db.execute("BEGIN IMMEDIATE")
    except Exception:
        db.execute("ROLLBACK")
        raise
    try:
        # Verify user owns the message's chat
        ok = db.execute("""
            SELECT 1 FROM messages m
            JOIN chats c ON m.chat_id = c.id
            WHERE m.id = ? AND c.user_id = ?
        """, (message_id, user_id)).fetchone()
        if not ok:
            db.execute("ROLLBACK")
            return None  # 403

        # Read previous rating before modifying
        prev = db.execute(
            "SELECT rating FROM message_feedback WHERE message_id = ? AND user_id = ?",
            (message_id, user_id)
        ).fetchone()
        prev_rating = prev[0] if prev else 0

        # Upsert or delete feedback record
        if rating == 0:
            db.execute(
                "DELETE FROM message_feedback WHERE message_id = ? AND user_id = ?",
                (message_id, user_id)
            )
        else:
            db.execute("""
                INSERT INTO message_feedback (message_id, user_id, rating, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(message_id, user_id) DO UPDATE SET rating=excluded.rating, created_at=excluded.created_at
            """, (message_id, user_id, rating, time.time()))

        # Adjust insight weights: delta = new_vote - old_vote (handles reversal).
        # 0.5 weight per vote (not 1.0): feedback quality correlates with but isn't solely
        # caused by insight content — model quality, prompt phrasing, and context all
        # contribute. A fractional nudge prevents over-penalizing insights that were
        # relevant but co-occurred with an unrelated model error.
        delta = rating - prev_rating  # e.g. +1→-1 gives delta=-2, apply as -1 up +1 down
        if delta != 0:
            activations = db.execute(
                "SELECT insight_id FROM insight_activations WHERE message_id = ?",
                (message_id,)
            ).fetchall()
            for (insight_id,) in activations:
                if delta > 0:
                    db.execute("UPDATE user_insights SET ups = MAX(ups + 0.5, 0), last_feedback_at = ? WHERE id = ?",
                               (time.time(), insight_id))
                else:
                    db.execute("UPDATE user_insights SET downs = MAX(downs + 0.5, 0), last_feedback_at = ? WHERE id = ?",
                               (time.time(), insight_id))
        db.execute("COMMIT")
        return rating
    except Exception:
        db.execute("ROLLBACK")
        raise
```

**Insight activation tracking:**
`_format_insights_for_prompt(insights)` accepts the `insights` list from `_get_top_insights()` and returns a formatted **string** — it does not return IDs. The IDs must be collected at the call site. `_get_top_insights` takes `(self, db, user_id, ...)` — `db` is required as the first argument after `self` (matches the existing call at line 1329: `self._get_top_insights(mem_db, user["id"], limit=max_inject)`):
```python
# In _handle_chat_stream() / _handle_chat(), before calling _format_insights_for_prompt():
insights = self._get_top_insights(mem_db, user_id, limit=5)  # pass db (mem_db) explicitly
injected_insight_ids = [row["id"] for row in insights]  # collect IDs before formatting
system_prompt_section = self._format_insights_for_prompt(insights)
```

After the response is persisted, record the activations. `_persist_chat_messages` must accept a new `injected_insight_ids=None` parameter and use `lastrowid` to capture the assistant message's integer primary key:
```python
# In _persist_chat_messages(), after assistant message INSERT:
assistant_message_id = db.execute(
    "INSERT INTO messages (chat_id, role, content, ...) VALUES (?, ?, ?, ...)",
    (...)
).lastrowid
if injected_insight_ids:
    db.executemany(
        "INSERT INTO insight_activations (message_id, insight_id, created_at) VALUES (?,?,?)",
        [(assistant_message_id, iid, time.time()) for iid in injected_insight_ids]
    )
```

**Scoring path note:** The `_get_top_insights()` function currently applies `CATEGORY_WEIGHTS` in Python after the SQL query. The feedback scoring query above uses `ORDER BY feedback_score * weight DESC`, treating `weight` as a SQL column. The `weight` column on `user_insights` is the base category weight stored at insight creation time. These two paths are consistent as long as `weight` in the DB matches the Python `CATEGORY_WEIGHTS` constants — which it should, since `weight` is written from `CATEGORY_WEIGHTS` at insight creation. No reconciliation needed, but implementers should verify `weight` is always initialized on INSERT.

**Cold start:** New insight with `ups=0, downs=0` scores `1/2 = 0.5` — competes in the middle of the distribution. No special handling needed.

**Decay floor:** Insights with no feedback never decay (decay multiplier = 1.0 when `last_feedback_at IS NULL`). Only insights that have received feedback are subject to decay.

### UI
- Thumbs icons in the message bottom row (see Section 2)
- On click: optimistic UI update (icon turns colored immediately), then `POST /api/messages/:id/feedback`
- On error: revert icon state, show brief error toast
- Vote state loaded from `messages` response on chat load — no separate fetch

### Security
- `POST /api/messages/:id/feedback` verifies the message belongs to a chat owned by the requesting user (see `_apply_feedback` — ownership check inside transaction)
- Rating value is validated server-side: must be `1`, `-1`, or `0` — no other values accepted
- One vote per user per message enforced by `PRIMARY KEY (message_id, user_id)`
- Insight weight adjustments are bounded: `MAX(value + delta, 0)` prevents negative weights
- No feedback data is exposed to other users — all queries are scoped to `user_id`

---

## 4. Message Pinning

### Problem
Valuable assistant responses are lost to compaction and long conversation history. Users need to save specific responses, retrieve them across sessions, and navigate to them within a conversation.

### DB Schema

```sql
CREATE TABLE IF NOT EXISTS pins (
    id          TEXT    PRIMARY KEY,    -- UUID
    user_id     TEXT    NOT NULL REFERENCES users(id)     ON DELETE CASCADE,
    message_id  INTEGER          REFERENCES messages(id)  ON DELETE SET NULL,
    chat_id     TEXT             REFERENCES chats(id)     ON DELETE SET NULL,
    chat_title  TEXT    NOT NULL,       -- snapshot at pin time
    content     TEXT    NOT NULL,       -- full message text copy at pin time
    pin_title   TEXT,                   -- 5-7 word LLM-generated nav label
    pinned_at   REAL    NOT NULL
);
CREATE INDEX idx_pins_user     ON pins(user_id, pinned_at DESC);
CREATE INDEX idx_pins_chat     ON pins(user_id, chat_id);
CREATE INDEX idx_pins_message  ON pins(message_id);

-- FTS5 for full-text search (integrated with existing /api/search)
-- Note: pins.id is a TEXT PRIMARY KEY (UUID). TEXT PKs do NOT alias the implicit integer
-- rowid in SQLite. The FTS content_rowid='rowid' refers to the integer rowid, not the UUID.
-- FTS search results return integer rowids; to fetch the full pin record, JOIN on:
--   SELECT p.* FROM pins p
--   JOIN (SELECT rowid FROM pins_fts WHERE pins_fts MATCH ?) fts ON p.rowid = fts.rowid
CREATE VIRTUAL TABLE IF NOT EXISTS pins_fts USING fts5(
    content,
    chat_title,
    pin_title,
    content='pins',
    content_rowid='rowid'
);
-- Triggers to keep FTS in sync.
-- pin_title may be NULL initially (filled asynchronously by title generation).
-- FTS5 handles NULL values as empty strings — no special handling needed.
CREATE TRIGGER pins_ai AFTER INSERT ON pins BEGIN
    INSERT INTO pins_fts(rowid, content, chat_title, pin_title)
    VALUES (new.rowid, new.content, new.chat_title, new.pin_title);
END;
CREATE TRIGGER pins_ad AFTER DELETE ON pins BEGIN
    INSERT INTO pins_fts(pins_fts, rowid, content, chat_title, pin_title)
    VALUES ('delete', old.rowid, old.content, old.chat_title, old.pin_title);
END;
CREATE TRIGGER pins_au AFTER UPDATE ON pins BEGIN
    INSERT INTO pins_fts(pins_fts, rowid, content, chat_title, pin_title)
    VALUES ('delete', old.rowid, old.content, old.chat_title, old.pin_title);
    INSERT INTO pins_fts(rowid, content, chat_title, pin_title)
    VALUES (new.rowid, new.content, new.chat_title, new.pin_title);
END;
```

### API
- `POST /api/messages/:id/pin` → creates pin, fires async title generation, returns pin record — only assistant-role messages may be pinned (server rejects user-role messages with 400)
- `DELETE /api/pins/:id` → removes pin (user must own it)
- `GET /api/pins` → all pins for current user, newest first, includes `chat_title`, `pin_title`, `pinned_at`
- `GET /api/chats/:id/pins` → pins for a specific chat (for in-chat navigator), returns `[{id, pin_title, message_id, pinned_at}]` — query must include `WHERE user_id = ? AND chat_id = ?` to prevent cross-user access
- `PATCH /api/pins/:id/title` → updates `pin_title` after async LLM generation — server validates `WHERE id = ? AND user_id = ?` before writing; title capped at 80 chars

### Pin Title Generation
Fire-and-forget after pin creation. Uses the model that was active for that chat:
```python
# Non-blocking, separate thread — launched via threading.Thread(target=..., daemon=True)
def _generate_pin_title(self, pin_id, content, user_id, chat_id):
    """Must open its own DB connection (thread-local via get_db()).
    Never capture the parent connection — SQLite connections are not thread-safe."""
    db = get_db()  # thread-local connection
    row = db.execute("SELECT model FROM chats WHERE id = ?", (chat_id,)).fetchone()
    model = row[0] if row else None
    if not model:
        return  # no model available, skip silently
    prompt = f"Summarize this in 5-7 words as a short navigation label. No punctuation.\n\n{content[:500]}"
    payload = {
        "model": model,
        "input": prompt,
        "max_output_tokens": 20,
        "temperature": 0.3,
        "store": False,
        "integrations": []
    }
    try:
        data = self._lmstudio_chat(payload, user_id, timeout=15)
        title = self._extract_content(data).strip()[:80]
        if title:
            db.execute("UPDATE pins SET pin_title = ? WHERE id = ?", (title, pin_id))
            db.commit()
    except Exception as e:
        log.warning(f"Pin title generation failed for pin {pin_id}: {e}")
```

### Persistence Semantics
- Pin stores a **content copy** — survives chat deletion and message compaction
- `message_id` and `chat_id` are nullable FKs with `ON DELETE SET NULL` — "Jump to source" degrades gracefully
- `chat_title` is snapshotted at pin time — remains accurate even if the chat is renamed/deleted
- **`_compact_chat` must be modified** to skip pinned messages — this is a required change, not optional. Pinned messages must survive compaction even when they fall outside the retention window. Uses `db.execute()` (no cursor variable — matches existing `_compact_chat` style). The existing local variable is `db_messages` (not `messages`):
  ```python
  # After loading db_messages, before computing half:
  pinned_ids = {r[0] for r in db.execute(
      "SELECT message_id FROM pins WHERE chat_id = ? AND message_id IS NOT NULL", (chat_id,)
  )}
  # Filter before computing half so the deletion count is based on compactable messages only.
  # This prevents pinned messages from reducing the effective compaction window.
  db_messages = [m for m in db_messages if m['id'] not in pinned_ids]
  half = len(db_messages) // 2
  ids_to_delete = [m["id"] for m in db_messages[:half]]
  ```

### Search Integration
Pins surface in the existing `/api/search` endpoint alongside chat messages. Both text search (FTS5) and semantic search (embeddings, if available) cover pin content. No separate search UI needed.

### UI

**Pin action (hover):**
- Thumbtack SVG icon appended to the message bottom row on `mouseenter`
- Removed on `mouseleave` — unless the message is already pinned (then always visible)
- Click → `POST /api/messages/:id/pin` → icon transitions to filled/accent state permanently

**Pin state indicator (always visible when pinned):**
- Filled thumbtack in `--accent` color at the end of the action row
- Click → opens in-chat pin navigator (scrolls navigator into view / expands it)

**Global Pins Panel:**
- Accessible from: sidebar section entry ("Pinned") + topbar thumbtack icon
- Both open the same right-side drawer panel (or sidebar section, on mobile: bottom sheet)
- Lists all pins across all chats: `pin_title` or truncated content, `chat_title`, `pinned_at` date
- Click → navigates to source chat + scrolls to message (if available), or shows content in place if source is gone
- Pins surface in the main search bar — no duplicate search UI in the panel

**In-Chat Pin Navigator:**
- Compact sticky widget, rendered when `GET /api/chats/:id/pins` returns ≥1 result
- Position: collapsible section above the input box, OR a small floating pill anchored to the topbar
- Shows entries as: `📌 [pin_title]` — click smooth-scrolls to `message_id`
- While `pin_title` is still generating: shows first 40 chars of content as fallback
- Entries listed in message order (top of conversation to bottom), not pin-time order

### Security
- Pins are strictly per-user — all queries include `WHERE user_id = ?`
- `POST /api/messages/:id/pin` verifies the user owns the source message's chat before creating the pin
- `DELETE /api/pins/:id` verifies the user owns the pin
- No pin sharing — pins are private annotations

---

## 5. Shared Implementation Notes

### Migration Strategy
All schema changes use `ALTER TABLE ... ADD COLUMN` with `try/except` (existing pattern in `init_db()`). New tables use `CREATE TABLE IF NOT EXISTS`. No destructive migrations.

### KNOWN_TABLES Update (server.py)
`server.py` has a `KNOWN_TABLES` local variable used inside the first-run auth migration path (the logic that migrates existing data from the `default` user when auth is first enabled). It is not a module-level constant or general schema validator — it only matters when enabling auth on an existing single-user database.

`message_feedback` has a `user_id` column and should be added to `KNOWN_TABLES` so it is included in the default-user data migration:
```python
KNOWN_TABLES = {
    ...,
    "message_feedback",
    # insight_activations has no user_id column — no migration needed
    # pins has user_id column — must be included for auth migration completeness
    "pins",
    # pins_fts is a virtual table with no user_id — exclude
}
```

### Right-Panel Conflict Resolution
Two features share the right-side panel space: **Per-Chat Settings** (3rd column) and **Global Pins Panel** (right drawer). These cannot be open simultaneously. Resolution:
- The per-chat settings panel and the global pins panel are mutually exclusive: opening one closes the other.
- They use a shared `rightPanelState` variable (`null` | `"settings"` | `"pins"`) to track which is open.
- Both panels use the same DOM slot (a `<div id="right-panel">` whose content and header are swapped on open); this ensures they never render simultaneously and avoids z-index conflicts.

### Version Bump
These features together constitute v0.3.0:
- Per-chat settings: minor feature addition
- Message UI refactor: visible change to all users
- Response feedback: new feature
- Message pinning: new feature

### Feature Checklist Updates
The following checklist entries need updating:
- `[-] Presence Penalty` → `[x] Presence Penalty` (verified working on native API)
- Add new sections for Feedback, Pinning, Per-Chat Settings

---

## Out of Scope (This Spec)
- Self-Consistency and CoVe (separate spec: `2026-03-16-inference-enhancements-design.md`)
- Pin tags / labels (Phase 2, Telegram-validated pattern)
- Implicit feedback signals (copy events, regeneration as fractional votes) — explicit thumbs only
- Pin sharing between users
- Feedback analytics dashboard
- A/B regeneration comparison (would enable Elo-based ranking — future)
