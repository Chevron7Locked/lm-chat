/* SPDX-License-Identifier: Apache-2.0 */
/**
 * usePresetModels / useSetPresetModels — TanStack Query hooks for the
 * preset-models settings surface.
 *
 * Routes (backend, 236cc54):
 *   GET /api/settings/preset-models
 *     → Record<string, { provider: string; model_id: string }>  (may be {})
 *   PUT /api/settings/preset-models
 *     body = same mapping  → sanitized mapping (unknown providers dropped)
 *
 * Mirrors the useProviders / useQuota patterns:
 *   - api.request() for JSON GET / PUT.
 *   - query key follows the [namespace, variant] array convention.
 *   - On mutation success: invalidate the GET query.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { ApiError } from "@/lib/api";

// ─── Wire shape ──────────────────────────────────────────────────────────────

/** One preset model assignment. */
interface PresetModelEntry {
  /** Provider slug — e.g. "lmstudio", "openrouter". */
  provider: string;
  /** Model id within that provider. */
  model_id: string;
}

/**
 * Full preset-models mapping: presetId → assignment.
 * An empty object means no presets have been assigned custom models.
 */
export type PresetModelsMap = Record<string, PresetModelEntry>;

// ─── Query keys ──────────────────────────────────────────────────────────────

const presetModelsKeys = {
  all: ["preset-models"] as const,
  settings: () => [...presetModelsKeys.all, "settings"] as const,
};

// ─── Hooks ───────────────────────────────────────────────────────────────────

/**
 * Fetch the current preset-models mapping.
 *
 * Returns an empty object when no preset models have been configured.
 * Always enabled — no auth guard needed (admin endpoint but reachable
 * after login like other settings endpoints).
 */
export function usePresetModels() {
  return useQuery<PresetModelsMap, ApiError>({
    queryKey: presetModelsKeys.settings(),
    queryFn: async () => api.request<PresetModelsMap>("/api/settings/preset-models"),
    // Stale after 60s — the setting changes rarely; no need to refetch on
    // every window focus.
    staleTime: 60_000,
  });
}

/**
 * Mutation: PUT /api/settings/preset-models with the full mapping.
 * On success: invalidate the GET query so the next read hits the server.
 */
export function useSetPresetModels() {
  const qc = useQueryClient();

  return useMutation<PresetModelsMap, ApiError, PresetModelsMap>({
    mutationFn: (mapping) =>
      api.request<PresetModelsMap>("/api/settings/preset-models", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(mapping),
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: presetModelsKeys.settings() });
    },
  });
}
