/* SPDX-License-Identifier: Apache-2.0 */
import { X } from "lucide-react";
import { BrandMark } from "@/components/BrandMark";
import { Sidebar } from "@/components/Sidebar";

// ─── MobileSidebarShell ──────────────────────────────────────────────────────
// Extracted from pages/Chat.tsx. Renders the
// sidebar wrapper (mobile drawer on mobile, inline desktop wrapper on
// desktop) plus the mobile backdrop scrim. JSX bodies are byte-identical to
// the original render; only the drawer-close callback (`endDrawerClose`,
// wraps `setDrawerClosing(false)` — see useMobileDrawer) and
// `onShowKeyboardHelp` cross the component boundary as props since this
// component can no longer reach into Chat's closure directly.

interface MobileSidebarShellProps {
  isMobile: boolean;
  mobileDrawerOpen: boolean;
  drawerClosing: boolean;
  sidebarCollapsed: boolean;
  setSidebarCollapsed: (next: boolean | ((prev: boolean) => boolean)) => void;
  onShowKeyboardHelp: () => void;
  endDrawerClose: () => void;
}

export function MobileSidebarShell({
  isMobile,
  mobileDrawerOpen,
  drawerClosing,
  sidebarCollapsed,
  setSidebarCollapsed,
  onShowKeyboardHelp,
  endDrawerClose,
}: MobileSidebarShellProps) {
  return (
    <>
      {/* Mobile drawer — proper slide-in with backdrop scrim,
          close button, body scroll lock, and keyboard focus management.
          Stays mounted through the slide-out exit while
          drawerClosing is true. Desktop: renders inline (no wrapper). */}
      {(!isMobile || mobileDrawerOpen || drawerClosing) && (
        <div
          className={
            isMobile
              ? mobileDrawerOpen
                ? "lmchat-mobile-sidebar-shell"
                : "lmchat-mobile-sidebar-shell lmchat-mobile-sidebar-shell--closing"
              : // Desktop: a stable hook class so focus mode can slide the
                // whole sidebar slot out of flow (see chat.css .is-focus-mode).
                "lmchat-sidebar-slot"
          }
          onAnimationEnd={(e) => {
            // Only the shell's own slide-out — ignore bubbled child
            // animations (nav rows, skeletons) so they can't unmount
            // the drawer mid-exit.
            if (e.target === e.currentTarget) {
              endDrawerClose();
            }
          }}
          data-testid="sidebar-shell"
        >
          {/* Mobile-only drawer header: brand mark + close button */}
          {isMobile && (
            <div className="lmchat-drawer-header-mobile">
              {/* Brand logo + wordmark row */}
              <div className="lmchat-drawer-brand">
                <BrandMark size={22} aria-hidden />
                {/* --fs-h2 (20px) Hubot Sans 700 editorial weight */}
                <span className="lmchat-brand-wordmark lmchat-brand-wordmark--display">
                  LM Chat
                </span>
              </div>
              <button
                type="button"
                className="lmchat-drawer-close-btn"
                aria-label="Close menu"
                data-testid="sidebar-close-btn"
                onClick={() => {
                  setSidebarCollapsed(true);
                }}
              >
                <X size={14} aria-hidden />
              </button>
            </div>
          )}
          <Sidebar
            collapsed={isMobile ? false : sidebarCollapsed}
            onToggle={() => {
              setSidebarCollapsed((v) => !v);
            }}
            onShowKeyboardHelp={onShowKeyboardHelp}
            mobile={isMobile}
          />
        </div>
      )}
      {/* The scrim stays mounted through the panel's
          slide-out (drawerClosing) and runs its own fade-out — previously it
          vanished instantly and the panel exited floating alone. Its
          animationend clears drawerClosing the same way the panel's does
          (both run 320ms; first to finish unmounts the pair, the panel's
          timeout fallback still guarantees teardown). */}
      {(mobileDrawerOpen || drawerClosing) && (
        <button
          type="button"
          aria-label="Close sidebar"
          onClick={() => {
            setSidebarCollapsed(true);
          }}
          className={
            mobileDrawerOpen
              ? "lmchat-mobile-backdrop"
              : "lmchat-mobile-backdrop lmchat-mobile-backdrop--closing"
          }
          onAnimationEnd={(e) => {
            if (e.target === e.currentTarget) {
              endDrawerClose();
            }
          }}
          data-testid="sidebar-backdrop"
        />
      )}
    </>
  );
}
