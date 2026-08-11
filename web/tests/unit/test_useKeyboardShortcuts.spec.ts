/**
 * Unit tests for useKeyboardShortcuts.
 *
 * Strategy: render the hook in a test component; fire synthetic keyboard
 * events on window; verify that the appropriate handler callbacks fire.
 *
 * themeStore is mocked because it calls window.matchMedia at module init time
 * and vitest runs module-level side effects before test setup.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";

// ─── Mock themeStore to avoid window.matchMedia dependency ───────────────────

const mockSetTheme = vi.fn();
const mockTheme = { theme: "dark" as "dark" | "light" | "system", setTheme: mockSetTheme };

vi.mock("@/stores/themeStore", () => ({
  useThemeStore: (selector: (s: typeof mockTheme) => unknown) => selector(mockTheme),
}));

import { useKeyboardShortcuts } from "@/hooks/useKeyboardShortcuts";

// ─── Helpers ─────────────────────────────────────────────────────────────────

/** Dispatch a KeyboardEvent on window. */
function fire(
  key: string,
  opts: { metaKey?: boolean; ctrlKey?: boolean; shiftKey?: boolean } = {}
): void {
  window.dispatchEvent(
    new KeyboardEvent("keydown", {
      key,
      metaKey: opts.metaKey ?? false,
      ctrlKey: opts.ctrlKey ?? false,
      shiftKey: opts.shiftKey ?? false,
      bubbles: true,
    })
  );
}

// ─── Tests ────────────────────────────────────────────────────────────────────

describe("useKeyboardShortcuts", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // Reset mockTheme to defaults.
    mockTheme.theme = "dark";
  });

  it("calls onFocusSearch on Cmd+K", () => {
    const onFocusSearch = vi.fn();
    renderHook(() => useKeyboardShortcuts({ onFocusSearch }));

    act(() => { fire("k", { metaKey: true }); });
    expect(onFocusSearch).toHaveBeenCalledOnce();
  });

  it("calls onFocusSearch on Ctrl+K", () => {
    const onFocusSearch = vi.fn();
    renderHook(() => useKeyboardShortcuts({ onFocusSearch }));

    act(() => { fire("k", { ctrlKey: true }); });
    expect(onFocusSearch).toHaveBeenCalledOnce();
  });

  it("calls onCommandPalette on Cmd+/", () => {
    const onCommandPalette = vi.fn();
    renderHook(() => useKeyboardShortcuts({ onCommandPalette }));

    act(() => { fire("/", { metaKey: true }); });
    expect(onCommandPalette).toHaveBeenCalledOnce();
  });

  it("calls onCommandPalette on Ctrl+/", () => {
    const onCommandPalette = vi.fn();
    renderHook(() => useKeyboardShortcuts({ onCommandPalette }));

    act(() => { fire("/", { ctrlKey: true }); });
    expect(onCommandPalette).toHaveBeenCalledOnce();
  });

  it("calls onEscape on Escape key", () => {
    const onEscape = vi.fn();
    renderHook(() => useKeyboardShortcuts({ onEscape }));

    act(() => { fire("Escape"); });
    expect(onEscape).toHaveBeenCalledOnce();
  });

  it("calls onToggleThinking on Cmd+J", () => {
    const onToggleThinking = vi.fn();
    renderHook(() => useKeyboardShortcuts({ onToggleThinking }));

    act(() => { fire("j", { metaKey: true }); });
    expect(onToggleThinking).toHaveBeenCalledOnce();
  });

  it("does not call handlers on unrelated keys", () => {
    const onFocusSearch = vi.fn();
    const onCommandPalette = vi.fn();
    renderHook(() => useKeyboardShortcuts({ onFocusSearch, onCommandPalette }));

    act(() => { fire("z", { metaKey: true }); });
    expect(onFocusSearch).not.toHaveBeenCalled();
    expect(onCommandPalette).not.toHaveBeenCalled();
  });

  it("does not call onFocusSearch on plain K (no modifier)", () => {
    const onFocusSearch = vi.fn();
    renderHook(() => useKeyboardShortcuts({ onFocusSearch }));

    act(() => { fire("k"); });
    expect(onFocusSearch).not.toHaveBeenCalled();
  });

  it("Cmd+Shift+L cycles theme via setTheme", () => {
    renderHook(() => useKeyboardShortcuts({}));

    act(() => { fire("L", { metaKey: true, shiftKey: true }); });
    // Starting from "dark" → cycles to "light".
    expect(mockSetTheme).toHaveBeenCalledWith("light");
  });

  it("removes listener on unmount", () => {
    const onEscape = vi.fn();
    const { unmount } = renderHook(() => useKeyboardShortcuts({ onEscape }));
    unmount();

    act(() => { fire("Escape"); });
    expect(onEscape).not.toHaveBeenCalled();
  });

  // ─── P13d additions (K-01, K-02, K-03, K-10) ───────────────────────────────

  it("calls onNewChat on Cmd+N (K-01)", () => {
    const onNewChat = vi.fn();
    renderHook(() => useKeyboardShortcuts({ onNewChat }));

    act(() => { fire("n", { metaKey: true }); });
    expect(onNewChat).toHaveBeenCalledOnce();
  });

  it("calls onNewChat on Ctrl+N (K-01)", () => {
    const onNewChat = vi.fn();
    renderHook(() => useKeyboardShortcuts({ onNewChat }));

    act(() => { fire("n", { ctrlKey: true }); });
    expect(onNewChat).toHaveBeenCalledOnce();
  });

  it("does NOT call onNewChat on Cmd+Shift+N (modifier guard)", () => {
    const onNewChat = vi.fn();
    renderHook(() => useKeyboardShortcuts({ onNewChat }));

    act(() => { fire("N", { metaKey: true, shiftKey: true }); });
    expect(onNewChat).not.toHaveBeenCalled();
  });

  it("calls onToggleSidebar on Cmd+Shift+S (K-02)", () => {
    const onToggleSidebar = vi.fn();
    renderHook(() => useKeyboardShortcuts({ onToggleSidebar }));

    act(() => { fire("S", { metaKey: true, shiftKey: true }); });
    expect(onToggleSidebar).toHaveBeenCalledOnce();
  });

  it("calls onToggleSidebar on Ctrl+Shift+S (K-02)", () => {
    const onToggleSidebar = vi.fn();
    renderHook(() => useKeyboardShortcuts({ onToggleSidebar }));

    act(() => { fire("S", { ctrlKey: true, shiftKey: true }); });
    expect(onToggleSidebar).toHaveBeenCalledOnce();
  });

  it("calls onOpenSettings on Cmd+, (K-03)", () => {
    const onOpenSettings = vi.fn();
    renderHook(() => useKeyboardShortcuts({ onOpenSettings }));

    act(() => { fire(",", { metaKey: true }); });
    expect(onOpenSettings).toHaveBeenCalledOnce();
  });

  it("calls onOpenSettings on Ctrl+, (K-03)", () => {
    const onOpenSettings = vi.fn();
    renderHook(() => useKeyboardShortcuts({ onOpenSettings }));

    act(() => { fire(",", { ctrlKey: true }); });
    expect(onOpenSettings).toHaveBeenCalledOnce();
  });

  it("calls onShowHelp on bare ? key (K-10)", () => {
    const onShowHelp = vi.fn();
    renderHook(() => useKeyboardShortcuts({ onShowHelp }));

    act(() => { fire("?"); });
    expect(onShowHelp).toHaveBeenCalledOnce();
  });

  it("calls onShowHelp on Shift+/ (which produces ? on US layouts)", () => {
    const onShowHelp = vi.fn();
    renderHook(() => useKeyboardShortcuts({ onShowHelp }));

    act(() => { fire("/", { shiftKey: true }); });
    expect(onShowHelp).toHaveBeenCalledOnce();
  });

  it("does NOT call onShowHelp when ? is pressed while an INPUT is focused", () => {
    const onShowHelp = vi.fn();
    renderHook(() => useKeyboardShortcuts({ onShowHelp }));

    // Mount a real input and focus it so the keydown's target matches.
    const input = document.createElement("input");
    document.body.appendChild(input);
    input.focus();

    act(() => {
      input.dispatchEvent(
        new KeyboardEvent("keydown", { key: "?", bubbles: true })
      );
    });
    expect(onShowHelp).not.toHaveBeenCalled();

    document.body.removeChild(input);
  });

  it("does NOT call onShowHelp when ? is pressed while a TEXTAREA is focused", () => {
    const onShowHelp = vi.fn();
    renderHook(() => useKeyboardShortcuts({ onShowHelp }));

    const ta = document.createElement("textarea");
    document.body.appendChild(ta);
    ta.focus();

    act(() => {
      ta.dispatchEvent(
        new KeyboardEvent("keydown", { key: "?", bubbles: true })
      );
    });
    expect(onShowHelp).not.toHaveBeenCalled();

    document.body.removeChild(ta);
  });

  it("does NOT call onShowHelp when ? is pressed in a contenteditable", () => {
    const onShowHelp = vi.fn();
    renderHook(() => useKeyboardShortcuts({ onShowHelp }));

    const div = document.createElement("div");
    // jsdom's `isContentEditable` reflects the property, not the attribute,
    // so set both for robustness across jsdom + real-browser semantics.
    div.setAttribute("contenteditable", "true");
    div.contentEditable = "true";
    document.body.appendChild(div);
    div.focus();

    act(() => {
      div.dispatchEvent(
        new KeyboardEvent("keydown", { key: "?", bubbles: true })
      );
    });
    expect(onShowHelp).not.toHaveBeenCalled();

    document.body.removeChild(div);
  });

  // ───────── P13j: Cmd/Ctrl+Shift+E export shortcut ─────────
  it("calls onExportChat on Cmd+Shift+E (P13j K-04)", () => {
    const onExportChat = vi.fn();
    renderHook(() => useKeyboardShortcuts({ onExportChat }));

    act(() => { fire("E", { metaKey: true, shiftKey: true }); });
    expect(onExportChat).toHaveBeenCalledOnce();
  });

  it("calls onExportChat on Ctrl+Shift+E with lowercase key (P13j K-04)", () => {
    const onExportChat = vi.fn();
    renderHook(() => useKeyboardShortcuts({ onExportChat }));

    act(() => { fire("e", { ctrlKey: true, shiftKey: true }); });
    expect(onExportChat).toHaveBeenCalledOnce();
  });

  it("does NOT call onExportChat for bare Shift+E (no modifier)", () => {
    const onExportChat = vi.fn();
    renderHook(() => useKeyboardShortcuts({ onExportChat }));

    act(() => { fire("E", { shiftKey: true }); });
    expect(onExportChat).not.toHaveBeenCalled();
  });
});
