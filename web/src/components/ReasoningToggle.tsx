/* SPDX-License-Identifier: Apache-2.0 */
import "@/styles/reasoning-toggle.css";
/**
 * ReasoningToggle — cycle button for reasoning-effort level.
 *
 * Click: cycles through off→low→medium→high→off on the global default.
 * Long-press or right-click: opens a dropdown for explicit picks.
 *
 * When `chatId` is provided the per-chat override is used (with "" falling
 * through to the global default). When omitted only the global default is
 * controlled.
 *
 * Stable across re-renders: state lives in chatSettingsStore (Zustand).
 */
import { useState, useRef, useCallback } from "react";
import type { KeyboardEvent, MouseEvent } from "react";
import { Brain } from "lucide-react";
import { useChatSettingsStore } from "@/stores/chatSettingsStore";
import type { ReasoningLevel } from "@/stores/chatSettingsStore";
import { useDropdownKeyboard } from "@/hooks/useDropdownKeyboard";
import { REASONING_LEVELS } from "@/hooks/useReasoningOverridePersistence";

const LEVEL_LABELS: Record<ReasoningLevel, string> = {
  off: "off",
  low: "low",
  medium: "med",
  high: "high",
};

const LONG_PRESS_MS = 500;

interface ReasoningToggleProps {
  /** When provided, controls per-chat override instead of global default. */
  chatId?: number | undefined;
  /**
   * fe-components-state-12: the chat's per-chat reasoning override,
   * PRE-RESOLVED by the mounting parent from `chat.settings` (server
   * truth, mirrored in the chatKeys cache) — the SAME value
   * ChatSettingsRail's reasoning `<select>` displays. "" means no override
   * (falls through to the global default below, same as the select's
   * blank "use global default" option).
   *
   * When provided, this is authoritative: the toggle stops deriving its
   * own value from `chatSettingsStore.chatOverrides` (which previously
   * never looked at `chat.settings` at all, so a chat visited before the
   * store's one-time hydrate ran — or created after it — showed a stale
   * or simply wrong level). This is what keeps the Rail and this toggle
   * from ever disagreeing.
   *
   * When omitted (a standalone/global-only mount with no Rail above it),
   * the toggle falls back to its original chatOverrides/effectiveReasoning
   * derivation, unchanged.
   */
  reasoningOverride?: ReasoningLevel | "" | undefined;
}

export function ReasoningToggle({ chatId, reasoningOverride }: ReasoningToggleProps) {
  const {
    globalReasoning,
    cycleGlobalReasoning,
    setChatReasoning,
    effectiveReasoning,
    chatOverrides,
  } = useChatSettingsStore();

  const [dropdownOpen, setDropdownOpen] = useState(false);
  const longPressTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const dropdownRef = useRef<HTMLDivElement>(null);

  // fe-components-state-12: prefer the caller's pre-resolved value (the
  // same derivation ChatSettingsRail's <select> uses) over deriving our
  // own from the store, so the two surfaces can never disagree. Falls back
  // to the legacy store-only derivation when no resolved value is supplied.
  const displayLevel: ReasoningLevel =
    reasoningOverride !== undefined
      ? reasoningOverride === ""
        ? globalReasoning
        : reasoningOverride
      : chatId !== undefined
        ? effectiveReasoning(chatId)
        : globalReasoning;

  // Show "(chat)" indicator when the chat has a non-empty override.
  const hasChatOverride =
    reasoningOverride !== undefined
      ? reasoningOverride !== ""
      : chatId !== undefined &&
        chatOverrides[chatId] !== undefined &&
        chatOverrides[chatId] !== "";

  const isActive = displayLevel !== "off";

  const handleClick = useCallback(() => {
    if (longPressTimerRef.current !== null) return; // long-press in progress
    if (chatId !== undefined) {
      // Cycle the per-chat override, starting from whatever is CURRENTLY
      // displayed — fe-components-state-12: the same value the select
      // shows, not a second independent read of the store.
      const idx = REASONING_LEVELS.indexOf(displayLevel);
      const next = REASONING_LEVELS[(idx + 1) % REASONING_LEVELS.length] ?? "off";
      setChatReasoning(chatId, next);
    } else {
      cycleGlobalReasoning();
    }
  }, [chatId, cycleGlobalReasoning, displayLevel, setChatReasoning]);

  const handlePointerDown = useCallback(() => {
    longPressTimerRef.current = setTimeout(() => {
      longPressTimerRef.current = null;
      setDropdownOpen(true);
    }, LONG_PRESS_MS);
  }, []);

  const handlePointerUp = useCallback(() => {
    if (longPressTimerRef.current !== null) {
      clearTimeout(longPressTimerRef.current);
      longPressTimerRef.current = null;
    }
  }, []);

  const handleContextMenu = useCallback((e: MouseEvent) => {
    e.preventDefault();
    setDropdownOpen((v) => !v);
  }, []);

  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLButtonElement>) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        handleClick();
      } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        setDropdownOpen(true);
      }
    },
    [handleClick],
  );

  const selectLevel = useCallback(
    (level: ReasoningLevel) => {
      if (chatId !== undefined) {
        setChatReasoning(chatId, level);
      } else {
        useChatSettingsStore.getState().setGlobalReasoning(level);
      }
      setDropdownOpen(false);
    },
    [chatId, setChatReasoning],
  );

  // ── Keyboard contract split ──────────────────────────────────────────────
  //
  // The CYCLE BUTTON (above) handles Enter/Space/Arrow with handleKeyDown:
  // pressing Enter/Space cycles the level (same as click); ArrowDown/Up opens
  // the dropdown. This is intentionally different from a standard menu trigger
  // because the primary action of the button is cycling, not opening.
  //
  // The DROPDOWN SUB-COMPONENT (below) uses useDropdownKeyboard: Arrow
  // Up/Down/Tab navigate between options, Escape closes, Home/End jump.
  // Items use role="option" inside role="listbox", so the selector matches
  // [role="option"] rather than the default [role="menuitem"].
  const { containerProps: dropdownKeyboardProps } = useDropdownKeyboard({
    open: dropdownOpen,
    onClose: () => {
      setDropdownOpen(false);
    },
    itemSelector: '[role="option"]:not([disabled])',
  });

  return (
    <div className="lmchat-reasoning-toggle" ref={dropdownRef}>
      <button
        type="button"
        aria-label={`Reasoning effort: ${displayLevel}. Click to cycle, right-click for options.`}
        aria-haspopup="listbox"
        aria-expanded={dropdownOpen}
        onClick={handleClick}
        onPointerDown={handlePointerDown}
        onPointerUp={handlePointerUp}
        onContextMenu={handleContextMenu}
        onKeyDown={handleKeyDown}
        className={`lmchat-reasoning-btn${isActive ? " lmchat-reasoning-btn--active" : ""}`}
        data-testid="reasoning-toggle"
      >
        {isActive && (
          <span className="lmchat-reasoning-btn__dot" aria-hidden="true" />
        )}
        <Brain size={13} aria-hidden style={{ marginRight: 2 }} />
        <span>
          {LEVEL_LABELS[displayLevel]}
          {hasChatOverride && (
            <span style={{ opacity: 0.6, marginLeft: "2px" }}>*</span>
          )}
        </span>
      </button>

      {dropdownOpen && (
        <div
          role="listbox"
          aria-label="Reasoning effort level"
          className="lmchat-reasoning-dropdown"
          onMouseLeave={() => {
            setDropdownOpen(false);
          }}
          {...dropdownKeyboardProps}
        >
          {REASONING_LEVELS.map((level) => (
            <button
              key={level}
              type="button"
              role="option"
              aria-selected={level === displayLevel}
              onClick={() => {
                selectLevel(level);
              }}
              className={`lmchat-reasoning-option${level === displayLevel ? " lmchat-reasoning-option--selected" : ""}`}
              data-testid={`reasoning-option-${level}`}
            >
              {level}
            </button>
          ))}
          {chatId !== undefined && (
            <button
              type="button"
              role="option"
              aria-selected={!hasChatOverride}
              onClick={() => {
                setChatReasoning(chatId, "");
                setDropdownOpen(false);
              }}
              className="lmchat-reasoning-option lmchat-reasoning-option--clear"
              data-testid="reasoning-option-clear"
            >
              use global default
            </button>
          )}
        </div>
      )}
    </div>
  );
}
