/* SPDX-License-Identifier: Apache-2.0 */
/**
 * InterruptedRow — shown above the Composer when an interrupted stream is
 * detected for the current chat.
 *
 * Detection: useSSE stores `lmchat:sse:<chatId>:msg_id` in localStorage when
 * a stream starts. If this key survives a page reload (i.e. no matching
 * chat.end cleared it), the stream was interrupted.
 *
 * Retry: clears the orphaned localStorage keys for the chat and calls
 * POST /api/chat/stream with previous_response_id (loaded from
 * `lmchat:sse:<chatId>:rid`) to resume the stream.
 *
 * The test for this component only checks the render-side affordance (the
 * role=alert div + Retry button visible in the DOM) — it does not exercise
 * the full retry-and-stream cycle.
 */
import type { CSSProperties } from "react";

const LS_PREFIX = "lmchat:sse";

/** Clear all orphaned SSE localStorage keys for `chatId`. */
export function clearOrphanedSSEKeys(chatId: number): void {
  try {
    const prefix = `${LS_PREFIX}:${String(chatId)}:`;
    const toRemove: string[] = [];
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (k?.startsWith(prefix)) toRemove.push(k);
    }
    for (const k of toRemove) localStorage.removeItem(k);
  } catch {
    // Ignore localStorage errors.
  }
}

/** Load the stored response_id for `chatId` if present. */
export function loadOrphanedResponseId(chatId: number): string | null {
  try {
    return localStorage.getItem(`${LS_PREFIX}:${String(chatId)}:rid`);
  } catch {
    return null;
  }
}

interface InterruptedRowProps {
  onRetry: () => void;
}

export function InterruptedRow({ onRetry }: InterruptedRowProps) {
  return (
    <div
      role="alert"
      aria-live="polite"
      style={containerStyle}
      data-testid="interrupted-row"
    >
      <span style={messageStyle}>
        Response was interrupted — pick up where it left off?
      </span>
      <button
        type="button"
        onClick={onRetry}
        className="lmchat-text-link"
        style={retryBtnStyle}
        aria-label="Resume interrupted response"
        data-testid="interrupted-retry-btn"
      >
        Resume
      </button>
    </div>
  );
}

const containerStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: "var(--space-glue-relaxed)",
  padding: "var(--space-glue-relaxed) var(--space-sibling-relaxed)",
  margin: "0 var(--space-group-relaxed) var(--space-glue-relaxed)",
  background: "var(--color-accent-quiet)",
  border: "1px solid var(--color-accent-border)",
  borderRadius: "var(--radius-sm)",
  fontSize: "0.875rem",
};

const messageStyle: CSSProperties = {
  flex: 1,
  color: "var(--color-text-muted)",
};

const retryBtnStyle: CSSProperties = {
  padding: "4px var(--space-glue-relaxed)",
  background: "var(--color-accent-quiet)",
  border: "1px solid var(--color-accent-border)",
  borderRadius: "var(--radius-xs)",
  fontSize: "0.8125rem",
  fontWeight: 500,
  color: "var(--color-accent)",
  cursor: "pointer",
  flexShrink: 0,
};
