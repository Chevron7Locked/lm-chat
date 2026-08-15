/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useKeyboardShortcuts — focus-mode chord (⌘/Ctrl+.) + Esc routing.
 *
 * Locks the wiring the design brief requires:
 *  - Cmd/Ctrl+. toggles focus mode (both the Ctrl and Meta modifiers).
 *  - a bare "." or a Shift-modified chord must NOT toggle.
 *  - Esc routes to onEscape (Chat's onEscape chain is what exits focus mode).
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { renderHook, cleanup, fireEvent } from "@testing-library/react";
import { useKeyboardShortcuts } from "@/hooks/useKeyboardShortcuts";

// The hook reads the theme store for the theme-cycle chord; stub it so the
// hook can mount without the real store.
vi.mock("@/stores/themeStore", () => ({
  useThemeStore: (sel: (s: { theme: string; setTheme: () => void }) => unknown) =>
    sel({ theme: "dark", setTheme: vi.fn() }),
}));

afterEach(() => {
  cleanup();
});

describe("useKeyboardShortcuts — focus mode (⌘/Ctrl+.)", () => {
  it("calls onToggleFocusMode on Ctrl+.", () => {
    const onToggleFocusMode = vi.fn();
    renderHook(() => { useKeyboardShortcuts({ onToggleFocusMode }); });
    fireEvent.keyDown(window, { key: ".", ctrlKey: true });
    expect(onToggleFocusMode).toHaveBeenCalledOnce();
  });

  it("calls onToggleFocusMode on Meta+. (Mac)", () => {
    const onToggleFocusMode = vi.fn();
    renderHook(() => { useKeyboardShortcuts({ onToggleFocusMode }); });
    fireEvent.keyDown(window, { key: ".", metaKey: true });
    expect(onToggleFocusMode).toHaveBeenCalledOnce();
  });

  it("does NOT toggle on a bare '.' with no modifier", () => {
    const onToggleFocusMode = vi.fn();
    renderHook(() => { useKeyboardShortcuts({ onToggleFocusMode }); });
    fireEvent.keyDown(window, { key: "." });
    expect(onToggleFocusMode).not.toHaveBeenCalled();
  });

  it("does NOT toggle on Ctrl+Shift+.", () => {
    const onToggleFocusMode = vi.fn();
    renderHook(() => { useKeyboardShortcuts({ onToggleFocusMode }); });
    fireEvent.keyDown(window, { key: ".", ctrlKey: true, shiftKey: true });
    expect(onToggleFocusMode).not.toHaveBeenCalled();
  });

  it("routes Esc to onEscape (Chat's focus-mode exit path)", () => {
    const onEscape = vi.fn();
    renderHook(() => { useKeyboardShortcuts({ onEscape }); });
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onEscape).toHaveBeenCalledOnce();
  });
});
