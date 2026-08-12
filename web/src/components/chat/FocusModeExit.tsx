/* SPDX-License-Identifier: Apache-2.0 */
import { Minimize2 } from "lucide-react";

interface FocusModeExitProps {
  /** Whether focus mode is active — the affordance renders only when true. */
  active: boolean;
  /** Leave focus mode. */
  onExit: () => void;
}

/**
 * FocusModeExit — the slim, always-reachable "leave focus mode" affordance.
 *
 * Renders only in focus mode and pins to the top of the reading column so the
 * user is never stranded once the top chrome is hidden. `Esc` and the
 * `⌘/Ctrl+.` toggle also exit; this is the visible, click/tap-able counterpart.
 * All visual styling lives in chat.css under `.lmchat-focus-exit`.
 */
export function FocusModeExit({ active, onExit }: FocusModeExitProps) {
  if (!active) return null;
  return (
    <button
      type="button"
      onClick={onExit}
      aria-label="Exit focus mode"
      title="Exit focus mode (Esc)"
      className="lmchat-focus-exit"
      data-testid="focus-mode-exit"
    >
      <Minimize2 size={14} aria-hidden />
      <span className="lmchat-focus-exit__label">Exit focus</span>
    </button>
  );
}
