/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Memory panel — view and manage pinned insights.
 *
 * Consumes the memory API:
 *   GET    /api/memory/pins       — list
 *   POST   /api/memory/pin        — add
 *   DELETE /api/memory/pin/{id}   — remove
 *   POST   /api/memory/reindex    — admin only
 *
 * Shows a reindex control only for admin users.
 *
 * Inline CSSProperties replaced with reading-rooms.css semantic classes.
 */
import { useEffect, useState } from "react";
import type { SyntheticEvent } from "react";
import { X, Pencil } from "lucide-react";
import { AppShell } from "@/components/AppShell";
import { useDocumentTitle } from "@/hooks/useDocumentTitle";
import {
  useMemoryPins,
  useAutoMemories,
  usePinInsight,
  useUnpinInsight,
  useMemoryReindex,
  useEditInsight,
  useRefineMemory,
  useRestoreMemory,
} from "@/hooks/useMemory";
import type { MemoryInsight } from "@/hooks/useMemory";
import { useAuthStore } from "@/stores/authStore";
import { useModels } from "@/hooks/useModels";
import { useEmbeddingStatus } from "@/hooks/useEmbeddingStatus";
import { useToast } from "@/stores/toastStore";
import { dedupeByKey } from "@/lib/dedupeByKey";
import "@/styles/reading-rooms.css";

// ─── Component ──────────────────────────────────────────────────────────────

export interface MemoryProps {
  /**
   * Right-rail mount (Chat.tsx RightPanel Drawer, ~380px). Skips the
   * AppShell wrapper — the page already lives inside Chat's <main>, and a
   * nested AppShell rendered a duplicate `<main id="main-content">`
   * landmark inside the dialog. Also drops the page header (the Drawer
   * banner already says "Memory") and applies compact rail padding.
   */
  inRail?: boolean;
}

export default function Memory({ inRail = false }: MemoryProps) {
  useDocumentTitle("Memory");
  const { data, isLoading, isError } = useMemoryPins();
  const { data: autoData } = useAutoMemories();
  const pin = usePinInsight();
  const unpin = useUnpinInsight();
  const reindex = useMemoryReindex();
  const editInsight = useEditInsight();
  const refineMemory = useRefineMemory();
  const restoreMemory = useRestoreMemory();
  const { data: modelsData } = useModels();
  const { data: embeddingStatus } = useEmbeddingStatus();
  const { user } = useAuthStore();
  const { push } = useToast();

  const [newText, setNewText] = useState("");
  // Reindex picker — initialized empty AND tracked here so we know if
  // the user explicitly chose something. We auto-default to the
  // currently-active embedding model below; without that, the dropdown
  // started blank every visit even when LM Studio had an embedding
  // model loaded, which read as "lost my setting".
  const [reindexModel, setReindexModel] = useState("");
  const [reindexModelTouched, setReindexModelTouched] = useState(false);
  // Auto-fill the picker with the active embedding model the FIRST
  // time it shows up. Subsequent user choices win (touched flag).
  useEffect(() => {
    if (reindexModelTouched) return;
    const active = embeddingStatus?.active_model_id;
    if (
      active !== undefined &&
      active !== null &&
      active !== "" &&
      reindexModel === ""
    ) {
      setReindexModel(active);
    }
  }, [embeddingStatus?.active_model_id, reindexModel, reindexModelTouched]);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editDraft, setEditDraft] = useState("");
  const [refineConfirmOpen, setRefineConfirmOpen] = useState(false);
  const [lastRefineHistoryId, setLastRefineHistoryId] = useState<number | null>(
    null,
  );
  // Adds search-on-list and per-row expansion.
  const [searchQuery, setSearchQuery] = useState("");
  const [expandedIds, setExpandedIds] = useState<Set<number>>(new Set());
  const toggleExpanded = (id: number): void => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  };

  async function handlePin(e: SyntheticEvent<HTMLFormElement>): Promise<void> {
    e.preventDefault();
    const text = newText.trim();
    if (text === "") return;
    try {
      await pin.mutateAsync({ text });
      setNewText("");
      push({ variant: "success", message: "Insight pinned." });
    } catch {
      push({
        variant: "error",
        message: "Couldn't pin that insight — try again.",
      });
    }
  }

  async function handleUnpin(id: number): Promise<void> {
    try {
      await unpin.mutateAsync(id);
      push({ variant: "info", message: "Insight removed." });
    } catch {
      push({
        variant: "error",
        message: "Couldn't remove that insight — try again.",
      });
    }
  }

  function beginEdit(p: MemoryInsight): void {
    setEditingId(p.id);
    setEditDraft(p.text);
  }
  function cancelEdit(): void {
    setEditingId(null);
    setEditDraft("");
  }
  async function saveEdit(id: number): Promise<void> {
    const trimmed = editDraft.trim();
    if (trimmed === "") {
      push({ variant: "error", message: "Insight can't be empty." });
      return;
    }
    try {
      await editInsight.mutateAsync({ id, content: trimmed });
      setEditingId(null);
      setEditDraft("");
      push({ variant: "success", message: "Insight updated." });
    } catch {
      push({
        variant: "error",
        message: "Couldn't update that insight — try again.",
      });
    }
  }

  async function handleRefineConfirm(): Promise<void> {
    setRefineConfirmOpen(false);
    try {
      const resp = await refineMemory.mutateAsync();
      setLastRefineHistoryId(resp.history_id);
      push({
        variant: "success",
        message: `Refined ${String(resp.before_count)} → ${String(resp.after_count)} items`,
      });
    } catch {
      push({
        variant: "error",
        message: "Couldn't refine memory — try again.",
      });
    }
  }

  async function handleUndoRefine(): Promise<void> {
    if (lastRefineHistoryId === null) return;
    try {
      await restoreMemory.mutateAsync({ historyId: lastRefineHistoryId });
      setLastRefineHistoryId(null);
      push({ variant: "info", message: "Refine undone." });
    } catch {
      push({
        variant: "error",
        message: "Couldn't undo that refinement — try again.",
      });
    }
  }

  async function handleReindex(): Promise<void> {
    if (reindexModel.trim() === "") {
      push({ variant: "warning", message: "Select an embedding model first." });
      return;
    }
    try {
      await reindex.mutateAsync({ embedding_model_id: reindexModel });
      push({ variant: "success", message: "Reindex started." });
    } catch {
      push({
        variant: "error",
        message: "Couldn't start reindexing — try again.",
      });
    }
  }

  const allPins: MemoryInsight[] = data ?? [];
  // Filter by case-insensitive substring match; rare-call so do it
  // inline rather than memoizing (the pin count is small).
  const pins: MemoryInsight[] =
    searchQuery.trim() === ""
      ? allPins
      : allPins.filter((p) =>
          p.text.toLowerCase().includes(searchQuery.toLowerCase().trim()),
        );
  // AUTO (distilled) memories — saved by the post-turn distillation pass,
  // distinct from admin-pinned insights. Same search filter applies.
  const allAuto: MemoryInsight[] = autoData ?? [];
  const autoMemories: MemoryInsight[] =
    searchQuery.trim() === ""
      ? allAuto
      : allAuto.filter((m) =>
          m.text.toLowerCase().includes(searchQuery.toLowerCase().trim()),
        );
  // 6-line clamp threshold — anything beyond this gets the "show more"
  // toggle. Picked empirically: 6 lines of 15px text ≈ 150px which
  // matches the "scannable" target.
  const CLAMP_CHAR_THRESHOLD = 350;

  const body = (
    <div className={inRail ? "rr-page rr-page--rail" : "rr-page"}>
      {/* Rail mount: the Drawer banner already says "Memory" — repeating
            the page header inside 380px wastes a chapter of vertical room. */}
      {!inRail && (
        <header className="rr-page-header">
          {/* Context-specific eyebrow — interior pages show section label */}
          <span className="rr-eyebrow">Your Library</span>
          <h1 className="rr-page-title">Memory</h1>
        </header>
      )}

      {/* Add insight form */}
      <form
        onSubmit={(e) => {
          void handlePin(e);
        }}
        className="rr-memory-input-row"
      >
        <input
          type="text"
          value={newText}
          onChange={(e) => {
            setNewText(e.target.value);
          }}
          placeholder="Add an insight to pin…"
          aria-label="New insight"
          // Match the backend PIN_TEXT_MAX_LENGTH to enforce
          // the cap locally before the round-trip.
          maxLength={8192}
          className="rr-memory-input"
        />
        <button
          type="submit"
          disabled={pin.isPending || newText.trim() === ""}
          className="rr-memory-pin-btn"
        >
          Pin
        </button>
      </form>

      {/* States */}
      {isLoading && <p className="rr-hint">Loading…</p>}
      {isError && (
        <p className="rr-hint rr-hint--error">
          Couldn't load memory — try again.
        </p>
      )}

      {/* Empty state — only when there are neither pinned nor auto memories
          (an auto-only library is not "nothing remembered"). */}
      {!isLoading && !isError && pins.length === 0 && autoMemories.length === 0 && (
        <div
          className="rr-empty-state rr-empty-state--memory"
          data-testid="memory-empty-state"
        >
          <h3 className="rr-empty-title">Nothing remembered yet.</h3>
          <p className="rr-empty-marginalia">
            Pin a message to save what mattered. Hover any reply, click the
            bookmark, it lands here.
          </p>
        </div>
      )}

      {/* Refine toolbar */}
      {pins.length > 0 && (
        <div className="rr-memory-toolbar">
          <button
            type="button"
            onClick={() => {
              setRefineConfirmOpen(true);
            }}
            disabled={refineMemory.isPending}
            className="lmchat-btn-primary"
            data-testid="memory-refine-btn"
          >
            {refineMemory.isPending ? "Refining…" : "Refine"}
          </button>
          {lastRefineHistoryId !== null && (
            <button
              type="button"
              onClick={() => {
                void handleUndoRefine();
              }}
              disabled={restoreMemory.isPending}
              className="lmchat-btn-secondary"
              data-testid="memory-undo-refine-btn"
            >
              {restoreMemory.isPending ? "Restoring…" : "Undo refine"}
            </button>
          )}
        </div>
      )}

      {/* Refine confirm */}
      {refineConfirmOpen && (
        <div
          role="alertdialog"
          aria-label="Confirm memory refine"
          className="rr-refine-confirm"
          data-testid="memory-refine-confirm"
        >
          <p className="rr-refine-confirm-text">
            LM Studio will rewrite {String(pins.length)} pinned insight
            {pins.length === 1 ? "" : "s"} — merging duplicates and tightening
            language. This can't be undone without restoring.
          </p>
          <div className="rr-refine-confirm-actions">
            <button
              type="button"
              onClick={() => {
                void handleRefineConfirm();
              }}
              className="lmchat-btn-primary"
              data-testid="memory-refine-confirm-yes"
            >
              Refine
            </button>
            <button
              type="button"
              onClick={() => {
                setRefineConfirmOpen(false);
              }}
              className="lmchat-btn-secondary"
              data-testid="memory-refine-confirm-no"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {/* Search — case-insensitive substring filter. Audit found
          /memory had no search input despite a growing pin list. */}
      {allPins.length > 0 && (
        <div className="rr-memory-search" role="search">
          <input
            type="search"
            placeholder="Search pinned insights…"
            value={searchQuery}
            onChange={(e) => {
              setSearchQuery(e.target.value);
            }}
            aria-label="Search pinned insights"
            data-testid="memory-search"
          />
        </div>
      )}

      {/* Memory list — index cards on canvas */}
      <ul className="rr-memory-list">
        {pins.map((p) => (
          <li
            key={p.id}
            className="rr-memory-row"
            data-testid={`memory-insight-${String(p.id)}`}
          >
            {editingId === p.id ? (
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  void saveEdit(p.id);
                }}
                className="rr-memory-edit-form"
                data-testid={`memory-insight-edit-form-${String(p.id)}`}
              >
                <input
                  autoFocus
                  type="text"
                  value={editDraft}
                  onChange={(e) => {
                    setEditDraft(e.target.value);
                  }}
                  onKeyDown={(e) => {
                    if (e.key === "Escape") cancelEdit();
                  }}
                  aria-label="Edit insight"
                  className="rr-memory-input"
                  data-testid={`memory-insight-edit-input-${String(p.id)}`}
                />
                <button
                  type="submit"
                  className="lmchat-btn-primary"
                  data-testid={`memory-insight-edit-save-${String(p.id)}`}
                >
                  Save
                </button>
                <button
                  type="button"
                  onClick={cancelEdit}
                  className="lmchat-btn-secondary"
                  data-testid={`memory-insight-edit-cancel-${String(p.id)}`}
                >
                  <X size={14} aria-hidden />
                </button>
              </form>
            ) : (
              <>
                {/* Marginalia date stamp in left gutter */}
                <span className="rr-memory-date">
                  {new Date(p.created_at).toLocaleDateString()}
                </span>
                {/* Body text on canvas — clamps long pins.
                    Expanding the row removes the clamp; collapsing
                    re-applies. */}
                {p.text.length > CLAMP_CHAR_THRESHOLD ? (
                  <span className="rr-memory-body-cell">
                    <span
                      className="rr-memory-body"
                      data-clamped={expandedIds.has(p.id) ? undefined : "true"}
                    >
                      {p.text}
                    </span>
                    <button
                      type="button"
                      onClick={() => {
                        toggleExpanded(p.id);
                      }}
                      className="rr-memory-show-more"
                      aria-expanded={expandedIds.has(p.id)}
                      data-testid={`memory-insight-toggle-${String(p.id)}`}
                    >
                      {expandedIds.has(p.id) ? "Show less" : "Show more"}
                    </button>
                  </span>
                ) : (
                  <span className="rr-memory-body">{p.text}</span>
                )}
                {/* Hover-revealed actions */}
                <span className="rr-memory-actions">
                  <button
                    type="button"
                    onClick={() => {
                      beginEdit(p);
                    }}
                    aria-label={`Edit: ${p.text}`}
                    className="rr-icon-btn"
                    data-testid={`memory-insight-edit-${String(p.id)}`}
                  >
                    <Pencil size={12} aria-hidden />
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      void handleUnpin(p.id);
                    }}
                    aria-label={`Unpin: ${p.text}`}
                    className="rr-icon-btn"
                  >
                    <X size={12} aria-hidden />
                  </button>
                </span>
              </>
            )}
          </li>
        ))}
      </ul>

      {/* Auto-saved memories — durable facts the assistant remembered on
          its own. Distinct from pinned insights: machine-saved, removable,
          clearly labelled. Only shown once at least one exists. */}
      {autoMemories.length > 0 && (
        <section
          className="rr-auto-memory-section"
          data-testid="memory-auto-section"
          aria-label="Remembered automatically"
        >
          <div className="rr-auto-memory-header">
            <h3 className="rr-admin-heading">Remembered automatically</h3>
            <p className="rr-hint">
              Durable facts saved from your conversations. These are recalled
              in future chats. Remove any that are wrong or stale.
            </p>
          </div>
          <ul className="rr-memory-list">
            {autoMemories.map((m) => (
              <li
                key={m.id}
                className="rr-memory-row"
                data-testid={`memory-auto-${String(m.id)}`}
              >
                <span className="rr-memory-date">
                  {new Date(m.created_at).toLocaleDateString()}
                </span>
                {m.text.length > CLAMP_CHAR_THRESHOLD ? (
                  <span className="rr-memory-body-cell">
                    <span
                      className="rr-memory-body"
                      data-clamped={expandedIds.has(m.id) ? undefined : "true"}
                    >
                      {m.text}
                    </span>
                    <button
                      type="button"
                      onClick={() => {
                        toggleExpanded(m.id);
                      }}
                      className="rr-memory-show-more"
                      aria-expanded={expandedIds.has(m.id)}
                      data-testid={`memory-auto-toggle-${String(m.id)}`}
                    >
                      {expandedIds.has(m.id) ? "Show less" : "Show more"}
                    </button>
                  </span>
                ) : (
                  <span className="rr-memory-body">{m.text}</span>
                )}
                <span className="rr-memory-actions">
                  <button
                    type="button"
                    onClick={() => {
                      void handleUnpin(m.id);
                    }}
                    aria-label={`Forget: ${m.text}`}
                    className="rr-icon-btn"
                    data-testid={`memory-auto-forget-${String(m.id)}`}
                  >
                    <X size={12} aria-hidden />
                  </button>
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* Admin: reindex */}
      {user?.is_admin === true && (
        <section className="rr-admin-section">
          <h3 className="rr-admin-heading">Reindex</h3>
          <div className="rr-admin-row">
            <select
              aria-label="Embedding model"
              value={reindexModel}
              onChange={(e) => {
                setReindexModel(e.target.value);
                setReindexModelTouched(true);
              }}
              className="rr-select"
              data-testid="memory-reindex-model-select"
            >
              <option value="" disabled>
                Select embedding model
              </option>
              {/* Each embedder shows whether it is LOADED vs downloaded-only,
                  and which one is currently active. Not-loaded options are
                  DISABLED — reindexing under an unloaded embedder fails (and
                  pinning one is exactly what silently killed memory before).
                  Load state comes from /api/models (m.loaded); active comes
                  from the resolver-aligned status snapshot. */}
              {(modelsData?.models ?? [])
                .filter((m) => m.capabilities.embedding)
                .map((m) => {
                  const isActive =
                    embeddingStatus?.active_model_id === m.id;
                  const suffix = `${m.loaded ? " · loaded" : " · not loaded"}${
                    isActive ? " · active" : ""
                  }`;
                  return (
                    <option key={m.id} value={m.id} disabled={!m.loaded}>
                      {m.name}
                      {suffix}
                    </option>
                  );
                })}
              {/* Fall back to LOADED embedding ids from the status snapshot
                  that /api/models filtering somehow missed. The snapshot now
                  lists loaded-only, so these are always selectable. The
                  snapshot is polled independently of /api/models and isn't
                  guaranteed unique on its own — the same id reported twice
                  here rendered the same embedder 3x (once from the
                  /api/models list above + twice from this snapshot).
                  Dedupe the snapshot's own entries before the cross-list
                  filter runs. */}
              {dedupeByKey(embeddingStatus?.loaded_embedding_models ?? [], (id) => id)
                .filter(
                  (id) => !(modelsData?.models ?? []).some((m) => m.id === id),
                )
                .map((id) => (
                  <option key={id} value={id}>
                    {id} · loaded
                    {embeddingStatus?.active_model_id === id ? " · active" : ""}
                  </option>
                ))}
            </select>
            <button
              type="button"
              onClick={() => {
                void handleReindex();
              }}
              disabled={reindex.isPending || reindexModel === ""}
              className="lmchat-btn-secondary"
            >
              {reindex.isPending ? "Running…" : "Reindex"}
            </button>
          </div>
        </section>
      )}
    </div>
  );
  return inRail ? body : <AppShell>{body}</AppShell>;
}
