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
        // TODO: drop dual-shape defense once ModelInfo aliases are gone
        // from the BE wire for a release. Then consume
        // `components["schemas"]["ModelInfo"]` directly.
        //
        // Historical: the backend used to serialise ModelInfo with
        // serialization aliases (`displayName` etc), so the wire mixed
        // casings. A wire normalization pass made it snake_case-only; this
        // normalizer keeps reading snake-first with the camelCase fallback
        // as the one-release safety net for the rollout. Fall back to `key`
        // when both spellings are missing OR empty — previously the ternary
        // `display_name !== "" ? display_name : key` kept `undefined` when
        // the field wasn't present, rendering blank <option> text in every
        // dropdown.
        const wire = m as Record<string, unknown>;
        const displayName =
          typeof wire.display_name === "string" && wire.display_name.length > 0
            ? wire.display_name
            : typeof wire.displayName === "string" &&
                wire.displayName.length > 0
              ? wire.displayName
              : m.key;
        // max_context_length is camelCase on the wire (FastAPI default
        // response_model_by_alias=True) but the pydantic field name is
        // snake_case. Defend against either.
        const maxContextLength =
          typeof wire.max_context_length === "number"
            ? wire.max_context_length
            : typeof wire.maxContextLength === "number"
              ? wire.maxContextLength
              : 0;
        // The actually-loaded context per instance — same dual-shape
        // defense for the BE alias.
        const loadedContextLength =
          typeof wire.loaded_context_length === "number"
            ? wire.loaded_context_length
            : typeof wire.loadedContextLength === "number"
              ? wire.loadedContextLength
              : 0;
        // size_bytes / params_string carry the same alias asymmetry; defend
        // here too while we're touching this normalizer.
        const sizeBytes =
          typeof wire.size_bytes === "number"
            ? wire.size_bytes
            : typeof wire.sizeBytes === "number"
              ? wire.sizeBytes
              : 0;
        const paramsString =
          typeof wire.params_string === "string"
            ? wire.params_string
            : typeof wire.paramsString === "string"
              ? wire.paramsString
              : "";
        // Normalize capabilities to the correct ModelCapabilities shape.
        // The BE serialises this as a full
        // Capabilities object; older responses or embedding models may omit
        // it entirely. Provide a safe default so consumers can always read
        // capabilities.vision / .trained_for_tool_use without null-checks.
        // Multi-provider: read provider slug from wire; default to "lmstudio"
        // for backward-compat with LM Studio-only responses.
        const provider =
          typeof wire.provider === "string" && wire.provider.length > 0
            ? wire.provider
            : "lmstudio";
        const rawCaps = wire.capabilities as Record<string, unknown> | null | undefined;
        const capabilities: ModelCapabilities = {
          vision: rawCaps?.vision === true,
          trained_for_tool_use: rawCaps?.trained_for_tool_use === true,
          reasoning:
            rawCaps?.reasoning != null &&
            typeof rawCaps.reasoning === "object" &&
            "default" in rawCaps.reasoning &&
            "allowed_options" in rawCaps.reasoning
              ? (rawCaps.reasoning as ModelCapabilities["reasoning"])
              : null,
          embedding: rawCaps?.embedding === true,
        };
        return {
          id: m.key,
          name: displayName,
          provider,
          capabilities,
          loaded: m.loaded_instances > 0,
          size_bytes: sizeBytes,
          params_string: paramsString,
          // Generated type marks these optional (pydantic defaults) —
          // normalise to the UI shape's non-optional contracts.
          quantization: m.quantization ?? null,
          loaded_instance_ids: m.loaded_instance_ids ?? [],
          max_context_length: maxContextLength,
          loaded_context_length: loadedContextLength,
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
