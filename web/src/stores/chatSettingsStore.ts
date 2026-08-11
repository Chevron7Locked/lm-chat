/* SPDX-License-Identifier: Apache-2.0 */
/**
 * chatSettingsStore — global reasoning-effort default + per-chat overrides.
 *
 * Global default: persisted to localStorage under "lmchat:settings:reasoning".
 * Per-chat overrides: persisted to the backend via the chats.settings JSON
 * column. The store exposes:
 *
 *   setChatReasoning(chatId, level, persist?)
 *     — updates the in-memory override.  When persist=true (default) the
 *       caller is expected to also fire useUpdateChat({ reasoning_effort })
 *       (see Chat.tsx / ReasoningToggle usage).
 *
 *   hydrateFromChats(chats)
 *     — seeds the in-memory overrides from chats[i].settings.reasoning_effort
 *       on first load, so the UI reflects persisted values after a page reload.
 *
 * A per-chat value of "" (empty string) means "fall through to global default".
 * The empty-string sentinel is never forwarded to LM Studio; the effective
 * value is always the resolved global.
 */
import { create } from "zustand";

export type ReasoningLevel = "off" | "low" | "medium" | "high";

const LS_KEY = "lmchat:settings:reasoning";
const ORDERED_LEVELS: ReasoningLevel[] = ["off", "low", "medium", "high"];

/** Minimal chat shape needed for hydration — avoids importing the full ChatSummary. */
export interface ChatHydrationEntry {
  id: number;
  settings?:
    | { reasoning_effort?: string | null; rag_enabled?: boolean }
    | undefined;
}

function readStoredReasoning(): ReasoningLevel {
  try {
    const v = localStorage.getItem(LS_KEY);
    if (v === "off" || v === "low" || v === "medium" || v === "high") return v;
  } catch {
    // localStorage unavailable — proceed with default.
  }
  return "off";
}

interface ChatSettingsState {
  /** Global default reasoning level. */
  globalReasoning: ReasoningLevel;
  /** Per-chat overrides. "" means fall-through to global. */
  chatOverrides: Record<number, ReasoningLevel | "">;
  setGlobalReasoning: (level: ReasoningLevel) => void;
  setChatReasoning: (chatId: number, level: ReasoningLevel | "") => void;
  /** Return the effective reasoning for a chat, respecting the "" sentinel. */
  effectiveReasoning: (chatId: number) => ReasoningLevel;
  /** Cycle globalReasoning through off→low→medium→high→off. */
  cycleGlobalReasoning: () => void;
  /**
   * Seed per-chat overrides from persisted backend data.
   *
   * Called once when `useChats` resolves on mount.  Reads
   * `chat.settings.reasoning_effort` for each chat and populates
   * `chatOverrides` so the UI reflects persisted values after a page reload.
   * Only seeds chats that have a non-null reasoning_effort; does not
   * overwrite overrides the user has already set in this session.
   *
   * Closes DEFERRED.md §1.
   */
  hydrateFromChats: (chats: ChatHydrationEntry[]) => void;
}

export const useChatSettingsStore = create<ChatSettingsState>((set, get) => ({
  globalReasoning: readStoredReasoning(),
  chatOverrides: {},

  setGlobalReasoning: (level: ReasoningLevel) => {
    try {
      localStorage.setItem(LS_KEY, level);
    } catch {
      // Ignore write failures.
    }
    set({ globalReasoning: level });
  },

  setChatReasoning: (chatId: number, level: ReasoningLevel | "") => {
    set((s) => ({
      chatOverrides: { ...s.chatOverrides, [chatId]: level },
    }));
  },

  effectiveReasoning: (chatId: number): ReasoningLevel => {
    const state = get();
    const override = state.chatOverrides[chatId];
    // Empty-string or undefined → fall through to global default.
    if (override === undefined || override === "") {
      return state.globalReasoning;
    }
    return override;
  },

  cycleGlobalReasoning: () => {
    const { globalReasoning, setGlobalReasoning } = get();
    const idx = ORDERED_LEVELS.indexOf(globalReasoning);
    const next = ORDERED_LEVELS[(idx + 1) % ORDERED_LEVELS.length] ?? "off";
    setGlobalReasoning(next);
  },

  hydrateFromChats: (chats: ChatHydrationEntry[]) => {
    const current = get().chatOverrides;
    const additions: Record<number, ReasoningLevel | ""> = {};
    for (const chat of chats) {
      const effort = chat.settings?.reasoning_effort;
      if (
        effort != null &&
        effort !== "" &&
        // Only seed if this chat doesn't already have an in-session override.
        !(chat.id in current)
      ) {
        // Validate the persisted value is a known level; fall through if not.
        if (
          effort === "off" ||
          effort === "low" ||
          effort === "medium" ||
          effort === "high"
        ) {
          additions[chat.id] = effort;
        }
      }
    }
    if (Object.keys(additions).length > 0) {
      set((s) => ({ chatOverrides: { ...additions, ...s.chatOverrides } }));
    }
  },
}));
