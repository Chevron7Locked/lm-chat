/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useLmStudioConfig — read-only view of the resolved LM Studio config
 * the backend exposes at GET /api/settings/lmstudio.
 *
 * Settings UI owns the FORM state (LmStudioSection.tsx); this hook is
 * for everywhere else in the app that needs to read the resolved
 * default_model — primarily Chat, which uses it as the fallback model
 * for chats that don't carry their own model_id yet.
 *
 * Without this hook the Settings → "default model" save had no effect
 * outside Settings: Chat fell back to "" and the model selector was
 * blank on every new chat.
 */
import { useQuery } from "@tanstack/react-query";
import { api, type ApiError } from "@/lib/api";
import { useAuthStore } from "@/stores/authStore";

type Source = "user" | "server_admin" | "env" | "unset";

export interface LmStudioResolvedConfig {
  base_url: string;
  default_model: string;
  api_key_set: boolean;
  source_base_url: Source;
  source_api_key: Source;
  source_default_model: Source;
  /** True when boot detected an undecryptable api_key envelope and cleared
   *  it (LM_CHAT_SECRET rotation). Drives the "re-enter API key" banner.
   *  Cleared on the next successful admin api_key save. */
  key_pruned?: boolean;
  /** True when refresh() received a 401 from LM Studio.
   *  Drives the "LM Studio auth failed" banner. Cleared after backoff + successful re-probe. */
  auth_failed?: boolean;
  /**
   * The currently persisted preferred embedder key, or null when no
   * preference is set (auto-pick applies). Optional — only present when the
   * backend supports the preferred-embedder setting.
   */
  preferred_embedding_model_id?: string | null;
  /**
   * Currently-LOADED embedding models available for selection.
   * Only models with a live instance appear (downloaded-but-unloaded quant
   * variants are excluded — pinning one silently kills memory). ``active``
   * flags the one the index/recall path actually resolves to, so the
   * selector can render an unambiguous "· active" marker without
   * re-deriving the resolver's pick. Optional — only present when the
   * backend supports the preferred-embedder setting.
   */
  loaded_embedding_models?: { key: string; active?: boolean }[];
  /**
   * Background-tasks model: the currently persisted preferred model key for
   * out-of-band auxiliary LLM calls (auto-memory distillation, chat titles,
   * follow-up chips), or null when unset ("Same as chat model" — the default).
   * Optional — only present once the BE has landed the background-model setting.
   */
  preferred_background_model_id?: string | null;
  /**
   * Currently-LOADED LLMs available for selection as the background-tasks
   * model. Only models with a live instance appear. Optional — only present
   * once the BE has landed the background-model setting.
   */
  loaded_background_models?: { key: string }[];
  /**
   * LM Studio endpoint-mode toggle: "native" (default) talks to LM Studio's
   * /api/v1/chat surface and LM Studio runs MCP tools itself server-side.
   * "openai_compat" talks to /v1/chat/completions and LM Chat drives MCP
   * tools itself through its own MCP Store. Optional for the same reason
   * the other fields above are optional (older backends may not expose it yet).
   */
  lm_studio_endpoint_mode?: "native" | "openai_compat";
}

export const lmStudioConfigKeys = {
  all: ["lmstudio-config"] as const,
  resolved: () => [...lmStudioConfigKeys.all, "resolved"] as const,
};

export function useLmStudioConfig() {
  const { isInitializing, user } = useAuthStore();
  return useQuery<LmStudioResolvedConfig, ApiError>({
    queryKey: lmStudioConfigKeys.resolved(),
    queryFn: () =>
      api.request<LmStudioResolvedConfig>("/api/settings/lmstudio"),
    staleTime: 60_000,
    enabled: !isInitializing && user !== null,
  });
}
