/* SPDX-License-Identifier: Apache-2.0 */
/**
 * KeyboardHelp — modal listing every global keyboard shortcut and every
 * built-in slash command.
 *
 * Closes an audit finding (no help dialog in v1; v0.5.x had `#kb-modal`
 * at `index.html:504-517`).
 *
 * UX:
 *   - Opened by pressing `?` (no modifier; only when no editable element
 *     is focused — gating handled in `useKeyboardShortcuts.ts`) OR by
 *     clicking the `?` button in the Sidebar footer.
 *   - Closed by Esc, by clicking the backdrop, or by clicking the close
 *     button in the modal's top-right corner.
 *   - Pure presentational — no app state; receives `open` + `onClose`.
 *   - Two sections: shortcuts (keyed by chord) and slash commands (sourced
 *     from `BUILTIN_COMMANDS` so the list stays in sync with SlashMenu).
 */
import { useEffect, useRef } from "react";
import { X } from "lucide-react";
import { BUILTIN_COMMANDS } from "@/components/SlashMenu";
import { usePlatform } from "@/hooks/usePlatform";
import { formatShortcut, type Chord } from "@/lib/formatShortcut";
import { useFocusTrap } from "@/hooks/useFocusTrap";
import "@/styles/keyboard-help.css";

// ─── Types ──────────────────────────────────────────────────────────────────

export interface KeyboardHelpProps {
  /** Whether the modal is currently visible. */
  open: boolean;
  /** Called when the modal should close (Esc, backdrop, close button). */
  onClose: () => void;
}

interface ShortcutEntry {
  /** Structured chord; rendered by formatShortcut to the platform idiom. */
  chord: Chord | null;
  /** Literal label for non-modifier keys (?, Esc). */
  literal?: string;
  /** Human-readable description. */
  description: string;
}

// ─── Shortcut registry ──────────────────────────────────────────────────────

export const SHORTCUTS: ShortcutEntry[] = [
  { chord: { mod: true, key: "N" }, description: "New chat" },
  {
    chord: { mod: true, shift: true, key: "S" },
    description: "Toggle sidebar",
  },
  { chord: { mod: true, key: "," }, description: "Open settings" },
  { chord: { mod: true, key: "." }, description: "Toggle focus mode" },
  { chord: { mod: true, key: "K" }, description: "Focus chat filter" },
  { chord: { mod: true, key: "/" }, description: "Open command palette" },
  { chord: { mod: true, key: "Enter" }, description: "Send message" },
  {
    chord: { mod: true, shift: true, key: "M" },
    description: "Toggle voice input",
  },
  {
    chord: { mod: true, shift: true, key: "L" },
    description: "Cycle theme (dark → light → system)",
  },
  { chord: { mod: true, key: "J" }, description: "Toggle thinking blocks" },
  {
    chord: { mod: true, shift: true, key: "E" },
    description: "Open chat export menu",
  },
  { chord: null, literal: "?", description: "Show keyboard shortcuts" },
  {
    chord: null,
    literal: "Esc",
    description: "Close overlay (palette / panel / menu)",
  },
];

// ─── Component ──────────────────────────────────────────────────────────────

export function KeyboardHelp({ open, onClose }: KeyboardHelpProps) {
  const platform = usePlatform();
  const cardRef = useRef<HTMLDivElement>(null);

  // WCAG 2.1.2: trap focus inside the modal while open; restore on close.
  useFocusTrap(cardRef, open);

  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent): void {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
    };
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="kbd-help-title"
      className="kbd-backdrop"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div ref={cardRef} className="kbd-card" data-testid="keyboard-help-card">
        <header className="kbd-header">
          <h2 id="kbd-help-title" className="kbd-title">
            Keyboard shortcuts
          </h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close shortcuts"
            className="kbd-close-btn"
            data-testid="keyboard-help-close"
          >
            <X size={14} aria-hidden />
          </button>
        </header>

        <div className="kbd-body">
          <section className="kbd-section" aria-labelledby="kbd-help-shortcuts">
            <h3 id="kbd-help-shortcuts" className="kbd-section-title">
              Shortcuts
            </h3>
            <ul className="kbd-list">
              {SHORTCUTS.map((s) => {
                const label =
                  s.chord !== null
                    ? formatShortcut(platform, s.chord)
                    : (s.literal ?? "");
                return (
                  <li key={label} className="kbd-row">
                    <kbd className="kbd-key">{label}</kbd>
                    <span className="kbd-desc">{s.description}</span>
                  </li>
                );
              })}
            </ul>
          </section>

          <section className="kbd-section" aria-labelledby="kbd-help-slash">
            <h3 id="kbd-help-slash" className="kbd-section-title">
              Slash commands
            </h3>
            <ul className="kbd-list">
              {BUILTIN_COMMANDS.map((cmd) => (
                <li key={cmd.name} className="kbd-row">
                  <kbd className="kbd-key">/{cmd.name}</kbd>
                  <span className="kbd-desc">
                    {cmd.description}
                    {cmd.comingSoon === true && (
                      <span className="kbd-badge">soon</span>
                    )}
                  </span>
                </li>
              ))}
            </ul>
          </section>
        </div>

        <footer className="kbd-footer">
          <span>
            <kbd className="kbd-key-inline">Esc</kbd> to close
          </span>
        </footer>
      </div>
    </div>
  );
}
