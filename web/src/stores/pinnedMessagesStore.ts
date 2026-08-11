/* SPDX-License-Identifier: Apache-2.0 */
/**
 * pinnedMessagesStore — frontend-only per-chat pinned message IDs.
 *
 * The backend `messages` schema does not currently carry a `pinned` column;
 * the pin-message UX from v0.5.x is reproduced client-side via localStorage
 * keyed per chat.  When (if) the backend gains a column, this store becomes
 * an in-memory mirror of the server state — the read/write surface stays the
 * same.
 *
 * Storage key: `lmchat:pinned-messages` → JSON `Record<chatId, number[]>`.
 *
 * Supports the pin-nav strip and the pinned-messages panel.
 */
import { create } from "zustand";

const LS_KEY = "lmchat:pinned-messages";

interface PinnedMessagesState {
  /** chatId → array of pinned message IDs (ordered by pin time). */
  byChat: Record<number, number[]>;
  /** Toggle the pinned state for a given (chatId, messageId). */
  toggle: (chatId: number, messageId: number) => void;
  /** Unconditionally pin (no-op if already pinned). */
  pin: (chatId: number, messageId: number) => void;
  /** Unconditionally unpin (no-op if not pinned). */
  unpin: (chatId: number, messageId: number) => void;
  /** Whether the given message is pinned in the given chat. */
  isPinned: (chatId: number, messageId: number) => boolean;
  /** All pinned IDs for a chat (empty array if none). */
  pinsFor: (chatId: number) => number[];
}

function readStored(): Record<number, number[]> {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (raw === null) return {};
    const parsed = JSON.parse(raw) as unknown;
    if (parsed === null || typeof parsed !== "object") return {};
    const out: Record<number, number[]> = {};
    for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
      const chatId = Number(k);
      if (!Number.isFinite(chatId)) continue;
      if (!Array.isArray(v)) continue;
      const ids = v.filter(
        (x): x is number => typeof x === "number" && Number.isFinite(x),
      );
      if (ids.length > 0) out[chatId] = ids;
    }
    return out;
  } catch {
    return {};
  }
}

function writeStored(state: Record<number, number[]>): void {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify(state));
  } catch {
    // Storage may be full or disabled — degrade silently.
  }
}

export const usePinnedMessagesStore = create<PinnedMessagesState>(
  (set, get) => ({
    byChat: readStored(),
    toggle: (chatId, messageId) => {
      const arr = get().byChat[chatId] ?? [];
      if (arr.includes(messageId)) {
        const next = arr.filter((id) => id !== messageId);
        const nextByChat: Record<number, number[]> = {};
        for (const [k, v] of Object.entries(get().byChat)) {
          const kn = Number(k);
          if (kn === chatId) continue;
          nextByChat[kn] = v;
        }
        if (next.length > 0) nextByChat[chatId] = next;
        writeStored(nextByChat);
        set({ byChat: nextByChat });
      } else {
        const nextByChat = { ...get().byChat, [chatId]: [...arr, messageId] };
        writeStored(nextByChat);
        set({ byChat: nextByChat });
      }
    },
    pin: (chatId, messageId) => {
      const arr = get().byChat[chatId] ?? [];
      if (arr.includes(messageId)) return;
      const nextByChat = { ...get().byChat, [chatId]: [...arr, messageId] };
      writeStored(nextByChat);
      set({ byChat: nextByChat });
    },
    unpin: (chatId, messageId) => {
      const arr = get().byChat[chatId];
      if (arr?.includes(messageId) !== true) return;
      const next = arr.filter((id) => id !== messageId);
      const nextByChat: Record<number, number[]> = {};
      for (const [k, v] of Object.entries(get().byChat)) {
        const kn = Number(k);
        if (kn === chatId) continue;
        nextByChat[kn] = v;
      }
      if (next.length > 0) nextByChat[chatId] = next;
      writeStored(nextByChat);
      set({ byChat: nextByChat });
    },
    isPinned: (chatId, messageId) => {
      return (get().byChat[chatId] ?? []).includes(messageId);
    },
    pinsFor: (chatId) => get().byChat[chatId] ?? [],
  }),
);
