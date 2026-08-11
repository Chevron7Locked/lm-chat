/* SPDX-License-Identifier: Apache-2.0 */
/**
 * usePrompts — TanStack Query hooks for the prompt library API surface.
 *
 * Invariant 3: GET /api/prompts returns list[Prompt] directly.
 */
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { ApiError } from "@/lib/api";
import { useAuthStore } from "@/stores/authStore";

// ─── Response shape ───────────────────────────────────────────────────────────

export interface Prompt {
  id: number;
  user_id: number;
  name: string;
  content: string;
  created_at: string;
  updated_at: string;
}

// ─── Request bodies ───────────────────────────────────────────────────────────

interface CreatePromptBody {
  name: string;
  content: string;
}

interface UpdatePromptBody {
  name?: string | undefined;
  content?: string | undefined;
}

// ─── Query keys ──────────────────────────────────────────────────────────────

const promptKeys = {
  all: ["prompts"] as const,
  list: () => [...promptKeys.all, "list"] as const,
};

// ─── Hooks ───────────────────────────────────────────────────────────────────

/**
 * List all prompt presets for the current user.
 *
 * GET /api/prompts returns list[Prompt] directly (Invariant 3).
 */
export function usePrompts() {
  const { isInitializing, user } = useAuthStore();
  return useQuery<Prompt[], ApiError>({
    queryKey: promptKeys.list(),
    queryFn: () => api.request<Prompt[]>("/api/prompts"),
    enabled: !isInitializing && user !== null,
  });
}

/** Create a new prompt preset.
 *
 * POST /api/prompts — form-encoded.
 */
export function useCreatePrompt() {
  const qc = useQueryClient();
  return useMutation<Prompt, ApiError, CreatePromptBody>({
    // PromptLibrary calls mutateAsync inside a try/catch — errors are handled
    // by the caller's catch block.  Declare meta.errorHandled so the global
    // MutationCache fallback does not also fire a "Couldn't save" toast.
    meta: { errorHandled: true },
    mutationFn: ({ name, content }) => {
      const params = new URLSearchParams();
      params.set("name", name);
      params.set("content", content);
      return api.request<Prompt>("/api/prompts", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: params.toString(),
      });
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: promptKeys.list() });
    },
  });
}

/** Update a prompt's name and/or content.
 *
 * PATCH /api/prompts/{id} — form-encoded.
 */
export function useUpdatePrompt(promptId: number) {
  const qc = useQueryClient();
  return useMutation<Prompt, ApiError, UpdatePromptBody>({
    // PromptLibrary calls mutateAsync inside a try/catch — errors are handled
    // by the caller's catch block.  Declare meta.errorHandled so the global
    // MutationCache fallback does not also fire a "Couldn't save" toast.
    meta: { errorHandled: true },
    mutationFn: (body) => {
      const params = new URLSearchParams();
      if (body.name !== undefined) params.set("name", body.name);
      if (body.content !== undefined) params.set("content", body.content);
      return api.request<Prompt>(`/api/prompts/${String(promptId)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: params.toString(),
      });
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: promptKeys.list() });
    },
  });
}

/** Delete a prompt.
 *
 * DELETE /api/prompts/{id}
 */
export function useDeletePrompt() {
  const qc = useQueryClient();
  return useMutation<{ status: string }, ApiError, number>({
    // PromptLibrary calls mutateAsync inside a try/catch — errors are handled
    // by the caller's catch block.  Declare meta.errorHandled so the global
    // MutationCache fallback does not also fire a "Couldn't save" toast.
    meta: { errorHandled: true },
    mutationFn: (promptId) =>
      api.request<{ status: string }>(`/api/prompts/${String(promptId)}`, {
        method: "DELETE",
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: promptKeys.list() });
    },
  });
}
