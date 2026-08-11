/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useMobileDrawer — mobile sidebar drawer state + its lifecycle effects.
 *
 * Extracted from pages/Chat.tsx so the drawer's
 * exit-transition, panel-open, chatId-change, and scroll-lock effects can be
 * reasoned about (and tested) independently of Chat's full mount surface.
 * Behavior-preserving: bodies are unchanged from Chat.tsx, only wrapped in
 * this hook. `panelView` is threaded in as an argument rather than owned
 * here since Chat.tsx still owns the `panelView` state itself (used well
 * beyond the drawer).
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useSidebarCollapsed } from "@/hooks/useSidebarCollapsed";
import type { PanelView } from "@/components/chat/shared";

// Drawer exit-animation fallback — slightly longer than the 320ms
// --duration-slow slide-out so animationend normally wins; the timer only
// fires where the animation can't run (jsdom, display:none ancestors).
const DRAWER_EXIT_FALLBACK_MS = 360;

export interface UseMobileDrawerArgs {
  isMobile: boolean;
  chatId: number | null;
  panelView: PanelView;
}

export interface UseMobileDrawerResult {
  sidebarCollapsed: boolean;
  setSidebarCollapsed: (next: boolean | ((prev: boolean) => boolean)) => void;
  mobileDrawerOpen: boolean;
  drawerClosing: boolean;
  /** Wraps `setDrawerClosing(false)` for the render's `onAnimationEnd`. */
  endDrawerClose: () => void;
}

export function useMobileDrawer({
  isMobile,
  chatId,
  panelView,
}: UseMobileDrawerArgs): UseMobileDrawerResult {
  // Default-collapsed on mobile.  Stays as a
  // single boolean so the keyboard shortcut (Cmd+Shift+S) keeps working.
  // Persist across reload + route changes.
  // Default to collapsed on mobile so the chat takes the full screen
  // initially; an explicit toggle wins after that.
  const [sidebarCollapsed, setSidebarCollapsed] = useSidebarCollapsed(isMobile);
  // Mobile drawer open state — when the sidebar is "expanded" on mobile it
  // renders as an overlay rather than reflowing the layout.
  const mobileDrawerOpen = isMobile && !sidebarCollapsed;
  // Drawer exit transition. The shell is
  // a conditional mount, so an instant unmount skipped the exit animation
  // entirely (320ms entrance, 0ms exit). When the drawer closes we keep the
  // shell mounted with the --closing modifier until its slide-out
  // animationend fires; a timeout fallback guarantees the shell can never
  // get stuck mounted (jsdom, display:none ancestors). Reduced-motion
  // users skip the exit state and unmount instantly.
  const [drawerClosing, setDrawerClosing] = useState(false);
  const prevDrawerOpenRef = useRef(mobileDrawerOpen);
  useEffect(() => {
    const wasOpen = prevDrawerOpenRef.current;
    prevDrawerOpenRef.current = mobileDrawerOpen;
    if (mobileDrawerOpen) {
      setDrawerClosing(false);
      return undefined;
    }
    if (!wasOpen || !isMobile) return undefined;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      return undefined;
    }
    setDrawerClosing(true);
    const fallback = window.setTimeout(() => {
      setDrawerClosing(false);
    }, DRAWER_EXIT_FALLBACK_MS);
    return () => {
      window.clearTimeout(fallback);
    };
  }, [mobileDrawerOpen, isMobile]);
  // When a right-panel view opens on
  // mobile, auto-close the sidebar drawer so it doesn't sit alongside.
  // Combined with topBar z-index > backdrop, the user can navigate
  // between sidebar and chat-header surfaces without manual cleanup.
  useEffect(() => {
    if (isMobile && panelView !== null && !sidebarCollapsed) {
      setSidebarCollapsed(true);
    }
  }, [isMobile, panelView, sidebarCollapsed]);

  // When the user selects a chat on mobile (chatId
  // changes), close the mobile drawer so the chat takes the full screen.
  // Only fires when the drawer is actually open — avoids a spurious
  // collapse on desktop or when the drawer is already closed.
  useEffect(() => {
    if (isMobile && mobileDrawerOpen) {
      setSidebarCollapsed(true);
    }
  }, [chatId, isMobile]);

  // Body scroll lock when mobile drawer is open — prevents
  // the background chat from scrolling through the scrim.
  useEffect(() => {
    if (mobileDrawerOpen) {
      const prev = document.body.style.overflow;
      document.body.style.overflow = "hidden";
      return () => {
        document.body.style.overflow = prev;
      };
    }
    return undefined;
  }, [mobileDrawerOpen]);

  const endDrawerClose = useCallback(() => {
    setDrawerClosing(false);
  }, []);

  return {
    sidebarCollapsed,
    setSidebarCollapsed,
    mobileDrawerOpen,
    drawerClosing,
    endDrawerClose,
  };
}
