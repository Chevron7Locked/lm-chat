/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Cluster 4 — Mobile flow tests (2026-06-10)
 *
 * Covers:
 *   1. useSidebarCollapsed — desktop cold-load from desktop key (ignores old key).
 *   2. useSidebarCollapsed — mobile cold-load always starts collapsed even when
 *      the desktop key was "false" (simulates prior desktop session).
 *   3. useSidebarCollapsed — mobile toggle does NOT write localStorage.
 *   4. useSidebarCollapsed — desktop toggle DOES write the desktop key.
 *
 * Integration-level tests that exercise Chat.tsx directly:
 *   - test_mobile_chat_select_closes_drawer → test_cluster4_task2_drawer_close.spec.tsx
 *   - test_mobile_overflow_menu_has_share_export → test_cluster4_mobile_overflow.spec.tsx
 *
 * This file tests the hook in isolation via renderHook.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";

// ─── localStorage stub ────────────────────────────────────────────────────────
// jsdom has localStorage but we reset between tests.

beforeEach(() => {
  localStorage.clear();
  vi.resetModules();
});

afterEach(() => {
  localStorage.clear();
});

// ─── Helper: import the hook fresh (module-level constants need fresh import) ──
async function freshHook() {
  const mod = await import("@/hooks/useSidebarCollapsed");
  return mod;
}

// ─── Tests ───────────────────────────────────────────────────────────────────

describe("useSidebarCollapsed — desktop", () => {
  it("defaults to false when nothing stored", async () => {
    const { useSidebarCollapsed } = await freshHook();
    const { result } = renderHook(() => useSidebarCollapsed(false));
    expect(result.current[0]).toBe(false);
  });

  it("reads from the desktop key on cold load", async () => {
    const { useSidebarCollapsed, SIDEBAR_DESKTOP_KEY } = await freshHook();
    localStorage.setItem(SIDEBAR_DESKTOP_KEY, "true");
    const { result } = renderHook(() => useSidebarCollapsed(false));
    expect(result.current[0]).toBe(true);
  });

  it("writes the desktop key when toggled", async () => {
    const { useSidebarCollapsed, SIDEBAR_DESKTOP_KEY } = await freshHook();
    const { result } = renderHook(() => useSidebarCollapsed(false));
    act(() => {
      result.current[1](true);
    });
    expect(localStorage.getItem(SIDEBAR_DESKTOP_KEY)).toBe("true");
  });

  it("does NOT write the old combined key", async () => {
    const { useSidebarCollapsed, __SIDEBAR_COLLAPSED_KEY } = await freshHook();
    const { result } = renderHook(() => useSidebarCollapsed(false));
    act(() => {
      result.current[1](true);
    });
    expect(localStorage.getItem(__SIDEBAR_COLLAPSED_KEY)).toBeNull();
  });
});

describe("useSidebarCollapsed — mobile cold-load ignores desktop key", () => {
  it("test_mobile_cold_load_sidebar_collapsed_when_desktop_key_was_expanded: always starts collapsed on mobile", async () => {
    const { useSidebarCollapsed, SIDEBAR_DESKTOP_KEY } = await freshHook();
    // Simulate prior desktop session that left the sidebar expanded (false = not collapsed).
    localStorage.setItem(SIDEBAR_DESKTOP_KEY, "false");

    const { result } = renderHook(() => useSidebarCollapsed(true));
    // Mobile cold-load should always start with the drawer closed (collapsed = true).
    expect(result.current[0]).toBe(true);
  });

  it("mobile toggle does NOT write localStorage", async () => {
    const { useSidebarCollapsed, SIDEBAR_DESKTOP_KEY, SIDEBAR_MOBILE_KEY } = await freshHook();
    const { result } = renderHook(() => useSidebarCollapsed(true));

    // Open the drawer (collapsed = false)
    act(() => {
      result.current[1](false);
    });

    // Neither the desktop key nor the mobile key should be written.
    expect(localStorage.getItem(SIDEBAR_DESKTOP_KEY)).toBeNull();
    expect(localStorage.getItem(SIDEBAR_MOBILE_KEY)).toBeNull();
  });

  it("mobile toggle updates in-memory state only", async () => {
    const { useSidebarCollapsed } = await freshHook();
    const { result } = renderHook(() => useSidebarCollapsed(true));

    // Initially collapsed.
    expect(result.current[0]).toBe(true);

    // Open the drawer.
    act(() => {
      result.current[1](false);
    });
    expect(result.current[0]).toBe(false);

    // Close again.
    act(() => {
      result.current[1](true);
    });
    expect(result.current[0]).toBe(true);
  });
});

// test_mobile_chat_select_closes_drawer lives in:
//   test_cluster4_task2_drawer_close.spec.tsx
// It renders the full Chat page with isMobile=true and navigates between
// chatIds to verify the useEffect at Chat.tsx:~178-183 fires correctly.
