/* SPDX-License-Identifier: Apache-2.0 */
/**
 * React-query hooks for the compaction API surfaces.
 *
 * GET /api/chats/{id}/compactions  → compaction span list
 * GET /api/chats/{id}/compactions/{cid}/messages → archived messages for a span
 */
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { ApiError } from "@/lib/api";
import type { MessageRecord } from "@/hooks/useChats";
import { useAuthStore } from "@/stores/authStore";

// ─── Response shapes ─────────────────────────────────────────────────────────

/** A single compaction span (the compacted archive). */
export interface CompactionSpan {
  id: number;
  /** Factual running summary of the archived span. */
  summary: string;
  /** The oldest archived message id — used as the display position anchor. */
  anchor_msg_id: number;
  /** Number of messages archived into this span. */
  archived_count: number;
  /** Total token count of the archived messages before compaction. */
  original_token_count: number;
  /** Token count of the summary text. */
  summary_token_count: number;
  /** ISO-8601 timestamp when the compaction was created. */
  created_at: string;
}

// ─── Query keys ──────────────────────────────────────────────────────────────

export const compactionKeys = {
  all: ["compactions"] as const,
  list: (chatId: number) => [...compactionKeys.all, chatId] as const,
  messages: (chatId: number, compactionId: number) =>
    [...compactionKeys.all, chatId, "messages", compactionId] as const,
};

// ─── Hooks ───────────────────────────────────────────────────────────────────

/**
 * Fetch the list of compaction spans for a chat.
 *
 * GET /api/chats/{id}/compactions
 *
 * Sorted by anchor_msg_id ascending (chronological order).
 */
export function useCompactions(chatId: number | null) {
  const { isInitializing, user } = useAuthStore();

  return useQuery<CompactionSpan[], ApiError>({
    queryKey: compactionKeys.list(chatId ?? 0),
    queryFn: async () => {
      // BE returns a BARE array (list[CompactionResponse]), not a {compactions}
      // envelope — matches the committed backend contract.
      const raw = await api.request<CompactionSpan[]>(
        `/api/chats/${String(chatId)}/compactions`,
      );
      // Copy before sorting so the react-query cache array isn't mutated.
      return [...raw].sort((a, b) => a.anchor_msg_id - b.anchor_msg_id);
    },
    enabled: chatId !== null && !isInitializing && user !== null,
  });
}

/**
 * Fetch the archived messages for a specific compaction span.
 *
 * GET /api/chats/{id}/compactions/{cid}/messages
 *
 * LAZY — only fetches when `enabled` is true (typically when the tab is expanded).
 * Messages are returned id-ordered.
 */
export function useCompactionMessages(
  chatId: number | null,
  compactionId: number | null,
  enabled = false,
) {
  const { isInitializing, user } = useAuthStore();

  return useQuery<MessageRecord[], ApiError>({
    queryKey: compactionKeys.messages(
      chatId ?? 0,
      compactionId ?? 0,
    ),
    queryFn: async () => {
      // BE returns a BARE array (list[Message]), not a {messages} envelope.
      return api.request<MessageRecord[]>(
        `/api/chats/${String(chatId)}/compactions/${String(compactionId)}/messages`,
      );
    },
    enabled:
      enabled &&
      chatId !== null &&
      compactionId !== null &&
      !isInitializing &&
      user !== null,
  });
}
