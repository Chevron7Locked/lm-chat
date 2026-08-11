/* SPDX-License-Identifier: Apache-2.0 */
/**
 * subSessionStore — global signal for "is a sub-session active right now?"
 *
 * Sub-sessions cannot be projected. The FE consequence is that the
 * move-to-project affordance on the parent chat's sidebar row should
 * hide while a sub-session is open — the visible UI surface is the
 * ephemeral session, and the move target ("the persistent main chat,
 * not the ephemeral session") is intentionally NOT what the user is
 * interacting with.
 *
 * Pages/components that own the sub-session lifecycle (currently
 * ``web/src/pages/Chat.tsx``) push the active chat id into this store
 * when a sub-session opens and clear it when it ends. The Sidebar
 * reads ``activeChatId`` to decide whether to hide the move affordance
 * on that row.
 *
 * Not persisted — purely in-memory; clears on reload (which is the
 * correct semantics since sub-sessions are themselves ephemeral).
 */
import { create } from "zustand";

interface SubSessionState {
  /**
   * Chat id that's currently hosting an open sub-session, or null
   * when none is active. Only one sub-session is ever open at a time
   * per the FE design.
   */
  activeChatId: number | null;
  /** Called by the Chat page when a sub-session opens. */
  setActive: (chatId: number) => void;
  /** Called when a sub-session ends (cancel / finalize / inject). */
  clear: () => void;
}

export const useSubSessionStore = create<SubSessionState>((set) => ({
  activeChatId: null,
  setActive: (chatId) => {
    set({ activeChatId: chatId });
  },
  clear: () => {
    set({ activeChatId: null });
  },
}));
