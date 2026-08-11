/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Title-generation store — Zustand 5.
 *
 * Tracks which chat IDs are currently mid-auto-title-generation so the
 * sidebar can render "Generating title…" italic+dimmed placeholder text
 * for them.  Lives outside React-Query because the loading state is not a
 * query of its own — it's a transient UI flag owned by the Chat page that
 * the Sidebar happens to read.
 *
 * The set is in-memory only.  A page reload during generation will leave
 * the chat at "New Chat"; the next assistant turn will retry.
 *
 * Usage
 * -----
 * Writer (pages/Chat.tsx, after second assistant message):
 *   const { begin, end } = useTitleGenerationStore.getState();
 *   begin(chatId);
 *   try { await mutate(chatId); } finally { end(chatId); }
 *
 * Reader (components/Sidebar.tsx, per item):
 *   const isGenerating = useTitleGenerationStore((s) => s.has(chatId));
 */
import { create } from "zustand";

interface TitleGenerationState {
  /** Set of chat IDs currently generating a title. */
  pending: ReadonlySet<number>;
  /** Whether the given chat ID is in the pending set. */
  has: (chatId: number) => boolean;
  /** Mark *chatId* as generating. */
  begin: (chatId: number) => void;
  /** Clear *chatId*'s generating flag. */
  end: (chatId: number) => void;
}

export const useTitleGenerationStore = create<TitleGenerationState>(
  (set, get) => ({
    pending: new Set<number>(),

    has: (chatId: number): boolean => get().pending.has(chatId),

    begin: (chatId: number): void => {
      set((s) => {
        const next = new Set(s.pending);
        next.add(chatId);
        return { pending: next };
      });
    },

    end: (chatId: number): void => {
      set((s) => {
        if (!s.pending.has(chatId)) return s;
        const next = new Set(s.pending);
        next.delete(chatId);
        return { pending: next };
      });
    },
  }),
);
