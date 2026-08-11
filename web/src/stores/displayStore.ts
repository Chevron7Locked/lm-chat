/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Display store — Zustand 5.
 *
 * Manages three visual-preference slices: text size, conversation density,
 * and message bubble style. Each preference is:
 *   - persisted to localStorage under its own key
 *   - applied as a data-attribute on document.documentElement immediately
 *     on init (even for defaults) so CSS is explicit and there is no FOUC
 *
 * Pattern mirrors themeStore; see that file for rationale notes.
 */
import { create } from "zustand";

// ─── Types ─────────────────────────────────────────────────────────────────

export type TextSize = "sm" | "md" | "lg";
export type Density = "comfortable" | "compact";
export type MessageStyle = "bubbles" | "flat";

interface DisplayState {
  textSize: TextSize;
  density: Density;
  messageStyle: MessageStyle;
  setTextSize: (v: TextSize) => void;
  setDensity: (v: Density) => void;
  setMessageStyle: (v: MessageStyle) => void;
}

// ─── localStorage keys ──────────────────────────────────────────────────────

const LS_TEXT_SIZE = "lmchat:text-size";
const LS_DENSITY = "lmchat:density";
const LS_MESSAGE_STYLE = "lmchat:message-style";

// ─── Readers ───────────────────────────────────────────────────────────────

function readTextSize(): TextSize {
  try {
    const v = localStorage.getItem(LS_TEXT_SIZE);
    if (v === "sm" || v === "md" || v === "lg") return v;
  } catch {
    // localStorage unavailable in sandboxed contexts — use default.
  }
  return "md";
}

function readDensity(): Density {
  try {
    const v = localStorage.getItem(LS_DENSITY);
    if (v === "comfortable" || v === "compact") return v;
  } catch {
    // ignore
  }
  return "comfortable";
}

function readMessageStyle(): MessageStyle {
  try {
    const v = localStorage.getItem(LS_MESSAGE_STYLE);
    if (v === "bubbles" || v === "flat") return v;
  } catch {
    // ignore
  }
  return "bubbles";
}

// ─── DOM application ───────────────────────────────────────────────────────

/** Apply all three preferences to documentElement at once. */
function applyAll(ts: TextSize, d: Density, ms: MessageStyle): void {
  const root = document.documentElement;
  root.setAttribute("data-text-size", ts);
  root.setAttribute("data-density", d);
  root.setAttribute("data-message-style", ms);
}

function persist(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    // Ignore write failures (private browsing with full storage).
  }
}

// ─── Store ─────────────────────────────────────────────────────────────────

function createStore() {
  return create<DisplayState>((set) => {
    const textSize = readTextSize();
    const density = readDensity();
    const messageStyle = readMessageStyle();

    // Apply immediately on module load — no FOUC.
    applyAll(textSize, density, messageStyle);

    return {
      textSize,
      density,
      messageStyle,

      setTextSize: (v: TextSize) => {
        document.documentElement.setAttribute("data-text-size", v);
        persist(LS_TEXT_SIZE, v);
        set({ textSize: v });
      },

      setDensity: (v: Density) => {
        document.documentElement.setAttribute("data-density", v);
        persist(LS_DENSITY, v);
        set({ density: v });
      },

      setMessageStyle: (v: MessageStyle) => {
        document.documentElement.setAttribute("data-message-style", v);
        persist(LS_MESSAGE_STYLE, v);
        set({ messageStyle: v });
      },
    };
  });
}

// Singleton store instance.
export const useDisplayStore = createStore();
