/* SPDX-License-Identifier: Apache-2.0 */
/**
 * TanStack Query hooks for /api/models.
 *
 * The model list feeds the Settings panel's model selector and the Chat
 * top-bar's per-chat model override.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { ApiError } from "@/lib/api";
import type { paths } from "@/types/api";
import { useAuthStore } from "@/stores/authStore";

/**
 * FE capability type mirroring the backend.
 *
 * The BE `Capabilities` pydantic model (models_service.py:209) has:
 *   - vision: bool
 *   - trained_for_tool_use: bool
 *   - reasoning: { default, allowed_options } | null
 *   - embedding: bool (derived)
 *
 * The previous `Record<string,boolean>` type was wrong for `reasoning`
 * (which is an object or null, not a boolean). This type fix surfaces the
 * correct shape so consumers can gate on `capabilities.reasoning !== null`
 * and access `capabilities.reasoning.default` / `allowed_options`.
 *
 * Runtime value is already passed through correctly in the normalizer —
 * `capabilities: m.capabilities` at line 133. This is a type-only change.
 */
export interface ModelCapabilities {
  vision: boolean;
  trained_for_tool_use: boolean;
  reasoning: {
    default: "off" | "on" | "low" | "medium" | "high";
    allowed_options: ("off" | "on" | "low" | "medium" | "high")[];
  } | null;
  embedding: boolean;
}

/** Normalised model shape used by all UI components. */
export interface ModelInfo {
  id: string;
  name: string;
  /** Provider slug — "lmstudio" for local models, or a cloud slug (e.g. "openrouter"). */
  provider: string;
  capabilities: ModelCapabilities;
  loaded: boolean;
  // Raw fields for AdminModels page.
  size_bytes: number;
  params_string: string;
  quantization: { name: string; bits_per_weight?: number | null } | null;
  loaded_instance_ids: string[];
  // max_context_length from LM Studio's wire response — the model's
  // ARCHITECTURAL MAX (e.g. 262144 for qwen3.5 9b). Distinct from
  // loaded_context_length below.
  max_context_length: number;
  // The actually-loaded context per instance (e.g. 98304 for the 9B
  // loaded at 96k). When ANY instance is loaded this is what context-aware
  // UIs should display — Settings dropdown, ChatSettingsRail, Composer's
  // context meter. Falls back to 0 when the model isn't loaded; consumers
  // should `loaded_context_length || max_context_length` to pick the right
  // number for display.
  loaded_context_length: number;
}

/**
 * Wire shape returned by GET /api/models — the GENERATED OpenAPI type.
 * The BE ModelInfo's LM Studio aliases are now validation-only, so
 * serialization (and therefore this wire) is snake_case everywhere;
 * `components["schemas"]["ModelInfo"]` is the single source of truth instead
 * of a hand-rolled mirror.
 *   key                   → id
 *   display_name          → name
 *   loaded_instances      (count) → loaded (bool)
 *   loaded_instance_ids   → loaded_instance_ids
 *   max_context_length    → max_context_length
 */
type ModelsListWire =
  paths["/api/models"]["get"]["responses"]["200"]["content"]["application/json"];

// GET /api/models returns ModelInfo[] — plain array, no envelope.
// This hook normalises the wire response to { models, total } so
// components can access data.models without breaking on an empty array.
export interface ModelListResponse {
  models: ModelInfo[];
  total: number;
}

export const modelKeys = {
  all: ["models"] as const,
  list: () => [...modelKeys.all, "list"] as const,
};

/** Fetch available LM Studio models.
 *
 * GET /api/models returns a plain generated-ModelInfo[] array with backend
 * field names (key, display_name, loaded_instances).  This hook normalises
 * the wire fields to the UI-facing ModelInfo shape (id, name, loaded).
 *
 * Gated on !isInitializing to suppress 401 spam during mount-time /me
 * hydration.
 */
export function useModels() {
  const { isInitializing, user } = useAuthStore();
  return useQuery<ModelListResponse, ApiError>({
    queryKey: modelKeys.list(),
    queryFn: async () => {
      const raw = await api.request<ModelsListWire>("/api/models");
      const models: ModelInfo[] = raw.map((m) => {
        // Fall back to `key` when `display_name` is empty or the wire
        // omits it (LM Studio doesn't always send it) — rendering
        // `undefined` would blank the <option> text in every dropdown.
        const displayName = m.display_name || m.key;
        // Provide a safe default so consumers can always read
        // capabilities.vision / .trained_for_tool_use without null-checks;
        // embedding models and older responses may omit `capabilities`.
        const capabilities: ModelCapabilities = {
          vision: m.capabilities?.vision === true,
          trained_for_tool_use: m.capabilities?.trained_for_tool_use === true,
          reasoning: m.capabilities?.reasoning ?? null,
          embedding: m.capabilities?.embedding === true,
        };
        return {
          id: m.key,
          name: displayName,
          // Multi-provider: default to "lmstudio" for backward-compat
          // with LM Studio-only responses.
          provider: m.provider || "lmstudio",
          capabilities,
          loaded: m.loaded_instances > 0,
          size_bytes: m.size_bytes,
          params_string: m.params_string ?? "",
          // Generated type marks these optional (pydantic defaults) —
          // normalise to the UI shape's non-optional contracts.
          quantization: m.quantization ?? null,
          loaded_instance_ids: m.loaded_instance_ids ?? [],
          max_context_length: m.max_context_length,
          loaded_context_length: m.loaded_context_length,
        };
      });
      return { models, total: models.length };
    },
    // staleTime must be ≤ useLmStudioStatus's `staleAfterMs` (30s) —
    // otherwise a remount with cached-but-aged data (e.g. navigating
    // back to the chat shell after 60s on Settings) reads as "fresh
    // enough, no refetch needed" to TanStack, the badge sees ageMs
    // > 30s, isFetching stays false, and the dot flashes yellow until
    // the next 25s refetch tick. Keeping staleTime under the badge's
    // threshold ensures any remount that would render yellow ALSO
    // triggers an immediate refetch, flipping isFetching=true so the
    // hook's "re-probing" branch keeps the dot green.
    staleTime: 20_000,
    refetchInterval: 25_000,
    refetchIntervalInBackground: false,
    enabled: !isInitializing && user !== null,
  });
}

/**
 * Force the backend to re-probe LM Studio and replace its in-memory model
 * cache, then invalidate the FE list query so consumers refetch the fresh
 * payload.
 *
 * GET /api/models reads the BE cache only — invalidating the FE query
 * alone returns the same stale list. The user-facing "Refresh list" buttons
 * (LmStudioSection, AdminModels) must call THIS hook to actually surface
 * loaded/unloaded changes.
 *
 * Endpoint is admin-only (POST /api/admin/models/refresh). A background
 * lifespan task already re-probes every
 * `lm_chat_models_cache_refresh_interval_seconds` (default 30 min), so
 * non-admin users still see updates — just not on demand.
 */
export function useRefreshModels() {
  const qc = useQueryClient();
  return useMutation<unknown, ApiError>({
    // Callers (LmStudioSection, AdminModels refresh buttons) pass per-call
    // onError with their own toasts — meta.errorHandled keeps the global
    // MutationCache fallback silent (dedup).
    meta: { errorHandled: true },
    mutationFn: async () => {
      return await api.request<unknown>("/api/admin/models/refresh", {
        method: "POST",
      });
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: modelKeys.list() });
    },
  });
}
