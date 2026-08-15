/* SPDX-License-Identifier: Apache-2.0 */
/**
 * TanStack Query hooks for the Projects v1 CRUD + child-write surfaces.
 *
 * The cache-key + invalidation-fan-out design drives the shapes below.
 *
 * Backend endpoints (see ``src/lmchat/routes/projects.py``):
 *
 *   POST   /api/projects                    create a project
 *   GET    /api/projects                    list all projects for caller
 *   GET    /api/projects/{id}               fetch one project
 *   PATCH  /api/projects/{id}               update name / description /
 *                                           system_prompt; ``clear=``
 *                                           semantics
 *   DELETE /api/projects/{id}               delete (children survive
 *                                           un-projected via FK SET NULL)
 *   POST   /api/projects/{id}/archive       soft-archive
 *   POST   /api/projects/{id}/unarchive     reverse of the above
 *   GET    /api/projects/{id}/knowledge-stats  KB capacity meter
 *   GET    /api/projects/{id}/export        portable JSON backup
 *   POST   /api/projects/{id}/regenerate-summary  regen rolling summary
 *   POST   /api/projects/{id}/chats         create chat IN this project
 *   POST   /api/projects/{id}/documents     upload doc INTO this project
 *
 * Cross-cutting writes that flip a child's project_id (and therefore
 * invalidate the project-scoped chats / documents views):
 *
 *   PATCH  /api/chats/{id}                  body: project_id=<n> or
 *                                           clear=project_id
 *   PATCH  /api/documents/{id}              body: project_id=<n> or
 *                                           clear=project_id
 *
 * "Turn this chat into a Project" (promote an EXISTING chat, carrying
 * selected documents along):
 *
 *   POST   /api/chats/{chat_id}/promote-to-project
 *                                           create a project from a chat
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { ApiError } from "@/lib/api";
import { useAuthStore } from "@/stores/authStore";
import { chatKeys } from "@/hooks/useChats";
import type { ChatSummary } from "@/hooks/useChats";
import { FOLDERS_QUERY_KEY } from "@/hooks/useFolders";

// ─── Wire shapes ─────────────────────────────────────────────────────────────

export interface ProjectResponse {
  id: number;
  user_id: number;
  name: string;
  description: string;
  system_prompt: string;
  /**
   * The embedding model pinned by the first document attach. NULL when no
   * docs are attached yet. Surfaced for the ReembedBanner which compares
   * this to the user's currently active embedding model and warns when they
   * differ.
   */
  embedding_model_id?: string | null;
  /**
   * Seeds chats.model_id on `POST /api/projects/{id}/chats`. NULL falls
   * back to the user's global default.
   */
  default_model_id?: string | null;
  /**
   * Per-project override for the RAG-mode inline/hybrid threshold (in
   * tokens). NULL falls back to the resolver's formula.
   */
  rag_threshold?: number | null;
  created_at: number;
  updated_at: number;
  /**
   * Unix-epoch seconds the project was archived at; NULL means it's active.
   * Soft — archiving never touches the project's chats/documents.
   */
  archived_at?: number | null;
  /**
   * The rolling auto-summary — accumulated understanding of this project's
   * conversations, regenerated out-of-band and injected into project chats
   * alongside `system_prompt`. "" = none generated yet.
   */
  summary?: string;
  /** Unix-epoch seconds of the last summary regeneration; null until
   *  the first one runs. */
  summary_updated_at?: number | null;
}

interface CreateProjectBody {
  name: string;
  description?: string;
  system_prompt?: string;
}

interface UpdateProjectBody {
  name?: string;
  description?: string;
  system_prompt?: string;
  /**
   * Seeds chats.model_id on new project chats. To reset to "use global
   * default", omit this field and add "default_model_id" to `clear` instead
   * of sending "".
   */
  default_model_id?: string;
  /**
   * Per-project RAG-mode inline/hybrid threshold override, in tokens. To
   * reset to "use the formula", omit this field and add "rag_threshold" to
   * `clear`.
   */
  rag_threshold?: number;
  /** Comma-separated field names to clear. Allowed:
   *  "description", "system_prompt", "default_model_id",
   *  "rag_threshold". */
  clear?: string;
}

// ─── Query keys ──────────────────────────────────────────────────────────────

export const projectKeys = {
  /** All project queries (parent invalidation). */
  all: ["projects"] as const,
  /** The flat project list. `includeArchived` gets its own cache slot —
   *  the sidebar's default view and the all-projects landing page's
   *  "Archived" section are genuinely different server responses. */
  list: (includeArchived = false) =>
    [...projectKeys.all, { includeArchived }] as const,
  /** A single project by id. */
  detail: (id: number) => [...projectKeys.all, id] as const,
};

// ─── Helpers ─────────────────────────────────────────────────────────────────

function projectsToForm(
  body: CreateProjectBody | UpdateProjectBody,
): Record<string, string> {
  // FastAPI form-encoding: omitted fields → don't touch; "" → omitted
  // (see /api/projects PATCH route docstring).
  const form: Record<string, string> = {};
  if ("name" in body) form.name = body.name;
  if ("description" in body) {
    form.description = body.description;
  }
  if ("system_prompt" in body) {
    form.system_prompt = body.system_prompt;
  }
  if ("default_model_id" in body) {
    form.default_model_id = body.default_model_id;
  }
  if ("rag_threshold" in body) {
    form.rag_threshold = String(body.rag_threshold);
  }
  if ("clear" in body) {
    form.clear = body.clear;
  }
  return form;
}

/**
 * Invalidate every cache key that depends on a project_id change.
 *
 * Called whenever a mutation crosses the project-boundary (create / update
 * / delete / move-child-between-projects). Centralized so every
 * project-touching mutation invalidates the same set, instead of each call
 * site re-deriving its own.
 *
 * This used to ALSO fan out to per-project-id scoped keys
 * (``["folders", {projectId}]``, ``["documents", {projectId}]``) and
 * per-chat keys (``chatKeys.detail(chatId)`` / ``chatKeys.messages(chatId)``)
 * taken from an ``opts`` parameter. Those were redundant no-ops: TanStack
 * Query's default invalidation match is prefix-based, so the broad
 * ``chatKeys.all`` / ``FOLDERS_QUERY_KEY`` / ``["documents"]`` invalidations
 * below already invalidate every query nested under those prefixes,
 * scoped or not. Removed the dead fan-out (and the now-unused ``opts``
 * param + call-site plumbing) rather than leave cargo-cult calls that
 * looked like targeted invalidation but never added coverage.
 */
function invalidateAllProjectScoped(
  qc: ReturnType<typeof useQueryClient>,
): void {
  // Project list + per-detail keys.
  void qc.invalidateQueries({ queryKey: projectKeys.all });
  // Chat lists: invalidate the whole `chats` prefix so BOTH the union list AND
  // the sidebar's listDirect variant refresh — plus every scoped variant.
  // Targeting just chatKeys.list() (["chats","list"]) left chatKeys.listDirect()
  // (["chats","list-direct",…], what the sidebar actually renders) stale, so a
  // project create / move / delete looked like a no-op until a window refocus.
  // React Query matches keys structurally, so "list" never matches
  // "list-direct". chatKeys.all (["chats"]) is a prefix of every chat query
  // — list, listDirect, detail(id), AND messages(id) alike.
  // Mirrors the useChats mutation fix.
  void qc.invalidateQueries({ queryKey: chatKeys.all });
  // Folders (project-scoped folder list is a separate query key, but still
  // prefixed by FOLDERS_QUERY_KEY, so this covers scoped folder queries too).
  void qc.invalidateQueries({ queryKey: FOLDERS_QUERY_KEY });
  // Documents (same prefix reasoning as folders above).
  void qc.invalidateQueries({ queryKey: ["documents"] });
}

// ─── Read hooks ──────────────────────────────────────────────────────────────

/** List projects owned by the current user.
 *
 * `includeArchived` defaults to false, matching the
 * sidebar's default view; pass true for the all-projects landing
 * page's "Archived" section.
 */
export function useProjects(includeArchived = false) {
  const { isInitializing, user } = useAuthStore();
  return useQuery<ProjectResponse[], ApiError>({
    queryKey: projectKeys.list(includeArchived),
    queryFn: () =>
      api.request<ProjectResponse[]>(
        `/api/projects${includeArchived ? "?include_archived=true" : ""}`,
      ),
    enabled: !isInitializing && user !== null,
    staleTime: 30_000,
  });
}

/** Fetch a single project by id.
 *
 * When ``projectId`` is null the disabled query still needs a key.
 * Using ``projectKeys.detail(0)`` would collide with a legitimate
 * ``useProject(0)`` AND merge every "I have no projectId" call site
 * into one cache slot. Use a sentinel key instead — mirrors the
 * ``useMessages(null)`` pattern.
 */
export function useProject(projectId: number | null) {
  const { isInitializing, user } = useAuthStore();
  return useQuery<ProjectResponse, ApiError>({
    queryKey:
      projectId !== null
        ? projectKeys.detail(projectId)
        : (["projects", "noop"] as const),
    queryFn: () =>
      api.request<ProjectResponse>(`/api/projects/${String(projectId)}`),
    enabled: projectId !== null && !isInitializing && user !== null,
    staleTime: 30_000,
  });
}

// ─── Write hooks ─────────────────────────────────────────────────────────────

export function useCreateProject() {
  const qc = useQueryClient();
  return useMutation<ProjectResponse, ApiError, CreateProjectBody>({
    // Callers (ProjectsSection, Sidebar) show their own catch toasts —
    // meta.errorHandled keeps the global MutationCache fallback silent.
    meta: { errorHandled: true },
    mutationFn: (body) =>
      api.postForm<ProjectResponse>("/api/projects", projectsToForm(body)),
    onSuccess: () => {
      invalidateAllProjectScoped(qc);
    },
  });
}

export function useUpdateProject(projectId: number) {
  const qc = useQueryClient();
  return useMutation<ProjectResponse, ApiError, UpdateProjectBody>({
    // Callers (Project.tsx rename + SettingsTab save) show their own catch
    // toasts — meta.errorHandled keeps the global fallback silent (dedup).
    meta: { errorHandled: true },
    mutationFn: (body) => {
      const form = projectsToForm(body);
      const params = new URLSearchParams(form);
      return api.request<ProjectResponse>(
        `/api/projects/${String(projectId)}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: params.toString(),
        },
      );
    },
    onSuccess: () => {
      invalidateAllProjectScoped(qc);
    },
  });
}

export function useDeleteProject() {
  const qc = useQueryClient();
  return useMutation<undefined, ApiError, { projectId: number }>({
    // Caller (Project.tsx delete) shows its own catch toast —
    // meta.errorHandled keeps the global MutationCache fallback silent.
    meta: { errorHandled: true },
    mutationFn: ({ projectId }) =>
      api.request<undefined>(`/api/projects/${String(projectId)}`, {
        method: "DELETE",
      }),
    onSuccess: () => {
      // ON DELETE SET NULL on chats/docs/insights — they survive
      // un-projected. Invalidate every project-scoped view via the
      // broad-prefix fan-out (a per-id scoped call here would be a redundant
      // no-op — see invalidateAllProjectScoped).
      invalidateAllProjectScoped(qc);
    },
  });
}

/** Archive a project. Soft — chats/documents are untouched;
 *  the project just drops out of the default sidebar/list. */
export function useArchiveProject() {
  const qc = useQueryClient();
  return useMutation<ProjectResponse, ApiError, { projectId: number }>({
    // Caller (Project.tsx danger zone) shows its own catch toast —
    // meta.errorHandled keeps the global MutationCache fallback silent.
    meta: { errorHandled: true },
    mutationFn: ({ projectId }) =>
      api.postForm<ProjectResponse>(
        `/api/projects/${String(projectId)}/archive`,
        {},
      ),
    onSuccess: () => {
      invalidateAllProjectScoped(qc);
    },
  });
}

/** Unarchive a project. Reverses `useArchiveProject`. */
export function useUnarchiveProject() {
  const qc = useQueryClient();
  return useMutation<ProjectResponse, ApiError, { projectId: number }>({
    meta: { errorHandled: true },
    mutationFn: ({ projectId }) =>
      api.postForm<ProjectResponse>(
        `/api/projects/${String(projectId)}/unarchive`,
        {},
      ),
    onSuccess: () => {
      invalidateAllProjectScoped(qc);
    },
  });
}

/**
 * Regenerate a project's rolling auto-summary.
 *
 * Hits `POST /api/projects/{id}/regenerate-summary`, which runs the OOB
 * summarizer synchronously (unlike the throttled post-turn auto-refresh,
 * an explicit "Regenerate" click always re-runs it) and returns the
 * updated `{summary, summary_updated_at}`. Fail-soft server-side — a
 * failed OOB call still returns 200 with the project's prior summary
 * unchanged, so this mutation only surfaces transport-level errors.
 */
export function useRegenerateProjectSummary(projectId: number) {
  const qc = useQueryClient();
  return useMutation<ProjectResponse, ApiError>({
    // Caller (Project.tsx summary card) shows its own catch toast —
    // meta.errorHandled keeps the global MutationCache fallback silent.
    meta: { errorHandled: true },
    mutationFn: () =>
      api.postForm<ProjectResponse>(
        `/api/projects/${String(projectId)}/regenerate-summary`,
        {},
      ),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: projectKeys.detail(projectId) });
    },
  });
}

/** Create a chat INSIDE a project (single round-trip).
 *
 * Returns ``ChatSummary`` — the route response is wrapped in
 * ``ChatResponse.model_validate`` (was raw dict), so the frontend page can
 * navigate to ``/c/{id}`` without the ``as`` cast.
 */
export function useCreateChatInProject(projectId: number) {
  const qc = useQueryClient();
  return useMutation<
    ChatSummary,
    ApiError,
    { title: string; incognito?: boolean }
  >({
    // Caller (Project.tsx new-chat-in-project) shows its own catch toast —
    // meta.errorHandled keeps the global MutationCache fallback silent.
    meta: { errorHandled: true },
    mutationFn: async ({ title, incognito = false }) => {
      const form: Record<string, string> = { title };
      if (incognito) form.incognito = "true";
      const created = await api.postForm<ChatSummary>(
        `/api/projects/${String(projectId)}/chats`,
        form,
      );
      return created;
    },
    onSuccess: () => {
      invalidateAllProjectScoped(qc);
    },
  });
}

/**
 * Re-embed every document in a project under the active embedding
 * model. Used when the embedding model was swapped after attaching docs;
 * existing chunks are encoded under the OLD model and retrieval
 * mis-cosines until they're rewritten.
 *
 * Hits ``POST /api/projects/{id}/re-embed``. Returns the
 * ``{documents_re_embedded, chunks_re_embedded,
 *    active_embedding_model_id}`` counters so the UI can show
 * "Re-embedded N docs / M chunks under <model>".
 */
export interface ReembedProjectResponse {
  documents_re_embedded: number;
  chunks_re_embedded: number;
  active_embedding_model_id: string;
}

export function useReembedProject(projectId: number) {
  const qc = useQueryClient();
  return useMutation<ReembedProjectResponse, ApiError>({
    // Caller (Project.tsx re-embed) shows its own catch toast —
    // meta.errorHandled keeps the global MutationCache fallback silent.
    meta: { errorHandled: true },
    mutationFn: () =>
      api.postForm<ReembedProjectResponse>(
        `/api/projects/${String(projectId)}/re-embed`,
        {},
      ),
    onSuccess: () => {
      // The project's embedding_model_id changed + every chunk's
      // embedding bytes changed. Invalidate the project view (badge
      // + warning re-resolve) and any chat/rag_mode that scopes here.
      invalidateAllProjectScoped(qc);
    },
  });
}

/**
 * KB capacity meter numbers for a project.
 *
 * Hits `GET /api/projects/{id}/knowledge-stats`, which reuses the same
 * corpus estimator + threshold formula as the per-chat RAG-mode badge
 * — the Documents tab's meter and that badge never disagree.
 */
export interface KnowledgeStatsResponse {
  corpus_tokens: number;
  threshold: number;
  ctx_window: number;
}

export function useProjectKnowledgeStats(projectId: number | null) {
  const { isInitializing, user } = useAuthStore();
  return useQuery<KnowledgeStatsResponse, ApiError>({
    queryKey:
      projectId !== null
        ? [...projectKeys.detail(projectId), "knowledge-stats"]
        : (["projects", "noop", "knowledge-stats"] as const),
    queryFn: () =>
      api.request<KnowledgeStatsResponse>(
        `/api/projects/${String(projectId)}/knowledge-stats`,
      ),
    enabled: projectId !== null && !isInitializing && user !== null,
    staleTime: 30_000,
  });
}

/** Wire shape for `GET /api/projects/{id}/export`. A
 *  portable backup/handoff bundle — NOT a sharing surface (single-admin
 *  app). Documents carry re-extracted text, never embedding vectors. */
export interface ProjectExportResponse {
  exported_at: string;
  project: {
    name: string;
    description: string;
    system_prompt: string;
    default_model_id: string | null;
    rag_threshold: number | null;
    embedding_model_id: string | null;
  };
  documents: {
    id: number;
    title: string;
    mime_type: string;
    byte_size: number;
    sha256: string;
    uploaded_at: string;
    text: string;
  }[];
  chats: {
    id: number;
    title: string;
    created_at: string;
    messages: {
      role: string;
      content: string;
      reasoning_content: string | null;
      created_at: string;
    }[];
  }[];
}

/** Strip characters unsafe in a downloaded filename. */
function sanitizeFilename(name: string): string {
  const cleaned = name.trim().replace(/[/\\?%*:|"<>]/g, "-");
  return cleaned === "" ? "project" : cleaned;
}

/**
 * Export a project as a portable JSON backup/handoff bundle.
 *
 * Fetches `GET /api/projects/{id}/export` then triggers a client-side
 * `<name>.lmchat-project.json` download. No sharing/roles UI — this is
 * a local backup artifact, not a multi-user surface.
 */
export function useExportProject() {
  return useMutation<undefined, ApiError, { projectId: number; name: string }>({
    // Caller (Project.tsx settings) shows its own catch toast —
    // meta.errorHandled keeps the global MutationCache fallback silent.
    meta: { errorHandled: true },
    mutationFn: async ({ projectId, name }) => {
      const bundle = await api.request<ProjectExportResponse>(
        `/api/projects/${String(projectId)}/export`,
      );
      const blob = new Blob([JSON.stringify(bundle, null, 2)], {
        type: "application/json",
      });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${sanitizeFilename(name)}.lmchat-project.json`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      return undefined;
    },
  });
}

/** Move a document into a project (or detach when null). */
export function useMoveDocumentToProject() {
  const qc = useQueryClient();
  return useMutation<
    unknown,
    ApiError,
    {
      documentId: number;
      oldProjectId: number | null;
      newProjectId: number | null;
    }
  >({
    mutationFn: ({ documentId, newProjectId }) => {
      const form: Record<string, string> = {};
      if (newProjectId === null) {
        form.clear = "project_id";
      } else {
        form.project_id = String(newProjectId);
      }
      const params = new URLSearchParams(form);
      return api.request<unknown>(`/api/documents/${String(documentId)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: params.toString(),
      });
    },
    onSuccess: () => {
      invalidateAllProjectScoped(qc);
    },
  });
}

/**
 * Promote an existing chat into a brand-new project, carrying selected
 * un-projected documents along.
 *
 * Hits ``POST /api/chats/{chat_id}/promote-to-project``. The chat's
 * message history, compactions, and embeddings are all ``chat_id``-scoped
 * and travel for free once the backend flips ``chats.project_id`` — the
 * route's real work (and this hook's payload) is the new project's
 * identity plus which un-projected documents should move with it.
 *
 * Returns the created project (``ProjectResponse`` shape) plus
 * ``moved_document_count``.
 */
export interface PromoteToProjectResponse extends ProjectResponse {
  moved_document_count: number;
}

export function usePromoteChatToProject(chatId: number) {
  const qc = useQueryClient();
  return useMutation<
    PromoteToProjectResponse,
    ApiError,
    { name?: string; system_prompt?: string; document_ids?: number[] }
  >({
    // Caller (PromoteToProjectModal) shows its own catch toast —
    // meta.errorHandled keeps the global MutationCache fallback silent.
    meta: { errorHandled: true },
    mutationFn: (body) => {
      const form: Record<string, string> = {};
      if (body.name !== undefined) form.name = body.name;
      if (body.system_prompt !== undefined) {
        form.system_prompt = body.system_prompt;
      }
      if (body.document_ids !== undefined && body.document_ids.length > 0) {
        form.document_ids = body.document_ids.join(",");
      }
      return api.postForm<PromoteToProjectResponse>(
        `/api/chats/${String(chatId)}/promote-to-project`,
        form,
      );
    },
    onSuccess: () => {
      invalidateAllProjectScoped(qc);
    },
  });
}
