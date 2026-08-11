/* SPDX-License-Identifier: Apache-2.0 */
/**
 * AppShell — main-content wrapper for routes that live under AppLayout.
 *
 * Refactor 2026-06-03: Sidebar persistence
 * across navigation was lost because every route mounted its own
 * `<Sidebar />` instance. AppLayout now owns Sidebar at the route-
 * tree level so it persists; this component is reduced to a thin
 * main-content wrapper (mobile top-bar + main landmark with
 * id="main-content"). Settings / Memory / Documents / Analytics / Prompts /
 * Admin routes use this for their main-area framing.
 *
 * Mobile nav (2026-06-25): the persistent
 * mobile bar used to show ONLY the LM Chat logo, redundant with each page's
 * own eyebrow/wordmark and with the drawer. It now carries a hamburger that
 * opens the same slide-in Sidebar drawer the chat route uses — the LM Chat
 * wordmark lives in the drawer header, not in the persistent bar. This also
 * gives these AppShell routes real navigation on mobile (previously the bar
 * was a dead logo link with no menu).
 */
import { useEffect, useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import { Menu, X } from "lucide-react";
import { BrandMark, BRAND_NAME } from "@/components/BrandMark";
import { LmStudioAuthBanner } from "@/components/LmStudioAuthBanner";
import { Sidebar } from "@/components/Sidebar";
import { useViewport } from "@/hooks/useViewport";

interface AppShellProps {
  children: ReactNode;
}

export function AppShell({ children }: AppShellProps) {
  const { isMobile } = useViewport();
  const [drawerOpen, setDrawerOpen] = useState(false);

  // Close the drawer whenever we cross back to desktop so a stale-open
  // drawer can't linger behind the desktop sidebar.
  useEffect(() => {
    if (!isMobile && drawerOpen) setDrawerOpen(false);
  }, [isMobile, drawerOpen]);

  // Body scroll-lock while the drawer is open (matches the chat drawer).
  useEffect(() => {
    if (!drawerOpen) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [drawerOpen]);

  // Esc closes the drawer.
  useEffect(() => {
    if (!drawerOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setDrawerOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
    };
  }, [drawerOpen]);

  return (
    <main
      id="main-content"
      tabIndex={-1}
      className="lmchat-app-main"
      style={mainAreaStyle}
    >
      <LmStudioAuthBanner />
      {isMobile && (
        <div
          style={mobileBarStyle}
          role="banner"
          aria-label={`${BRAND_NAME} navigation`}
        >
          <button
            type="button"
            aria-label="Open menu"
            aria-expanded={drawerOpen}
            onClick={() => {
              setDrawerOpen(true);
            }}
            className="lmchat-mobile-menu-btn"
            data-testid="appshell-mobile-menu"
          >
            <Menu size={18} aria-hidden />
          </button>
        </div>
      )}
      {isMobile && drawerOpen && (
        <>
          <div
            className="lmchat-mobile-sidebar-shell"
            data-testid="appshell-sidebar-shell"
          >
            <div className="lmchat-drawer-header-mobile">
              <div className="lmchat-drawer-brand">
                <BrandMark size={22} />
                <span className="lmchat-brand-wordmark lmchat-brand-wordmark--display">
                  {BRAND_NAME}
                </span>
              </div>
              <button
                type="button"
                className="lmchat-drawer-close-btn"
                aria-label="Close menu"
                data-testid="appshell-sidebar-close"
                onClick={() => {
                  setDrawerOpen(false);
                }}
              >
                <X size={14} aria-hidden />
              </button>
            </div>
            <Sidebar
              collapsed={false}
              onToggle={() => {
                setDrawerOpen(false);
              }}
              mobile
            />
          </div>
          <button
            type="button"
            aria-label="Close menu"
            onClick={() => {
              setDrawerOpen(false);
            }}
            className="lmchat-mobile-backdrop"
          />
        </>
      )}
      {children}
    </main>
  );
}

const mainAreaStyle: CSSProperties = {
  flex: 1,
  minWidth: 0,
  // Height comes from .lmchat-app-main (100vh fallback → 100dvh). Inline
  // styles can't express the two-line dvh fallback, so it lives in CSS.
  overflowY: "auto",
  overscrollBehavior: "contain",
  background: "transparent",
};

const mobileBarStyle: CSSProperties = {
  position: "sticky",
  top: 0,
  zIndex: 10,
  display: "flex",
  alignItems: "center",
  minHeight: "64px",
  /* group padding: glue-relaxed vertical, sibling-relaxed horizontal */
  padding: "0 var(--space-sibling-relaxed)",
  background: "transparent",
  borderBottom: "1px solid var(--color-border)",
};
