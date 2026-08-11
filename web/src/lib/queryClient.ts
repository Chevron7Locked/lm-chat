/* SPDX-License-Identifier: Apache-2.0 */
/**
 * TanStack Query v5 singleton QueryClient.
 *
 * Defaults:
 *  - staleTime: 5 minutes — reduce redundant refetches for stable lists.
 *  - gcTime (was cacheTime in v4): 10 minutes — keep cached data in memory.
 *  - retry: 1 for all errors; 0 for 401 (unauthenticated — no point retrying).
 *  - refetchOnWindowFocus: true (TQ5 default; kept for freshness).
 *
 * TanStack Query v5 breaking changes applied here:
 *  - `cacheTime` renamed to `gcTime`.
 *  - All query/mutation options use single object signature.
 *  - `staleTime` defaults to 0 in TQ5; we override to 5min for server lists.
 *
 * Global MutationCache.onError fallback.  Any mutation that doesn't handle
 * its own errors will fall through here and show a generic "Couldn't save"
 * toast.
 *
 * Dedup guard (TanStack Query v5):
 *  - Hook-level onError (useMutation({ onError })) IS visible on
 *    mutation.options.onError — checking it suppresses the global fallback for
 *    admin mutations (useSetUserRole, useRevokeUserSessions, useDeleteUser,
 *    useIssueAdminInvite, useUpdateQuota) that define their own error toasts.
 *  - Per-call mutate(payload, { onError }) callbacks are stored on the
 *    observer's private #mutateOptions and are NOT visible on mutation.options.
 *    Mutations whose callers pass per-call onError set
 *    meta: { errorHandled: true } in useMutation options so the global handler
 *    skips them (e.g. useUpdateChat, useCreateChat, useAppendMessage,
 *    useRegenerateMessage).
 */
import { MutationCache, QueryClient } from "@tanstack/react-query";
import { useToastStore } from "@/stores/toastStore";

export const queryClient = new QueryClient({
  mutationCache: new MutationCache({
    onError: (error, _variables, _context, mutation) => {
      // Skip if the mutation defines its own hook-level onError handler
      // (hook-level handlers are visible on mutation.options).
      if (mutation.options.onError !== undefined) return;
      // Skip if the mutation declares meta.errorHandled (covers per-call
      // mutate(vars, { onError }) callers that are not visible on options).
      if (mutation.meta?.errorHandled) return;
      const detail =
        (error as { detail?: unknown }).detail ??
        (error instanceof Error ? error.message : String(error));
      const msg = typeof detail === "string" && detail.length > 0
        ? `Couldn't save — ${detail}`
        : "Couldn't save. Try again.";
      useToastStore.getState().push({ variant: "error", message: msg });
    },
  }),
  defaultOptions: {
    queries: {
      staleTime: 5 * 60 * 1_000, // 5 min
      gcTime: 10 * 60 * 1_000, // 10 min
      retry: (failureCount, error) => {
        // Do not retry authentication failures — they will not self-heal.
        const status = (error as { status?: number }).status;
        if (status === 401 || status === 403) return false;
        return failureCount < 1;
      },
    },
    mutations: {
      retry: 0,
    },
  },
});
