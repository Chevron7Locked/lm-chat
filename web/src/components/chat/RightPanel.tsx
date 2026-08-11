/* SPDX-License-Identifier: Apache-2.0 */
import { lazy, Suspense } from "react";
import { Drawer } from "@/components/ui/Drawer";

// ─── Lazy panels ─────────────────────────────────────────────────────────────

// Per-chat settings rail replaces the previous LazySettings panel.
const LazyChatSettingsRail = lazy(() =>
  import("@/components/ChatSettingsRail").then((m) => ({
    default: m.ChatSettingsRail,
  })),
);
const LazyMemory = lazy(() => import("@/pages/Memory"));
const LazyDocuments = lazy(() => import("@/pages/Documents"));

// ─── RightPanel ──────────────────────────────────────────────────────────────

interface RightPanelProps {
  view: "settings" | "memory" | "documents";
  chatId: number | null;
  onClose: () => void;
}

export function RightPanel({ view, chatId, onClose }: RightPanelProps) {
  const label =
    view === "settings"
      ? "Chat settings"
      : view === "memory"
        ? "Memory"
        : "Documents";
  return (
    <Drawer
      isOpen={true}
      onClose={onClose}
      side="right"
      title={label}
      // 380px — matches ChatSettingsRail's own Drawer mount so all three
      // right-rail views share one width (default was 360 for two of them).
      width={380}
      showFooter={true}
    >
      <Suspense
        fallback={<div className="lmchat-drawer-fallback">Loading…</div>}
      >
        {view === "settings" ? (
          chatId !== null ? (
            <LazyChatSettingsRail chatId={chatId} />
          ) : (
            <div className="lmchat-drawer-fallback">
              Select a chat to configure per-chat settings.
            </div>
          )
        ) : view === "memory" ? (
          // inRail: no nested AppShell (duplicate <main id="main-content">
          // inside the dialog), no duplicate page header, compact padding.
          <LazyMemory inRail />
        ) : (
          <LazyDocuments inRail />
        )}
      </Suspense>
    </Drawer>
  );
}
