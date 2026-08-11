/* SPDX-License-Identifier: Apache-2.0 */
/**
 * TanStack Query mutation hooks for model lifecycle operations.
 *
 * Routes:
 *   POST /api/models/load      → load a model instance (admin-only)
 *   POST /api/models/unload    → unload one or all instances (admin-only)
 *   POST /api/models/download  → download a model from hub (admin-only)
 *
 * Each mutation invalidates the modelKeys.list() query on success so
 * AdminModels refreshes automatically.
 *
 * NOTE: The download mutation is implemented but the frontend Download
 * button is currently gated pending confirmation of the upstream success shape.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { ApiError } from "@/lib/api";
import { modelKeys } from "@/hooks/useModels";

// ─── Wire shapes ─────────────────────────────────────────────────────────────

export interface LoadModelVars {
  model: string;
}

export interface LoadModelResult {
  instance_id: string;
  model: string;
  load_time_seconds: number;
  status: string;
}

export interface UnloadModelVars {
  /** Single-instance unload: provide instance_id. */
  instance_id?: string;
  /** Unload all: provide model key + set all=true. */
  model?: string;
  all?: boolean;
}

export interface UnloadModelResult {
  ok: boolean;
  unloaded: string[];
}

// ─── Hooks ───────────────────────────────────────────────────────────────────

/**
 * Mutation to load a model instance.
 *
 * On success, invalidates the model list so the loaded indicator updates.
 */
export function useLoadModel() {
  const qc = useQueryClient();
  return useMutation<LoadModelResult, ApiError, LoadModelVars>({
    mutationFn: ({ model }) =>
      api.postForm<LoadModelResult>("/api/models/load", { model }),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: modelKeys.list() });
    },
  });
}

/**
 * Mutation to unload one or all instances of a model.
 *
 * When ``all=true``, also provide ``model`` (the model key).
 * When unloading a single instance, provide ``instance_id``.
 *
 * On success, invalidates the model list so the loaded indicator updates.
 */
export function useUnloadModel() {
  const qc = useQueryClient();
  return useMutation<UnloadModelResult, ApiError, UnloadModelVars>({
    mutationFn: ({ instance_id, model, all }) => {
      const fields: Record<string, string> = {};
      if (instance_id != null) fields.instance_id = instance_id;
      if (model != null) fields.model = model;
      if (all === true) fields.all = "true";
      return api.postForm<UnloadModelResult>("/api/models/unload", fields);
    },
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: modelKeys.list() });
    },
  });
}
