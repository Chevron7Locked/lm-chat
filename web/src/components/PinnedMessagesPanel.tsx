/* SPDX-License-Identifier: Apache-2.0 */
/**
 * PinnedMessagesPanel — popover-style panel listing every pinned message in
 * the current chat.  Each entry shows a longer content preview than the
 * compact pin-nav strip.  Click to scroll the message into view; click the
 * unpin button to remove the pin.
 *
 * The trigger lives in the chat TopBar; this component is positioned as a
 * floating panel anchored beneath that trigger.
 */
import { useCallback, useEffect, useRef } from "react";
import { X } from "lucide-react";
import { usePinnedMessagesStore } from "@/stores/pinnedMessagesStore";

interface PinnedMessagesPanelProps {
  chatId: number;
  /** Whether the panel is currently open. */
  open: boolean;
  /** Close handler.  Fired on Escape or click-outside. */
  onClose: () => void;
  /** Messages from the chat — used to derive content previews. */
  messages: { id: number | string; content: string; role: string }[];
}

export function PinnedMessagesPanel({
  chatId,
  open,
  onClose,
  messages,
}: PinnedMessagesPanelProps) {
  // Subscribe to this chat's slice; default to a referentially-stable empty
  // array when no pins exist so React 19's useSyncExternalStore doesn't
  // re-render on every store tick.  See PinNavStrip for the same pattern.
  const pinIds = usePinnedMessagesStore((s) => s.byChat[chatId]);
  const unpin = usePinnedMessagesStore((s) => s.unpin);
  const panelRef = useRef<HTMLDivElement>(null);

  // Close on Escape.
  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
    };
  }, [open, onClose]);

  // Close on click outside.
  useEffect(() => {
    if (!open) return;
    function onClick(e: MouseEvent) {
      const node = panelRef.current;
      if (node === null) return;
      if (e.target instanceof Node && !node.contains(e.target)) {
        onClose();
      }
    }
    // Defer so the initial open-click doesn't immediately close the panel.
    const t = window.setTimeout(() => {
      window.addEventListener("mousedown", onClick);
    }, 0);
    return () => {
      window.clearTimeout(t);
      window.removeEventListener("mousedown", onClick);
    };
  }, [open, onClose]);

  const handleScroll = useCallback(
    (id: number) => {
      const el = document.querySelector<HTMLElement>(
        `[data-message-id='${String(id)}']`,
      );
      if (el !== null) {
        el.scrollIntoView({ behavior: "smooth", block: "center" });
      }
      onClose();
    },
    [onClose],
  );

  if (!open) return null;

  const rows = (pinIds ?? [])
    .map((id) => {
      const m = messages.find((x) => Number(x.id) === id);
      if (m === undefined) return null;
      return { id, role: m.role, content: m.content };
    })
    .filter(
      (r): r is { id: number; role: string; content: string } => r !== null,
    );

  return (
    <div
      ref={panelRef}
      role="dialog"
      aria-modal="false"
      aria-label="Pinned messages"
      data-testid="pinned-messages-panel"
      className="lmchat-pinned-panel"
    >
      <header className="lmchat-pinned-panel__header">
        <h3 className="lmchat-pinned-panel__title">Pinned messages</h3>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close pinned messages"
          className="lmchat-pinned-panel__close"
        >
          <X size={14} aria-hidden />
        </button>
      </header>
      {rows.length === 0 ? (
        <p className="lmchat-pinned-panel__empty">
          No pinned messages yet. Pin a message from its actions menu.
        </p>
      ) : (
        <ul className="lmchat-pinned-panel__list">
          {rows.map((r) => {
            const preview = r.content.trim();
            const truncated =
              preview.length > 220 ? `${preview.slice(0, 220)}…` : preview;
            const roleCls =
              r.role === "user"
                ? "lmchat-pinned-panel__role-badge lmchat-pinned-panel__role-badge--user"
                : "lmchat-pinned-panel__role-badge lmchat-pinned-panel__role-badge--assistant";
            return (
              <li key={r.id} className="lmchat-pinned-panel__row">
                <button
                  type="button"
                  onClick={() => {
                    handleScroll(r.id);
                  }}
                  className="lmchat-pinned-panel__row-btn"
                  data-testid={`pinned-panel-item-${String(r.id)}`}
                >
                  <span className={roleCls}>{r.role}</span>
                  <span className="lmchat-pinned-panel__preview">
                    {truncated === ""
                      ? `(empty message #${String(r.id)})`
                      : truncated}
                  </span>
                </button>
                <button
                  type="button"
                  onClick={() => {
                    unpin(chatId, r.id);
                  }}
                  aria-label={`Unpin message ${String(r.id)}`}
                  className="lmchat-pinned-panel__unpin"
                >
                  <X size={12} aria-hidden />
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
