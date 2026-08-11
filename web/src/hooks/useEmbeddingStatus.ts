/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useEmbeddingStatus — view of the memory-indexing pipeline's health.
 *
 * Drives the visibility card in Settings → LM Studio so the admin can
 * tell at a glance which embedding model is being used + whether
 * messages are getting indexed. The embedding model used to be picked
 * silently by `MemoryService._default_embedding_model` (first loaded
 * embedding model in LM Studio); there was no UI surface for it, and
 * "is my memory actually working?" had no answer.
 */
import { useQuery } from "@tanstack/react-query";
import { api, type ApiError } from "@/lib/api";
import { useAuthStore } from "@/stores/authStore";

/**
 * Sentinel union that mirrors the chat-level ``/api/chats/{id}/rag_mode``
 * embedding-status code so the Settings UI and the chat badge draw from
 * the same source-of-truth.
 */
type SettingsEmbeddingStatus =
  | "ok"
  | "no_embedding_model"
  | "pinned_model_unavailable";

export interface EmbeddingStatus {
  active_model_id: string | null;
  loaded_embedding_models: string[];
  total_indexed_messages: number;
  last_indexed_at: number | null;
  models_in_use: Record<string, number>;
  /**
   * Resolver sentinel from
   * ``retrieval_service.resolve_embedding_model_status``. For Settings
   * (user-scoped, project_id=None) the only firable values are
   * ``"ok"`` and ``"no_embedding_model"``; ``"pinned_model_unavailable"``
   * is project-scoped and shouldn't surface here, but the union stays
   * consistent with the chat badge for the shared render component.
   */
  embedding_status: SettingsEmbeddingStatus;
}

export const embeddingStatusKeys = {
  all: ["embedding-status"] as const,
  current: () => [...embeddingStatusKeys.all, "current"] as const,
};

export function useEmbeddingStatus() {
  const { isInitializing, user } = useAuthStore();
  return useQuery<EmbeddingStatus, ApiError>({
    queryKey: embeddingStatusKeys.current(),
    queryFn: () => api.request<EmbeddingStatus>("/api/memory/embedding/status"),
    // Re-poll so the count + last-indexed-at advance as the user chats.
    // Mirrors useModels' 25s cadence — same tier of "soft
    // health, no hard SLA" data.
    refetchInterval: 25_000,
    refetchIntervalInBackground: false,
    staleTime: 25_000,
    // When the auth gate transitions ``enabled: false → true`` (route
    // entry while the auth store hydrates) a stale-but-fresh cache would
    // otherwise render until the next 25s tick. ``refetchOnMount: "always"``
    // forces a fetch on every mount regardless of cache age so the
    // Settings card never shows last-session's snapshot.
    refetchOnMount: "always",
    refetchOnReconnect: true,
    enabled: !isInitializing && user !== null,
  });
}
