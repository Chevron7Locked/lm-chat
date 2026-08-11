/* SPDX-License-Identifier: Apache-2.0 */
/**
 * CompactionTab — a "folded page" at a compaction boundary.
 *
 * Collapsed: reads as a SEAM, not a card — a hairline rule on each side of a
 * small pill tab ("N turns folded"). The tab is the fold: a page of chat
 * history was folded away here.
 *
 * Expanded: reveals the fold's contents — the running summary, then a DENSE
 * TRANSCRIPT rendered directly from the raw MessageRecord fields (role +
 * content only, plain text, no markdown, no <ChatMessage> bubbles). This is
 * archived history, not live chat, and it must never look like it.
 *
 * Uses grid-template-rows for the expand/collapse animation (project's motion
 * convention — animate grid-rows, not height).
 *
 * Theme-aware, Midnight Atelier design system.
 */
import { useState, useCallback } from "react";
import { BookMarked, ChevronDown } from "lucide-react";
import { useCompactionMessages } from "@/hooks/useCompactions";
import type { CompactionSpan } from "@/hooks/useCompactions";
import "@/styles/compaction-tab.css";

interface CompactionTabProps {
  compaction: CompactionSpan;
  chatId: number;
}

/** Role marker shown in the dense transcript — mapped, not raw role strings. */
const ROLE_LABELS: Record<string, string> = {
  user: "USER",
  assistant: "MODEL",
  system: "SYS",
  tool: "TOOL",
};

function roleLabel(role: string): string {
  return ROLE_LABELS[role] ?? role.toUpperCase();
}

/** Seam tab label — "N turns folded". */
function formatFoldedLabel(span: CompactionSpan): string {
  const n = span.archived_count;
  return `${String(n)} turn${n === 1 ? "" : "s"} folded`;
}

/** Token-metadata line under the summary — real "→" glyph, not "->". */
function formatTokenMeta(span: CompactionSpan): string {
  const orig = span.original_token_count.toLocaleString();
  const summary = span.summary_token_count.toLocaleString();
  return `~${orig} → ~${summary} tokens`;
}

export function CompactionTab({ compaction, chatId }: CompactionTabProps) {
  const [expanded, setExpanded] = useState(false);

  const toggle = useCallback(() => {
    setExpanded((prev) => !prev);
  }, []);

  const { data: archivedMessages, isLoading } = useCompactionMessages(
    chatId,
    compaction.id,
    expanded,
  );

  const bodyId = `compaction-${String(compaction.id)}-body`;

  return (
    <div
      className="lmchat-compaction-tab"
      role="region"
      aria-label={`Compaction: ${compaction.summary}`}
    >
      {/* The seam — a page was folded here. Flanking rules are decorative;
          the pill is the whole toggle, keyboard-accessible. */}
      <div className="lmchat-compaction-tab__seam">
        <span className="lmchat-compaction-tab__rule" aria-hidden="true" />
        <button
          type="button"
          className="lmchat-compaction-tab__tab"
          aria-expanded={expanded}
          aria-controls={bodyId}
          onClick={toggle}
        >
          <BookMarked
            size={14}
            aria-hidden="true"
            className="lmchat-compaction-tab__tab-icon"
          />
          <span className="lmchat-compaction-tab__tab-label">
            {formatFoldedLabel(compaction)}
          </span>
          <ChevronDown
            size={14}
            aria-hidden="true"
            className={
              expanded
                ? "lmchat-compaction-tab__tab-chevron lmchat-compaction-tab__tab-chevron--expanded"
                : "lmchat-compaction-tab__tab-chevron"
            }
          />
          <span className="sr-only">
            {expanded ? "Collapse compaction" : "Expand compaction"}
          </span>
        </button>
        <span className="lmchat-compaction-tab__rule" aria-hidden="true" />
      </div>

      {/* The fold's contents — animated via grid-template-rows. */}
      <div
        id={bodyId}
        className={
          expanded
            ? "lmchat-compaction-tab__body lmchat-compaction-tab__body--expanded"
            : "lmchat-compaction-tab__body"
        }
      >
        <div className="lmchat-compaction-tab__body-inner">
          <div className="lmchat-compaction-tab__summary-block">
            <span className="lmchat-compaction-tab__eyebrow">Summary</span>
            <p className="lmchat-compaction-tab__summary-text">
              {compaction.summary}
            </p>
            <span className="lmchat-compaction-tab__meta">
              {formatTokenMeta(compaction)}
            </span>
          </div>

          {isLoading && (
            <div className="lmchat-compaction-tab__loading" role="status">
              Loading archived messages…
            </div>
          )}

          {archivedMessages && archivedMessages.length > 0 && (
            <>
              <div className="lmchat-compaction-tab__archived-eyebrow">
                {`Archived · ${String(archivedMessages.length)} message${archivedMessages.length === 1 ? "" : "s"}`}
              </div>
              <div className="lmchat-compaction-tab__transcript">
                {archivedMessages.map((msg) => (
                  <div className="lmchat-compaction-tab__row" key={msg.id}>
                    <span
                      className={`lmchat-compaction-tab__role lmchat-compaction-tab__role--${msg.role}`}
                    >
                      {roleLabel(msg.role)}
                    </span>
                    <span className="lmchat-compaction-tab__text">
                      {msg.content}
                    </span>
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
