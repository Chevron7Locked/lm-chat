/* SPDX-License-Identifier: Apache-2.0 */
/**
 * `RagModeBadge` — small chip in the composer area showing the
 * resolver's current pick (INLINE / HYBRID / FOCUSED) + supporting
 * numbers.
 *
 * Reads `GET /api/chats/{id}/rag_mode` via `useChatRagMode`. Tooltip on
 * hover shows the corpus size + threshold to explain WHY the resolver
 * picked the mode and whether a per-project `rag_threshold` override
 * would change it.
 *
 * The badge does NOT mutate the chat. Mode 3 (FOCUSED) is opted into
 * by setting `chats.settings.focused_document_id` elsewhere (the
 * Documents tab will gain a "focus on this doc" affordance in a
 * follow-up); for now the badge surfaces the resolved mode so the
 * user can confirm focused-mode is active when they expect it.
 */
import { useChatRagMode } from "@/hooks/useChatRagMode";
import type { RagModeName } from "@/hooks/useChatRagMode";
import type { CSSProperties } from "react";

interface RagModeBadgeProps {
  chatId: number | null;
}

const LABELS: Record<RagModeName, string> = {
  inline: "Inline",
  hybrid: "Hybrid",
  focused: "Focused",
};

const COLORS: Record<RagModeName, { bg: string; fg: string }> = {
  inline: { bg: "var(--color-surface-elevated)", fg: "var(--color-text)" },
  hybrid: { bg: "var(--color-surface-elevated)", fg: "var(--color-text)" },
  focused: { bg: "var(--color-accent-quiet)", fg: "var(--color-accent)" },
};

export function RagModeBadge({ chatId }: RagModeBadgeProps) {
  const { data, isLoading, isError } = useChatRagMode(chatId);

  if (chatId === null || isLoading || isError || data === undefined) {
    return null;
  }

  const mode = data.mode;
  const colors = COLORS[mode];
  const tooltip = buildTooltip(data);

  // When the project's pinned embedding model isn't currently loaded
  // OR no embedding model is loaded at all, retrieval silently skips.
  // The badge surfaces a warning state so the degradation is visible.
  const embeddingStatus = data.embedding_status;
  const isWarn = embeddingStatus !== "ok";
  const warnLabel = (() => {
    if (embeddingStatus === "pinned_model_unavailable") {
      return (
        "Pinned embedding model " +
        `'${data.embedding_model_pinned ?? "?"}'` +
        " is not currently loaded — RAG retrieval is skipped"
      );
    }
    if (embeddingStatus === "no_embedding_model") {
      return "No embedding model loaded — RAG retrieval is skipped";
    }
    return "";
  })();

  if (isWarn) {
    // Skipped state — collapse to a 6px warm-amber dot with tooltip.
    // Avoids a full-width banner while still surfacing the degradation.
    return (
      <span
        data-testid="rag-mode-badge"
        data-mode={mode}
        data-embedding-status={embeddingStatus}
        title={warnLabel}
        aria-label={warnLabel}
        style={dotStyle}
      />
    );
  }

  return (
    <span
      data-testid="rag-mode-badge"
      data-mode={mode}
      data-embedding-status={embeddingStatus}
      style={{
        ...badgeStyle,
        background: colors.bg,
        color: colors.fg,
      }}
      title={tooltip}
      aria-label={`RAG mode: ${LABELS[mode]}. ${tooltip}`}
    >
      {LABELS[mode]}
    </span>
  );
}

function buildTooltip(d: {
  project_corpus_tokens: number | null;
  threshold_tokens: number | null;
  focused_document_id: number | null;
}): string {
  const parts: string[] = [];
  if (d.focused_document_id !== null) {
    parts.push(`Focused doc: #${String(d.focused_document_id)}`);
  }
  if (d.project_corpus_tokens !== null) {
    parts.push(`Project corpus: ~${String(d.project_corpus_tokens)} tokens`);
  }
  if (d.threshold_tokens !== null) {
    parts.push(`Inline threshold: ${String(d.threshold_tokens)} tokens`);
  }
  return parts.join(" · ");
}

const dotStyle: CSSProperties = {
  display: "inline-block",
  width: 6,
  height: 6,
  borderRadius: "50%",
  background: "var(--color-warning)",
  flexShrink: 0,
  cursor: "help",
};

const badgeStyle: CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  padding: "2px 8px",
  fontSize: 11,
  borderRadius: 999,
  fontWeight: 500,
  cursor: "help",
  whiteSpace: "nowrap",
};
