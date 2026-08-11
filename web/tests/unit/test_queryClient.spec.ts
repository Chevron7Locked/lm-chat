/**
 * Unit tests for queryClient singleton.
 *
 * Verifies that the singleton is a QueryClient instance with the expected
 * default options (staleTime, gcTime, retry policy).
 *
 * MutationCache.onError dedup tests use the REAL production queryClient
 * imported from @/lib/queryClient so that any change to the guard logic
 * immediately breaks these tests (unlike an inline copy that could diverge
 * silently).
 */
import { describe, it, expect, vi } from "vitest";
import { QueryClient } from "@tanstack/react-query";

describe("queryClient", () => {
  it("exports a QueryClient instance", async () => {
    const { queryClient } = await import("@/lib/queryClient");
    expect(queryClient).toBeInstanceOf(QueryClient);
  });

  it("staleTime is 5 minutes (300_000ms)", async () => {
    const { queryClient } = await import("@/lib/queryClient");
    const opts = queryClient.getDefaultOptions();
    expect(opts.queries?.staleTime).toBe(5 * 60 * 1_000);
  });

  it("gcTime is 10 minutes (600_000ms)", async () => {
    const { queryClient } = await import("@/lib/queryClient");
    const opts = queryClient.getDefaultOptions();
    expect(opts.queries?.gcTime).toBe(10 * 60 * 1_000);
  });

  it("retry function returns false for 401", async () => {
    const { queryClient } = await import("@/lib/queryClient");
    const opts = queryClient.getDefaultOptions();
    const retry = opts.queries?.retry;
    expect(typeof retry).toBe("function");
    if (typeof retry === "function") {
      const should401 = retry(0, { status: 401 } as Error & { status: number });
      expect(should401).toBe(false);
    }
  });

  it("retry function returns false for 403", async () => {
    const { queryClient } = await import("@/lib/queryClient");
    const opts = queryClient.getDefaultOptions();
    const retry = opts.queries?.retry;
    if (typeof retry === "function") {
      const should403 = retry(0, { status: 403 } as Error & { status: number });
      expect(should403).toBe(false);
    }
  });

  it("retry function returns true on first failure (non-401)", async () => {
    const { queryClient } = await import("@/lib/queryClient");
    const opts = queryClient.getDefaultOptions();
    const retry = opts.queries?.retry;
    if (typeof retry === "function") {
      const should500 = retry(0, { status: 500 } as Error & { status: number });
      expect(should500).toBe(true);
    }
  });

  it("retry function returns false on second failure (non-401)", async () => {
    const { queryClient } = await import("@/lib/queryClient");
    const opts = queryClient.getDefaultOptions();
    const retry = opts.queries?.retry;
    if (typeof retry === "function") {
      const shouldStopAt1 = retry(1, { status: 500 } as Error & { status: number });
      expect(shouldStopAt1).toBe(false);
    }
  });

  it("mutations have retry: 0", async () => {
    const { queryClient } = await import("@/lib/queryClient");
    const opts = queryClient.getDefaultOptions();
    expect(opts.mutations?.retry).toBe(0);
  });
});

// ─── MutationCache.onError dedup — real production queryClient ───────────────
//
// These tests import the REAL production queryClient singleton and call
// MutationCache.config.onError directly.  If the guard logic in queryClient.ts
// is changed or removed, these tests fail immediately — unlike an inline copy
// of the guard that could pass even after a regression.
//
// Guard logic (TanStack Query v5):
//  1. Hook-level onError (useMutation({ onError })) IS visible on
//     mutation.options.onError — checked first to suppress global fallback for
//     admin mutations (useSetUserRole, useRevokeUserSessions, etc.).
//  2. Per-call mutate(vars, { onError }) handlers are NOT visible on
//     mutation.options.  Mutations that use per-call handlers declare
//     meta: { errorHandled: true } to suppress the global fallback.

describe("MutationCache.onError dedup — real production queryClient", () => {
  it("global fallback fires for a mutation without meta.errorHandled and without options.onError", async () => {
    vi.resetModules();
    const { queryClient } = await import("@/lib/queryClient");
    const { useToastStore } = await import("@/stores/toastStore");
    const initialCount = useToastStore.getState().toasts.length;

    const cacheConfig = queryClient.getMutationCache()["config"];
    expect(typeof cacheConfig.onError).toBe("function");

    const mutationNoMeta = {
      meta: undefined,
      options: { mutationFn: async () => undefined },
    } as unknown as Parameters<typeof cacheConfig.onError>[3];

    cacheConfig.onError!(new Error("fail"), undefined, undefined, mutationNoMeta);

    const afterCount = useToastStore.getState().toasts.length;
    expect(afterCount).toBeGreaterThan(initialCount);
    const last = useToastStore.getState().toasts[afterCount - 1]!;
    expect(last.variant).toBe("error");
    expect(last.message.toLowerCase()).toContain("couldn't save");
  });

  it("global fallback is suppressed for a mutation with meta.errorHandled = true", async () => {
    vi.resetModules();
    const { queryClient } = await import("@/lib/queryClient");
    const { useToastStore } = await import("@/stores/toastStore");
    const initialCount = useToastStore.getState().toasts.length;

    const cacheConfig = queryClient.getMutationCache()["config"];
    expect(typeof cacheConfig.onError).toBe("function");

    // Simulate a mutation with meta.errorHandled = true (e.g. useUpdateChat,
    // useCreateChat, useAppendMessage, useRegenerateMessage).
    const mutationWithMeta = {
      meta: { errorHandled: true },
      options: { mutationFn: async () => undefined },
    } as unknown as Parameters<typeof cacheConfig.onError>[3];

    cacheConfig.onError!(new Error("fail"), undefined, undefined, mutationWithMeta);

    expect(useToastStore.getState().toasts.length).toBe(initialCount);
  });

  it("global fallback is suppressed for a mutation with hook-level options.onError (admin mutation pattern)", async () => {
    // Hook-level onError IS visible on mutation.options.onError.
    // The guard must skip the global fallback for admin mutations like
    // useSetUserRole, useRevokeUserSessions, useDeleteUser, useIssueAdminInvite,
    // useUpdateQuota that define their own error toasts via hook-level onError.
    vi.resetModules();
    const { queryClient } = await import("@/lib/queryClient");
    const { useToastStore } = await import("@/stores/toastStore");
    const initialCount = useToastStore.getState().toasts.length;

    const cacheConfig = queryClient.getMutationCache()["config"];
    expect(typeof cacheConfig.onError).toBe("function");

    const adminMutation = {
      meta: undefined,
      options: {
        mutationFn: async () => undefined,
        // Hook-level onError — visible on mutation.options (admin pattern).
        onError: (_err: unknown) => { /* handles its own error toast */ },
      },
    } as unknown as Parameters<typeof cacheConfig.onError>[3];

    cacheConfig.onError!(new Error("Admin op failed"), undefined, undefined, adminMutation);

    // Global fallback must NOT fire — admin mutation handles its own error.
    expect(useToastStore.getState().toasts.length).toBe(initialCount);
  });

  it("end-to-end: meta.errorHandled + per-call mutate onError produce exactly ONE toast", async () => {
    // Cluster 2 closeout regression: drives a REAL mutation through the
    // production queryClient (not a synthetic mutation object).  The
    // mutationFn rejects; the per-call onError pushes its own specific
    // toast (mirroring AdminIntegrations / Chat.tsx callers).  Because the
    // hook declares meta: { errorHandled: true }, the global
    // MutationCache.onError fallback must stay silent — exactly ONE toast.
    vi.resetModules();
    const { queryClient } = await import("@/lib/queryClient");
    const { useToastStore } = await import("@/stores/toastStore");
    // TanStack's MutationObserver is the non-React core of useMutation —
    // same options surface (meta, per-call mutate overrides), no JSX needed.
    const { MutationObserver: TQMutationObserver } = await import(
      "@tanstack/react-query"
    );
    const initialCount = useToastStore.getState().toasts.length;

    const observer = new TQMutationObserver(queryClient, {
      mutationFn: async (): Promise<void> => {
        throw new Error("save failed");
      },
      meta: { errorHandled: true },
    });
    // TQ v5 only delivers per-call mutate callbacks when the observer has
    // at least one subscriber (matches a mounted useMutation hook).
    const unsubscribe = observer.subscribe(() => undefined);

    await expect(
      observer.mutate(undefined, {
        onError: () => {
          // Per-call handler — shows the caller's specific toast.
          useToastStore.getState().push({
            variant: "error",
            message: "Couldn't save the integrations list — try again.",
          });
        },
      }),
    ).rejects.toThrow("save failed");
    unsubscribe();

    const after = useToastStore.getState().toasts;
    // Exactly ONE new toast: the per-call one.  A second (global
    // "Couldn't save") toast here means the dedup guard regressed.
    expect(after.length).toBe(initialCount + 1);
    expect(after[after.length - 1]!.message).toBe(
      "Couldn't save the integrations list — try again.",
    );
  });
});
