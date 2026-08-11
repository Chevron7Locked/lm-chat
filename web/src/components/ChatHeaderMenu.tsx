/* SPDX-License-Identifier: Apache-2.0 */
/**
 * ChatHeaderMenu — export + share dropdown for the chat top-bar.
 *
 * Surfaces:
 *   - Export → Markdown   (calls `downloadChatAsMarkdown`)
 *   - Export → JSON       (calls `downloadChatAsJson`)
 *   - Share               (POST /api/chats/:id/share → copy URL, then DELETE)
 *
 * Surfaces in the top-right of the chat header.  Hidden when chatId is
 * null.  When the chat is incognito, the Share row renders disabled
 * with a tooltip explaining the privacy invariant — the user
 * shouldn't have to click and read a 403 to learn why.
 *
 * Imperative-open support: the parent (Chat.tsx) wires a Cmd/Ctrl+Shift+E
 * shortcut to `openExportMenu()` via a ref-style handle.  The
 * `openSignal` prop is a monotonically-increasing number; each tick
 * opens the menu.  This avoids prop drilling a setOpen callback while
 * still letting an effect react.
 *
 * Layout R1 (F1, audit 2026-06-10): `hiddenTrigger` renders the trigger
 * button visually hidden (sr-only pattern) while the dropdown panel still
 * renders in normal flow when opened via `openSignal`.  This replaced a
 * 0×0 `opacity: 0` wrapper in Chat.tsx that clipped the panel, making the
 * menu structurally invisible.  A `createPortal` alternative was rejected:
 * the panel is absolutely positioned against this wrapper, so portaling to
 * `document.body` loses the anchor (the hidden trigger has no measurable
 * rect) and fights the wrapRef click-outside handler.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactElement } from "react";
import { ChevronDown } from "lucide-react";
import { api } from "@/lib/api";
import { downloadChatAsJson, downloadChatAsMarkdown } from "@/lib/chatExport";
import type { ExportChat, ExportMessage } from "@/lib/chatExport";
import { useToast } from "@/stores/toastStore";

interface ChatHeaderMenuProps {
  chatId: number | null;
  chat: ExportChat | null;
  messages: readonly ExportMessage[];
  incognito: boolean;
  /** Monotonically-increasing counter; each new value opens the menu. */
  openSignal: number;
  /**
   * Render the trigger visually hidden (sr-only) — the menu is opened
   * imperatively via `openSignal` (overflow item / keyboard shortcut)
   * but the panel still renders in normal flow when open.
   */
  hiddenTrigger?: boolean;
}

interface ShareResponse {
  token: string;
  url: string;
  chat_id: number;
  created_at: string;
}

export function ChatHeaderMenu({
  chatId,
  chat,
  messages,
  incognito,
  openSignal,
  hiddenTrigger = false,
}: ChatHeaderMenuProps): ReactElement | null {
  const [open, setOpen] = useState(false);
  const [shareUrl, setShareUrl] = useState<string | null>(null);
  const [sharing, setSharing] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);
  const { push } = useToast();

  // Imperative open: each increment of openSignal opens the menu.
  // The first render sees openSignal=0 and we don't auto-open.
  const lastOpenSignal = useRef(openSignal);
  useEffect(() => {
    if (openSignal !== lastOpenSignal.current) {
      lastOpenSignal.current = openSignal;
      setOpen(true);
    }
  }, [openSignal]);

  // Hydrate any existing share token when the chat changes / menu opens.
  useEffect(() => {
    if (chatId === null || !open) return;
    const ctl = { cancelled: false };
    void (async () => {
      try {
        const data = await api.request<ShareResponse | null>(
          `/api/chats/${String(chatId)}/share`,
        );
        if (!ctl.cancelled && data !== null) {
          setShareUrl(`${window.location.origin}${data.url}`);
        }
      } catch {
        // Silently ignore; the user will see no URL and can click Share to mint.
      }
    })();
    return () => {
      ctl.cancelled = true;
    };
  }, [chatId, open]);

  // Click-outside to close.
  useEffect(() => {
    if (!open) return;
    function handleClick(e: MouseEvent) {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", handleClick);
    return () => {
      document.removeEventListener("mousedown", handleClick);
    };
  }, [open]);

  const handleExportMarkdown = useCallback((): void => {
    if (chat === null) return;
    const ok = downloadChatAsMarkdown(chat, messages);
    setOpen(false);
    if (ok) {
      push({ variant: "success", message: "Markdown downloaded." });
    } else {
      push({
        variant: "error",
        message: "Couldn't download — the browser blocked the export.",
      });
    }
  }, [chat, messages, push]);

  const handleExportJson = useCallback((): void => {
    if (chat === null) return;
    const ok = downloadChatAsJson(chat, messages);
    setOpen(false);
    if (ok) {
      push({ variant: "success", message: "JSON downloaded." });
    } else {
      push({
        variant: "error",
        message: "Couldn't download — the browser blocked the export.",
      });
    }
  }, [chat, messages, push]);

  const handleShare = useCallback(async (): Promise<void> => {
    if (chatId === null) return;
    setSharing(true);
    try {
      const data = await api.request<ShareResponse>(
        `/api/chats/${String(chatId)}/share`,
        { method: "POST" },
      );
      const absolute = `${window.location.origin}${data.url}`;
      setShareUrl(absolute);
      try {
        // Older browsers (and many test environments) lack Clipboard API.
        // navigator.clipboard is typed non-nullable but is undefined in
        // older browsers / jsdom; cast through unknown to gate defensively.
        const clip = navigator.clipboard as unknown as Clipboard | undefined;
        if (clip !== undefined) {
          await clip.writeText(absolute);
          push({ variant: "success", message: "Share URL copied." });
        } else {
          push({ variant: "info", message: "Share URL ready." });
        }
      } catch {
        push({ variant: "info", message: "Share URL ready." });
      }
    } catch (err) {
      const status = (err as { status?: number }).status;
      if (status === 403) {
        push({
          variant: "error",
          message: "Incognito chats cannot be shared.",
        });
      } else {
        push({
          variant: "error",
          message: "Couldn't share this chat — try again.",
        });
      }
    } finally {
      setSharing(false);
    }
  }, [chatId, push]);

  const handleStopSharing = useCallback(async (): Promise<void> => {
    if (chatId === null) return;
    setSharing(true);
    try {
      await api.request<undefined>(`/api/chats/${String(chatId)}/share`, {
        method: "DELETE",
      });
      setShareUrl(null);
      push({ variant: "info", message: "Share link revoked." });
    } catch {
      push({
        variant: "error",
        message: "Couldn't revoke that link — try again.",
      });
    } finally {
      setSharing(false);
    }
  }, [chatId, push]);

  const handleCopyExisting = useCallback(async (): Promise<void> => {
    if (shareUrl === null) return;
    try {
      const clip = navigator.clipboard as unknown as Clipboard | undefined;
      if (clip !== undefined) {
        await clip.writeText(shareUrl);
        push({ variant: "success", message: "Share URL copied." });
      }
    } catch {
      push({
        variant: "error",
        message: "Couldn't copy the link — try again.",
      });
    }
  }, [shareUrl, push]);

  if (chatId === null || chat === null) return null;

  return (
    <div
      ref={wrapRef}
      className={
        hiddenTrigger
          ? "lmchat-header-menu lmchat-header-menu--hidden-trigger"
          : "lmchat-header-menu"
      }
    >
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="Chat menu (export / share)"
        data-testid="chat-header-menu-button"
        className={
          hiddenTrigger
            ? "lmchat-header-menu__trigger lmchat-header-menu__trigger--hidden"
            : "lmchat-header-menu__trigger"
        }
        onClick={() => {
          setOpen((v) => !v);
        }}
      >
        {open && (
          <span className="lmchat-header-menu__dot" aria-hidden="true" />
        )}
        Share{" "}
        <ChevronDown size={12} aria-hidden style={{ verticalAlign: "-2px" }} />
      </button>
      {open && (
        <div
          role="menu"
          className="lmchat-header-menu__panel"
          data-testid="chat-header-menu-panel"
        >
          <div className="lmchat-header-menu__section">Export</div>
          <button
            type="button"
            role="menuitem"
            className="lmchat-header-menu__item"
            data-testid="chat-export-markdown"
            onClick={handleExportMarkdown}
          >
            Markdown (.md)
          </button>
          <button
            type="button"
            role="menuitem"
            className="lmchat-header-menu__item"
            data-testid="chat-export-json"
            onClick={handleExportJson}
          >
            JSON (.json)
          </button>

          <div className="lmchat-header-menu__divider" />

          <div className="lmchat-header-menu__section">Share</div>
          {incognito ? (
            <button
              type="button"
              role="menuitem"
              aria-disabled="true"
              disabled
              title="Incognito chats cannot be shared (privacy invariant)"
              className="lmchat-header-menu__item"
              data-testid="chat-share-disabled-incognito"
            >
              Share — disabled for incognito
            </button>
          ) : shareUrl !== null ? (
            <>
              <div
                className="lmchat-header-menu__url-box"
                data-testid="chat-share-url"
              >
                {shareUrl}
              </div>
              <button
                type="button"
                role="menuitem"
                className="lmchat-header-menu__item"
                data-testid="chat-share-copy"
                onClick={() => {
                  void handleCopyExisting();
                }}
              >
                Copy link
              </button>
              <button
                type="button"
                role="menuitem"
                className="lmchat-header-menu__item"
                disabled={sharing}
                data-testid="chat-share-stop"
                onClick={() => {
                  void handleStopSharing();
                }}
              >
                {sharing ? "Stopping…" : "Stop sharing"}
              </button>
            </>
          ) : (
            <button
              type="button"
              role="menuitem"
              className="lmchat-header-menu__item"
              disabled={sharing}
              data-testid="chat-share-create"
              onClick={() => {
                void handleShare();
              }}
            >
              {sharing ? "Creating link…" : "Create public link"}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
