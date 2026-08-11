/* SPDX-License-Identifier: Apache-2.0 */
/**
 * TanStack Query hooks for the documents API surface.
 *
 * Consumes the documents endpoints:
 *   GET    /api/documents              — list user's documents (plain array)
 *   POST   /api/documents              — multipart upload
 *   DELETE /api/documents/{id}         — soft-delete
 *   GET    /api/documents/{id}/chunks  — chunk previews
 *
 * GET /api/documents returns list[DocumentResponse] (no envelope).
 * The hook exposes data as Document[] directly.
 *
 * Every mutation hook has at least one error-path unit test
 * (isError, error.status).
 */
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { ApiError } from "@/lib/api";
import { useAuthStore } from "@/stores/authStore";

// ─── Wire types ──────────────────────────────────────────────────────────────

/** Matches DocumentResponse from the backend. */
export interface Document {
  id: number;
  title: string;
  mime_type: string;
  byte_size: number;
  chunk_count: number;
  embedding_model_id: string;
  sha256: string;
  uploaded_at: string;
  /** Documents can be scoped to a project. Null for un-projected docs.
   *  Backend column added in migration 0021. */
  project_id?: number | null;
}

/** Matches UploadResponse from the backend. */
export interface UploadResponse {
  id: number;
  filename: string;
  chunk_count: number;
}

/** Matches ChunkPreview from the backend. */
export interface ChunkPreview {
  ordinal: number;
  text_preview: string;
}

// ─── Query keys ──────────────────────────────────────────────────────────────

export const documentKeys = {
  all: ["documents"] as const,
  list: () => [...documentKeys.all, "list"] as const,
  chunks: (documentId: number) =>
    [...documentKeys.all, "chunks", documentId] as const,
};

// ─── Hooks ───────────────────────────────────────────────────────────────────

/**
 * List the authenticated user's non-deleted documents.
 *
 * GET /api/documents returns a plain Document[] array (no envelope).
 * The hook exposes data as Document[] directly.
 *
 * Gated on !isInitializing to suppress 401 spam during mount-time /me
 * hydration.
 */
export function useDocuments() {
  const { isInitializing, user } = useAuthStore();
  return useQuery<Document[], ApiError>({
    queryKey: documentKeys.list(),
    queryFn: () => api.request<Document[]>("/api/documents"),
    enabled: !isInitializing && user !== null,
  });
}

/**
 * Upload a document via multipart/form-data.
 *
 * POST /api/documents with a FormData body. When `projectId` is given,
 * uploads straight into that project via the dedicated
 * `POST /api/projects/{id}/documents` shim (single round-trip — no
 * un-projected-then-move race window). Invalidates the documents list
 * on success.
 */
export function useUploadDocument(projectId?: number) {
  const qc = useQueryClient();
  return useMutation<UploadResponse, ApiError, File>({
    // Callers (Documents.tsx, Project.tsx upload) show their own catch
    // toasts — meta.errorHandled keeps the global fallback silent (dedup).
    meta: { errorHandled: true },
    mutationFn: (file: File) => {
      const form = new FormData();
      form.append("file", file);
      const url =
        projectId !== undefined
          ? `/api/projects/${String(projectId)}/documents`
          : "/api/documents";
      return api.request<UploadResponse>(url, {
        method: "POST",
        body: form,
        // No Content-Type header — browser sets multipart/form-data with boundary.
      });
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: documentKeys.list() });
    },
  });
}

/**
 * Soft-delete a document by id.
 *
 * DELETE /api/documents/{id} returns 204.
 * Invalidates the documents list on success.
 */
export function useDeleteDocument() {
  const qc = useQueryClient();
  return useMutation<undefined, ApiError, number>({
    // Callers (Documents.tsx, Project.tsx delete) show their own catch
    // toasts — meta.errorHandled keeps the global fallback silent (dedup).
    meta: { errorHandled: true },
    mutationFn: (documentId: number) =>
      api.request<undefined>(`/api/documents/${documentId.toString()}`, {
        method: "DELETE",
      }),
    onSuccess: (_data, documentId) => {
      void qc.invalidateQueries({ queryKey: documentKeys.list() });
      // Drop the deleted document's cached chunk previews. Leaving them in
      // the cache lets a later remount / window-focus refetch hit
      // GET /api/documents/{id}/chunks for a row that no longer exists
      // (404 → console "Failed to load resource").
      qc.removeQueries({ queryKey: documentKeys.chunks(documentId) });
    },
  });
}

/**
 * Fetch chunk previews for a document.
 *
 * GET /api/documents/{id}/chunks returns ChunkPreview[] (no envelope).
 * Only fetches when documentId is non-null and the user is authenticated.
 */
export function useDocumentChunks(documentId: number | null) {
  const { isInitializing, user } = useAuthStore();
  return useQuery<ChunkPreview[], ApiError>({
    queryKey: documentKeys.chunks(documentId ?? 0),
    queryFn: () =>
      api.request<ChunkPreview[]>(
        `/api/documents/${(documentId ?? 0).toString()}/chunks`,
      ),
    enabled: !isInitializing && user !== null && documentId !== null,
  });
}
