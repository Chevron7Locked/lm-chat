/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useModelList — derived view of /api/models for UI consumers.
 *
 * Wraps the low-level `useModels` TanStack Query with a UI-facing
 * state machine driven by `useLmStudioStore`.  Consumers (chat header
 * model selector, Settings → LM Studio default-model dropdown) read
 * a single `status` field and a normalised `models` array; they do
 * not need to reach into TanStack internals or worry about empty /
 * error / loading branches.
 *
 * The hook also keeps `lmStudioStore` in sync with the background
 * probe's outcome so other components (status badge, badges in other
 * tabs) see the same status without duplicating the logic.
 */
import { useEffect, useMemo } from "react";
import { useModels, type ModelInfo } from "@/hooks/useModels";
import {
  useLmStudioStore,
  type LmStudioConnectionStatus,
} from "@/stores/lmStudioStore";
import { useQueryClient } from "@tanstack/react-query";
import { modelKeys } from "@/hooks/useModels";

export interface UseModelListReturn {
  status: LmStudioConnectionStatus;
  /** All models the backend reports, loaded or not. */
  models: readonly ModelInfo[];
  /** Subset of `models` where `loaded === true`. */
  loadedModels: readonly ModelInfo[];
  /** Last error string, if status === "error". */
  error: string | null;
  /** True while a TanStack refetch is in flight. */
  isFetching: boolean;
  /**
   * Re-read the model list from TanStack's cache/refetch path.
   *
   * fe-13: this does NOT force the backend to re-probe LM Studio — GET
   * /api/models only reads the BE's in-memory model cache, so invalidating
   * the FE query alone just re-fetches the SAME stale payload. Renamed from
   * `refresh` (misleading: implied a live re-probe) to `revalidate` to
   * describe what it actually does. Callers that need loaded/unloaded
   * state to actually change must trigger a real BE re-probe first (see
   * `useRefreshModels`, which POSTs /api/admin/models/refresh — admin-only
   * — before invalidating this same query).
   */
  revalidate: () => Promise<void>;
}

export function useModelList(): UseModelListReturn {
  const { data, isError, isFetching, error, dataUpdatedAt, errorUpdatedAt } =
    useModels();
  const status = useLmStudioStore((s) => s.status);
  const lastError = useLmStudioStore((s) => s.lastError);
  const beginProbe = useLmStudioStore((s) => s.beginProbe);
  const resolveProbe = useLmStudioStore((s) => s.resolveProbe);
  const qc = useQueryClient();

  // Mirror TanStack's "in flight" state into the store's "probing" hint
  // so the badge spins even before the first result arrives.
  useEffect(() => {
    if (isFetching) beginProbe();
  }, [isFetching, beginProbe]);

  // Push the resolved query result into the store whenever it changes.
  useEffect(() => {
    if (isFetching) return;
    if (isError) {
      const msg =
        (error as { detail?: string } | null)?.detail ??
        (error as Error | null)?.message ??
        "LM Studio probe failed.";
      resolveProbe({ ok: false, error: msg });
      return;
    }
    if (dataUpdatedAt === 0 && errorUpdatedAt === 0) {
      // No probe completed yet — leave the store at "idle".
      return;
    }
    const models = data?.models ?? [];
    const loaded = models.filter((m) => m.loaded).length;
    resolveProbe({
      ok: true,
      modelCount: models.length,
      loadedCount: loaded,
    });
  }, [
    isError,
    isFetching,
    error,
    data,
    dataUpdatedAt,
    errorUpdatedAt,
    resolveProbe,
  ]);

  const models = useMemo<readonly ModelInfo[]>(
    () => data?.models ?? [],
    [data],
  );
  const loadedModels = useMemo<readonly ModelInfo[]>(
    () => models.filter((m) => m.loaded),
    [models],
  );

  const revalidate = async (): Promise<void> => {
    await qc.invalidateQueries({ queryKey: modelKeys.list() });
  };

  return {
    status,
    models,
    loadedModels,
    error: lastError,
    isFetching,
    revalidate,
  };
}
