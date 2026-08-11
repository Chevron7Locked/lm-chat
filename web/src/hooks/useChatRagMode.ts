/* SPDX-License-Identifier: Apache-2.0 */
/**
 * `useChatRagMode` — read-only TanStack Query hook for the
 * `GET /api/chats/{id}/rag_mode` endpoint.
 *
 * The endpoint returns the RAG-mode the backend's
 * `rag_mode_resolver.resolve_rag_mode` would pick for this chat
 * (INLINE / HYBRID / FOCUSED) + the supporting numbers the badge
 * UI surfaces (project corpus size, threshold, focused doc id).
 *
 * The result is short-lived: the user might pin or detach a focused
 * doc, swap embedding models, or add documents to the
 * project — any of which changes the mode. ``staleTime`` is tight
 * (5s) so the badge stays roughly live without thrashing the
 * server.
 */
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { ApiError } from "@/lib/api";
import { useAuthStore } from "@/stores/authStore";

export type RagModeName = "inline" | "hybrid" | "focused";

type EmbeddingStatus =
  | "ok"
  | "pinned_model_unavailable"
  | "no_embedding_model";

export interface RagModeResponse {
  mode: RagModeName;
  project_corpus_tokens: number | null;
  threshold_tokens: number | null;
  focused_document_id: number | null;
  /**
   * Backend's embedding-resolution status. `"ok"` means retrieval will
   * run; the other two mean retrieval will silently skip and the UI
   * should surface a warning.
   */
  embedding_status: EmbeddingStatus;
  embedding_model_pinned: string | null;
  embedding_model_active: string | null;
}

const chatRagModeKeys = {
  all: ["chats", "rag_mode"] as const,
  byChat: (chatId: number) => [...chatRagModeKeys.all, chatId] as const,
};

export function useChatRagMode(chatId: number | null) {
  const { isInitializing, user } = useAuthStore();
  return useQuery<RagModeResponse, ApiError>({
    queryKey:
      chatId !== null
        ? chatRagModeKeys.byChat(chatId)
        : (["chats", "rag_mode", "noop"] as const),
    queryFn: () =>
      api.request<RagModeResponse>(`/api/chats/${String(chatId)}/rag_mode`),
    enabled: chatId !== null && !isInitializing && user !== null,
    staleTime: 5_000,
  });
}
