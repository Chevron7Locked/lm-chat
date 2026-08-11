/* SPDX-License-Identifier: Apache-2.0 */
/**
 * TanStack Query hooks for the memory/pins API surface.
 *
 * Consumes the memory_service endpoints:
 *   GET  /api/memory/pins   — list pinned insights
 *   POST /api/memory/pin    — add a new pin
 *   DELETE /api/memory/pin/{id} — remove a pin
 *   POST /api/memory/reindex  — admin: rebuild embeddings
 */
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { ApiError } from "@/lib/api";
import { useAuthStore } from "@/stores/authStore";

// MemoryInsight matches the backend wire shape: MemoryInsight[] (plain array).
// GET /api/memory/pins returns list[MemoryInsight] — no envelope.
export interface MemoryInsight {
  id: number;
  text: string;
  created_at: string;
  // True for admin-pinned insights, false for AUTO (distilled) memories
  // saved by the post-turn distillation pass. The backend wire model always
  // carries this; older callers that ignored it still typecheck.
  pinned: boolean;
}

export const memoryKeys = {
  all: ["memory"] as const,
  pins: () => [...memoryKeys.all, "pins"] as const,
  auto: () => [...memoryKeys.all, "auto"] as const,
};

/** List pinned insights for the current user.
 *
 * GET /api/memory/pins returns a plain MemoryInsight[] array (no envelope).
 * The hook exposes data as MemoryInsight[] directly.
 *
 * Gated on !isInitializing to suppress 401 spam during mount-time /me
 * hydration.
 */
export function useMemoryPins() {
  const { isInitializing, user } = useAuthStore();
  return useQuery<MemoryInsight[], ApiError>({
    queryKey: memoryKeys.pins(),
    queryFn: () => api.request<MemoryInsight[]>("/api/memory/pins"),
    enabled: !isInitializing && user !== null,
  });
}

/** List AUTO (distilled) memories for the current user.
 *
 * GET /api/memory/auto returns a plain MemoryInsight[] array (pinned=false,
 * state='active') — memories the post-turn distillation pass saved
 * automatically, without explicit pinning. Same gating as useMemoryPins.
 */
export function useAutoMemories() {
  const { isInitializing, user } = useAuthStore();
  return useQuery<MemoryInsight[], ApiError>({
    queryKey: memoryKeys.auto(),
    queryFn: () => api.request<MemoryInsight[]>("/api/memory/auto"),
    enabled: !isInitializing && user !== null,
  });
}

/** Pin a new insight.
 *
 * POST /api/memory/pin expects Form-encoded body (text=…) per the backend's
 * Form(...) parameter declaration.
 */
export function usePinInsight() {
  const qc = useQueryClient();
  return useMutation<MemoryInsight, ApiError, { text: string }>({
    // Callers (Memory.tsx, Chat.tsx pin-insight) show their own catch
    // toasts — meta.errorHandled keeps the global fallback silent (dedup).
    meta: { errorHandled: true },
    mutationFn: ({ text }) => {
      const params = new URLSearchParams();
      params.set("text", text);
      return api.request<MemoryInsight>("/api/memory/pin", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: params.toString(),
      });
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: memoryKeys.pins() });
    },
  });
}

/** Unpin an insight by id. */
export function useUnpinInsight() {
  const qc = useQueryClient();
  return useMutation<{ status: string }, ApiError, number>({
    // Caller (Memory.tsx unpin) shows its own catch toast —
    // meta.errorHandled keeps the global MutationCache fallback silent.
    meta: { errorHandled: true },
    mutationFn: (id) =>
      api.request<{ status: string }>(`/api/memory/pin/${String(id)}`, {
        method: "DELETE",
      }),
    onSuccess: () => {
      // The DELETE route removes pinned OR auto memories by id, so refresh
      // both lists.
      void qc.invalidateQueries({ queryKey: memoryKeys.pins() });
      void qc.invalidateQueries({ queryKey: memoryKeys.auto() });
    },
  });
}

/** Reindex embeddings (admin only).
 *
 * POST /api/memory/reindex expects Form-encoded body (embedding_model_id=…)
 * per the backend's Form(...) parameter declaration.
 */
export function useMemoryReindex() {
  return useMutation<
    { status: string },
    ApiError,
    { embedding_model_id: string }
  >({
    // Caller (Memory.tsx reindex) shows its own catch toast —
    // meta.errorHandled keeps the global MutationCache fallback silent.
    meta: { errorHandled: true },
    mutationFn: ({ embedding_model_id }) => {
      const params = new URLSearchParams();
      params.set("embedding_model_id", embedding_model_id);
      return api.request<{ status: string }>("/api/memory/reindex", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: params.toString(),
      });
    },
  });
}

/** Edit an existing pinned insight.
 *
 * PATCH /api/memory/insights/{id} (form-encoded ``content``).  Cache
 * invalidation refreshes the Memory list on success.
 */
export function useEditInsight() {
  const qc = useQueryClient();
  return useMutation<MemoryInsight, ApiError, { id: number; content: string }>({
    // Caller (Memory.tsx inline edit) shows its own catch toast —
    // meta.errorHandled keeps the global MutationCache fallback silent.
    meta: { errorHandled: true },
    mutationFn: ({ id, content }) => {
      const params = new URLSearchParams();
      params.set("content", content);
      return api.request<MemoryInsight>(`/api/memory/insights/${String(id)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: params.toString(),
      });
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: memoryKeys.pins() });
    },
  });
}

/** Refine response shape returned by POST /api/memory/refine. */
export interface RefineResponse {
  insights: MemoryInsight[];
  history_id: number;
  before_count: number;
  after_count: number;
}

/** Refine pinned insights through LM Studio.
 *
 * The destructive replace is backed by a memory_insights_history row;
 * the returned ``history_id`` powers the "Undo" button.
 */
export function useRefineMemory() {
  const qc = useQueryClient();
  return useMutation<RefineResponse, ApiError>({
    // Caller (Memory.tsx refine) shows its own catch toast —
    // meta.errorHandled keeps the global MutationCache fallback silent.
    meta: { errorHandled: true },
    mutationFn: () =>
      api.request<RefineResponse>("/api/memory/refine", { method: "POST" }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: memoryKeys.pins() });
    },
  });
}

/** Restore pinned insights from a refine-snapshot. */
export function useRestoreMemory() {
  const qc = useQueryClient();
  return useMutation<MemoryInsight[], ApiError, { historyId: number }>({
    // Caller (Memory.tsx undo-refine) shows its own catch toast —
    // meta.errorHandled keeps the global MutationCache fallback silent.
    meta: { errorHandled: true },
    mutationFn: ({ historyId }) =>
      api.request<MemoryInsight[]>(`/api/memory/restore/${String(historyId)}`, {
        method: "POST",
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: memoryKeys.pins() });
    },
  });
}
