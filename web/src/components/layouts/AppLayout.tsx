/* SPDX-License-Identifier: Apache-2.0 */
/**
 * AppLayout — top-level layout for authenticated routes.
 *
 * Renders the Sidebar ONCE here so it persists across navigation
 * between /, /chats/:id, /settings, /memory, /documents, /analytics,
 * /prompts, /setup/lm-studio. Previously each page mounted its own
 * `<Sidebar />`, so navigation triggered unmount/remount → animation
 * flicker, chat-list state lost, "page reload feel" (2026-06-03).
 *
 * Mobile: sidebar is hidden (it's desktop-only); the page-level
 * shell still renders its own mobile drawer via the `<Outlet />`
 * children (Chat.tsx in particular has rich mobile drawer logic
 * keyed to its TopBar burger). Lifting that to here would require
 * unifying the chat-specific TopBar + drawer with the settings/
 * library top-bar treatment — a bigger refactor than this batch
 * targets. The desktop sidebar persistence is the visible win.
 */
import { Outlet } from "react-router-dom";
import { Sidebar } from "@/components/Sidebar";
import { useViewport } from "@/hooks/useViewport";
import { useSidebarCollapsed } from "@/hooks/useSidebarCollapsed";

export function AppLayout() {
  const { isMobile } = useViewport();
  const [sidebarCollapsed, setSidebarCollapsed] = useSidebarCollapsed();

  return (
    <div className="lmchat-app-layout">
      {!isMobile && (
        <Sidebar
          collapsed={sidebarCollapsed}
          onToggle={() => {
            setSidebarCollapsed((v) => !v);
          }}
        />
      )}
      <Outlet />
    </div>
  );
}
