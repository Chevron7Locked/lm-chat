/* SPDX-License-Identifier: Apache-2.0 */
/**
 * SlashPalette — modal command picker opened by Cmd/Ctrl + /.
 *
 * Distinct from the Composer's inline SlashMenu (which opens by typing "/"
 * as the first character of the message).  The palette is a global
 * overlay; the menu is a Composer-local autocomplete.  Both share the
 * BUILTIN_COMMANDS registry.
 *
 * UX:
 *   - Arrow Up / Down navigate the list, wrapping at the ends.
 *   - Enter executes the highlighted command via the onSelect callback.
 *   - Esc closes the palette without executing.
 *   - Clicking outside the palette closes it (handled by the backdrop).
 *   - Type-ahead filtering: any text typed into the search input prefix-
 *     filters BUILTIN_COMMANDS.
 *
 * The palette deliberately does NOT execute the command itself — Composer
 * already owns the slash-command dispatch logic.  When the user picks a
 * command from the palette we hand it off to Composer by populating the
 * textarea with the command text and focusing it; the user can add args
 * and submit normally.  This keeps a single dispatch site for slash
 * commands.
 */
import { useEffect, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent } from "react";
import { BUILTIN_COMMANDS, filterCommands } from "@/components/SlashMenu";
import type { SlashCommand } from "@/components/SlashMenu";
import { useFocusTrap } from "@/hooks/useFocusTrap";

// ─── Props ──────────────────────────────────────────────────────────────────

export interface SlashPaletteProps {
  /** Whether the palette is currently visible. */
  open: boolean;
  /** Called when the palette should close (Esc, backdrop click). */
  onClose: () => void;
  /** Called with the selected command when the user picks one. */
  onSelect: (cmd: SlashCommand) => void;
}

// ─── Component ──────────────────────────────────────────────────────────────

export function SlashPalette({ open, onClose, onSelect }: SlashPaletteProps) {
  const [query, setQuery] = useState("");
  const [highlightIdx, setHighlightIdx] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const cardRef = useRef<HTMLDivElement>(null);

  // WCAG 2.1.2: trap focus inside the palette while open; restore on close.
  // initialFocusRef points to the search input so typing works immediately.
  useFocusTrap(cardRef, open, { initialFocusRef: inputRef });

  // Reset state every time the palette opens.
  useEffect(() => {
    if (open) {
      setQuery("");
      setHighlightIdx(0);
    }
  }, [open]);

  if (!open) return null;

  const matches: SlashCommand[] =
    query === "" ? BUILTIN_COMMANDS : filterCommands(query);
  const safeIdx = Math.min(highlightIdx, Math.max(0, matches.length - 1));

  function commitSelection(cmd: SlashCommand | undefined): void {
    if (cmd === undefined) return;
    if (cmd.comingSoon === true) {
      // Don't dispatch a coming-soon command; just close.
      onClose();
      return;
    }
    onSelect(cmd);
    onClose();
  }

  function handleKeyDown(e: ReactKeyboardEvent<HTMLInputElement>): void {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setHighlightIdx((i) =>
        matches.length === 0 ? 0 : (i + 1) % matches.length,
      );
      return;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      setHighlightIdx((i) =>
        matches.length === 0 ? 0 : (i - 1 + matches.length) % matches.length,
      );
      return;
    }
    if (e.key === "Enter") {
      e.preventDefault();
      commitSelection(matches[safeIdx]);
      return;
    }
    if (e.key === "Escape") {
      e.preventDefault();
      onClose();
      return;
    }
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Slash command palette"
      className="lmchat-palette-backdrop"
      onClick={(e) => {
        // Backdrop click (not card click) closes the palette.
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div ref={cardRef} className="lmchat-palette-card">
        <input
          ref={inputRef}
          type="text"
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setHighlightIdx(0);
          }}
          onKeyDown={handleKeyDown}
          placeholder="Type a command…"
          aria-label="Search commands"
          className="lmchat-palette-input"
        />

        <ul
          role="listbox"
          aria-label="Available slash commands"
          className="lmchat-palette-list"
        >
          {matches.length === 0 ? (
            <li className="lmchat-palette-empty">
              No commands match "{query}"
            </li>
          ) : (
            matches.map((cmd, i) => (
              <li
                key={cmd.name}
                role="option"
                aria-selected={i === safeIdx}
                className={`lmchat-palette-item${i === safeIdx ? " lmchat-palette-item--active" : ""}`}
                onMouseEnter={() => {
                  setHighlightIdx(i);
                }}
                onMouseDown={(e) => {
                  // Use onMouseDown so the click commits before the input
                  // blur swaps focus + closes the palette.
                  e.preventDefault();
                  commitSelection(cmd);
                }}
              >
                <span className="lmchat-palette-cmd-name">/{cmd.name}</span>
                <span className="lmchat-palette-cmd-desc">
                  {cmd.description}
                </span>
                {cmd.comingSoon === true && (
                  <span className="lmchat-palette-badge">soon</span>
                )}
              </li>
            ))
          )}
        </ul>

        <div className="lmchat-palette-footer">
          <kbd className="lmchat-palette-kbd">↑↓</kbd> navigate
          <kbd className="lmchat-palette-kbd">Enter</kbd> run
          <kbd className="lmchat-palette-kbd">Esc</kbd> close
        </div>
      </div>
    </div>
  );
}
