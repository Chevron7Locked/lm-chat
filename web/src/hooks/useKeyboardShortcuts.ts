/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useKeyboardShortcuts — registers global keyboard shortcuts for lm-chat.
 *
 * Shortcuts:
 *   Cmd/Ctrl+K          — focus search input (via composerRef or searchRef)
 *   Cmd/Ctrl+/          — open command palette
 *   Cmd/Ctrl+Enter      — submit composer (handled in Composer.tsx directly)
 *   Cmd/Ctrl+N          — new chat
 *   Cmd/Ctrl+Shift+S    — toggle sidebar collapse
 *   Cmd/Ctrl+,          — open Settings
 *   Cmd/Ctrl+Shift+E    — open the chat export menu
 *   Cmd/Ctrl+.          — toggle focus mode
 *   ?                   — open keyboard help modal (only fires
 *                         when no editable element has focus)
 *   Esc                 — close panels / toasts / slash menu
 *   Cmd/Ctrl+Shift+L    — toggle theme cycle (dark → light → system → dark)
 *   Cmd/Ctrl+J          — toggle thinking blocks
 *
 * This hook attaches window-level listeners. Components that need per-element
 * shortcuts (Composer Enter) handle those locally.
 */
import { useEffect } from "react";
import { useThemeStore } from "@/stores/themeStore";
import type { Theme } from "@/stores/themeStore";

interface ShortcutHandlers {
  /** Called when Cmd/Ctrl+K is pressed. */
  onFocusSearch?: (() => void) | undefined;
  /** Called when Cmd/Ctrl+/ is pressed. */
  onCommandPalette?: (() => void) | undefined;
  /** Called when Esc is pressed. */
  onEscape?: (() => void) | undefined;
  /** Called when Cmd/Ctrl+J is pressed. */
  onToggleThinking?: (() => void) | undefined;
  /** Called when Cmd/Ctrl+N is pressed (new chat). */
  onNewChat?: (() => void) | undefined;
  /** Called when Cmd/Ctrl+Shift+S is pressed (toggle sidebar). */
  onToggleSidebar?: (() => void) | undefined;
  /** Called when Cmd/Ctrl+, is pressed (open settings). */
  onOpenSettings?: (() => void) | undefined;
  /** Called when ? is pressed with no editable element focused. */
  onShowHelp?: (() => void) | undefined;
  /** Called when Cmd/Ctrl+Shift+E is pressed (export chat). */
  onExportChat?: (() => void) | undefined;
  /** Called when Cmd/Ctrl+. is pressed (toggle focus mode). */
  onToggleFocusMode?: (() => void) | undefined;
}

const THEME_CYCLE: Theme[] = ["dark", "light", "system"];

/**
 * Return true when the currently focused element is one that swallows raw
 * text input (input/textarea/contenteditable). Used to gate single-key,
 * unmodified shortcuts like `?` so they don't fire while the user is
 * typing into the composer.
 */
function isEditableTarget(target: EventTarget | null): boolean {
  if (target === null || !(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
  // `isContentEditable` is the spec property but jsdom returns false even when
  // contenteditable="true" is set (no rendering). Fall back to the attribute
  // / contentEditable string so the guard works in both real browsers and the
  // unit-test environment.
  if (target.isContentEditable) return true;
  if (
    target.contentEditable === "true" ||
    target.contentEditable === "plaintext-only"
  ) {
    return true;
  }
  return false;
}

export function useKeyboardShortcuts(handlers: ShortcutHandlers): void {
  const setTheme = useThemeStore((s) => s.setTheme);
  const theme = useThemeStore((s) => s.theme);

  useEffect(() => {
    function handle(e: KeyboardEvent): void {
      const mod = e.metaKey || e.ctrlKey;

      if (mod && !e.shiftKey && e.key === "k") {
        e.preventDefault();
        handlers.onFocusSearch?.();
        return;
      }

      if (mod && !e.shiftKey && e.key === "/") {
        e.preventDefault();
        handlers.onCommandPalette?.();
        return;
      }

      // Cmd/Ctrl+N — new chat.
      if (mod && !e.shiftKey && (e.key === "n" || e.key === "N")) {
        e.preventDefault();
        handlers.onNewChat?.();
        return;
      }

      // Cmd/Ctrl+Shift+S — toggle sidebar collapse.
      if (mod && e.shiftKey && (e.key === "s" || e.key === "S")) {
        e.preventDefault();
        handlers.onToggleSidebar?.();
        return;
      }

      // Cmd/Ctrl+Shift+E — open the chat export menu.
      // Placed BEFORE the bare-letter `?` branch so the Shift+E chord
      // doesn't fall through to a `?`-like key dispatch on layouts where
      // shifted "/" is "?".
      if (mod && e.shiftKey && (e.key === "e" || e.key === "E")) {
        e.preventDefault();
        handlers.onExportChat?.();
        return;
      }

      // Cmd/Ctrl+, — open Settings.
      if (mod && !e.shiftKey && e.key === ",") {
        e.preventDefault();
        handlers.onOpenSettings?.();
        return;
      }

      // Cmd/Ctrl+. — toggle focus mode.
      if (mod && !e.shiftKey && e.key === ".") {
        e.preventDefault();
        handlers.onToggleFocusMode?.();
        return;
      }

      if (e.key === "Escape") {
        handlers.onEscape?.();
        return;
      }

      if (mod && e.shiftKey && e.key === "L") {
        e.preventDefault();
        const idx = THEME_CYCLE.indexOf(theme);
        const next = THEME_CYCLE[(idx + 1) % THEME_CYCLE.length];
        if (next !== undefined) setTheme(next);
        return;
      }

      if (mod && !e.shiftKey && e.key === "j") {
        e.preventDefault();
        if (handlers.onToggleThinking !== undefined) {
          handlers.onToggleThinking();
        }
        return;
      }

      // Unmodified `?` opens the keyboard help modal, but ONLY
      // when no editable element is focused (so a user typing "?" into the
      // composer doesn't accidentally trigger it). Most layouts deliver "?"
      // as Shift+/, so we explicitly accept either key === "?" or
      // (shift && key === "/").
      if (!mod && (e.key === "?" || (e.shiftKey && e.key === "/"))) {
        if (isEditableTarget(e.target)) return;
        e.preventDefault();
        handlers.onShowHelp?.();
        return;
      }
    }

    window.addEventListener("keydown", handle);
    return () => {
      window.removeEventListener("keydown", handle);
    };
  }, [handlers, theme, setTheme]);
}
