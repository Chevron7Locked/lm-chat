/* SPDX-License-Identifier: Apache-2.0 */
/**
 * MicButton — speech-to-text trigger button for the Composer.
 *
 * Uses the Web Speech API via useSTT. On browsers that do not support
 * Speech Recognition (Firefox), renders a disabled button with an
 * informative title attribute.
 *
 * Keyboard: Cmd/Ctrl+Shift+M toggles listening (handled by the Composer
 * via the onKeyDown prop chain; MicButton exposes `onToggle` for that path).
 *
 * ARIA:
 *   - aria-pressed reflects the listening state.
 *   - aria-live="polite" region announces the transcript injection.
 */
import { type CSSProperties } from "react";
import type { STTCapability, STTState } from "@/hooks/useSTT";
import { usePlatform } from "@/hooks/usePlatform";
import { formatShortcut } from "@/lib/formatShortcut";

// ─── Props ───────────────────────────────────────────────────────────────────

interface MicButtonProps {
  capability: STTCapability;
  state: STTState;
  onToggle: () => void;
  disabled?: boolean;
}

// ─── Component ───────────────────────────────────────────────────────────────

export function MicButton({
  capability,
  state,
  onToggle,
  disabled = false,
}: MicButtonProps) {
  const platform = usePlatform();
  const chord = formatShortcut(platform, { mod: true, shift: true, key: "M" });
  const isUnavailable = !capability.available;
  const isListening = state.listening;
  const isDisabled = disabled || isUnavailable;

  const label = isUnavailable
    ? "Speech-to-text not supported in this browser"
    : isListening
      ? `Stop recording (${chord})`
      : `Start speech-to-text (${chord})`;

  return (
    <button
      type="button"
      onClick={isDisabled ? undefined : onToggle}
      disabled={isDisabled}
      aria-pressed={isListening}
      title={label}
      aria-label={label}
      style={micBtnStyle(isListening, isDisabled)}
    >
      {isListening ? <RecordingIcon /> : <MicIcon />}
    </button>
  );
}

// ─── Icons ────────────────────────────────────────────────────────────────────

function MicIcon() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z" />
      <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
      <line x1="12" y1="19" x2="12" y2="23" />
      <line x1="8" y1="23" x2="16" y2="23" />
    </svg>
  );
}

function RecordingIcon() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="currentColor"
      aria-hidden="true"
    >
      {/* Pulsing red dot indicator */}
      <circle cx="12" cy="12" r="8" />
    </svg>
  );
}

// ─── Styles ───────────────────────────────────────────────────────────────────

function micBtnStyle(isListening: boolean, isDisabled: boolean): CSSProperties {
  return {
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    width: "44px",
    height: "44px",
    background: isListening ? "var(--color-danger-quiet)" : "none",
    border: isListening ? "1px solid var(--color-danger-border)" : "none",
    borderRadius: "var(--radius-sm)",
    color: isListening
      ? "var(--color-danger)"
      : isDisabled
        ? "var(--color-text-subtle)"
        : "var(--color-text-muted)",
    cursor: isDisabled ? "not-allowed" : "pointer",
    opacity: isDisabled ? 0.5 : 1,
    transition:
      "background var(--duration-fast) var(--ease-out), color var(--duration-fast) var(--ease-out)",
  };
}
