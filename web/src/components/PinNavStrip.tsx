/* SPDX-License-Identifier: Apache-2.0 */
/**
 * PinNavStrip — thin horizontal strip near the top of the chat area listing
 * pinned messages.  Click a pin to scroll its message into view.
 *
 * Hidden when no pins exist for the chat.  Plays well with horizontal
 * overflow (pins flow horizontally with `overflow-x: auto`).
 */
import { useCallback } from "react";
import type { CSSProperties } from "react";
import { Pin, X } from "lucide-react";
import { usePinnedMessagesStore } from "@/stores/pinnedMessagesStore";

interface PinnedMessagePreview {
  id: number;
  /** Truncated content used as the visible pin title. */
  preview: string;
}

interface PinNavStripProps {
  chatId: number;
  /** All messages in the chat — used to derive preview titles for pinned IDs. */
  messages: { id: number | string; content: string; role: string }[];
  /** Maximum preview length per pin title. */
  previewLen?: number;
}

export function PinNavStrip({
  chatId,
  messages,
  previewLen = 40,
}: PinNavStripProps) {
  // Subscribe to the chat's own slice to keep getSnapshot stable across
  // renders (Zustand requires a referentially-stable result for unchanged
  // input — otherwise React 19's useSyncExternalStore throws the
  // "getSnapshot should be cached" warning + an infinite update loop).
  const pinIds = usePinnedMessagesStore((s) => s.byChat[chatId]);
  const unpin = usePinnedMessagesStore((s) => s.unpin);

  const handleClick = useCallback((id: number) => {
    const el = document.querySelector<HTMLElement>(
      `[data-message-id='${String(id)}']`,
    );
    if (el !== null) {
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      // Brief flash to draw the eye.
      el.style.transition = "background var(--duration-base) var(--ease-out)";
      const prev = el.style.background;
      el.style.background = "var(--color-accent-quiet)";
      window.setTimeout(() => {
        el.style.background = prev;
      }, 1_200);
    }
  }, []);

  if (pinIds === undefined || pinIds.length === 0) return null;

  const previews: PinnedMessagePreview[] = pinIds
    .map((id) => {
      const m = messages.find((x) => Number(x.id) === id);
      if (m === undefined) return null;
      const content = m.content.trim();
      const preview =
        content.length > previewLen
          ? `${content.slice(0, previewLen)}…`
          : content;
      return { id, preview: preview === "" ? `#${String(id)}` : preview };
    })
    .filter((p): p is PinnedMessagePreview => p !== null);

  if (previews.length === 0) return null;

  return (
    <nav
      aria-label="Pinned messages"
      data-testid="pin-nav-strip"
      style={stripStyle}
    >
      <span style={labelStyle} aria-hidden="true">
        Pinned
      </span>
      <ul style={listStyle}>
        {previews.map((p) => (
          <li key={p.id} style={listItemStyle}>
            <button
              type="button"
              onClick={() => {
                handleClick(p.id);
              }}
              style={pinBtnStyle}
              title={p.preview}
              data-testid={`pin-nav-item-${String(p.id)}`}
            >
              <Pin
                size={12}
                aria-hidden
                style={{ marginRight: "4px", flexShrink: 0 }}
              />
              <span style={pinTextStyle}>{p.preview}</span>
            </button>
            <button
              type="button"
              onClick={() => {
                unpin(chatId, p.id);
              }}
              aria-label={`Unpin message ${String(p.id)}`}
              style={unpinBtnStyle}
            >
              <X size={12} aria-hidden />
            </button>
          </li>
        ))}
      </ul>
    </nav>
  );
}

const stripStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: "var(--space-glue-relaxed)",
  padding: "4px var(--space-sibling-relaxed)",
  borderBottom: "1px solid var(--color-border)",
  background: "var(--color-surface)",
  overflowX: "auto",
  overflowY: "hidden",
  flexShrink: 0,
  minHeight: "32px",
};

const labelStyle: CSSProperties = {
  fontSize: "0.6875rem",
  fontWeight: 600,
  textTransform: "uppercase",
  letterSpacing: "0.06em",
  color: "var(--color-text-subtle)",
  flexShrink: 0,
};

const listStyle: CSSProperties = {
  listStyle: "none",
  margin: 0,
  padding: 0,
  display: "flex",
  gap: "var(--space-glue)",
  alignItems: "center",
};

const listItemStyle: CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  background: "var(--color-surface-elevated)",
  border: "1px solid var(--color-border-default)",
  borderRadius: "var(--radius-sm)",
  overflow: "hidden",
};

const pinBtnStyle: CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  maxWidth: "180px",
  padding: "2px var(--space-glue)",
  background: "none",
  border: "none",
  fontSize: "0.75rem",
  color: "var(--color-text-muted)",
  cursor: "pointer",
};

const pinTextStyle: CSSProperties = {
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const unpinBtnStyle: CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  width: "20px",
  height: "20px",
  background: "none",
  border: "none",
  borderLeft: "1px solid var(--color-border-default)",
  fontSize: "0.6875rem",
  color: "var(--color-text-subtle)",
  cursor: "pointer",
};
