/* SPDX-License-Identifier: Apache-2.0 */
/**
 * TanStack Query hooks for the MCP integrations list API.
 *
 * WHY this hook exists
 * --------------------
 * LM Studio does not expose MCP server enumeration over HTTP.  The admin
 * supplies the available integration IDs via env var or the Admin: Integrations
 * page; this hook retrieves that list so the chat composer can render a picker.
 *
 * useIntegrationsList()       — GET /api/integrations/available
 * useUpdateIntegrationsList() — PUT /api/integrations/available (admin only)
 */
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { ApiError } from "@/lib/api";

// ─── Types ───────────────────────────────────────────────────────────────────

export interface IntegrationEntry {
  id: number;
  value: string;
  sort_order: number;
  /**
   * Admin-set default-seed flag.  When true, the chat composer
   * pre-selects this integration on a fresh chat session; when false,
   * the integration is opt-in per-message.  Optional in older responses
   * (treated as false when absent).
   */
  enabled_by_default?: boolean;
  created_at: string;
  updated_at: string;
  source?: "lmstudio" | "store"; // which MCP system; absent in older responses (treat as "lmstudio")
}

export interface IntegrationSetEntry {
  value: string;
  sort_order: number;
  /**
   * Optional admin-set default-seed flag.  Server defaults to
   * false when omitted, so existing PUT clients keep working without
   * change.
   */
  enabled_by_default?: boolean;
}

// ─── Query key ───────────────────────────────────────────────────────────────

export const INTEGRATIONS_LIST_KEY = ["integrations", "available"] as const;

// ─── Hooks ───────────────────────────────────────────────────────────────────

/**
 * Fetch the current list of available MCP integration IDs.
 *
 * Returns DB-backed entries when the admin has set any; falls back to the
 * ``LM_CHAT_AVAILABLE_INTEGRATIONS`` env var otherwise.
 *
 * The query is disabled when the user is not yet resolved to prevent
 * unnecessary 401s during the auth hydration window.
 */
export function useIntegrationsList() {
  return useQuery<IntegrationEntry[], ApiError>({
    queryKey: INTEGRATIONS_LIST_KEY,
    queryFn: () =>
      api.request<IntegrationEntry[]>("/api/integrations/available"),
    staleTime: 60_000, // 1 minute — the list changes infrequently
    retry: 1,
  });
}

/**
 * Mutation to replace the integrations list (admin only).
 *
 * Accepts an array of ``{value, sort_order}`` objects and sends them as
 * a JSON-encoded form field (per the existing form-encoded mutation invariant).
 * Invalidates the integrations list query on success so dependent UI updates.
 */
export function useUpdateIntegrationsList() {
  const queryClient = useQueryClient();

  return useMutation<IntegrationEntry[], ApiError, IntegrationSetEntry[]>({
    // AdminIntegrations passes per-call onError via mutate(entries, { onError }).
    // TanStack Query v5 stores those on the observer's private #mutateOptions —
    // not visible on mutation.options — so we declare meta.errorHandled to
    // prevent the global MutationCache fallback from double-toasting.
    meta: { errorHandled: true },
    mutationFn: (entries: IntegrationSetEntry[]) => {
      const body = new URLSearchParams({
        entries: JSON.stringify(entries),
      }).toString();
      return api.request<IntegrationEntry[]>("/api/integrations/available", {
        method: "PUT",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body,
      });
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: INTEGRATIONS_LIST_KEY });
    },
  });
}
