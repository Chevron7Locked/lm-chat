/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useSidebarCollapsed — sidebar collapse state with separate desktop/mobile keys.
 *
 * The single `lmchat:sidebar:collapsed` key is split into two distinct keys:
 *
 *   - Desktop: `lmchat:sidebar:desktop:collapsed` (persisted, default false)
 *   - Mobile:  `lmchat:sidebar:mobile:open` (session-only, never persisted,
 *              always closed on cold load regardless of desktop state)
 *
 * The old `lmchat:sidebar:collapsed` key is ignored (hard-cut).
 * Mobile drawer state is held purely in React state (no localStorage write)
 * so a cold load to /chats/:id always starts with the drawer closed.
 */
import { useCallback, useEffect, useState } from "react";

/** Desktop persistence key (persisted across reload). */
export const SIDEBAR_DESKTOP_KEY = "lmchat:sidebar:desktop:collapsed";

/**
 * Mobile open key — exported for tests that need to verify it is never written.
 * The mobile drawer state is never actually written to localStorage; this constant
 * names the logical key for documentation and test assertions.
 */
export const SIDEBAR_MOBILE_KEY = "lmchat:sidebar:mobile:open";

/**
 * @deprecated - The old combined key. Exported so tests can seed legacy
 * state and verify the split keys take precedence.
 */
export const __SIDEBAR_COLLAPSED_KEY = "lmchat:sidebar:collapsed";

function readDesktopStored(): boolean {
  if (typeof window === "undefined") return false;
  try {
    const raw = localStorage.getItem(SIDEBAR_DESKTOP_KEY);
    // Only use the desktop key. Never fall back to the old combined key.
    return raw === null ? false : raw === "true";
  } catch {
    return false;
  }
}

function writeDesktopStored(value: boolean): void {
  if (typeof window === "undefined") return;
  try {
    localStorage.setItem(SIDEBAR_DESKTOP_KEY, value ? "true" : "false");
  } catch {
    // localStorage can throw in private browsing — silently ignore.
  }
}

/**
 * useSidebarCollapsed(isMobile)
 *
 * Returns [collapsed, setCollapsed] where:
 *
 * - Desktop: `collapsed` is loaded from / persisted to
 *   `lmchat:sidebar:desktop:collapsed`. Cross-tab sync via `storage` event.
 *
 * - Mobile: `collapsed` is purely in-memory (always true = drawer-closed on
 *   cold load). Toggling does NOT write localStorage so the mobile drawer
 *   state never bleeds into the desktop experience or across sessions.
 */
export function useSidebarCollapsed(
  isMobile = false,
): [boolean, (next: boolean | ((prev: boolean) => boolean)) => void] {
  // Desktop: initialize from localStorage (desktop key only).
  // Mobile: always start collapsed (drawer closed on cold load).
  const [value, setValue] = useState<boolean>(() => {
    if (isMobile) return true; // mobile cold-load: drawer always closed
    return readDesktopStored();
  });

  // Re-initialise when isMobile flips (e.g., viewport resize crossing
  // the breakpoint). Desktop → read stored; Mobile → force closed.
  const [prevIsMobile, setPrevIsMobile] = useState(isMobile);
  if (prevIsMobile !== isMobile) {
    setPrevIsMobile(isMobile);
    setValue(isMobile ? true : readDesktopStored());
  }

  // Cross-tab sync for the desktop key only.
  useEffect(() => {
    if (isMobile || typeof window === "undefined") return;
    const onStorage = (e: StorageEvent): void => {
      if (e.key !== SIDEBAR_DESKTOP_KEY) return;
      setValue(e.newValue === "true");
    };
    window.addEventListener("storage", onStorage);
    return () => { window.removeEventListener("storage", onStorage); };
  }, [isMobile]);

  const set = useCallback(
    (next: boolean | ((prev: boolean) => boolean)): void => {
      setValue((prev) => {
        const resolved = typeof next === "function" ? next(prev) : next;
        // Mobile: session-only — never write localStorage.
        // Desktop: persist to the desktop key.
        if (!isMobile) {
          writeDesktopStored(resolved);
        }
        return resolved;
      });
    },
    [isMobile],
  );

  return [value, set];
}
