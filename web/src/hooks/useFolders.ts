/* SPDX-License-Identifier: Apache-2.0 */
/**
 * TanStack Query hooks for the folder catalogue API.
 *
 * Folders are user-named buckets stored two ways on the backend:
 *
 *   1. ``user_prefs.folders`` — JSON array of names the user has
 *      created (possibly empty).
 *   2. ``chats.folder``       — the bucket each chat lives in.
 *
 * The GET /api/folders endpoint returns the union (sorted, deduped).
 * Rename + delete migrate both layers atomically; the hooks below
 * invalidate both the folders query and the chats query so the sidebar
 * re-renders without a stale view.
 *
 * Endpoints
 * ---------
 *   GET    /api/folders            → string[]
 *   POST   /api/folders            (form: name)                → string[]
 *   PATCH  /api/folders/{name}     (form: new_name)            → string[]
 *   DELETE /api/folders/{name}                                  → string[]
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { ApiError } from "@/lib/api";
import { useAuthStore } from "@/stores/authStore";
import { chatKeys } from "@/hooks/useChats";

// ─── Query keys ──────────────────────────────────────────────────────────────

/** Scope filter mirroring ``ChatScope`` from useChats.ts.
 *
 *   undefined → union (legacy default); GET /api/folders.
 *   { projectId: <n> } → project-scoped; GET /api/folders?project_id=n.
 *
 * The cache-key shape is stable so the un-scoped query and scoped
 * queries never collide.
 */
export type FolderScope = { projectId: number } | undefined;

export const FOLDERS_QUERY_KEY = ["folders"] as const;

function foldersKey(scope: FolderScope) {
  if (scope === undefined) return FOLDERS_QUERY_KEY;
  return ["folders", scope] as const;
}

// ─── Hooks ───────────────────────────────────────────────────────────────────

/**
 * Fetch the visible folder list for the current user.
 *
 * Gated on !isInitializing to suppress 401 spam during mount-time /me
 * hydration (consistent with other authenticated queries in v1).
 */
export function useFolders(scope?: FolderScope) {
  const { isInitializing, user } = useAuthStore();
  const url =
    scope === undefined
      ? "/api/folders"
      : `/api/folders?project_id=${String(scope.projectId)}`;
  return useQuery<string[], ApiError>({
    queryKey: foldersKey(scope),
    queryFn: () => api.request<string[]>(url),
    enabled: !isInitializing && user !== null,
    staleTime: 30_000,
  });
}

/**
 * Add a folder name to the catalogue.
 *
 * Idempotent at the backend — re-adding the same name returns the
 * existing list unchanged.
 */
export function useAddFolder() {
  const qc = useQueryClient();
  return useMutation<string[], ApiError, { name: string }>({
    // Caller (Sidebar folder-create) shows its own catch toast —
    // meta.errorHandled keeps the global MutationCache fallback silent.
    meta: { errorHandled: true },
    mutationFn: ({ name }) => api.postForm<string[]>("/api/folders", { name }),
    onSuccess: (data) => {
      // Optimistic-set so the sidebar shows the new (possibly empty)
      // bucket immediately; an invalidation in parallel keeps the
      // canonical store in sync.
      qc.setQueryData<string[]>(FOLDERS_QUERY_KEY, data);
      void qc.invalidateQueries({ queryKey: FOLDERS_QUERY_KEY });
    },
  });
}

/**
 * Rename a folder.  The backend atomically updates the prefs JSON
 * array AND any matching ``chats.folder`` values, so we invalidate
 * the chats list too.
 */
export function useRenameFolder() {
  const qc = useQueryClient();
  return useMutation<string[], ApiError, { oldName: string; newName: string }>({
    // Caller (Sidebar folder-rename) shows its own catch toast —
    // meta.errorHandled keeps the global MutationCache fallback silent.
    meta: { errorHandled: true },
    mutationFn: ({ oldName, newName }) => {
      const params = new URLSearchParams();
      params.set("new_name", newName);
      return api.request<string[]>(
        `/api/folders/${encodeURIComponent(oldName)}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: params.toString(),
        },
      );
    },
    onSuccess: (data) => {
      qc.setQueryData<string[]>(FOLDERS_QUERY_KEY, data);
      void qc.invalidateQueries({ queryKey: FOLDERS_QUERY_KEY });
      // Chats may have moved between buckets — refresh the chat list.
      void qc.invalidateQueries({ queryKey: chatKeys.list() });
    },
  });
}

/**
 * Delete a folder.  Chats inside the folder are unfoldered
 * (``folder=null``); they are NOT deleted.
 */
export function useDeleteFolder() {
  const qc = useQueryClient();
  return useMutation<string[], ApiError, { name: string }>({
    // Caller (Sidebar folder-delete) shows its own catch toast —
    // meta.errorHandled keeps the global MutationCache fallback silent.
    meta: { errorHandled: true },
    mutationFn: ({ name }) =>
      api.request<string[]>(`/api/folders/${encodeURIComponent(name)}`, {
        method: "DELETE",
      }),
    onSuccess: (data) => {
      qc.setQueryData<string[]>(FOLDERS_QUERY_KEY, data);
      void qc.invalidateQueries({ queryKey: FOLDERS_QUERY_KEY });
      void qc.invalidateQueries({ queryKey: chatKeys.list() });
    },
  });
}
