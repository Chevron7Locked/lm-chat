/* SPDX-License-Identifier: Apache-2.0 */
/**
 * TanStack Query hooks for the chat + message API surfaces.
 *
 * All requests go through the global `api` client which attaches credentials.
 * Mutation helpers invalidate the affected queries so the UI stays consistent.
 */
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { ApiError } from "@/lib/api";
import type { ToolCall } from "@/hooks/useSSE";
import type { components, paths } from "@/types/api";
import { useAuthStore } from "@/stores/authStore";

// ─── Response shapes (match the backend wire contract) ─────────────────────
//
// The request call sites below are typed against the GENERATED OpenAPI types
// (web/src/types/api.ts, `make web-codegen`) instead of hand-rolled wire
// mirrors:
//
//   GET  /api/chats           → ChatResponse[]            (plain array)
//   GET  /api/chats/{id}      → ChatWithMessagesResponse  (has .messages[])
//   POST /api/chats           → ChatResponse              (Form-encoded body)
//   PATCH /api/chats/{id}     → ChatResponse              (Form-encoded body)
//
// The hooks normalise these to the UI-facing shapes below (ChatSummary,
// MessageRecord) at the wire boundary (toChatSummary / toMessageRecord).

interface AbCompareSettings {
  enabled: boolean;
  model_a?: string | undefined;
  model_b?: string | undefined;
}

export interface ChatSettings {
  /** Toggle RAG augmentation for this chat. */
  rag_enabled?: boolean;
  /** Per-chat reasoning level; "" clears override. */
  reasoning_effort?: string | null;
  /** A/B compare settings; enabled + optional model pair. */
  ab_compare?: AbCompareSettings;
  // ─── Per-chat rail fields ────────────────────────────────────────────
  /** Per-chat system prompt override. */
  system_prompt?: string | null;
  /** Sampler temperature (0-2). */
  temperature?: number | null;
  /** Nucleus sampling threshold (0-1). */
  top_p?: number | null;
  /** Top-K filter. */
  top_k?: number | null;
  /** Min-P filter (0-1). */
  min_p?: number | null;
  /** Token repetition penalty. */
  repeat_penalty?: number | null;
  /** Max output token cap. */
  max_tokens?: number | null;
  /** Alias for reasoning_effort surfaced in the v0.5.x rail. */
  reasoning?: string | null;
  /** Opt-in to self-consistency orchestration. */
  self_consistency_enabled?: boolean;
  /** Opt-in to chain-of-verification orchestration. */
  chain_of_verification_enabled?: boolean;
  /** When true, the request sets store=false upstream. */
  stateless?: boolean;
  /** Forward-compat: name of the currently-active preset. */
  active_preset?: string | null;
  /**
   * When set, the chat's RAG branching skips retrieval and injects ordered
   * chunks of THIS document only. Set/cleared via the RAG-mode badge UI.
   */
  focused_document_id?: number | null;
  /** Multi-provider: dispatch provider for this chat. */
  provider?: string | null;
  /**
   * Per-chat override for the tool-call repeat-loop cut threshold (K),
   * 0-100. ``null``/absent inherits the global admin default (then the
   * config default, 16).
   */
  repeat_warning_cut_k?: number | null;
}

export interface ChatSummary {
  id: number;
  title: string;
  folder: string | null;
  pinned: boolean;
  updated_at: string;
  model_id: string | null;
  /** Per-chat settings JSON blob. Present after migration 0003. */
  settings?: ChatSettings;
  /** DnD sort order within each folder / pinned section. */
  display_order: number;
  /** Incognito mode flag. */
  incognito?: boolean;
  /** UNIX epoch seconds; null when not incognito. */
  incognito_expires_at?: number | null;
  /** Optional project membership. */
  project_id?: number | null;
  /**
   * When the chat was moved out of a project, a snapshot of the project's
   * identity is captured AT detach time so the separator-turn UI can render
   * "Detached from {name} on {date}" even after the project is later
   * deleted. NULL = never detached.
   */
  detached_from_project_meta?: {
    project_id: number;
    name: string;
    detached_at: number;
    system_prompt_hash: string;
  } | null;
  /** Free-form user tags. Empty array = no tags. */
  tags: string[];
  /**
   * ISO timestamp the chat was archived at; null means it's active.
   * Soft — archiving never touches the chat's messages.
   */
  archived_at: string | null;
}

export interface MessageRecord {
  id: number;
  chat_id: number;
  role: "user" | "assistant" | "system" | "tool";
  content: string;
  reasoning_content: string | null;
  /**
   * Why the producing stream terminated (migration 0026): "stop" | "length"
   * | null. "length" renders the Continue chip on persisted assistant rows.
   * Optional because pre-0026 backends omit the field entirely.
   */
  stop_reason?: string | null;
  /**
   * Tool calls the producing stream executed (migration 0024), persisted by
   * the backend in the same shape as the live useSSE ToolCall accumulator
   * ({id, name, arguments, status, result?}). null/absent for user/system
   * rows, tool-free turns, and pre-0024 backends.
   */
  tool_calls?: ToolCall[] | null;
  /**
   * FK to compactions.id when the row has been archived by a /compact call.
   * NULL for rows written before migration 0037 (no backfill).
   */
  compaction_id?: number | null;
  created_at: string;
}

// Normalised list shape the components consume (not a raw wire shape).
export interface ChatListResponse {
  chats: ChatSummary[];
  total: number;
}

// Normalised messages shape the components consume (not a raw wire shape).
export interface MessageListResponse {
  messages: MessageRecord[];
  total: number;
  /** True when older messages exist beyond this page. */
  has_more: boolean;
  /** The smallest id in the current page (used as before_id cursor). */
  oldest_id: number | null;
}

// ─── Generated wire types ───────────────────────────────────────────────────
//
// Single source of truth: web/src/types/api.ts (openapi-typescript output of
// docs/api/openapi.yaml). When the BE wire drifts, these aliases break the
// typecheck instead of silently desyncing a hand-rolled mirror.

type ChatWire = components["schemas"]["ChatResponse"];
type ChatsListWire =
  paths["/api/chats"]["get"]["responses"]["200"]["content"]["application/json"];
type ChatDetailWire =
  paths["/api/chats/{chat_id}"]["get"]["responses"]["200"]["content"]["application/json"];
type MessageWire = components["schemas"]["Message"];

/** Normalise one generated ChatResponse into the UI-facing ChatSummary. */
function toChatSummary(c: ChatWire): ChatSummary {
  return {
    id: c.id,
    title: c.title,
    folder: c.folder,
    pinned: c.pinned,
    updated_at: c.updated_at,
    model_id: c.model_id ?? null,
    // Wire boundary: the BE serialises `settings` as an open JSON blob
    // (additionalProperties); the all-optional ChatSettings narrows it here.
    settings: c.settings,
    display_order: c.display_order,
    incognito: c.incognito,
    incognito_expires_at: c.incognito_expires_at ?? null,
    project_id: c.project_id ?? null,
    // Boundary tolerance: older backends omit `tags` — default to no tags
    // rather than letting `.map`/`.includes` call sites crash on undefined.
    tags: tagsOrEmpty(c.tags),
    archived_at: c.archived_at ?? null,
  };
}

/**
 * Runtime tolerance (tested): older backends omit `has_more` even though
 * the generated type marks it required on the modern wire. The `unknown`
 * parameter keeps the legacy-absence branch honest under the type-aware
 * lint (the wire type alone would prove the fallback "unreachable").
 */
function hasMoreOrFalse(v: unknown): boolean {
  return v === true;
}

/**
 * Runtime tolerance (tested): older backends omit `tags` even though the
 * generated type marks it required on the modern wire. The `unknown`
 * parameter keeps the legacy-absence branch honest under the type-aware
 * lint — same pattern as `hasMoreOrFalse` above.
 */
function tagsOrEmpty(v: unknown): string[] {
  return Array.isArray(v) ? (v as string[]) : [];
}

/** Normalise one generated Message into the UI-facing MessageRecord. */
function toMessageRecord(m: MessageWire): MessageRecord {
  return {
    id: m.id,
    chat_id: m.chat_id,
    // Boundary cast: the wire `role` is an open string; the DB only stores
    // the four MessageRecord roles.
    role: m.role as MessageRecord["role"],
    content: m.content,
    reasoning_content: m.reasoning_content,
    stop_reason: m.stop_reason ?? null,
    // Boundary cast: wire tool_calls entries are open JSON objects; the BE
    // accumulator (streaming_service._accumulate_tool_call) only writes the
    // FE ToolCall shape {id, name, arguments, status, result?}.
    tool_calls: (m.tool_calls ?? null) as ToolCall[] | null,
    compaction_id: m.compaction_id ?? null,
    created_at: m.created_at,
  };
}

interface CreateChatBody {
  title?: string | undefined;
  folder?: string | undefined;
  model_id?: string | undefined;
  /** Create the chat in incognito mode. */
  incognito?: boolean | undefined;
}

interface UpdateChatBody {
  title?: string | undefined;
  folder?: string | undefined;
  pinned?: boolean | undefined;
  /** Per-chat model selection. Persisted on TopBar selector change. */
  model_id?: string | undefined;
  /** Toggle RAG augmentation for this chat. */
  rag_enabled?: boolean | undefined;
  /**
   * Per-chat reasoning level.
   * "" clears the override (falls through to global default).
   */
  reasoning_effort?: string | undefined;
  /** A/B compare enabled flag. */
  ab_compare_enabled?: boolean | undefined;
  /** Model A for A/B compare. */
  ab_compare_model_a?: string | undefined;
  /** Model B for A/B compare. */
  ab_compare_model_b?: string | undefined;
  // ─── Per-chat rail fields ──────────────────────────────────────────
  /** Per-chat system prompt override. Empty string clears. */
  system_prompt?: string | undefined;
  /** Sampler temperature (0-2). */
  temperature?: number | undefined;
  /** Nucleus sampling threshold (0-1). */
  top_p?: number | undefined;
  /** Top-K filter. */
  top_k?: number | undefined;
  /** Min-P filter (0-1). */
  min_p?: number | undefined;
  /** Token repetition penalty. */
  repeat_penalty?: number | undefined;
  /** Max output token cap. */
  max_tokens?: number | undefined;
  /**
   * Alias for ``reasoning_effort`` surfaced in the v0.5.x right-rail
   * UI. Kept distinct from ``reasoning_effort`` so the rail can
   * present the field under its UI name. Empty string clears the override.
   */
  reasoning?: string | undefined;
  /** Opt-in to self-consistency orchestration. */
  self_consistency_enabled?: boolean | undefined;
  /** Opt-in to chain-of-verification orchestration. */
  chain_of_verification_enabled?: boolean | undefined;
  /** When true, the request sets store=false upstream. */
  stateless?: boolean | undefined;
  /** Forward-compat: name of the currently-active preset. */
  active_preset?: string | undefined;
  /** Move chat into a project. */
  project_id?: number | undefined;
  /** Comma-separated list of fields to explicitly NULL. Accepted names:
   *  ``project_id`` (detach from project) and ``model_id`` (reset the
   *  per-chat model override back to "Auto" — the flat ``model_id=""`` param
   *  is ignored server-side, so clearing must go through this path). */
  clear?: string | undefined;
  /** Multi-provider: provider slug for the model (e.g. "openrouter", "lmstudio"). */
  provider?: string | undefined;
  /**
   * Per-chat override for the tool-call repeat-loop cut threshold (K).
   * String (not number): mirrors ``reasoning_effort``'s wire shape so an
   * explicit empty string can clear the override (falls through to the
   * global admin default, then the config default). Callers pass
   * ``String(n)`` to set, ``""`` to clear.
   */
  repeat_warning_cut_k?: string | undefined;
  /**
   * Replaces the chat's whole tag list. Sent as a JSON-encoded array —
   * mirrors the ``ab_compare`` JSON-blob wire shape.
   */
  tags?: string[] | undefined;
}

// ─── Query keys ────────────────────────────────────────────────────────────

/** Scope filter the chat list / folder list queries respect.
 *
 *  - undefined / { projectId: null, unscoped: false }  → union (legacy default)
 *  - { projectId: null, unscoped: true }               → un-projected only
 *  - { projectId: <n> }                                → in project n
 *
 * The shape is stable so TanStack Query's structural-key hashing
 * never collides between scopes (the union and un-projected queries cannot share a key).
 */
export type ChatScope =
  | { projectId: number }
  | { projectId: null; unscoped: true }
  | undefined;

export const chatKeys = {
  all: ["chats"] as const,
  list: (scope?: ChatScope) => {
    if (scope === undefined) return [...chatKeys.all, "list"] as const;
    return [...chatKeys.all, "list", scope] as const;
  },
  // useChatsDirect returns ChatSummary[] directly; useChats wraps it as
  // {chats, total}. Sharing a queryKey put both shapes in the same cache
  // slot and the second consumer (whichever) crashed on the wrong shape.
  // Split the key so each shape gets its own cache row.
  listDirect: (scope?: ChatScope) => {
    if (scope === undefined) return [...chatKeys.all, "list-direct"] as const;
    return [...chatKeys.all, "list-direct", scope] as const;
  },
  detail: (id: number) => [...chatKeys.all, id] as const,
  messages: (chatId: number) => [...chatKeys.all, chatId, "messages"] as const,
};

// ─── Hooks ─────────────────────────────────────────────────────────────────

/** List all chats for the current user.
 *
 * GET /api/chats returns a plain ChatSummary[] array (not an envelope).
 * This hook normalises the wire response to { chats, total } so components
 * can access data.chats without breaking when the array is empty.
 *
 * Gated on !isInitializing to suppress 401 spam during mount-time /me
 * hydration.
 */
function chatsUrlFor(scope: ChatScope): string {
  if (scope === undefined) return "/api/chats";
  if ("unscoped" in scope) return "/api/chats?unscoped=true";
  return `/api/chats?project_id=${String(scope.projectId)}`;
}

export function useChats(scope?: ChatScope) {
  const { isInitializing, user } = useAuthStore();
  return useQuery<ChatListResponse, ApiError>({
    queryKey: chatKeys.list(scope),
    queryFn: async () => {
      const raw = await api.request<ChatsListWire>(chatsUrlFor(scope));
      const chats = raw.map(toChatSummary);
      return { chats, total: chats.length };
    },
    enabled: !isInitializing && user !== null,
  });
}

/**
 * Wrapper that returns ChatSummary[] directly (no envelope).
 *
 * Used by Sidebar.tsx for DnD ordering. Sort is applied by display_order
 * within each group (pinned, per-folder, ungrouped).
 */
export function useChatsDirect(scope?: ChatScope) {
  const { isInitializing, user } = useAuthStore();
  return useQuery<ChatSummary[], ApiError>({
    queryKey: chatKeys.listDirect(scope),
    queryFn: async () => {
      const raw = await api.request<ChatsListWire>(chatsUrlFor(scope));
      return raw.map(toChatSummary);
    },
    enabled: !isInitializing && user !== null,
  });
}

/** Fetch messages for a single chat.
 *
 * Messages are embedded in GET /api/chats/{id} (no separate /messages route).
 * Normalises the response to { messages, total, has_more, oldest_id }
 * to support cursor-based "load older" pagination.
 *
 * Gated on !isInitializing to suppress 401 spam during mount-time /me
 * hydration.
 */
export function useMessages(chatId: number | null) {
  const { isInitializing, user } = useAuthStore();
  return useQuery<MessageListResponse, ApiError>({
    queryKey: chatKeys.messages(chatId ?? 0),
    queryFn: async () => {
      const raw = await api.request<ChatDetailWire>(
        `/api/chats/${String(chatId)}`,
      );
      const messages = raw.messages.map(toMessageRecord);
      const oldest_id =
        messages.length > 0 ? (messages[0]?.id ?? null) : null;
      return {
        messages,
        total: messages.length,
        // Runtime tolerance (tested): older backends omit has_more even
        // though the generated type marks it required on the modern wire.
        has_more: hasMoreOrFalse(raw.has_more),
        oldest_id,
      };
    },
    enabled: chatId !== null && !isInitializing && user !== null,
  });
}

/** Create a new chat.
 *
 * POST /api/chats expects Form-encoded body (title=…) per the backend's
 * Form(...) parameter declaration.
 */
export function useCreateChat() {
  const qc = useQueryClient();
  return useMutation<ChatSummary, ApiError, CreateChatBody>({
    // Callers pass per-call onError via mutate(payload, { onError }).
    // TanStack Query v5 stores those on the observer's private #mutateOptions —
    // not visible on mutation.options — so we declare meta.errorHandled to
    // prevent the global MutationCache fallback from double-toasting.
    meta: { errorHandled: true },
    mutationFn: async (body) => {
      const created = toChatSummary(
        await api.postForm<ChatWire>("/api/chats", {
          ...(body.title !== undefined ? { title: body.title } : {}),
          ...(body.folder !== undefined ? { folder: body.folder } : {}),
          ...(body.model_id !== undefined ? { model_id: body.model_id } : {}),
          // Forward the incognito flag as a form field. When true,
          // the backend marks the new chat incognito and schedules its TTL.
          ...(body.incognito !== undefined
            ? { incognito: String(body.incognito) }
            : {}),
        }),
      );
      return created;
    },
    onSuccess: () => {
      // Invalidate the whole chats key prefix — list / listDirect /
      // detail / messages all derive from server state that mutations
      // touch. Targeting just `chatKeys.list()` left `chatKeys.listDirect()`
      // (the sidebar) stale after delete — deleting a chat wasn't removing
      // it from the sidebar UI.
      void qc.invalidateQueries({ queryKey: chatKeys.all });
    },
  });
}

/** Update a chat (title, folder, pinned).
 *
 * PATCH /api/chats/{id} expects Form-encoded body per the backend's
 * Form(...) parameter declarations.
 */
export function useUpdateChat(chatId: number) {
  const qc = useQueryClient();
  return useMutation<ChatSummary, ApiError, UpdateChatBody>({
    // Callers (ChatSettingsRail, Chat.tsx model-change, etc.) always pass
    // per-call onError via mutate(payload, { onError }).  TanStack Query v5
    // stores those on the observer's private #mutateOptions and they are NOT
    // visible on mutation.options — so the global MutationCache.onError
    // cannot detect them via mutation.options.onError.  We declare
    // meta.errorHandled = true so the global fallback skips this mutation
    // entirely, preventing double toasts.
    meta: { errorHandled: true },
    mutationFn: async (body) => {
      const params = new URLSearchParams();
      if (body.title !== undefined) params.set("title", body.title);
      if (body.folder !== undefined) params.set("folder", body.folder);
      if (body.pinned !== undefined) params.set("pinned", String(body.pinned));
      if (body.tags !== undefined) params.set("tags", JSON.stringify(body.tags));
      if (body.model_id !== undefined) params.set("model_id", body.model_id);
      if (body.rag_enabled !== undefined)
        params.set("rag_enabled", String(body.rag_enabled));
      // Persist per-chat reasoning_effort to backend settings blob.
      if (body.reasoning_effort !== undefined)
        params.set("reasoning_effort", body.reasoning_effort);
      // A/B compare settings.
      if (body.ab_compare_enabled !== undefined)
        params.set("ab_compare_enabled", String(body.ab_compare_enabled));
      if (body.ab_compare_model_a !== undefined)
        params.set("ab_compare_model_a", body.ab_compare_model_a);
      if (body.ab_compare_model_b !== undefined)
        params.set("ab_compare_model_b", body.ab_compare_model_b);
      // Per-chat rail fields ----------------------------------------
      if (body.system_prompt !== undefined)
        params.set("system_prompt", body.system_prompt);
      if (body.temperature !== undefined)
        params.set("temperature", String(body.temperature));
      if (body.top_p !== undefined) params.set("top_p", String(body.top_p));
      if (body.top_k !== undefined) params.set("top_k", String(body.top_k));
      if (body.min_p !== undefined) params.set("min_p", String(body.min_p));
      if (body.repeat_penalty !== undefined)
        params.set("repeat_penalty", String(body.repeat_penalty));
      if (body.max_tokens !== undefined)
        params.set("max_tokens", String(body.max_tokens));
      // ``reasoning`` is the rail's UI key (distinct from the legacy
      // ``reasoning_effort`` send-pill key).
      if (body.reasoning !== undefined) params.set("reasoning", body.reasoning);
      if (body.self_consistency_enabled !== undefined)
        params.set(
          "self_consistency_enabled",
          String(body.self_consistency_enabled),
        );
      if (body.chain_of_verification_enabled !== undefined)
        params.set(
          "chain_of_verification_enabled",
          String(body.chain_of_verification_enabled),
        );
      if (body.stateless !== undefined)
        params.set("stateless", String(body.stateless));
      if (body.active_preset !== undefined)
        params.set("active_preset", body.active_preset);
      if (body.provider !== undefined) params.set("provider", body.provider);
      // Per-chat repeat-loop cut threshold (K). Sent as-is (including "")
      // — mirrors reasoning_effort's empty-string-clears handling above.
      if (body.repeat_warning_cut_k !== undefined)
        params.set("repeat_warning_cut_k", body.repeat_warning_cut_k);
      // Explicit-NULL list (e.g. "model_id" to reset the picker to "Auto",
      // "project_id" to detach). FastAPI can't tell an omitted form field
      // from an empty one, so clears must be named here rather than sent as "".
      if (body.clear !== undefined) params.set("clear", body.clear);
      return toChatSummary(
        await api.request<ChatWire>(`/api/chats/${String(chatId)}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: params.toString(),
        }),
      );
    },
    onSuccess: () => {
      // Invalidate the whole chats key prefix — list / listDirect /
      // detail / messages all derive from server state that mutations
      // touch. Targeting just `chatKeys.list()` left `chatKeys.listDirect()`
      // (the sidebar) stale after delete — deleting a chat wasn't removing
      // it from the sidebar UI.
      void qc.invalidateQueries({ queryKey: chatKeys.all });
      void qc.invalidateQueries({ queryKey: chatKeys.detail(chatId) });
    },
  });
}

/** Delete a chat.
 *
 * DELETE /api/chats/{id} returns 204 No Content (generated spec) — the
 * previous hand-rolled `{status: string}` TData was a fiction; no caller
 * reads the result.
 */
export function useDeleteChat() {
  const qc = useQueryClient();
  return useMutation<undefined, ApiError, number>({
    // Callers handle their own errors (Sidebar per-call onError toast,
    // Chat.tsx catch toast) — meta.errorHandled keeps the global
    // MutationCache fallback silent (dedup).
    meta: { errorHandled: true },
    mutationFn: (chatId) =>
      api.request<undefined>(`/api/chats/${String(chatId)}`, {
        method: "DELETE",
      }),
    onSuccess: () => {
      // Invalidate the whole chats key prefix — list / listDirect /
      // detail / messages all derive from server state that mutations
      // touch. Targeting just `chatKeys.list()` left `chatKeys.listDirect()`
      // (the sidebar) stale after delete — deleting a chat wasn't removing
      // it from the sidebar UI.
      void qc.invalidateQueries({ queryKey: chatKeys.all });
    },
  });
}

/** Clear a chat's message history (/clear command).
 *
 * DELETE /api/chats/{id}/messages removes every message but keeps the chat
 * shell (title, per-chat settings, project link). Returns the count cleared.
 */
export function useClearChatMessages() {
  const qc = useQueryClient();
  return useMutation<{ cleared: number }, ApiError, number>({
    // Caller (Chat.tsx) shows its own toast; keep the global fallback silent.
    meta: { errorHandled: true },
    mutationFn: (chatId) =>
      api.request<{ cleared: number }>(
        `/api/chats/${String(chatId)}/messages`,
        { method: "DELETE" },
      ),
    onSuccess: () => {
      // The chat survives but its message list changed — invalidate the whole
      // prefix so the detail/messages queries refetch (now empty).
      void qc.invalidateQueries({ queryKey: chatKeys.all });
    },
  });
}

/** Fork a chat at a given message boundary.
 *
 * POST /api/chats/{id}/fork expects Form-encoded body (at_message_id=…) per
 * the backend's Form(...) parameter declaration.
 */
export function useForkChat(chatId: number) {
  const qc = useQueryClient();
  return useMutation<ChatSummary, ApiError, { at_message_id: number }>({
    // Caller (Chat.tsx handleFork) shows its own "Fork failed." toast —
    // meta.errorHandled keeps the global MutationCache fallback silent.
    meta: { errorHandled: true },
    mutationFn: async ({ at_message_id }) => {
      const params = new URLSearchParams();
      params.set("at_message_id", String(at_message_id));
      return toChatSummary(
        await api.request<ChatWire>(`/api/chats/${String(chatId)}/fork`, {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: params.toString(),
        }),
      );
    },
    onSuccess: () => {
      // Invalidate the whole chats key prefix — list / listDirect /
      // detail / messages all derive from server state that mutations
      // touch. Targeting just `chatKeys.list()` left `chatKeys.listDirect()`
      // (the sidebar) stale after delete — deleting a chat wasn't removing
      // it from the sidebar UI.
      void qc.invalidateQueries({ queryKey: chatKeys.all });
    },
  });
}

/** Reorder a chat (DnD move to folder at display_order position, or the
 * MoveToFolderMenu "move to folder" / "remove from folder" actions).
 *
 * PATCH /api/chats/reorder expects form-encoded body:
 * chat_id=<n>&folder=<name>&display_order=<n>
 *
 * `folder: null` means "the ungrouped/pinned bucket" — sent as an
 * EXPLICIT `clear_folder=true` sentinel rather than by
 * omitting the `folder` field. The two are functionally equivalent on the
 * wire today (the backend's Form default is already `None`), but making
 * the clear explicit means the contract doesn't silently depend on that
 * default staying `None` forever, and it's what actually reads as
 * "clear this" when the caller is the MoveToFolderMenu's "Remove from
 * folder" action rather than a same-bucket DnD reorder.
 */
export function useReorderChat() {
  const qc = useQueryClient();
  // NOT retyped against the generated spec — the BE route declares
  // `response_model=None`, so the generated type is `unknown`
  // (a downgrade from the known `{ok}` ack shape).
  return useMutation<
    { ok: boolean },
    ApiError,
    { chat_id: number; folder: string | null; display_order: number }
  >({
    mutationFn: ({ chat_id, folder, display_order }) => {
      const params = new URLSearchParams();
      params.set("chat_id", String(chat_id));
      if (folder !== null) {
        params.set("folder", folder);
      } else {
        params.set("clear_folder", "true");
      }
      params.set("display_order", String(display_order));
      return api.request<{ ok: boolean }>("/api/chats/reorder", {
        method: "PATCH",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: params.toString(),
      });
    },
    onSuccess: () => {
      // Invalidate the whole chats key prefix — list / listDirect /
      // detail / messages all derive from server state that mutations
      // touch. Targeting just `chatKeys.list()` left `chatKeys.listDirect()`
      // (the sidebar) stale after delete — deleting a chat wasn't removing
      // it from the sidebar UI.
      void qc.invalidateQueries({ queryKey: chatKeys.all });
    },
  });
}

/** Append a message to a chat's history.
 *
 * POST /api/chats/{id}/messages (form-encoded; role + content required).
 * Returns the inserted Message record (id, chat_id, role, content, etc.).
 * Invalidates the messages query so the appended turn appears in the UI on
 * the next render.
 *
 * Used by the A/B compare "Use this response" flow to land the selected
 * pane's assistant text in the chat's message history.
 */
export function useAppendMessage(chatId: number) {
  const qc = useQueryClient();
  return useMutation<
    MessageRecord,
    ApiError,
    {
      role: "user" | "assistant" | "system" | "tool";
      content: string;
      reasoning_content?: string;
      model_id?: string;
    }
  >({
    // Callers pass per-call onError via mutate(payload, { onError }).
    // Declare meta.errorHandled so the global MutationCache fallback does not
    // also fire a generic toast when the caller already handles the error.
    meta: { errorHandled: true },
    mutationFn: async (body) => {
      const params = new URLSearchParams();
      params.set("role", body.role);
      params.set("content", body.content);
      if (body.reasoning_content !== undefined)
        params.set("reasoning_content", body.reasoning_content);
      if (body.model_id !== undefined) params.set("model_id", body.model_id);
      return toMessageRecord(
        await api.request<MessageWire>(
          `/api/chats/${String(chatId)}/messages`,
          {
            method: "POST",
            headers: { "Content-Type": "application/x-www-form-urlencoded" },
            body: params.toString(),
          },
        ),
      );
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: chatKeys.messages(chatId) });
      void qc.invalidateQueries({ queryKey: chatKeys.detail(chatId) });
    },
  });
}

/** Edit a user-role message body.
 *
 * PATCH /api/messages/{id} expects form-encoded ``content``.  On success the
 * messages cache for the parent chat is invalidated so the updated text
 * reaches the UI on the next render.
 */
export function useEditMessage(chatId: number) {
  const qc = useQueryClient();
  return useMutation<
    MessageRecord,
    ApiError,
    { messageId: number; content: string }
  >({
    mutationFn: async ({ messageId, content }) => {
      const params = new URLSearchParams();
      params.set("content", content);
      return toMessageRecord(
        await api.request<MessageWire>(`/api/messages/${String(messageId)}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: params.toString(),
        }),
      );
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: chatKeys.messages(chatId) });
      void qc.invalidateQueries({ queryKey: chatKeys.detail(chatId) });
    },
  });
}

/** Delete a single message (per-message destructive action).
 *
 * DELETE /api/messages/{id}. On success the messages cache for the parent
 * chat is invalidated so the UI re-renders without the deleted row.
 * Per-message delete belongs in the hover action bar so a failed assistant
 * turn (empty body + only a Regenerate button) can be dismissed without
 * taking the whole chat down.
 */
export function useDeleteMessage(chatId: number) {
  const qc = useQueryClient();
  // DELETE /api/messages/{id} returns 204 No Content (generated spec) —
  // the previous hand-rolled `{status: string}` TData was a fiction.
  return useMutation<undefined, ApiError, number>({
    // Caller (Chat.tsx handleDeleteMessage) passes per-call onError with its
    // own toast — meta.errorHandled keeps the global fallback silent (dedup).
    meta: { errorHandled: true },
    mutationFn: (messageId) =>
      api.request<undefined>(`/api/messages/${String(messageId)}`, {
        method: "DELETE",
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: chatKeys.messages(chatId) });
      void qc.invalidateQueries({ queryKey: chatKeys.detail(chatId) });
    },
  });
}

/** Regenerate an assistant message.
 *
 * POST /api/chats/{chat_id}/messages/{message_id}/regenerate.
 *
 * Two-step contract:
 *   - call without ``?confirm=true`` → server returns HTTP 412 with
 *     ``{detail: {code, subsequent_count, chat_id, message_id}}``.
 *   - call with ``?confirm=true`` → server deletes the boundary message
 *     and all subsequent ones, returning ``{deleted, chat_id}``.
 *
 * The caller follows the confirmation flow client-side and then triggers
 * the existing SSE stream to replay the assistant turn.
 */
export interface RegenerateConfirmDetail {
  code: string;
  subsequent_count: number;
  chat_id: number;
  message_id: number;
}

export interface RegenerateResult {
  deleted: number;
  chat_id: number;
  /**
   * Content of the user prompt that triggered the deleted assistant
   * turn. The backend now deletes the whole turn (user prompt +
   * assistant reply + everything after) and returns the prompt text so
   * the caller can resubmit it as a fresh turn — LM Studio rejects an
   * empty `input` array, so we can't just rely on previous_response_id.
   * Null only when the deleted assistant turn had no preceding user
   * message (a malformed chat; the UI should surface an error).
   */
  prior_user_content: string | null;
}

export function useRegenerateMessage(chatId: number) {
  const qc = useQueryClient();
  // NOT retyped against the generated spec — the BE route is untyped
  // (dict return), so the generated response type is `unknown`;
  // the hand-rolled RegenerateResult above remains the best contract.
  return useMutation<
    RegenerateResult,
    ApiError,
    { messageId: number; confirm: boolean }
  >({
    // Per-call onError in Chat.tsx handles the 412 confirm-required flow and
    // generic error toast.  Declare meta.errorHandled to prevent the global
    // MutationCache fallback from also firing a generic "Couldn't save" toast.
    meta: { errorHandled: true },
    mutationFn: ({ messageId, confirm }) => {
      const qs = confirm ? "?confirm=true" : "";
      return api.request<RegenerateResult>(
        `/api/chats/${String(chatId)}/messages/${String(messageId)}/regenerate${qs}`,
        { method: "POST" },
      );
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: chatKeys.messages(chatId) });
      void qc.invalidateQueries({ queryKey: chatKeys.detail(chatId) });
    },
  });
}

/** Compact a chat (head-trim message history to target_tokens).
 *
 * POST /api/chats/{id}/compact expects Form-encoded body (target_tokens=…)
 * per the backend's Form(...) parameter declaration.
 */
export function useCompactChat(chatId: number) {
  const qc = useQueryClient();
  // Typed against the generated CompactResultResponse — the previous
  // hand-rolled `{status: string}` TData never matched the wire
  // ({chat_id, removed_message_ids, remaining/original_token_count}).
  return useMutation<
    components["schemas"]["CompactResultResponse"],
    ApiError,
    { target_tokens: number }
  >({
    // Caller (Chat.tsx handleCompact) shows its own "Compact failed." toast —
    // meta.errorHandled keeps the global MutationCache fallback silent.
    meta: { errorHandled: true },
    mutationFn: ({ target_tokens }) => {
      const params = new URLSearchParams();
      params.set("target_tokens", String(target_tokens));
      return api.request<components["schemas"]["CompactResultResponse"]>(
        `/api/chats/${String(chatId)}/compact`,
        {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: params.toString(),
        },
      );
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: chatKeys.messages(chatId) });
    },
  });
}

/** Auto-generate a chat title from the first turns.
 *
 * POST /api/chats/{id}/generate-title — no request body.  Returns
 * ``{title}`` with the persisted title.  Idempotent: when the chat
 * already has a user-set title the backend returns it unchanged without
 * calling LM Studio.
 *
 * The frontend triggers this after the second assistant message lands
 * (see ``pages/Chat.tsx``); a 502 from a transient upstream failure is
 * swallowed silently so the user never sees an auto-title error toast.
 */
// Aliased to the generated wire type (kept under the existing exported
// name — useAutotitleEffect imports it).
export type GenerateTitleResult = components["schemas"]["GenerateTitleResponse"];

export function useGenerateTitle() {
  const qc = useQueryClient();
  return useMutation<GenerateTitleResult, ApiError, number>({
    // Auto-title is a background nicety — the caller swallows failures BY
    // DESIGN (the user is never bothered). meta.errorHandled keeps the
    // global MutationCache fallback from surfacing a toast anyway.
    meta: { errorHandled: true },
    mutationFn: (chatId) =>
      api.request<GenerateTitleResult>(
        `/api/chats/${String(chatId)}/generate-title`,
        { method: "POST" },
      ),
    onSuccess: (data, chatId) => {
      // Optimistically patch every cached chat-list row so the sidebar (and
      // any other mounted list) updates immediately, without waiting for a
      // refetch.
      //
      // This used to target only chatKeys.list() with a
      // ChatSummary[] updater. Two bugs: (1) wrong key — the sidebar reads
      // chatKeys.listDirect(scope), which chatKeys.list() never matches
      // (React Query keys match structurally); (2) wrong shape —
      // chatKeys.list() holds a {chats, total} ENVELOPE, not a bare array,
      // so `.map` on `existing` threw whenever useChats() was mounted.
      // Fan out over every cached key under chatKeys.all and branch on the
      // actual shape found there (bare array for listDirect, {chats,...}
      // envelope for list; anything else — chat detail/messages — is left
      // untouched).
      qc.setQueriesData<unknown>({ queryKey: chatKeys.all }, (old: unknown) => {
        if (Array.isArray(old)) {
          return (old as ChatSummary[]).map((c) =>
            c.id === chatId ? { ...c, title: data.title } : c,
          );
        }
        if (
          old !== null &&
          typeof old === "object" &&
          "chats" in old &&
          Array.isArray(old.chats)
        ) {
          const envelope = old as { chats: ChatSummary[] };
          return {
            ...envelope,
            chats: envelope.chats.map((c) =>
              c.id === chatId ? { ...c, title: data.title } : c,
            ),
          };
        }
        return old;
      });
      // Still invalidate so server-truth lands on the next idle moment
      // (catches any race against a manual rename).
      // Invalidate the whole chats key prefix — list / listDirect /
      // detail / messages all derive from server state that mutations
      // touch. Targeting just `chatKeys.list()` left `chatKeys.listDirect()`
      // (the sidebar) stale after delete — deleting a chat wasn't removing
      // it from the sidebar UI.
      void qc.invalidateQueries({ queryKey: chatKeys.all });
      void qc.invalidateQueries({ queryKey: chatKeys.detail(chatId) });
    },
  });
}

// ─── Archive ─────────────────────────────────────────────────────────────────
//
// Soft-archive, mirroring useProjects' useArchiveProject / useUnarchiveProject
// (see hooks/useProjects.ts). GET /api/chats excludes archived chats by
// default (backend `include_archived` query param defaults to false), so the
// existing useChats/useChatsDirect list queries already reflect archiving
// with no changes needed there — only a dedicated query for the "Archived"
// section is new.

/**
 * List the caller's archived chats (un-projected — matches the sidebar's
 * scope). Fetches with `include_archived=true` then filters client-side to
 * just the archived subset, mirroring Projects.tsx's active/archived split.
 * A separate cache key from `chatKeys.listDirect()` — same reasoning as
 * `projectKeys.list(includeArchived)` getting its own slot in useProjects.ts.
 */
export function useArchivedChats() {
  const { isInitializing, user } = useAuthStore();
  return useQuery<ChatSummary[], ApiError>({
    queryKey: [...chatKeys.all, "list-direct", "archived"] as const,
    queryFn: async () => {
      const raw = await api.request<ChatsListWire>(
        "/api/chats?unscoped=true&include_archived=true",
      );
      return raw.map(toChatSummary).filter((c) => c.archived_at !== null);
    },
    enabled: !isInitializing && user !== null,
  });
}

/** Archive a chat. Soft — messages are untouched; the chat just drops out
 *  of the default sidebar/list. */
export function useArchiveChat() {
  const qc = useQueryClient();
  return useMutation<ChatSummary, ApiError, number>({
    // Caller (Sidebar row action) shows its own catch toast —
    // meta.errorHandled keeps the global MutationCache fallback silent.
    meta: { errorHandled: true },
    mutationFn: async (chatId) =>
      toChatSummary(
        await api.postForm<ChatWire>(`/api/chats/${String(chatId)}/archive`, {}),
      ),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: chatKeys.all });
    },
  });
}

/** Unarchive a chat. Reverses `useArchiveChat`. */
export function useUnarchiveChat() {
  const qc = useQueryClient();
  return useMutation<ChatSummary, ApiError, number>({
    meta: { errorHandled: true },
    mutationFn: async (chatId) =>
      toChatSummary(
        await api.postForm<ChatWire>(`/api/chats/${String(chatId)}/unarchive`, {}),
      ),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: chatKeys.all });
    },
  });
}
