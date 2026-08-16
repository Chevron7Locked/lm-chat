/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useLmStudioHealth — live reachability probe for the LM Studio connection.
 *
 * Polls GET /api/lmstudio/health every 10 s to get a LIVE signal from the
 * ModelsService probe. The endpoint's own re-probe gate uses a 30-second
 * TTL dedicated to this path (separate from the 5-second TTL chat turns
 * use to resolve a loaded model) — sized at 3x this hook's poll interval
 * so most polls are served from cache: ~2 upstream probes/minute for an
 * idle tab instead of one per poll. This is distinct from useModels, which
 * reads the 30-minute catalog cache and cannot detect LM Studio going down
 * until the next scheduled full refresh.
 *
 * Response shape (from ModelsService.live_health()):
 *   { reachable: boolean, loaded_count: number, auth_failed: boolean,
 *     last_probe_at: number | null }
 *
 * The FE consumes `reachable` as the PRIMARY signal in useLmStudioStatus.
 */
import { useQuery } from "@tanstack/react-query";
import { api, type ApiError } from "@/lib/api";
import { useAuthStore } from "@/stores/authStore";

export interface LmStudioHealthResponse {
  /** True when LM Studio answered the most recent probe (even if 0 models). */
  reachable: boolean;
  /** Number of LLM-type models with at least one loaded instance. */
  loaded_count: number;
  /** True when LM Studio is reachable but returned 401 (bad API key). */
  auth_failed: boolean;
  /** Epoch seconds of the last probe, or null if never probed. */
  last_probe_at: number | null;
}

const lmStudioHealthKeys = {
  all: ["lmstudio-health"] as const,
  live: () => [...lmStudioHealthKeys.all, "live"] as const,
};

/**
 * Poll the live LM Studio reachability endpoint.
 *
 * Refetches every 10 s while the tab is visible; pauses in background tabs
 * (refetchIntervalInBackground: false) to avoid unnecessary backend load.
 * Auth-gated: does not fetch until the user session is hydrated.
 */
export function useLmStudioHealth() {
  const { isInitializing, user } = useAuthStore();
  return useQuery<LmStudioHealthResponse, ApiError>({
    queryKey: lmStudioHealthKeys.live(),
    queryFn: () =>
      api.request<LmStudioHealthResponse>("/api/lmstudio/health"),
    staleTime: 5_000,
    refetchInterval: 10_000,
    refetchIntervalInBackground: false,
    enabled: !isInitializing && user !== null,
  });
}
